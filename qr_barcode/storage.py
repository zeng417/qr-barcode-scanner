"""扫描事件库：事件和 MQTT 待发项同事务提交，CSV 是可重建的导出文件。"""

from __future__ import annotations

from collections import deque
import csv
import datetime
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid

logger = logging.getLogger(__name__)


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,
        qos INTEGER NOT NULL, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)""")
    if "attempts" not in {row[1] for row in db.execute("PRAGMA table_info(outbox)")}:
        db.execute("ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, topic TEXT,
        qos INTEGER NOT NULL, state TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS mqtt_failures (
        id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, qos INTEGER NOT NULL,
        payload TEXT NOT NULL, attempts INTEGER NOT NULL, reason TEXT NOT NULL)""")
    db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
    db.commit()


def promote_pending(db, limit):
    # 存储线程和发送线程共用队列；先取得写锁，再读取名额和候选事件。
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    room = max(0, limit - db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
    rows = db.execute("SELECT event_id, topic, qos, payload FROM events WHERE state='deferred' "
                      "ORDER BY rowid LIMIT ?", (room,)).fetchall()
    for event_id, topic, qos, payload in rows:
        db.execute("INSERT INTO outbox(topic,qos,payload) VALUES (?,?,?)", (topic, qos, payload))
        db.execute("UPDATE events SET state='queued' WHERE event_id=?", (event_id,))
    return len(rows)


class EventStore:
    def __init__(self, path, max_pending=1000):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=0.2)
        self.max_pending = max_pending
        try:
            ensure_schema(self.db)
        except Exception:
            self.db.close()
            raise

    def record(self, event, topic=None, qos=1):
        payload = json.dumps(event, ensure_ascii=False)
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                                     (event['event_id'], payload, topic, qos,
                                      'deferred' if topic is not None else 'none'))
            if cursor.rowcount and topic is not None:
                promote_pending(self.db, self.max_pending)

    def import_csv(self, path):
        key = "imported:" + os.path.abspath(path)
        if self.db.execute("SELECT 1 FROM metadata WHERE key=?", (key,)).fetchone():
            return
        with self.db:
            if os.path.exists(path):
                with open(path, newline='', encoding='utf-8') as source:
                    rows = csv.reader(source)
                    next(rows, None)
                    for row in rows:
                        if len(row) != 3:
                            raise ValueError("Existing CSV must have three columns")
                        event = {'event_id': uuid.uuid4().hex, 'timestamp': row[0],
                                 'type': row[1], 'content': row[2]}
                        self.db.execute("INSERT INTO events VALUES (?,?,NULL,1,'none')",
                                        (event['event_id'], json.dumps(event, ensure_ascii=False)))
            self.db.execute("INSERT INTO metadata VALUES (?, '1')", (key,))

    def export_csv(self, path, header):
        directory = Path(path).parent
        directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', newline='', encoding='utf-8',
                                             dir=directory, delete=False) as target:
                temporary = target.name
                writer = csv.writer(target)
                writer.writerow(header)
                for (payload,) in self.db.execute("SELECT payload FROM events ORDER BY rowid"):
                    event = json.loads(payload)
                    writer.writerow([event['timestamp'], event['type'], event['content']])
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def close(self):
        self.db.close()


class EventRecorder:
    """后台提交与导出；磁盘临时故障时有限缓存在内存中，恢复后重试。"""

    def __init__(self, database, csv_path, header, topic=None, qos=1, max_pending=1000,
                 max_buffer=1000, on_record=None, close_timeout=3.0):
        self.database, self.csv_path, self.header = database, csv_path, header
        self.topic, self.qos = topic, qos
        self.max_pending, self.max_buffer = max_pending, max_buffer
        self.on_record = on_record
        self.close_timeout = close_timeout
        self._condition = threading.Condition()
        self._queue = deque()
        self._closing = False
        self._deadline = float('inf')
        self._dirty = True
        self._last_export = float('-inf')
        self._last_error = float('-inf')
        self._dropped = False
        self._thread = threading.Thread(target=self._run, name='event-recorder', daemon=True)
        self._thread.start()

    def add(self, kind, content):
        event = {'event_id': uuid.uuid4().hex,
                 'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
                 'type': kind, 'content': content}
        with self._condition:
            if self._closing or len(self._queue) >= self.max_buffer:
                self._dropped = True
                logger.error("扫描事件内存缓冲已满或已关闭，本次事件未保存")
                return False
            self._queue.append(event)
            self._condition.notify_all()
        return True

    def _run(self):
        store = None
        imported = False
        try:
            while True:
                with self._condition:
                    if self._closing and time.monotonic() >= self._deadline:
                        return
                    event = self._queue[0] if self._queue else None
                failed = False
                try:
                    if store is None:
                        store = EventStore(self.database, self.max_pending)
                    import_error = None
                    if not imported:
                        try:
                            store.import_csv(self.csv_path)
                            imported = True
                        except (OSError, sqlite3.Error, ValueError) as exc:
                            import_error = exc
                    if event is not None:
                        store.record(event, self.topic, self.qos)
                        with self._condition:
                            self._queue.popleft()
                        self._dirty = True
                        if self.on_record is not None:
                            self.on_record()
                    if import_error is not None:
                        # 保护未成功导入的旧 CSV，但新事件仍能独立提交到数据库。
                        raise import_error
                    if self._dirty and (self._closing or time.monotonic() - self._last_export >= 1):
                        store.export_csv(self.csv_path, self.header)
                        self._dirty = False
                        self._last_export = time.monotonic()
                except (OSError, sqlite3.Error, ValueError):
                    failed = True
                    if time.monotonic() - self._last_error >= 1:
                        logger.exception("存储失败，将重试；已提交事件保留在 SQLite，未提交事件暂存内存")
                        self._last_error = time.monotonic()
                    if store is not None:
                        store.close()
                        store = None
                with self._condition:
                    if self._closing and not self._queue and not self._dirty:
                        return
                    if failed or not self._queue:
                        self._condition.wait(timeout=0.2)
        finally:
            if store is not None:
                store.close()

    def close(self):
        with self._condition:
            self._closing = True
            self._deadline = time.monotonic() + self.close_timeout
            self._condition.notify_all()
        self._thread.join(timeout=self.close_timeout + 0.5)
        with self._condition:
            success = not self._queue and not self._dirty and not self._dropped
            if not success:
                logger.error("存储关闭时仍有 %s 条内存事件，CSV 待导出=%s；请检查磁盘与权限",
                             len(self._queue), self._dirty)
            return success
