import threading
import unittest
from unittest.mock import Mock

import numpy as np

from qr_barcode.detector import DetectedCode
from qr_barcode.scanning import DetectionWorker, ScanDeduplicator


class DeduplicatorTests(unittest.TestCase):
    def setUp(self):
        self.dedup = ScanDeduplicator()
        self.code = self.make_code()

    @staticmethod
    def make_code(x=0, kind="QR", data="A"):
        return DetectedCode(kind, [[x, 0], [x+20, 0], [x+20, 20], [x, 20]], data)

    def test_same_content_at_two_positions_emits_two_events(self):
        other = self.make_code(100)
        self.assertEqual(self.dedup.update([self.code, other]), [self.code, other])
        # 检测结果次序改变，不应改变实例身份。
        self.assertEqual(self.dedup.update([other, self.code]), [])

    def test_continuously_visible_moving_code_does_not_repeat(self):
        self.assertEqual(self.dedup.update([self.code]), [self.code])
        for x in (3, 9, 16, 25):
            self.assertEqual(self.dedup.update([self.make_code(x)]), [])

    def test_reappearance_after_one_absent_detection_is_a_new_event(self):
        self.dedup.update([self.code])
        self.dedup.update([])
        self.assertEqual(self.dedup.update([self.code]), [self.code])

    def test_one_instance_reappears_while_identical_other_stays_visible(self):
        other = self.make_code(100)
        self.dedup.update([self.code, other])
        self.assertEqual(self.dedup.update([other]), [])
        self.assertEqual(self.dedup.update([other, self.code]), [self.code])

    def test_new_instance_near_existing_instance_is_reported(self):
        self.dedup.update([self.code])
        moved, added = self.make_code(2), self.make_code(24)
        self.assertEqual(self.dedup.update([added, moved]), [added])

    def test_far_new_position_is_reported_without_empty_frame(self):
        self.dedup.update([self.code])
        other = self.make_code(100)
        self.assertEqual(self.dedup.update([other]), [other])

    def test_nearest_match_does_not_steal_another_instances_only_candidate(self):
        self.dedup.update([self.make_code(0), self.make_code(20)])
        # 9 离旧位置 0 最近，但 -10 只能与 0 匹配；应延续两枚实例。
        self.assertEqual(self.dedup.update([self.make_code(9), self.make_code(-10)]), [])

    def test_same_position_with_different_content_or_kind_is_new(self):
        self.dedup.update([self.code])
        bar, changed = self.make_code(kind="BARCODE"), self.make_code(data="B")
        self.assertEqual(self.dedup.update([bar, changed]), [bar, changed])

    def test_unknown_positions_continue_by_count(self):
        first, second = DetectedCode("QR", None, "A"), DetectedCode("QR", None, "A")
        self.assertEqual(len(self.dedup.update([first, second])), 2)
        self.assertEqual(self.dedup.update([first, second]), [])
        self.dedup.update([])
        self.assertEqual(len(self.dedup.update([first, second])), 2)

    def test_positioned_matches_take_priority_over_unknown_positions(self):
        other = self.make_code(100)
        unknown = DetectedCode("QR", None, "A")
        self.dedup.update([self.code, other])
        self.assertEqual(self.dedup.update([unknown, other]), [])
        self.assertEqual(self.dedup.update([self.code, other]), [])

    def test_optional_miss_tolerance_counts_completed_detections(self):
        dedup = ScanDeduplicator(max_missed_detections=1)
        dedup.update([self.code])
        dedup.update([])
        self.assertEqual(dedup.update([self.code]), [])
        dedup.update([])
        dedup.update([])
        self.assertEqual(dedup.update([self.code]), [self.code])

    def test_absent_instances_are_removed(self):
        self.dedup.update([self.code])
        self.dedup.update([])
        self.assertEqual(self.dedup._tracks, [])

    def test_barcode_needs_consecutive_confirmations_but_qr_is_immediate(self):
        dedup = ScanDeduplicator(barcode_confirmations=3)
        bar = self.make_code(kind='BARCODE', data='4006381333931')
        self.assertEqual(dedup.update([bar, self.code]), [self.code])
        self.assertEqual(dedup.visible_codes, [self.code])
        self.assertEqual(dedup.update([bar, self.code]), [])
        self.assertEqual(dedup.update([bar, self.code]), [bar])
        self.assertEqual(dedup.visible_codes, [bar, self.code])
        self.assertEqual(dedup.update([bar, self.code]), [])

    def test_fluctuating_ean_values_do_not_accumulate_confirmation_hits(self):
        dedup = ScanDeduplicator(barcode_confirmations=3)
        # 日志中的值均通过 EAN 校验位，但交替出现不能当作可靠识别。
        for data in ['4006381333931', '9000384333931', '1029387333937'] * 3:
            self.assertEqual(dedup.update([self.make_code(kind='BARCODE', data=data)]), [])
            self.assertEqual(dedup.visible_codes, [])

    def test_unconfirmed_barcode_resets_hits_on_miss_even_with_miss_tolerance(self):
        dedup = ScanDeduplicator(max_missed_detections=1, barcode_confirmations=3)
        bar = self.make_code(kind='BARCODE')
        dedup.update([bar])
        dedup.update([bar])
        dedup.update([])
        self.assertEqual(dedup.update([bar]), [])
        self.assertEqual(dedup.update([bar]), [])
        self.assertEqual(dedup.update([bar]), [bar])

    def test_identical_barcode_instances_confirm_independently_and_reconfirm_on_return(self):
        dedup = ScanDeduplicator(barcode_confirmations=3)
        left, right = self.make_code(kind='BARCODE'), self.make_code(100, kind='BARCODE')
        dedup.update([left])
        self.assertEqual(dedup.update([left, right]), [])
        self.assertEqual(dedup.update([right, left]), [left])
        self.assertEqual(dedup.update([left, right]), [right])
        dedup.update([right])
        self.assertEqual(dedup.update([right, left]), [])
        self.assertEqual(dedup.update([left, right]), [])
        self.assertEqual(dedup.update([right, left]), [left])

    def test_unconfirmed_barcode_cannot_transfer_hits_to_a_distant_instance(self):
        dedup = ScanDeduplicator(barcode_confirmations=3)
        dedup.update([self.make_code(kind='BARCODE')])
        dedup.update([self.make_code(kind='BARCODE')])
        distant = self.make_code(100, kind='BARCODE')
        self.assertEqual(dedup.update([distant]), [])
        self.assertEqual(dedup.update([distant]), [])
        self.assertEqual(dedup.update([distant]), [distant])


