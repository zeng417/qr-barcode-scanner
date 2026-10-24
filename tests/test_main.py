from contextlib import ExitStack
import unittest
from unittest.mock import Mock, patch

import numpy as np

import qr_barcode.main as module
from qr_barcode.detector import DetectedCode
from qr_barcode.scanning import ScanResult


class MainLoopTests(unittest.TestCase):
    def run_main(self, results, publisher=None):
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        capture = Mock()
        capture.isOpened.return_value = True
        capture.get.return_value = 40
        capture.read.return_value = (True, frame)
        detector, recorder, worker = Mock(), Mock(), Mock()
        detector.engines = ["test"]
        worker.poll.side_effect = results
        with ExitStack() as stack:
            for name, replacement in {
                "CodeDetector": Mock(return_value=detector),
                "CsvRecorder": Mock(return_value=recorder),
                "DetectionWorker": Mock(return_value=worker),
                "create_publisher": Mock(return_value=publisher),
                "window_size": Mock(return_value=(40, 40)),
                "maybe_open_url": Mock(return_value=False),
                "draw_code": Mock(),
            }.items():
                stack.enter_context(patch.object(module, name, replacement))
            stack.enter_context(patch.object(module.time, "monotonic", return_value=1))
            stack.enter_context(patch.object(module.cv2, "VideoCapture", return_value=capture))
            for name in ["namedWindow", "resizeWindow", "imshow", "destroyAllWindows"]:
                stack.enter_context(patch.object(module.cv2, name))
            stack.enter_context(patch.object(module.cv2, "waitKey",
                                             side_effect=[-1] * (len(results)-1) + [ord("q")]))
            output = stack.enter_context(patch("builtins.print"))
            module.main()
            refresh_count = module.cv2.imshow.call_count
        return detector, recorder, worker, capture, output, refresh_count

    def test_ui_refreshes_while_background_detector_has_no_result(self):
        detector, recorder, worker, capture, _, refreshes = self.run_main([None, None, None])
        self.assertEqual(refreshes, 3)
        self.assertEqual(worker.submit.call_count, 3)
        detector.detect.assert_not_called()
        worker.close.assert_called_once()
        capture.release.assert_called_once()

    def test_completed_results_are_deduplicated_and_publish_failure_is_visible(self):
        qr = DetectedCode("QR", None, "A")
        publisher = Mock()
        publisher.publish.return_value = False
        results = [ScanResult(0, [qr]), None, ScanResult(.2, [qr])]
        _, recorder, _, _, output, _ = self.run_main(results, publisher)
        recorder.add.assert_called_once_with("QR", "A")
        publisher.publish.assert_called_once_with("QR", "A")
        self.assertTrue(any("MQTT 事件未能入队" in str(call) for call in output.call_args_list))
        publisher.close.assert_called_once()

    def test_positions_and_reappearance_generate_distinct_record_and_publish_calls(self):
        left = DetectedCode("QR", [[0, 0], [10, 0], [10, 10], [0, 10]], "A")
        right = DetectedCode("QR", [[25, 0], [35, 0], [35, 10], [25, 10]], "A")
        publisher = Mock()
        results = [ScanResult(0, [left, right]), None,
                   ScanResult(.1, [right]), ScanResult(.2, [right, left])]
        _, recorder, _, _, _, _ = self.run_main(results, publisher)
        self.assertEqual(recorder.add.call_count, 3)
        self.assertEqual(publisher.publish.call_count, 3)


if __name__ == "__main__":
    unittest.main()
