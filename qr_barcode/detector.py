"""检测模块：zxing-cpp 为主，pyzbar、OpenCV QRCodeDetector 补充。

每个检测结果是一个 DetectedCode 对象：
- kind: "QR" 或 "BARCODE"
- points: 多边形包围框顶点（接受 numpy 数组或列表，构造后统一为 int32 数组）
- data: 解码出的文本
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import copy
import time
from typing import Optional

import cv2
import numpy as np

from .qr_regions import iter_qr_regions

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
    source: str = ""  # 二维码内容冲突按引擎优先级处理，一维码内容冲突暂缓
    confirmed_by: frozenset[str] = frozenset()  # 同一帧同一位置同内容的引擎证据

    def __post_init__(self) -> None:
        if self.points is not None:
            self.points = np.asarray(self.points, dtype=np.int32).reshape(-1, 2)
            if len(self.points) < 3:
                raise ValueError("points must contain at least three polygon vertices")


class CodeDetector:
    """多引擎组合的二维码/条码检测器。

    合并各引擎结果，均失败后限频执行补白边、放大/二值化兜底。
    """

    def __init__(self, retry_every: int = 5, retry_interval: float = 0.5,
                 require_barcode_agreement: bool = False) -> None:
        self._qr = cv2.QRCodeDetector()
        self._require_barcode_agreement = require_barcode_agreement
        # 连续无码达到 retry_every 次检测且满足时间间隔后，才执行增强识别
        self._retry_every = max(1, retry_every)
        self._failed_frames = 0
        self._retry_interval = max(0.0, retry_interval)
        self._last_retry = float("-inf")
        self._barcode_enhancement_active = False

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
        if not results:
            results.extend(self._decode_qr_regions(gray))
        accepted = self._accepted_results(results)
        if accepted and not (self._barcode_enhancement_active
                             and not any(code.kind == 'BARCODE' for code in accepted)):
            self._barcode_enhancement_active = False
            self._failed_frames = 0
            return accepted

        # 没有可接受的结果时增强；原始候选被过滤后也必须允许重试。
        self._failed_frames += 1
        now = time.monotonic()
        if (self._barcode_enhancement_active
                or (self._failed_frames >= self._retry_every
                    and now - self._last_retry >= self._retry_interval)):
            self._failed_frames = 0
            self._last_retry = now
            results.extend(self._detect_qr_fallback(gray))
            accepted = self._accepted_results(results)
            # 增强后才能读出的条码必须在后续帧继续尝试增强，否则中间空帧
            # 会打断事件层的连续确认，导致永远不能上报。增强再次失败即恢复限频。
            self._barcode_enhancement_active = any(code.kind == 'BARCODE' for code in accepted)
        return accepted

    def _accepted_results(self, results: list[DetectedCode]) -> list[DetectedCode]:
        merged = self._merge_results(results)
        if self._require_barcode_agreement and _HAS_ZXING and _HAS_PYZBAR:
            merged = [code for code in merged if code.kind != 'BARCODE'
                      or {'zxing-cpp', 'pyzbar'} <= code.confirmed_by]
        return merged

    def _decode_qr_regions(self, gray: np.ndarray) -> list[DetectedCode]:
        """原图失败时提取深色背景上的码，再将区域中的识别框映射回原图。"""
        found = []
        for region, to_scene in iter_qr_regions(gray):
            results = self._decode_zxing(region)
            results.extend(self._decode_zbar(region))
            results.extend(self._decode_opencv(region))
            for code in results:
                if code.points is not None:
                    code.points = np.rint(cv2.perspectiveTransform(
                        code.points.astype(np.float32)[None], to_scene)[0]).astype(np.int32)
                # 区域内的轴对齐矩形经过透视映射后不再是原图上的矩形。
                code.rect = None
            found.extend(results)
        return self._merge_results(found)

    @staticmethod
    def _box_iou(first: DetectedCode, second: DetectedCode) -> float:
        if first.points is None or second.points is None:
            return 0.0
        lo = np.maximum(first.points.min(axis=0), second.points.min(axis=0))
        hi = np.minimum(first.points.max(axis=0), second.points.max(axis=0))
        intersection = float(np.prod(np.maximum(hi - lo, 0)))
        area = float(np.prod(np.ptp(first.points, axis=0)))
        second_area = float(np.prod(np.ptp(second.points, axis=0)))
        union = area + second_area - intersection
        return intersection / union if union > 0 else 0.0

    @staticmethod
    def _same_region(first: DetectedCode, second: DetectedCode) -> bool:
        if CodeDetector._box_iou(first, second) >= 0.7:
            return True
        # 一维码的扫描线覆盖范围常因引擎而异：一个框可能仅覆盖另一个框
        # 的部分宽度或高度。只对两个已知引擎使用较小框覆盖率来关联。
        if (first.kind != 'BARCODE' or second.kind != 'BARCODE'
                or {first.source, second.source} != {'zxing-cpp', 'pyzbar'}
                or first.points is None or second.points is None):
            return False
        lo = np.maximum(first.points.min(axis=0), second.points.min(axis=0))
        hi = np.minimum(first.points.max(axis=0), second.points.max(axis=0))
        intersection = float(np.prod(np.maximum(hi - lo, 0)))
        smaller = min(float(np.prod(np.ptp(first.points, axis=0))),
                      float(np.prod(np.ptp(second.points, axis=0))))
        return smaller > 0 and intersection / smaller >= 0.8

    @staticmethod
    def _merge_results(codes: list[DetectedCode]) -> list[DetectedCode]:
        """合并同位置重复框；一维码同位置内容冲突时全部暂缓。"""
        merged: list[DetectedCode] = []
        rejected = set()
        for index, code in enumerate(codes):
            if code.kind != 'BARCODE':
                continue
            for other_index in range(index + 1, len(codes)):
                other = codes[other_index]
                if (other.kind == 'BARCODE' and code.data != other.data
                        and CodeDetector._same_region(code, other)):
                    rejected.update((index, other_index))
        priority = {"zxing-cpp": 0, "pyzbar": 1, "OpenCV": 2}
        accepted = [code for index, code in enumerate(codes) if index not in rejected]
        for candidate in sorted(accepted, key=lambda item: priority.get(item.source, 3)):
            # 合并证据只写入输出副本，不能给上游对象留下跨帧的引擎证据。
            code = copy(candidate)
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
                if CodeDetector._same_region(code, existing):
                    if code.kind == 'BARCODE' and code.data == existing.data:
                        existing.confirmed_by = (existing.confirmed_by | code.confirmed_by
                                                 | {existing.source, code.source}) - {''}
                    duplicate = True
                    break
            if not duplicate:
                merged.append(code)
        return merged

    def _decode_opencv(self, image: np.ndarray, scale: float = 1.0,
                       offset: tuple[int, int] = (0, 0)) -> list[DetectedCode]:
        """支持多码；先扣除输入图上的补边偏移，再按缩放比例还原坐标。"""
        results = []
        if hasattr(self._qr, "detectAndDecodeMulti"):
            try:
                ok, texts, points, _ = self._qr.detectAndDecodeMulti(image)
                if ok and points is not None:
                    results = [self._make_qr(text, (pts - offset) / scale)
                               for text, pts in zip(texts, points) if text]
            except (cv2.error, UnicodeError):
                pass
        if not results:
            try:
                data, points, _ = self._qr.detectAndDecode(image)
                if data:
                    results.append(self._make_qr(
                        data, None if points is None else (points - offset) / scale))
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
        """补足裁边图的静区，并合并放大、锐化 / 二值化后的结果。"""
        # 紧贴图像边缘的二维码可能被 OpenCV 错定位。补白边恢复静区；
        # 放在限频兜底中，避免每个无码摄像头帧都额外调用解码器。
        border = max(16, round(min(gray.shape[:2]) * 0.2))
        padded = cv2.copyMakeBorder(gray, border, border, border, border,
                                    cv2.BORDER_CONSTANT, value=255)
        found = self._decode_opencv(padded, offset=(border, border))

        big = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        sharp = cv2.addWeighted(big, 2.0, cv2.GaussianBlur(big, (0, 0), 2), -1.0, 0)
        _, otsu = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants = (sharp, otsu)

        for variant in variants:
            found.extend(self._decode_zxing(variant, 2.0))
            found.extend(self._decode_zbar(variant, 2.0))
            found.extend(self._decode_opencv(variant, 2.0))

        # 曲面/畸变码
        data, points = self._decode_qr_curved(gray)
        if data:
            found.append(self._make_qr(data, points))
        return self._merge_results(found)
