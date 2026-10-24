import csv
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from qr_barcode.storage import EventStore, EventRecorder, promote_pending


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'events.db')
        self.csv_path = str(Path(self.temp.name) / 'history.csv')
        self.header = ['timestamp', 'type', 'content']

    def tearDown(self):
        self.temp.cleanup()

    def event(self, event_id='A'):
        return dict(event_id=event_id, timestamp='2026-10-24T12:00:00', type='QR', content='中文')

    def test_event_and_outbox_commit_together_and_retry_is_idempotent(self):
        store = EventStore(self.path)
        try:
            store.record(self.event(), 'scans')
            store.record(self.event(), 'scans')
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        finally:
            store.close()

    def test_outbox_failure_rolls_back_event_in_same_transaction(self):
        store = EventStore(self.path)
        try:
            store.db.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'disk error'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                store.record(self.event(), 'scans')
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        finally:
            store.close()

    def test_full_outbox_defers_delivery_without_losing_scan(self):
        store = EventStore(self.path, max_pending=1)
        try:
            store.record(self.event('A'), 'scans')
            store.record(self.event('B'), 'scans')
            self.assertEqual(store.db.execute("SELECT state FROM events WHERE event_id='B'").fetchone()[0], 'deferred')
            with store.db:
                store.db.execute('DELETE FROM outbox')
                promote_pending(store.db, 1)
            self.assertEqual(json.loads(store.db.execute('SELECT payload FROM outbox').fetchone()[0])['event_id'], 'B')
        finally:
            store.close()

    def test_legacy_csv_imported_once_and_failed_export_keeps_old_csv(self):
        Path(self.csv_path).write_text('timestamp,type,content\nold,QR,old-content\n')
        store = EventStore(self.path)
        try:
            store.import_csv(self.csv_path)
            store.import_csv(self.csv_path)
            store.record(self.event())
            with patch('qr_barcode.storage.os.replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    store.export_csv(self.csv_path, self.header)
            self.assertIn('old-content', Path(self.csv_path).read_text())
            store.export_csv(self.csv_path, self.header)
            with open(self.csv_path) as f:
                self.assertEqual(len(list(csv.reader(f))), 3)
        finally:
            store.close()

    def test_failed_import_rolls_back_all_rows_before_retry(self):
        Path(self.csv_path).write_text('timestamp,type,content\nold,QR,old-content\nbad,row\n')
        store = EventStore(self.path)
        try:
            with self.assertRaises(ValueError):
                store.import_csv(self.csv_path)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        finally:
            store.close()

    def test_background_disk_failure_recovers_without_blocking_add(self):
        real_record = EventStore.record
        fail = threading.Event()
        fail.set()
        attempted = threading.Event()

        def record(store, *args, **kwargs):
            if fail.is_set():
                attempted.set()
                raise sqlite3.OperationalError('disk full')
            return real_record(store, *args, **kwargs)

        with patch.object(EventStore, 'record', record), self.assertLogs('qr_barcode.storage', level='ERROR'):
            recorder = EventRecorder(self.path, self.csv_path, self.header)
            self.assertTrue(recorder.add('QR', 'recover'))
            self.assertTrue(attempted.wait(2))
            fail.clear()
            self.assertTrue(recorder.close())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)

    def test_csv_failure_does_not_prevent_durable_events_and_rebuilds_on_restart(self):
        persisted = threading.Event()
        original = EventStore.record

        def record(store, *args, **kwargs):
            original(store, *args, **kwargs)
            persisted.set()

        with patch.object(EventStore, 'record', record), patch.object(EventStore, 'export_csv', side_effect=OSError('read only')), self.assertLogs('qr_barcode.storage', level='ERROR'):
            recorder = EventRecorder(self.path, self.csv_path, self.header, close_timeout=.1)
            recorder.add('QR', 'durable')
            self.assertTrue(persisted.wait(2))
            # close waits for durable insert but reports the failed CSV export.
            self.assertFalse(recorder.close())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        recorder = EventRecorder(self.path, self.csv_path, self.header)
        self.assertTrue(recorder.close())
        self.assertIn('durable', Path(self.csv_path).read_text())

    def test_malformed_old_csv_is_preserved_but_new_events_are_still_durable(self):
        Path(self.csv_path).write_text('timestamp,type,content\nbroken,row\n')
        recorded = threading.Event()
        original = EventStore.record

        def record(store, *args, **kwargs):
            original(store, *args, **kwargs)
            recorded.set()

        with patch.object(EventStore, 'record', record), self.assertLogs('qr_barcode.storage', level='ERROR'):
            recorder = EventRecorder(self.path, self.csv_path, self.header, close_timeout=.1)
            recorder.add('QR', 'new-event')
            self.assertTrue(recorded.wait(2))
            self.assertFalse(recorder.close())
        self.assertIn('broken,row', Path(self.csv_path).read_text())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
