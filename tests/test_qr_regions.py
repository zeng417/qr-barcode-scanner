from pathlib import Path
import unittest

import cv2
import numpy as np

from qr_barcode.qr_regions import iter_qr_regions


class QrRegionTests(unittest.TestCase):
    def test_blank_and_unrelated_squares_do_not_form_qr_region(self):
        for value in (0, 127, 255):
            self.assertEqual(list(iter_qr_regions(np.full((200, 300), value, np.uint8))), [])
        self.assertEqual(list(iter_qr_regions(np.zeros((100, 100), np.uint8), max_regions=0)), [])
        unrelated = np.zeros((400, 400), np.uint8)
        for x, y in ((40, 40), (170, 150), (260, 280)):
            cv2.rectangle(unrelated, (x, y), (x+40, y+40), 255, 8)
        self.assertEqual(list(iter_qr_regions(unrelated)), [])

    def test_rotated_qr_keeps_perspective_coordinates_and_region_budget(self):
        gray = cv2.imread(str(Path(__file__).parent / 'fixtures' / 'qr_screen_dark.png'), 0)
        self.assertIsNotNone(gray)
        for rotations in range(4):
            with self.subTest(rotations=rotations):
                rotated = np.ascontiguousarray(np.rot90(gray, rotations))
                regions = list(iter_qr_regions(rotated, max_regions=1))
                self.assertEqual(len(regions), 1)
                region, to_scene = regions[0]
                self.assertTrue(np.all(region[:20] == 255))
                self.assertTrue(np.all(region[:, :20] == 255))
                # 此样本为 33 模块；校正区域为每模块 8 像素并补 4 模块白边。
                corners = np.float32([[[32, 32], [296, 32], [296, 296], [32, 296]]])
                scene = cv2.perspectiveTransform(corners, to_scene)[0]
                self.assertTrue(np.all(scene >= 0))
                self.assertTrue(np.all(scene < (rotated.shape[1], rotated.shape[0])))
                self.assertGreater(cv2.contourArea(scene), rotated.size * 0.5)


if __name__ == '__main__':
    unittest.main()
