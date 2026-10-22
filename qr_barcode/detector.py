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

    def __init__(self) -> None:
        self._qr = cv2.QRCodeDetector()

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

    def _detect_qr(self, frame: np.ndarray) -> list[DetectedCode]:
        """OpenCV QRCodeDetector：返回带多边形包围框的二维码。"""
        out = []
        try:
            data, points, _ = self._qr.detectAndDecode(frame)
        except cv2.error:  # 某些 OpenCV 版本对空角点会抛异常
            return out
        if data:
            pts = points.reshape(-1, 2).astype(int) if points is not None else None
            out.append(DetectedCode(kind="QR", points=pts, data=data))
        return out

    def _detect_barcode(self, gray: np.ndarray) -> list[DetectedCode]:
        """pyzbar：识别一维条码，返回其边界矩形与文本。"""
        out = []
        for obj in pyzbar.decode(gray):
            data = obj.data.decode("utf-8", errors="replace")
            x, y, w, h = obj.rect.left, obj.rect.top, obj.rect.width, obj.rect.height
            pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]])
            out.append(DetectedCode(kind="BARCODE", points=pts, data=data, rect=(x, y, w, h)))
        return out