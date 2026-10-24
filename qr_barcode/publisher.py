"""MQTT 后台发送：共享事件库、确认唤醒、超时重连和失败事件隔离。"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid

from .storage import ensure_schema, promote_pending

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(self, broker, port, topic, client_id='', username='', password='', qos=1,
                 outbox_path='records/mqtt_outbox.sqlite3', max_pending=1000,
                 close_timeout=3.0, ack_timeout=10.0, max_attempts=3):
        if qos not in (0, 1, 2) or max_pending < 1 or close_timeout < 0 or ack_timeout <= 0 or max_attempts < 1:
            raise ValueError('Invalid MQTT queue, QoS or timeout configuration')
        if not topic or '+' in topic or '#' in topic or '\0' in topic:
            raise ValueError('Invalid MQTT publish topic')
        self.broker, self.port, self.topic, self.qos = broker, port, topic, qos
        self._client_id = client_id or f'qr-barcode-{os.getpid()}'
        self._username, self._password = username, password
        self._client = self._db = None
        self._condition = threading.Condition()
        self._wake = threading.Event()
        self._max_pending, self._close_timeout = max_pending, close_timeout
        self._ack_timeout, self._max_attempts = ack_timeout, max_attempts
        self._closing = self._closed = False
        self._deadline = float('inf')
        self._inflight = self._inflight_row = None
        self._confirmed = False
        self._sent_at = self._retry_at = 0.0
        self._acks = queue.SimpleQueue()
        if mqtt is None:
            return
        Path(outbox_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(outbox_path, check_same_thread=False, timeout=0.2)
        try:
            ensure_schema(self._db)
            self._start_client()
        except Exception:
            self._db.close()
            raise
        self._thread = threading.Thread(target=self._run, name='mqtt-outbox', daemon=True)
        self._thread.start()

    def _start_client(self):
        if hasattr(mqtt, 'CallbackAPIVersion'):
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self._client_id)
        else:
            client = mqtt.Client(client_id=self._client_id)
        if self._username:
            client.username_pw_set(self._username, self._password)
        client.on_publish = self._on_publish
        client.on_connect = lambda *args: self.wake()
        client.on_disconnect = lambda *args: self.wake()
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.max_queued_messages_set(1)
        self._client = client
        try:
            client.connect_async(self.broker, self.port)
            client.loop_start()
        except Exception:
            self._client = None
            client.loop_stop()
            raise

    def wake(self):
        self._wake.set()

    @property
    def available(self):
        return self._client is not None

    @property
    def connected(self):
        return self._client is not None and self._client.is_connected()

    @property
    def pending_count(self):
        with self._condition:
            if self._db is None or self._closed:
                return 0
            return self._db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]

    def publish(self, kind, content):
        """供独立调用者入队；主程序由 EventRecorder 原子写入事件和待发项。"""
        event = {'event_id': uuid.uuid4().hex,
                 'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
                 'type': kind, 'content': content}
        with self._condition:
            if self._db is None or self._closing or self._closed:
                return False
            try:
                if self.pending_count >= self._max_pending:
                    logger.error('MQTT 待发队列已满，新事件未入队')
                    return False
                with self._db:
                    self._db.execute('INSERT INTO outbox(topic,qos,payload) VALUES (?,?,?)',
                                     (self.topic, self.qos, json.dumps(event, ensure_ascii=False)))
            except sqlite3.Error:
                logger.exception('MQTT 入队失败')
                return False
            self.wake()
            return True

    def _complete(self, row, error=None):
        row_id, topic, qos, payload, attempts = row
        event_id = json.loads(payload).get('event_id')
        with self._db:
            if error:
                self._db.execute('INSERT INTO mqtt_failures(topic,qos,payload,attempts,reason) '
                                 'VALUES (?,?,?,?,?)', (topic, qos, payload, attempts, error))
            self._db.execute('UPDATE events SET state=? WHERE event_id=?',
                             ('failed' if error else 'sent', event_id))
            self._db.execute('DELETE FROM outbox WHERE id=?', (row_id,))
        if error:
            logger.error('MQTT 事件 %s 已保存在 mqtt_failures：%s', event_id, error)

    def _clear_inflight(self):
        self._inflight = self._inflight_row = None
        self._confirmed = False

    def _send_pending(self):
        now = time.monotonic()
        if self._inflight is not None:
            while not self._confirmed:
                try:
                    mid, success = self._acks.get_nowait()
                except queue.Empty:
                    break
                if mid != self._inflight.mid:
                    continue
                if success:
                    self._confirmed = True
                else:
                    if self._inflight_row[4] >= self._max_attempts:
                        self._complete(self._inflight_row, 'Broker rejected message')
                    else:
                        logger.warning('MQTT Broker 拒绝消息，将有限重试')
                    self._clear_inflight()
                    self._retry_at = now + 0.1
                    return 'progress'
            if self._confirmed:
                self._complete(self._inflight_row)
                self._clear_inflight()
                return 'progress'
            if self.connected and now - self._sent_at >= self._ack_timeout:
                if self._inflight_row[4] >= self._max_attempts:
                    self._complete(self._inflight_row, 'Acknowledgement timed out')
                else:
                    logger.warning('MQTT 确认超时，将重连后重试同一事件')
                self._clear_inflight()
                return 'restart'
            return None
        if not self.connected or now < self._retry_at:
            return None
        with self._db:
            promote_pending(self._db, self._max_pending)
        row = self._db.execute('SELECT id,topic,qos,payload,attempts FROM outbox ORDER BY id LIMIT 1').fetchone()
        if row is None:
            return None
        if row[4] >= self._max_attempts:
            self._complete(row, 'Retry budget exhausted before restart')
            return 'progress'
        try:
            info = self._client.publish(row[1], row[3], qos=row[2])
        except (ValueError, TypeError) as exc:
            self._complete(row, str(exc))
            return 'progress'
        if info.rc == mqtt.MQTT_ERR_SUCCESS or (row[2] > 0 and info.rc == mqtt.MQTT_ERR_NO_CONN):
            with self._db:
                self._db.execute('UPDATE outbox SET attempts=attempts+1 WHERE id=?', (row[0],))
            self._inflight = info
            self._inflight_row = (*row[:4], row[4] + 1)
            self._sent_at = now
            return 'progress'
        self._retry_at = now + 0.2
        logger.warning('MQTT 发布失败 rc=%s，事件保留在本地', info.rc)
        return None

    def _on_publish(self, client, userdata, mid, *args):
        if client is self._client:
            self._acks.put((mid, not (args and getattr(args[0], 'is_failure', False))))
            self.wake()

    def _restart_client(self):
        old = self._client
        self._client = None
        try:
            old.disconnect()
        finally:
            old.loop_stop()
        while not self._acks.empty():
            self._acks.get_nowait()
        self._start_client()

    def _run(self):
        while True:
            self._wake.clear()
            try:
                if self._client is None and not self._closing:
                    self._start_client()
                with self._condition:
                    if self._closing and (time.monotonic() >= self._deadline or (
                            self.pending_count == 0 and not self._db.execute(
                                "SELECT 1 FROM events WHERE state='deferred' LIMIT 1").fetchone())):
                        return
                    action = self._send_pending()
                if action == 'restart':
                    self._restart_client()
                if action:
                    continue
            except Exception:
                logger.exception('MQTT 后台发送失败，事件仍保留在本地')
            # 回调和新事件立即唤醒；超时仅用于检查连接及确认期限。
            wait = min(0.2, self._ack_timeout)
            if self._closing:
                wait = min(wait, max(0, self._deadline - time.monotonic()))
            self._wake.wait(wait)

    def close(self):
        if self._db is None or self._closed:
            return
        with self._condition:
            self._closing = True
            self._deadline = time.monotonic() + self._close_timeout
            self.wake()
        self._thread.join()
        try:
            if self.pending_count or self._db.execute(
                    "SELECT 1 FROM events WHERE state='deferred' LIMIT 1").fetchone():
                logger.warning('MQTT 待确认事件保留在本地，下次启动继续发送')
            if self._client is not None:
                self._client.disconnect()
        finally:
            if self._client is not None:
                self._client.loop_stop()
            with self._condition:
                self._db.close()
                self._closed = True
