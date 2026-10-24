"""MQTT 发布器：本地有界待发队列，确认后删除，重连或重启后继续发送。"""

from __future__ import annotations

import datetime
import json
import logging
import os
import queue
from pathlib import Path
import sqlite3
import threading
import time
import uuid

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)


class MqttPublisher:
    """publish 返回入队是否成功；送达由后台确认，采用至少一次语义。"""

    def __init__(
        self,
        broker: str,
        port: int,
        topic: str,
        client_id: str = "",
        username: str = "",
        password: str = "",
        qos: int = 1,
        outbox_path: str = "records/mqtt_outbox.sqlite3",
        max_pending: int = 1000,
        close_timeout: float = 3.0,
    ) -> None:
        if qos not in (0, 1, 2) or max_pending < 1 or close_timeout < 0:
            raise ValueError("Invalid MQTT qos, queue size or close timeout")
        self.broker = broker
        self.port = port
        self.topic = topic
        self.qos = qos
        self._client = None
        self._db = None
        self._condition = threading.Condition()
        self._max_pending = max_pending
        self._close_timeout = close_timeout
        self._closing = False
        self._closed = False
        self._deadline = float("inf")
        self._inflight = None
        self._inflight_id = None
        self._inflight_confirmed = False
        self._acks = queue.SimpleQueue()
        if mqtt is None:
            return

        client_id = client_id or f"qr-barcode-{os.getpid()}"
        if hasattr(mqtt, "CallbackAPIVersion"):
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        else:
            self._client = mqtt.Client(client_id=client_id)
        if username:
            self._client.username_pw_set(username, password)
        self._client.on_publish = self._on_publish
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.max_queued_messages_set(1)
        Path(outbox_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(outbox_path, check_same_thread=False, timeout=0.5)
        self._db.execute("""CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL, qos INTEGER NOT NULL, payload TEXT NOT NULL)""")
        self._db.commit()
        try:
            self._client.connect_async(broker, port)
            self._client.loop_start()
        except Exception:
            self._db.close()
            raise
        self._thread = threading.Thread(target=self._run, name="mqtt-outbox", daemon=True)
        self._thread.start()

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    @property
    def pending_count(self) -> int:
        with self._condition:
            if self._db is None or self._closed:
                return 0
            return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def publish(self, kind: str, content: str) -> bool:
        """持久化后返回 True，即使尚未连接；队列满或写入失败时明确报错。"""
        payload = json.dumps({
            "event_id": uuid.uuid4().hex,
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "type": kind,
            "content": content,
        }, ensure_ascii=False)
        with self._condition:
            if self._db is None or self._closing or self._closed:
                return False
            try:
                if self.pending_count >= self._max_pending:
                    logger.error("MQTT 待发队列已满（%s 条），新事件未入队", self._max_pending)
                    return False
                self._db.execute("INSERT INTO outbox(topic, qos, payload) VALUES (?, ?, ?)",
                                 (self.topic, self.qos, payload))
                self._db.commit()
            except sqlite3.Error:
                self._db.rollback()
                logger.exception("MQTT 事件写入本地待发队列失败")
                return False
            self._condition.notify_all()
            return True

    def _send_pending(self) -> None:
        """在条件锁内调用；同一时间只提交一条，避免重连时重复塞入 paho 队列。"""
        if self._inflight is not None:
            while not self._inflight_confirmed:
                try:
                    mid, success = self._acks.get_nowait()
                except queue.Empty:
                    break
                if mid != self._inflight.mid:
                    continue
                if success:
                    self._inflight_confirmed = True
                else:
                    logger.warning("MQTT Broker 拒绝消息，保留本地事件以便重试")
                    self._inflight = None
                    self._inflight_id = None
                break
            if self._inflight_confirmed:
                # 若本地删除/提交临时失败，下一轮继续完成删除，不丢失已收到的 ACK。
                try:
                    self._db.execute("DELETE FROM outbox WHERE id = ?", (self._inflight_id,))
                    self._db.commit()
                except sqlite3.Error:
                    self._db.rollback()
                    raise
                self._inflight = None
                self._inflight_id = None
                self._inflight_confirmed = False
            self._condition.notify_all()
            return
        if not self.connected:
            return
        row = self._db.execute("SELECT id, topic, qos, payload FROM outbox ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return
        row_id, topic, qos, payload = row
        info = self._client.publish(topic, payload, qos=qos)
        # QoS 1/2 在发送前断连时，paho 已经将消息放入内部队列，会自行重发。
        accepted = info.rc == mqtt.MQTT_ERR_SUCCESS or (
            qos > 0 and info.rc == mqtt.MQTT_ERR_NO_CONN)
        if accepted:
            self._inflight, self._inflight_id = info, row_id
        else:
            logger.warning("MQTT 发布失败（rc=%s），保留本地事件以便重试", info.rc)

    def _on_publish(self, client, userdata, mid, *args) -> None:
        # 同时兼容 paho 1.x/2.x；不在网络回调里获取数据库锁。
        success = not (args and getattr(args[0], "is_failure", False))
        self._acks.put((mid, success))

    def _run(self) -> None:
        with self._condition:
            while True:
                if self._closing and (time.monotonic() >= self._deadline or self.pending_count == 0):
                    return
                try:
                    self._send_pending()
                except Exception:
                    # 保留磁盘记录；错误可见，短暂网络/存储故障后继续尝试。
                    logger.exception("MQTT 后台发送失败，事件仍保留在本地")
                self._condition.wait(timeout=0.1)

    def close(self) -> None:
        if self._client is None or self._closed:
            return
        with self._condition:
            self._closing = True
            self._deadline = time.monotonic() + self._close_timeout
            self._condition.notify_all()
        # 保持网络循环运行，给待发消息和 PUBACK 一个有限的完成时间。
        self._thread.join()
        remaining = self.pending_count
        if remaining:
            logger.warning("MQTT 仍有 %s 条事件未确认，已保留在本地，下次启动继续发送", remaining)
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            with self._condition:
                self._db.close()
                self._closed = True
