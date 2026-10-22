"""检测模块：使用 OpenCV QRCodeDetector 识别二维码，pyzbar 识别一维条码。

每个检测结果是一个 DetectedCode 对象：
- kind: "QR" 或 "BARCODE"
- points: 多边形包围框顶点（numpy 数组或列表）
- data: 解码出的文本
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

# pyzbar 为可选依赖，缺少时仅保留二维码识别能力
try:
    from pyzbar import pyzbar
    _HAS_PYZBAR = True
except Exception:  # pragma: no cover - 环境无关
    _HAS_PYZBAR = False


@dataclass
class DetectedCode:
    kind: str  # "QR" | "BARCODE"
    points: np.ndarray  # Nx2 的四边形顶点坐标
    data: str
    rect: Optional[tuple] = None  # 条码的近似矩形 (x, y, w, h)


class CodeDetector:
    """组合 OpenCV 二维码检测与 pyzbar 一维条码检测。"""

    def __init__(self, retry_every: int = 3) -> None:
        self._qr = cv2.QRCodeDetector()
        # 常规解码失败后，每隔 retry_every 帧才跑一次兜底策略，避免拖慢实时画面
        self._retry_every = max(1, retry_every)
        self._failed_frames = 0

    @property
    def barcode_supported(self) -> bool:
        return _HAS_PYZBAR

    def detect(self, frame: np.ndarray) -> list[DetectedCode]:
        """对单帧图像执行识别，返回命中的码列表。"""
        results: list[DetectedCode] = []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame

        results.extend(self._detect_qr(frame))
        if _HAS_PYZBAR:
            results.extend(self._detect_barcode(gray))
        return results

    def _decode_qr(self, image: np.ndarray) -> tuple[str, Optional[np.ndarray]]:
        """对单张图像解码二维码，失败返回 ("", None)。"""
        try:
            data, points, _ = self._qr.detectAndDecode(image)
        except cv2.error:  # 某些 OpenCV 版本对空角点会抛异常
            return "", None
        return data, points

    def _decode_qr_curved(self, image: np.ndarray) -> tuple[str, Optional[np.ndarray]]:
        """处理曲面/透视畸变的二维码（部分 OpenCV 版本不支持则跳过）。"""
        if not hasattr(self._qr, "detectAndDecodeCurved"):
            return "", None
        try:
            data, points, _ = self._qr.detectAndDecodeCurved(image)
        except cv2.error:
            return "", None
        return data, points

    @staticmethod
    def _make_qr(data: str, points: Optional[np.ndarray], scale: float = 1.0) -> DetectedCode:
        """构造二维码结果，并把增强图像的坐标换算回原图。"""
        pts = None
        if points is not None:
            pts = (points.reshape(-1, 2) / scale).astype(int)
        return DetectedCode(kind="QR", points=pts, data=data)

    def _decode_qr_roi(self, frame: np.ndarray) -> tuple[str, Optional[np.ndarray]]:
        """先定位二维码区域，裁剪放大后再解码，适合画面中较小的码。"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        try:
            ok, points = self._qr.detect(gray)
        except cv2.error:
            return "", None
        if not ok or points is None:
            return "", None

        x, y, w, h = cv2.boundingRect(points.astype(np.int32))
        pad = max(10, int(0.3 * max(w, h)))
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1 = min(gray.shape[1], x + w + pad)
        y1 = min(gray.shape[0], y + h + pad)
        crop = gray[y0:y1, x0:x1]
        if crop.size == 0:
            return "", None

        scale = 2.0
        enlarged = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        # 补白边，避免裁剪切掉二维码必需的静区
        pad2 = max(8, int(0.1 * max(enlarged.shape[:2])))
        padded = cv2.copyMakeBorder(
            enlarged, pad2, pad2, pad2, pad2, cv2.BORDER_CONSTANT, value=255
        )
        data, pts, _ = self._qr.detectAndDecode(padded)
        if data and pts is not None:
            pts = (pts.reshape(-1, 2) - pad2) / scale + np.array([x0, y0])
        return data, pts

    def _decode_qr_contour(self, frame: np.ndarray) -> tuple[str, Optional[np.ndarray]]:
        """针对深色背景/缺少静区：按亮色区域逐个定位候选码，补白边后解码。"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # 用较大的闭运算核把码内部的白色模块连成一块，同时排除整幅背景
        k = max(5, int(min(gray.shape[:2]) * 0.02)) | 1
        merged = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
        contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        h_img, w_img = gray.shape[:2]
        candidates = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w < 20 or h < 20:
                continue
            if not 0.6 <= w / h <= 1.7:  # 二维码接近正方形
                continue
            if w * h < 0.002 * w_img * h_img:  # 区域太小不可能是码
                continue
            candidates.append((w * h, x, y, w, h))

        for _, x, y, w, h in sorted(candidates, reverse=True)[:3]:
            crop = gray[y:y + h, x:x + w]
            pad = max(8, int(0.15 * max(w, h)))
            padded = cv2.copyMakeBorder(crop, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
            data, pts, _ = self._qr.detectAndDecode(padded)
            if data:
                if pts is not None:
                    pts = pts.reshape(-1, 2) - pad + np.array([x, y])
                return data, pts
        return "", None

    def _detect_qr(self, frame: np.ndarray) -> list[DetectedCode]:
        """OpenCV QRCodeDetector：返回带多边形包围框的二维码。"""
        data, points = self._decode_qr(frame)
        if data:
            self._failed_frames = 0
            return [self._make_qr(data, points)]

        # 常规解码失败：每隔 retry_every 帧才执行一次较重的兜底策略
        self._failed_frames += 1
        if self._failed_frames < self._retry_every:
            return []
        self._failed_frames = 0

        # 小码/远距离：先定位码区域，裁剪放大后解码
        data, points = self._decode_qr_roi(frame)
        if data:
            return [self._make_qr(data, points)]

        # 深色背景/缺少静区：按亮色区域定位并补白边
        data, points = self._decode_qr_contour(frame)
        if data:
            return [self._make_qr(data, points)]

        # 曲面/畸变码
        data, points = self._decode_qr_curved(frame)
        if data:
            return [self._make_qr(data, points)]

        # 最后尝试二值化 / 反色 / 自适应阈值 / 锐化放大等增强图像
        for variant, scale in self._enhance_variants(frame):
            data, points = self._decode_qr(variant)
            if data:
                return [self._make_qr(data, points, scale)]
        return []

    def _enhance_variants(self, frame: np.ndarray) -> list[tuple[np.ndarray, float]]:
        """生成增强图像及其坐标缩放比例（用于把包围框映射回原图）。"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame

        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
        )

        variants: list[tuple[np.ndarray, float]] = []
        # 补白边：很多二维码（截图、贴边、深色背景）缺少静区导致无法定位
        pad = max(8, int(0.08 * max(gray.shape[:2])))
        variants.append(
            (cv2.copyMakeBorder(gray, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255), 1.0)
        )
        variants.extend([
            (otsu, 1.0),                    # 光照不均、整体偏暗
            (cv2.bitwise_not(gray), 1.0),   # 深底浅色的反色码
            (adaptive, 1.0),                # 局部过曝/阴影
        ])

        # 锐化提升模糊码边缘；窄画面再放大 2 倍，帮助识别远距离小码
        sharp = cv2.addWeighted(
            gray, 2.0, cv2.GaussianBlur(gray, (0, 0), 3), -1.0, 0
        )
        variants.append((sharp, 1.0))
        if gray.shape[1] < 960:
            variants.append(
                (cv2.resize(sharp, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 2.0)
            )
        return variants

    def _detect_barcode(self, gray: np.ndarray) -> list[DetectedCode]:
        """pyzbar：识别一维条码，返回其边界矩形与文本。"""
        out = []
        for obj in pyzbar.decode(gray):
            data = obj.data.decode("utf-8", errors="replace")
            x, y, w, h = obj.rect.left, obj.rect.top, obj.rect.width, obj.rect.height
            pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]])
            out.append(DetectedCode(kind="BARCODE", points=pts, data=data, rect=(x, y, w, h)))
        return out