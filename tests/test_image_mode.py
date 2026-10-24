from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

import qr_barcode.detector as detector_module
import qr_barcode.main as module


class ImageModeTests(unittest.TestCase):
    fixture = Path(__file__).parent / 'fixtures' / 'code128_abc_2024_001.png'

    @unittest.skipUnless(detector_module._HAS_ZXING or detector_module._HAS_PYZBAR,
                         'Requires a barcode decoder')
    def test_user_code128_file_is_decoded_once_without_camera_or_frame_confirmation(self):
        original = self.fixture.read_bytes()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as stdout, \
                patch.object(module.cv2, 'VideoCapture') as camera, \
                patch.object(module, 'EventRecorder') as recorder, \
                patch.object(module, 'create_publisher') as publisher, \
                patch.object(module.webbrowser, 'open') as browser:
            output = str(Path(directory) / '标注结果.png')
            status = module.cli(['--image', str(self.fixture), '--output', output])
            self.assertIsNotNone(cv2.imread(output))
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue().count('[BARCODE] ABC-2024-001'), 1)
        self.assertEqual(self.fixture.read_bytes(), original)
        camera.assert_not_called()
        recorder.assert_not_called()
        publisher.assert_not_called()
        browser.assert_not_called()

    def test_camera_mode_remains_the_default(self):
        with patch.object(module, 'main', return_value=2) as main:
            self.assertEqual(module.cli([]), 2)
        main.assert_called_once()

    def test_output_requires_an_image_argument(self):
        with redirect_stdout(io.StringIO()), patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                module.cli(['--output', '/tmp/output.png'])
        self.assertEqual(error.exception.code, 2)

    def test_unreadable_empty_and_blank_images_report_distinct_failures(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            root = Path(directory)
            self.assertEqual(module.scan_image(str(root / 'missing.png')), 1)
            (root / 'empty.png').write_bytes(b'')
            self.assertEqual(module.scan_image(str(root / 'empty.png')), 1)
            (root / 'invalid.png').write_bytes(b'this is not an image')
            self.assertEqual(module.scan_image(str(root / 'invalid.png')), 1)
            cv2.imwrite(str(root / 'blank.png'), np.full((80, 200), 255, np.uint8))
            self.assertEqual(module.scan_image(str(root / 'blank.png')), 3)

    @unittest.skipUnless(detector_module._HAS_ZXING or detector_module._HAS_PYZBAR,
                         'Requires a barcode decoder')
    def test_output_failure_is_reported_and_input_cannot_be_overwritten(self):
        with redirect_stdout(io.StringIO()), patch.object(module.cv2, 'imwrite', return_value=False):
            self.assertEqual(module.scan_image(str(self.fixture), '/tmp/output.png'), 2)
            self.assertEqual(module.scan_image(str(self.fixture), str(self.fixture)), 2)


if __name__ == '__main__':
    unittest.main()
