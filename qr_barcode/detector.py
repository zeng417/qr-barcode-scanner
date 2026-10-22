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

    def __init__(self, enhance_every: int = 3) -> None:
        self._qr = cv2.QRCodeDetector()
        # 原图识别失败后，每隔 enhance_every 帧做一次增强图像重试，避免逐帧多次解码拖慢实时性
        self._enhance_every = max(1, enhance_every)
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

    def _detect_qr(self, frame: np.ndarray) -> list[DetectedCode]:
        """OpenCV QRCodeDetector：返回带多边形包围框的二维码。"""
        data, points = self._decode_qr(frame)
        if data:
            self._failed_frames = 0
            pts = points.reshape(-1, 2).astype(int) if points is not None else None
            return [DetectedCode(kind="QR", points=pts, data=data)]

        # 原图失败：按间隔尝试增强图像（二值化 / 反色 / 闭运算 / 对比度增强）
        self._failed_frames += 1
        if self._failed_frames < self._enhance_every:
            return []
        self._failed_frames = 0

        for variant, scale in self._enhance_variants(frame):
            data, points = self._decode_qr(variant)
            if data:
                pts = None
                if points is not None:
                    pts = (points.reshape(-1, 2) / scale).astype(int)
                return [DetectedCode(kind="QR", points=pts, data=data)]
        return []

    def _enhance_variants(self, frame: np.ndarray) -> list[tuple[np.ndarray, float]]:
        """生成增强图像及其坐标缩放比例（用于把包围框映射回原图）。"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame

        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
        )

        variants: list[tuple[np.ndarray, float]] = [
            (otsu, 1.0),             # 光照不均、整体偏暗
            (cv2.bitwise_not(otsu), 1.0),  # 深底浅色的反色码
            (adaptive, 1.0),         # 局部过曝/阴影
        ]

        # 距离较远的小码：锐化后放大 2 倍再解码
        if gray.shape[1] <= 1280:
            sharp = cv2.addWeighted(
                gray, 2.0, cv2.GaussianBlur(gray, (0, 0), 3), -1.0, 0
            )
            sharp = cv2.resize(sharp, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            variants.append((sharp, 2.0))
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