class WorkerTests(unittest.TestCase):
    def test_close_drains_completed_and_in_progress_results_without_deadlock(self):
        second = threading.Event()

        def detect(frame):
            if frame[0, 0] == 2:
                second.set()
            return [DetectedCode('QR', None, str(frame[0, 0]))]

        worker = DetectionWorker(Mock(detect=detect))
        worker.submit(np.array([[1]], dtype=np.uint8))
        # Wait until result 1 occupies the output slot, without consuming it.
        from test_publisher import wait_until
        wait_until(lambda: worker._results.qsize() == 1)
        worker.submit(np.array([[2]], dtype=np.uint8))
        self.assertTrue(second.wait(2))
        completed = worker.close()
        self.assertEqual([result.codes[0].data for result in completed], ['1', '2'])
        self.assertFalse(worker._thread.is_alive())

    def test_submission_does_not_wait_for_decode_and_pending_frame_is_latest_copy(self):
        entered, release = threading.Event(), threading.Event()
        seen = []

        def detect(frame):
            seen.append(int(frame[0, 0]))
            if len(seen) == 1:
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test did not release detector")
            return []

        worker = DetectionWorker(Mock(detect=detect))
        try:
            worker.submit(np.array([[1]], dtype=np.uint8), 1)
            self.assertTrue(entered.wait(2))
            worker.submit(np.array([[2]], dtype=np.uint8), 2)
            frame = np.array([[3]], dtype=np.uint8)
            worker.submit(frame, 3)
            frame[:] = 99
            release.set()
            self.assertEqual(worker._results.get(timeout=2).captured_at, 1)
            self.assertEqual(worker._results.get(timeout=2).captured_at, 3)
            self.assertEqual(seen, [1, 3])
        finally:
            release.set()
            worker.close()
        self.assertFalse(worker._thread.is_alive())

    def test_empty_frame_submission_interval(self):
        worker = DetectionWorker(Mock(detect=Mock(return_value=[])), empty_every=3)
        try:
            for i in range(4):
                worker.submit(np.array([[i]], dtype=np.uint8), i)
                if i in (0, 3):
                    self.assertEqual(worker._results.get(timeout=2).captured_at, i)
        finally:
            worker.close()

    def test_detection_exception_is_relayed_to_main_thread(self):
        worker = DetectionWorker(Mock(detect=Mock(side_effect=ValueError("bad frame"))))
        try:
            worker.submit(np.zeros((1, 1), dtype=np.uint8))
            result = worker._results.get(timeout=2)
            self.assertIsInstance(result.error, ValueError)
        finally:
            worker.close()

    def test_close_with_unconsumed_results_does_not_deadlock(self):
        detected = threading.Event()

        def detect(frame):
            detected.set()
            return []

        worker = DetectionWorker(Mock(detect=detect))
        worker.submit(np.zeros((1, 1), dtype=np.uint8))
        self.assertTrue(detected.wait(2))
        worker.close()
        self.assertFalse(worker._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
