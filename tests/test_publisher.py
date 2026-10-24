import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import qr_barcode.publisher as module
from qr_barcode.publisher import MqttPublisher


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("background operation timed out")


class FakeClient:
    def __init__(self, **kwargs):
        self.online = threading.Event()
        self.sent = []
        self.actions = []
        self.next_rc = 0
        self.auto_ack = False

    def username_pw_set(self, *args):
        pass

    def reconnect_delay_set(self, **kwargs):
        pass

    def max_queued_messages_set(self, count):
        self.queue_limit = count

    def connect_async(self, *args):
        pass

    def loop_start(self):
        pass

    def is_connected(self):
        return self.online.is_set()

    def publish(self, topic, payload, qos):
        mid = len(self.sent) + 1
        self.sent.append((mid, topic, json.loads(payload), qos))
        if self.auto_ack:
            self.ack(mid)
        return SimpleNamespace(rc=self.next_rc, mid=mid)

    def ack(self, mid, failed=False):
        self.on_publish(self, None, mid, SimpleNamespace(is_failure=failed), None)

    def disconnect(self):
        self.actions.append("disconnect")

    def loop_stop(self):
        self.actions.append("loop_stop")


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "outbox.sqlite3")
        self.clients = []
        self.publishers = []

        def create(**kwargs):
            client = FakeClient(**kwargs)
            self.clients.append(client)
            return client

        self.patch = patch.object(module, "mqtt", SimpleNamespace(
            Client=create, MQTT_ERR_SUCCESS=0, MQTT_ERR_NO_CONN=4))
        self.patch.start()

    def tearDown(self):
        for publisher in self.publishers:
            publisher.close()
        self.patch.stop()
        self.temp.cleanup()

    def publisher(self, **kwargs):
        publisher = MqttPublisher("localhost", 1883, "scans", outbox_path=self.path,
                                  close_timeout=kwargs.pop("close_timeout", 0), **kwargs)
        self.publishers.append(publisher)
        return publisher

    def test_offline_events_are_replayed_after_restart_and_removed_only_after_ack(self):
        first = self.publisher()
        self.assertTrue(first.publish("QR", "离线扫码"))
        self.assertEqual(first.pending_count, 1)
        with self.assertLogs(module.logger, level="WARNING"):
            first.close()
        second = self.publisher()
        client = self.clients[-1]
        client.online.set()
        wait_until(lambda: len(client.sent) == 1)
        self.assertEqual(client.sent[0][2]["content"], "离线扫码")
        self.assertTrue(client.sent[0][2]["event_id"])
        self.assertEqual(second.pending_count, 1)
        client.ack(client.sent[0][0])
        wait_until(lambda: second.pending_count == 0)

    def test_reconnect_sends_without_another_scan_and_no_resubmission_before_ack(self):
        publisher = self.publisher()
        client = self.clients[-1]
        publisher.publish("QR", "A")
        publisher.publish("QR", "B")
        client.online.set()
        wait_until(lambda: len(client.sent) == 1)
        client.online.clear()
        client.online.set()
        # A remains in flight: B must wait for its acknowledgement.
        client.ack(1)
        wait_until(lambda: len(client.sent) == 2)
        self.assertEqual([item[2]["content"] for item in client.sent], ["A", "B"])
        client.ack(2)
        wait_until(lambda: publisher.pending_count == 0)

    def test_connection_lost_during_publish_uses_callback_not_message_info_rc(self):
        publisher = self.publisher()
        client = self.clients[-1]
        client.next_rc = 4  # paho keeps QoS 1 queued even when publish returns NO_CONN.
        client.online.set()
        publisher.publish("QR", "A")
        wait_until(lambda: len(client.sent) == 1)
        client.ack(1)
        wait_until(lambda: publisher.pending_count == 0)
        self.assertEqual(len(client.sent), 1)

    def test_bounded_queue_reports_overflow_and_does_not_drop_existing_event(self):
        publisher = self.publisher(max_pending=1)
        self.assertTrue(publisher.publish("QR", "A"))
        with self.assertLogs(module.logger, level="ERROR"):
            self.assertFalse(publisher.publish("QR", "B"))
        self.assertEqual(publisher.pending_count, 1)

    def test_close_drains_acknowledged_messages_before_stopping_network(self):
        publisher = self.publisher(close_timeout=1)
        client = self.clients[-1]
        client.online.set()
        client.auto_ack = True  # Callback may run before publish returns.
        publisher.publish("QR", "A")
        publisher.close()
        self.assertEqual(client.actions, ["disconnect", "loop_stop"])
        reopened = self.publisher()
        self.assertEqual(reopened.pending_count, 0)
        self.assertFalse(publisher.publish("QR", "closed"))

    def test_broker_rejection_is_not_treated_as_delivery(self):
        publisher = self.publisher()
        client = self.clients[-1]
        client.online.set()
        publisher.publish("QR", "A")
        wait_until(lambda: len(client.sent) == 1)
        with self.assertLogs(module.logger, level="WARNING"):
            client.ack(1, failed=True)
            wait_until(lambda: len(client.sent) == 2)
        self.assertEqual(publisher.pending_count, 1)
        self.assertEqual(client.sent[0][2]["event_id"], client.sent[1][2]["event_id"])
        client.ack(2)
        wait_until(lambda: publisher.pending_count == 0)

    def test_missing_optional_dependency_does_not_create_an_outbox(self):
        with patch.object(module, "mqtt", None):
            publisher = self.publisher()
            self.assertFalse(publisher.available)
            self.assertFalse(publisher.publish("QR", "A"))
            publisher.close()
        self.assertFalse(Path(self.path).exists())

    def replacement_factory(self, **kwargs):
        client = FakeClient(**kwargs)
        client.online.set()
        client.auto_ack = True
        self.clients.append(client)
        return client

    def test_ack_timeout_reconnects_and_retries_same_event_id(self):
        publisher = self.publisher(ack_timeout=.05)
        original = self.clients[-1]
        original.online.set()
        module.mqtt.Client = self.replacement_factory
        publisher.publish('QR', 'A')
        wait_until(lambda: len(self.clients) >= 2 and publisher.pending_count == 0)
        replacement = self.clients[-1]
        self.assertEqual(original.sent[0][2]['event_id'], replacement.sent[0][2]['event_id'])
        self.assertEqual(original.actions, ['disconnect', 'loop_stop'])

    def test_unacknowledged_event_is_archived_so_next_event_can_send(self):
        import sqlite3
        publisher = self.publisher(ack_timeout=.05, max_attempts=1)
        original = self.clients[-1]
        original.online.set()
        module.mqtt.Client = self.replacement_factory
        with self.assertLogs(module.logger, level='ERROR'):
            publisher.publish('QR', 'bad')
            publisher.publish('QR', 'good')
            wait_until(lambda: len(self.clients) >= 2 and publisher.pending_count == 0)
        self.assertEqual(self.clients[-1].sent[0][2]['content'], 'good')
        with sqlite3.connect(self.path) as db:
            payload = db.execute('SELECT payload FROM mqtt_failures').fetchone()[0]
            self.assertEqual(json.loads(payload)['content'], 'bad')

    def test_shared_event_store_promotes_deferred_events_and_tracks_delivery(self):
        import sqlite3
        from qr_barcode.storage import EventStore
        publisher = self.publisher(max_pending=1)
        store = EventStore(self.path, max_pending=1)
        try:
            for event_id in ('A', 'B'):
                store.record(dict(event_id=event_id, timestamp='now', type='QR', content=event_id), 'scans')
            client = self.clients[-1]
            client.auto_ack = True
            client.online.set()
            publisher.wake()
            wait_until(lambda: store.db.execute("SELECT COUNT(*) FROM events WHERE state='sent'").fetchone()[0] == 2)
            self.assertEqual([item[2]['event_id'] for item in client.sent], ['A', 'B'])
        finally:
            store.close()

    def test_legacy_outbox_schema_is_migrated_without_losing_pending_messages(self):
        import sqlite3
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, qos INTEGER, payload TEXT)')
            db.execute('INSERT INTO outbox(topic,qos,payload) VALUES (?,?,?)',
                       ('scans', 1, json.dumps(dict(event_id='legacy', content='old'))))
        publisher = self.publisher()
        client = self.clients[-1]
        client.online.set()
        client.auto_ack = True
        publisher.wake()
        wait_until(lambda: publisher.pending_count == 0)
        self.assertEqual(client.sent[0][2]['event_id'], 'legacy')


if __name__ == "__main__":
    unittest.main()
