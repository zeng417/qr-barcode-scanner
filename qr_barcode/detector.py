"""检测模块：zxing-cpp 为主，pyzbar、OpenCV QRCodeDetector 补充。

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

# 解码库均为可选依赖，缺少时自动降级
try:
    import zxingcpp
    _HAS_ZXING = True
except Exception:  # pragma: no cover - 环境无关
    zxingcpp = None
    _HAS_ZXING = False

try:
    from pyzbar import pyzbar
    _HAS_PYZBAR = True
except Exception:  # pragma: no cover - 环境无关
    pyzbar = None
    _HAS_PYZBAR = False


@dataclass
class DetectedCode:
    kind: str  # "QR" | "BARCODE"
    points: Optional[np.ndarray]  # Nx2 的四边形顶点坐标
    data: str
    rect: Optional[tuple] = None  # 条码的近似矩形 (x, y, w, h)


class CodeDetector:
    """多引擎组合的二维码/条码检测器。

    识别顺序：zxing-cpp -> pyzbar -> OpenCV，均失败后再做放大/二值化兜底。
    """

    def __init__(self, retry_every: int = 5) -> None:
        self._qr = cv2.QRCodeDetector()
        # 没识别到码时，每隔 retry_every 帧才跑一次较重的兜底，避免拖慢实时画面
        self._retry_every = max(1, retry_every)
        self._failed_frames = 0

    @property
    def barcode_supported(self) -> bool:
        return _HAS_PYZBAR or _HAS_ZXING

    @property
    def engines(self) -> list[str]:
        """当前可用的解码引擎名称。"""
        names = []
        if _HAS_ZXING:
            names.append("zxing-cpp")
        if _HAS_PYZBAR:
            names.append("pyzbar")
        names.append("OpenCV")
        return names

    def detect(self, frame: np.ndarray) -> list[DetectedCode]:
        """对单帧图像执行识别，返回命中的码列表。"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame

        # zxing-cpp 对实拍码（透视、模糊、屏幕摩尔纹、反色）识别率最高且耗时最低
        results = self._decode_zxing(gray)
        if not results and _HAS_PYZBAR:
            results = self._decode_zbar(gray)
        if any(code.kind == "QR" for code in results):
            self._failed_frames = 0
            return results

        # 未识别到二维码时才执行较重的兜底策略
        self._failed_frames += 1
        if self._failed_frames >= self._retry_every:
            self._failed_frames = 0
            results.extend(self._detect_qr_fallback(gray))
        return results

    def _decode_zxing(self, image: np.ndarray, scale: float = 1.0) -> list[DetectedCode]:
        """用 zxing-cpp 解码二维码/一维条码，结果坐标换算回原图。"""
        if not _HAS_ZXING:
            return []
        try:
            found = zxingcpp.read_barcodes(image)
        except Exception:  # 解码库对异常图像可能直接报错
            return []

        out: list[DetectedCode] = []
        for result in found:
            if not result.valid or not result.text:
                continue
            pos = result.position
            points = np.array(
                [
                    [pos.top_left.x, pos.top_left.y],
                    [pos.top_right.x, pos.top_right.y],
                    [pos.bottom_right.x, pos.bottom_right.y],
                    [pos.bottom_left.x, pos.bottom_left.y],
                ],
                dtype=np.float32,
            )
            kind = "QR" if "QR" in result.format.name else "BARCODE"
            out.append(
                DetectedCode(
                    kind=kind,
                    points=(points / scale).astype(int),
                    data=result.text,
                )
            )
        return out

    def _decode_zbar(self, image: np.ndarray, scale: float = 1.0) -> list[DetectedCode]:
        """用 zbar 解码二维码/一维条码，结果坐标换算回原图。"""
        if not _HAS_PYZBAR:
            return []
        try:
            found = pyzbar.decode(image)
        except Exception:  # zbar 对异常图像可能直接报错
            return []

        out: list[DetectedCode] = []
        for obj in found:
            data = obj.data.decode("utf-8", errors="replace")
            if not data:
                continue
            if obj.type == "QRCODE" and obj.polygon:
                points = np.array(
                    [[p.x, p.y] for p in obj.polygon], dtype=np.float32
                )
                out.append(
                    DetectedCode(kind="QR", points=(points / scale).astype(int), data=data)
                )
                continue

            x, y, w, h = obj.rect
            points = np.array(
                [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float32
            )
            # 少数情况下二维码没有 polygon，此时用外接矩形标记为二维码
            kind = "QR" if obj.type == "QRCODE" else "BARCODE"
            out.append(
                DetectedCode(
                    kind=kind,
                    points=(points / scale).astype(int),
                    data=data,
                    rect=(
                        int(x / scale),
                        int(y / scale),
                        int(w / scale),
                        int(h / scale),
                    ),
                )
            )
        return out

    @staticmethod
    def _make_qr(data: str, points: Optional[np.ndarray]) -> DetectedCode:
        """构造二维码结果。"""
        pts = None if points is None else points.reshape(-1, 2).astype(int)
        return DetectedCode(kind="QR", points=pts, data=data)

    def _decode_qr_curved(self, image: np.ndarray) -> tuple[str, Optional[np.ndarray]]:
        """处理曲面/透视畸变的二维码（部分 OpenCV 版本不支持则跳过）。"""
        if not hasattr(self._qr, "detectAndDecodeCurved"):
            return "", None
        try:
            data, points, _ = self._qr.detectAndDecodeCurved(image)
        except cv2.error:
            return "", None
        return data, points

    def _detect_qr_fallback(self, gray: np.ndarray) -> list[DetectedCode]:
        """放大 + 锐化 / 二值化后再次解码，适合画面中较小、较模糊的码。"""
        big = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        sharp = cv2.addWeighted(big, 2.0, cv2.GaussianBlur(big, (0, 0), 2), -1.0, 0)
        _, otsu = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants = (sharp, otsu)

        # zxing-cpp 最强，逐一变体尝试
        for variant in variants:
            found = [
                code for code in self._decode_zxing(variant, 2.0) if code.kind == "QR"
            ]
            if found:
                return found[:1]

        # zxing 未命中时再让 zbar 补一次（不同引擎对不同模糊码各有优势）
        found = [
            code for code in self._decode_zbar(sharp, 2.0) if code.kind == "QR"
        ]
        if found:
            return found[:1]

        # zxing/pyzbar 均不可用时，用 OpenCV 常规解码作为降级方案
        if not _HAS_ZXING and not _HAS_PYZBAR:
            try:
                data, points, _ = self._qr.detectAndDecode(gray)
            except cv2.error:
                data, points = "", None
            if data:
                return [self._make_qr(data, points)]

        # 曲面/畸变码
        data, points = self._decode_qr_curved(gray)
        if data:
            return [self._make_qr(data, points)]
        return []
