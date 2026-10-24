"""检测模块：zxing-cpp 为主，pyzbar、OpenCV QRCodeDetector 补充。

每个检测结果是一个 DetectedCode 对象：
- kind: "QR" 或 "BARCODE"
- points: 多边形包围框顶点（接受 numpy 数组或列表，构造后统一为 int32 数组）
- data: 解码出的文本
"""

from __future__ import annotations

from dataclasses import dataclass
import time
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
    points: Optional[np.ndarray | list]  # 构造时接受列表，内部统一为 Nx2 int32 数组
    data: str
    rect: Optional[tuple] = None  # 条码的近似矩形 (x, y, w, h)
    source: str = ""  # 跨引擎同位置的内容冲突按引擎优先级处理

    def __post_init__(self) -> None:
        if self.points is not None:
            self.points = np.asarray(self.points, dtype=np.int32).reshape(-1, 2)
            if len(self.points) < 3:
                raise ValueError("points must contain at least three polygon vertices")


class CodeDetector:
    """多引擎组合的二维码/条码检测器。

    合并各引擎结果，均失败后限频执行放大/二值化兜底。
    """

    def __init__(self, retry_every: int = 5, retry_interval: float = 0.5) -> None:
        self._qr = cv2.QRCodeDetector()
        # 连续无码达到 retry_every 次检测且满足时间间隔后，才执行增强识别
        self._retry_every = max(1, retry_every)
        self._failed_frames = 0
        self._retry_interval = max(0.0, retry_interval)
        self._last_retry = float("-inf")

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

        results = self._decode_zxing(gray)
        results.extend(self._decode_zbar(gray))
        results.extend(self._decode_opencv(gray))
        if results:
            self._failed_frames = 0
            return self._merge_results(results)

        # 仅在完全无码时增强；帧数和时间双重限频。
        self._failed_frames += 1
        now = time.monotonic()
        if (self._failed_frames >= self._retry_every
                and now - self._last_retry >= self._retry_interval):
            self._failed_frames = 0
            self._last_retry = now
            results.extend(self._detect_qr_fallback(gray))
        return self._merge_results(results)

    @staticmethod
    def _merge_results(codes: list[DetectedCode]) -> list[DetectedCode]:
        """以外接框标准 IoU >= 0.7 合并重复框；跨引擎同位置内容冲突时优先 ZXing。"""
        merged: list[DetectedCode] = []
        priority = {"zxing-cpp": 0, "pyzbar": 1, "OpenCV": 2}
        for code in sorted(codes, key=lambda item: priority.get(item.source, 3)):
            duplicate = False
            for existing in merged:
                if code.kind != existing.kind:
                    continue
                if code.data != existing.data and not (
                        code.source and existing.source and code.source != existing.source):
                    continue
                if code.points is None or existing.points is None:
                    # 缺少位置时无法判断是否为同一实体，保留结果供事件层关联。
                    continue
                lo = np.maximum(code.points.min(axis=0), existing.points.min(axis=0))
                hi = np.minimum(code.points.max(axis=0), existing.points.max(axis=0))
                intersection = float(np.prod(np.maximum(hi - lo, 0)))
                area = float(np.prod(np.ptp(code.points, axis=0)))
                existing_area = float(np.prod(np.ptp(existing.points, axis=0)))
                union = area + existing_area - intersection
                if union > 0 and intersection / union >= 0.7:
                    duplicate = True
                    break
            if not duplicate:
                merged.append(code)
        return merged

    def _decode_opencv(self, image: np.ndarray, scale: float = 1.0) -> list[DetectedCode]:
        """常规 OpenCV 解码始终参与，优先支持同帧多个二维码。"""
        results = []
        if hasattr(self._qr, "detectAndDecodeMulti"):
            try:
                ok, texts, points, _ = self._qr.detectAndDecodeMulti(image)
                if ok and points is not None:
                    results = [self._make_qr(text, pts / scale)
                               for text, pts in zip(texts, points) if text]
            except (cv2.error, UnicodeError):
                pass
        if not results:
            try:
                data, points, _ = self._qr.detectAndDecode(image)
                if data:
                    results.append(self._make_qr(
                        data, None if points is None else points / scale))
            except (cv2.error, UnicodeError):
                pass
        return results

    @staticmethod
    def _payload_text(raw: bytes) -> str:
        """UTF-8 文本直接保留；其他字节用可逆的十六进制表示，不替换或丢弃。"""
        try:
            return raw.decode("utf-8")
        except UnicodeError:
            return "hex:" + raw.hex()

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
            if not result.valid:
                continue
            raw = getattr(result, "bytes", None)
            data = self._payload_text(bytes(raw)) if raw is not None else result.text
            if not data:
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
                    data=data,
                    source="zxing-cpp",
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
            data = self._payload_text(obj.data)
            if not data:
                continue
            if obj.type == "QRCODE" and obj.polygon:
                points = np.array(
                    [[p.x, p.y] for p in obj.polygon], dtype=np.float32
                )
                out.append(
                    DetectedCode(kind="QR", points=(points / scale).astype(int), data=data,
                                 source="pyzbar")
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
                    source="pyzbar",
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
        return DetectedCode(kind="QR", points=pts, data=data, source="OpenCV")

    def _decode_qr_curved(self, image: np.ndarray) -> tuple[str, Optional[np.ndarray]]:
        """处理曲面/透视畸变的二维码（部分 OpenCV 版本不支持则跳过）。"""
        if not hasattr(self._qr, "detectAndDecodeCurved"):
            return "", None
        try:
            data, points, _ = self._qr.detectAndDecodeCurved(image)
        except (cv2.error, UnicodeError):
            return "", None
        return data, points

    def _detect_qr_fallback(self, gray: np.ndarray) -> list[DetectedCode]:
        """放大 + 锐化 / 二值化后再次解码，适合画面中较小、较模糊的码。"""
        big = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        sharp = cv2.addWeighted(big, 2.0, cv2.GaussianBlur(big, (0, 0), 2), -1.0, 0)
        _, otsu = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants = (sharp, otsu)

        found = []
        for variant in variants:
            found.extend(self._decode_zxing(variant, 2.0))
            found.extend(self._decode_zbar(variant, 2.0))
            found.extend(self._decode_opencv(variant, 2.0))

        # 曲面/畸变码
        data, points = self._decode_qr_curved(gray)
        if data:
            found.append(self._make_qr(data, points))
        return self._merge_results(found)
