"""从二维码的三个白色内环提取区域，处理黑色外沿与深色背景相连的情况。"""

from itertools import combinations
from collections.abc import Iterator

import cv2
import numpy as np


def _inner_rings(binary: np.ndarray) -> list[np.ndarray]:
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    rings = []
    for index, contour in enumerate(contours):
        area = cv2.contourArea(contour)
        child = hierarchy[0, index, 2]
        if area < 100 or child < 0:
            continue
        # 标准定位图案的白色内环为 5x5，中间深色区域为 3x3。
        if not 0.2 < cv2.contourArea(contours[child]) / area < 0.55:
            continue
        if hierarchy[0, child, 2] >= 0:
            continue
        quad = cv2.approxPolyDP(contour, 0.035 * cv2.arcLength(contour, True), True)
        if len(quad) != 4 or not cv2.isContourConvex(quad):
            continue
        quad = quad.reshape(4, 2).astype(np.float32)
        edges = np.linalg.norm(quad - np.roll(quad, 1, axis=0), axis=1)
        if edges.min() > 0 and edges.max() <= edges.min() * 2:
            rings.append(quad)
    # 限制组合搜索量，复杂背景也不会产生无界的候选解码次数。
    return sorted(rings, key=cv2.contourArea, reverse=True)[:12]


def iter_qr_regions(gray: np.ndarray, max_regions: int = 4) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """生成 (校正并补白边的区域, 区域坐标到原图的透视变换)。

    只利用定位图案估计区域，不改写二维码的数据模块；内容仍须由解码器校验。
    """
    if max_regions <= 0:
        return
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    adaptive = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY, 51, -5)
    emitted = 0
    for binary in (otsu, adaptive):
        for triple in combinations(_inner_rings(binary), 3):
            centers = np.array([quad.mean(axis=0) for quad in triple])
            sizes = np.array([np.sqrt(cv2.contourArea(quad)) for quad in triple])
            if sizes.max() > sizes.min() * 1.8:
                continue
            for corner in range(3):
                right, bottom = (corner + 1) % 3, (corner + 2) % 3
                u, v = centers[right] - centers[corner], centers[bottom] - centers[corner]
                width, height = np.linalg.norm(u), np.linalg.norm(v)
                if min(width, height) < sizes.mean() * 2:
                    continue
                if max(width, height) > min(width, height) * 1.6:
                    continue
                if abs(np.dot(u, v) / (width * height)) > 0.3:
                    continue
                if np.linalg.det(np.stack([u, v])) < 0:
                    right, bottom, u, v = bottom, right, v, u
                basis = np.linalg.inv(np.stack([u, v], axis=1))
                ordered = []
                for index in (corner, right, bottom):
                    relative = (triple[index] - centers[index]) @ basis.T
                    sums = relative.sum(axis=1)
                    differences = relative[:, 0] - relative[:, 1]
                    indices = [sums.argmin(), differences.argmax(), sums.argmax(), differences.argmin()]
                    if len(set(indices)) != 4:
                        break
                    ordered.append(triple[index][indices])
                if len(ordered) != 3:
                    continue
                unit_x = np.mean([np.linalg.norm(q[1] - q[0]) / 5 for q in ordered])
                unit_y = np.mean([np.linalg.norm(q[3] - q[0]) / 5 for q in ordered])
                estimate = (np.linalg.norm(u) / unit_x + np.linalg.norm(v) / unit_y) / 2 + 7
                # QR 边长只允许 21, 25, ..., 177 个模块。
                modules = round((estimate - 21) / 4) * 4 + 21
                if not 21 <= modules <= 177:
                    continue
                n = modules
                template = np.float32([
                    [1, 1], [6, 1], [6, 6], [1, 6],
                    [n-6, 1], [n-1, 1], [n-1, 6], [n-6, 6],
                    [1, n-6], [6, n-6], [6, n-1], [1, n-1],
                ])
                observed = np.concatenate(ordered)
                transform, _ = cv2.findHomography(template, observed, 0)
                if transform is None or not np.isfinite(transform).all():
                    continue
                projected = cv2.perspectiveTransform(template[None], transform)[0]
                if np.sqrt(np.mean(np.square(projected - observed))) > min(unit_x, unit_y) * 0.7:
                    continue
                scale = min(8.0, 1024 / (n + 8))
                side = round(n * scale)
                to_scene = transform @ np.diag([1 / scale, 1 / scale, 1])
                try:
                    inverse = np.linalg.inv(to_scene)
                except np.linalg.LinAlgError:
                    continue
                region = cv2.warpPerspective(gray, inverse, (side, side), borderValue=255)
                border = round(4 * scale)
                region = cv2.copyMakeBorder(region, border, border, border, border,
                                           cv2.BORDER_CONSTANT, value=255)
                to_scene = to_scene @ np.array([[1, 0, -border], [0, 1, -border], [0, 0, 1]])
                yield region, to_scene
                emitted += 1
                if emitted >= max_regions:
                    return
