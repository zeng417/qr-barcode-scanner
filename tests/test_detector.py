import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

import qr_barcode.detector as module
from qr_barcode.detector import CodeDetector, DetectedCode


def code(data, x=0, kind="QR"):
    return DetectedCode(kind, np.array([[x, 0], [x+20, 0], [x+20, 20], [x, 20]]), data)


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.detector = CodeDetector(retry_every=1)
        self.image = np.zeros((80, 200), dtype=np.uint8)

    def test_merges_engines_but_keeps_same_content_at_different_positions(self):
        self.detector._decode_zxing = Mock(return_value=[code("A")])
        self.detector._decode_zbar = Mock(return_value=[code("B", 40, "BARCODE"), code("A", 1)])
        self.detector._decode_opencv = Mock(return_value=[code("A", 80)])
        found = self.detector.detect(self.image)
        self.assertEqual([(c.kind, c.data, c.points[0, 0]) for c in found],
                         [("QR", "A", 0), ("BARCODE", "B", 40), ("QR", "A", 80)])

    def test_merge_requires_standard_iou_at_least_point_seven(self):
        # 偏移 3 像素的 IoU 为 17/23；偏移 4 像素为 16/24。
        first = code("A")
        self.assertEqual(len(self.detector._merge_results([first, code("A", 3)])), 1)
        self.assertEqual(len(self.detector._merge_results([first, code("A", 4)])), 2)
        # 交集/较小面积为 0.5，但标准 IoU 只有 1/3，不能合并。
        self.assertEqual(len(self.detector._merge_results([first, code("A", 10)])), 2)

    def test_containment_does_not_imply_same_code(self):
        large = code("A")
        small = DetectedCode("QR", [[5, 5], [15, 5], [15, 15], [5, 15]], "A")
        self.assertEqual(len(self.detector._merge_results([large, small])), 2)

    def test_list_vertices_are_normalized_for_merge_and_drawing(self):
        from qr_barcode.main import draw_code

        listed = DetectedCode("QR", code("A").points.tolist(), "A")
        self.assertEqual(listed.points.dtype, np.dtype(np.int32))
        self.assertEqual(len(self.detector._merge_results([listed, code("A", 1)])), 1)
        frame = np.zeros((80, 200, 3), dtype=np.uint8)
        draw_code(frame, listed)
        self.assertTrue(np.any(frame))

    def test_degenerate_boxes_are_not_merged(self):
        line = DetectedCode("QR", [[0, 0], [0, 10], [0, 20]], "A")
        self.assertEqual(len(self.detector._merge_results([line, line])), 2)

    def test_unknown_position_does_not_absorb_located_codes(self):
        unknown = DetectedCode("QR", None, "A")
        self.assertEqual(len(self.detector._merge_results([unknown, code("A"), code("A", 80)])), 3)

    def test_opencv_normal_decode_runs_even_when_other_engines_are_installed(self):
        self.detector._decode_zxing = Mock(return_value=[])
        self.detector._decode_zbar = Mock(return_value=[])
        self.detector._qr = Mock()
        self.detector._qr.detectAndDecodeMulti.return_value = (False, (), None, ())
        self.detector._qr.detectAndDecode.return_value = ("opencv-only", code("x").points, None)
        with patch.object(module, "_HAS_ZXING", True), patch.object(module, "_HAS_PYZBAR", True):
            self.assertEqual([c.data for c in self.detector.detect(self.image)], ["opencv-only"])
        self.detector._qr.detectAndDecode.assert_called_once()

    def test_opencv_multi_keeps_all_nonempty_results(self):
        self.detector._qr = Mock()
        self.detector._qr.detectAndDecodeMulti.return_value = (
            True, ("A", "", "B"), np.array([code("A").points, code("").points, code("B", 60).points]), ())
        self.assertEqual([c.data for c in self.detector._decode_opencv(self.image)], ["A", "B"])

    def test_opencv_multi_error_uses_single_decoder(self):
        self.detector._qr = Mock()
        self.detector._qr.detectAndDecodeMulti.side_effect = cv2.error("failed")
        self.detector._qr.detectAndDecode.return_value = ("single", code("x").points, None)
        self.assertEqual(self.detector._decode_opencv(self.image)[0].data, "single")

    def test_opencv_encoding_errors_are_isolated(self):
        self.detector._qr = Mock()
        error = UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid byte')
        self.detector._qr.detectAndDecodeMulti.side_effect = error
        self.detector._qr.detectAndDecode.side_effect = error
        self.detector._qr.detectAndDecodeCurved.side_effect = error
        self.assertEqual(self.detector._decode_opencv(self.image), [])
        self.assertEqual(self.detector._decode_qr_curved(self.image), ('', None))

    def test_opencv_removes_padding_before_rescaling_multi_and_single_results(self):
        self.detector._qr = Mock()
        original = np.array([code("A").points, code("B", 60).points])
        transformed = original * 2 + (32, 24)
        self.detector._qr.detectAndDecodeMulti.return_value = (
            True, ("A", "B"), transformed, ())
        found = self.detector._decode_opencv(self.image, 2.0, (32, 24))
        self.assertEqual([c.data for c in found], ["A", "B"])
        for result, expected in zip(found, original):
            np.testing.assert_array_equal(result.points, expected)

        self.detector._qr.detectAndDecodeMulti.return_value = (False, (), None, ())
        self.detector._qr.detectAndDecode.return_value = ("A", transformed[0], None)
        found = self.detector._decode_opencv(self.image, 2.0, (32, 24))
        np.testing.assert_array_equal(found[0].points, original[0])

    def test_real_qr_without_quiet_zone_works_with_only_opencv(self):
        image = cv2.imread(str(Path(__file__).parent / "fixtures" / "qr_no_quiet_zone.png"))
        self.assertIsNotNone(image)
        with patch.object(module, "_HAS_ZXING", False), patch.object(module, "_HAS_PYZBAR", False):
            found = self.detector.detect(image)
        self.assertEqual([c.data for c in found],
                         ["https://v.xiumius.cn/board/v5/7M0Za/724797659"])
        self.assertEqual(found[0].source, "OpenCV")
        # 白边必须扣除，否则框会整体偏移到图像以外。
        self.assertTrue(np.all(found[0].points >= 0))
        self.assertTrue(np.all(found[0].points < (image.shape[1], image.shape[0])))
        self.assertTrue(np.all(found[0].points.min(axis=0) <= 3))
        self.assertTrue(np.all(found[0].points.max(axis=0) >= 156))

    @unittest.skipUnless(module._HAS_ZXING or module._HAS_PYZBAR, "Requires a screen QR decoder")
    def test_real_camera_screen_qrs_are_found_without_waiting_for_enhancement(self):
        for name in ("qr_screen_tilted.png", "qr_screen_small.png", "qr_screen_dark.png"):
            with self.subTest(image=name):
                gray = cv2.imread(str(Path(__file__).parent / "fixtures" / name), 0)
                self.assertIsNotNone(gray)
                detector = CodeDetector()
                # 直接测试区域提取，不能因为某个版本的原图解码成功而绕过回归。
                regions = detector._decode_qr_regions(gray)
                self.assertEqual([c.data for c in regions],
                                 ["https://v.xiumius.cn/board/v5/7M0Za/724797659"])
                points = regions[0].points
                self.assertTrue(np.all(points >= 0))
                self.assertTrue(np.all(points < (gray.shape[1], gray.shape[0])))
                # 区域坐标应还原成截图里的大框，不能停留在校正图的小框里。
                self.assertGreater(np.ptp(points[:, 0]), gray.shape[1] * 0.7)
                self.assertGreater(np.ptp(points[:, 1]), gray.shape[0] * 0.7)
                detector._detect_qr_fallback = Mock(return_value=[])
                self.assertEqual([c.data for c in detector.detect(gray)], [regions[0].data])
                detector._detect_qr_fallback.assert_not_called()

    def test_region_results_are_transformed_and_merged(self):
        transform = np.array([[2, 0, 100], [0, 3, 50], [0, 0, 1]], dtype=float)
        self.detector._decode_zxing = Mock(side_effect=lambda image: [code("A")])
        self.detector._decode_zbar = Mock(return_value=[])
        self.detector._decode_opencv = Mock(return_value=[])
        with patch.object(module, "iter_qr_regions", return_value=iter([
                (self.image, transform), (self.image, transform)])):
            found = self.detector._decode_qr_regions(self.image)
        self.assertEqual(len(found), 1)
        np.testing.assert_array_equal(found[0].points,
                                      [[100, 50], [140, 50], [140, 110], [100, 110]])

    def test_binary_payload_uses_lossless_hex_representation(self):
        self.assertEqual(self.detector._payload_text(b'\xff\xfeABC'), 'hex:fffe414243')
        self.assertEqual(self.detector._payload_text('中文'.encode()), '中文')

    @unittest.skipUnless(module._HAS_ZXING and hasattr(module.zxingcpp, 'create_barcode'),
                         'Requires zxing-cpp image writer')
    def test_real_non_utf8_qr_does_not_crash(self):
        barcode = module.zxingcpp.create_barcode(b'\xff\xfeABC', module.zxingcpp.BarcodeFormat.QRCode)
        image = np.asarray(module.zxingcpp.write_barcode_to_image(barcode, scale=8))
        found = self.detector.detect(image)
        self.assertIn('hex:fffe414243', [c.data for c in found])
        self.assertEqual(len(found), 1)

    def test_conflicting_engine_encodings_at_same_position_use_primary_decoder(self):
        primary = code('hex:fffe414243')
        primary.source = 'zxing-cpp'
        secondary = code('ÿþABC')
        secondary.source = 'pyzbar'
        found = self.detector._merge_results([secondary, primary])
        self.assertEqual([c.data for c in found], ['hex:fffe414243'])

    def test_enhancement_keeps_all_codes_and_merges_across_variants(self):
        self.detector._decode_zxing = Mock(side_effect=[[code("A"), code("B", 40)], [code("A")]])
        self.detector._decode_zbar = Mock(return_value=[code("C", 80, "BARCODE")])
        self.detector._decode_opencv = Mock(side_effect=[
            [code("E", 160)], [code("D", 120)], [code("D", 120)]])
        self.detector._decode_qr_curved = Mock(return_value=("", None))
        self.assertEqual([c.data for c in self.detector._detect_qr_fallback(self.image)],
                         ["E", "A", "B", "C", "D"])

    def test_barcode_success_does_not_trigger_expensive_enhancement(self):
        self.detector._decode_zxing = Mock(return_value=[code("B", kind="BARCODE")])
        self.detector._decode_zbar = Mock(return_value=[])
        self.detector._decode_opencv = Mock(return_value=[])
        self.detector._detect_qr_fallback = Mock(return_value=[])
        for _ in range(8):
            self.detector.detect(self.image)
        self.detector._detect_qr_fallback.assert_not_called()

    def test_enhancement_is_limited_by_frame_count_and_elapsed_time(self):
        detector = CodeDetector(retry_every=2, retry_interval=0.5)
        detector._decode_zxing = Mock(return_value=[])
        detector._decode_zbar = Mock(return_value=[])
        detector._decode_opencv = Mock(return_value=[])
        detector._detect_qr_fallback = Mock(return_value=[])
        with patch.object(module.time, "monotonic", side_effect=[0, .1, .2, .3, .6]):
            for _ in range(5):
                detector.detect(self.image)
        self.assertEqual(detector._detect_qr_fallback.call_count, 2)

    @unittest.skipUnless(module._HAS_ZXING, "zxing-cpp not installed")
    def test_real_mixed_qr_and_barcode_image(self):
        # write_barcode is also available in the project's minimum zxing-cpp version.
        qr = np.asarray(module.zxingcpp.write_barcode(module.zxingcpp.BarcodeFormat.QRCode,
                                                     "qr-integration", 160, 160))
        bar = np.asarray(module.zxingcpp.write_barcode(module.zxingcpp.BarcodeFormat.Code128,
                                                      "123456789", 300, 120))
        canvas = np.full((260, 620), 255, dtype=np.uint8)
        canvas[40:40+qr.shape[0], 20:20+qr.shape[1]] = qr
        canvas[60:60+bar.shape[0], 260:260+bar.shape[1]] = bar
        found = CodeDetector().detect(canvas)
        self.assertEqual({(c.kind, c.data) for c in found},
                         {("QR", "qr-integration"), ("BARCODE", "123456789")})


if __name__ == "__main__":
    unittest.main()
