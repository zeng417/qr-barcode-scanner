from contextlib import ExitStack
import unittest
from unittest.mock import Mock, patch
import numpy as np
import qr_barcode.main as module
from qr_barcode.detector import DetectedCode
from qr_barcode.scanning import ScanResult


class MainLoopTests(unittest.TestCase):
    def run_main(self, results, publisher=None, completed=(), reads=None, keys=None,
                 recorder_ok=True, add_ok=True):
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        capture = Mock()
        capture.isOpened.return_value = True
        capture.get.return_value = 40
        capture.read.return_value = (True, frame)
        if reads is not None:
            capture.read.side_effect = [(ok, frame if ok else None) for ok in reads]
        detector, recorder, worker = Mock(), Mock(), Mock()
        recorder.close.return_value = recorder_ok
        recorder.add.return_value = add_ok
        detector.engines = ['test']
        worker.poll.side_effect = results
        worker.close.return_value = list(completed)
        with ExitStack() as stack:
            for name, replacement in {
                'CodeDetector': Mock(return_value=detector),
                'EventRecorder': Mock(return_value=recorder),
                'DetectionWorker': Mock(return_value=worker),
                'create_publisher': Mock(return_value=publisher),
                'window_size': Mock(return_value=(40, 40)),
                'maybe_open_url': Mock(return_value=False), 'draw_code': Mock(),
            }.items():
                stack.enter_context(patch.object(module, name, replacement))
            stack.enter_context(patch.object(module.cv2, 'VideoCapture', return_value=capture))
            for name in ['namedWindow', 'resizeWindow', 'imshow', 'destroyAllWindows']:
                stack.enter_context(patch.object(module.cv2, name))
            stack.enter_context(patch.object(module.cv2, 'getWindowProperty', return_value=1))
            stack.enter_context(patch.object(module.cv2, 'waitKey', side_effect=(keys if keys is not None
                else [-1] * (len(results)-1) + [ord('q')])))
            output = stack.enter_context(patch('builtins.print'))
            self.status = module.main()
            refresh_count = module.cv2.imshow.call_count
        return detector, recorder, worker, capture, output, refresh_count

    def test_ui_refreshes_while_background_detector_has_no_result(self):
        detector, recorder, worker, capture, _, refreshes = self.run_main([None, None, None])
        self.assertEqual(refreshes, 3)
        self.assertEqual(worker.submit.call_count, 3)
        detector.detect.assert_not_called()
        worker.close.assert_called_once()
        capture.release.assert_called_once()
        self.assertEqual(self.status, 0)

    def test_buffer_failure_is_visible_and_sets_error_status(self):
        qr = DetectedCode('QR', None, 'A')
        _, recorder, _, _, output, _ = self.run_main([ScanResult(0, [qr]), None,
                                                     ScanResult(.2, [qr])], add_ok=False)
        recorder.add.assert_called_once_with('QR', 'A')
        self.assertEqual(self.status, 2)
        self.assertTrue(any('存储缓冲' in str(call) for call in output.call_args_list))

    def test_positions_and_reappearance_generate_distinct_events(self):
        left = DetectedCode('QR', [[0, 0], [10, 0], [10, 10], [0, 10]], 'A')
        right = DetectedCode('QR', [[25, 0], [35, 0], [35, 10], [25, 10]], 'A')
        results = [ScanResult(0, [left, right]), None,
                   ScanResult(.1, [right]), ScanResult(.2, [right, left])]
        _, recorder, _, _, _, _ = self.run_main(results)
        self.assertEqual(recorder.add.call_count, 3)

    def test_shutdown_persists_completed_result_before_closing_storage(self):
        result = ScanResult(0, [DetectedCode('QR', None, 'last')])
        _, recorder, _, _, _, _ = self.run_main([None], completed=[result])
        self.assertEqual(recorder.method_calls[0][0], 'add')
        recorder.add.assert_called_once_with('QR', 'last')
        recorder.close.assert_called_once()

    def test_camera_failure_retries_then_returns_nonzero(self):
        _, _, _, capture, _, _ = self.run_main([], reads=[False, False, False], keys=[-1, -1])
        self.assertEqual(capture.read.call_count, 3)
        self.assertEqual(self.status, 1)

    def test_transient_camera_failure_recovers(self):
        self.run_main([None], reads=[False, True], keys=[-1, ord('q')])
        self.assertEqual(self.status, 0)

    def test_storage_close_failure_returns_nonzero(self):
        self.run_main([None], recorder_ok=False)
        self.assertEqual(self.status, 2)

    def test_snapshot_checks_encoder_result_and_uses_unique_names(self):
        with patch.object(module.os, 'makedirs'), patch.object(module.cv2, 'imwrite', return_value=False), patch('builtins.print'):
            self.assertFalse(module.save_snapshot(None, '/tmp'))
        with patch.object(module.os, 'makedirs'), patch.object(module.cv2, 'imwrite', return_value=True) as write, patch('builtins.print'):
            self.assertTrue(module.save_snapshot(None, '/tmp'))
            self.assertTrue(module.save_snapshot(None, '/tmp'))
            self.assertNotEqual(write.call_args_list[0].args[0], write.call_args_list[1].args[0])


if __name__ == '__main__':
    unittest.main()
