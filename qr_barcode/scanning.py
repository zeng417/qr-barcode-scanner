"""后台识别与码实例跟踪；摄像头和窗口始终由主线程处理。"""

from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time

import numpy as np

from .detector import CodeDetector, DetectedCode


@dataclass
class ScanResult:
    captured_at: float
    codes: list[DetectedCode]
    error: Exception | None = None


class DetectionWorker:
    """待识别帧只保留最新一帧；已完成的结果等待主线程领取。"""

    def __init__(self, detector: CodeDetector, empty_every: int = 1) -> None:
        self._detector = detector
        self._empty_every = max(1, empty_every)
        self._skip = 0
        self._empty = True
        self._condition = threading.Condition()
        self._pending: tuple[float, np.ndarray] | None = None
        self._results: queue.Queue[ScanResult] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="code-detector", daemon=True)
        self._thread.start()

    def submit(self, frame: np.ndarray, captured_at: float | None = None) -> None:
        with self._condition:
            if self._stop.is_set():
                return
            if self._empty and self._skip > 0:
                self._skip -= 1
                return
            self._skip = self._empty_every - 1 if self._empty else 0
            # 主线程会在原帧上绘制，后台必须持有独立副本。
            self._pending = (time.monotonic() if captured_at is None else captured_at,
                             frame.copy())
            self._condition.notify()

    def poll(self) -> ScanResult | None:
        try:
            return self._results.get_nowait()
        except queue.Empty:
            return None

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._condition:
                self._condition.wait_for(lambda: self._pending is not None or self._stop.is_set())
                if self._stop.is_set():
                    return
                captured_at, frame = self._pending
                self._pending = None
            try:
                result = ScanResult(captured_at, self._detector.detect(frame))
            except Exception as exc:
                result = ScanResult(captured_at, [], exc)
            with self._condition:
                self._empty = not result.codes
            # 不覆盖已完成的结果，以免丢失短暂出现的扫码事件。
            while not self._stop.is_set():
                try:
                    self._results.put(result, timeout=0.1)
                    break
                except queue.Full:
                    pass
            if result.error is not None:
                return

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._pending = None
            self._condition.notify_all()
        # 本地原生解码无法中断；不让异常缓慢的解码阻塞关闭窗口。
        self._thread.join(timeout=1.0)


@dataclass
class _CodeTrack:
    code: DetectedCode
    missed: int = 0


class ScanDeduplicator:
    """按类型、内容及位置一对一关联；新实例和离开后重现的实例生成事件。"""

    def __init__(self, max_missed_detections: int = 0,
                 max_center_distance: float = 0.75) -> None:
        if max_missed_detections < 0 or max_center_distance <= 0:
            raise ValueError("Invalid tracking miss limit or center distance")
        self.max_missed_detections = max_missed_detections
        self.max_center_distance = max_center_distance
        self._tracks: list[_CodeTrack] = []

    def _distance(self, previous: DetectedCode, current: DetectedCode) -> float | None:
        if (previous.kind, previous.data) != (current.kind, current.data):
            return None
        if previous.points is None or current.points is None:
            # 无坐标时无法分辨实体，按同内容实例的数量延续，优先匹配有坐标的码。
            return self.max_center_distance + 1
        old_lo, old_hi = previous.points.min(axis=0), previous.points.max(axis=0)
        new_lo, new_hi = current.points.min(axis=0), current.points.max(axis=0)
        old_center, new_center = (old_lo + old_hi) / 2, (new_lo + new_hi) / 2
        size = max(1.0, (np.linalg.norm(old_hi - old_lo) + np.linalg.norm(new_hi - new_lo)) / 2)
        distance = float(np.linalg.norm(new_center - old_center) / size)
        return distance if distance <= self.max_center_distance else None

    def update(self, codes: list[DetectedCode]) -> list[DetectedCode]:
        """仅在收到一次完整检测结果后调用；跳帧或尚无结果不代表实例离开。"""
        candidates = {index: [] for index in range(len(codes))}
        for old_index, track in enumerate(self._tracks):
            for new_index, code in enumerate(codes):
                distance = self._distance(track.code, code)
                if distance is not None:
                    candidates[new_index].append((distance, old_index))

        for choices in candidates.values():
            choices.sort()
        matched_old = {}
        matches = {}

        def assign(new_index, visited):
            for _, old_index in candidates[new_index]:
                if old_index in visited:
                    continue
                visited.add(old_index)
                if old_index not in matched_old or assign(matched_old[old_index], visited):
                    matched_old[old_index] = new_index
                    matches[new_index] = old_index
                    return True
            return False

        # 从最近位置开始一对一关联，允许重新分配已有配对。
        # 单纯贪心会抢占其他实例唯一能匹配的位置，错误地产生“新码”事件。
        for new_index in sorted(candidates, key=lambda index: (
                candidates[index][0][0] if candidates[index] else float("inf"), index)):
            assign(new_index, set())

        fresh, tracks = [], []
        for new_index, code in enumerate(codes):
            if new_index in matches:
                track = self._tracks[matches[new_index]]
                if code.points is not None:
                    track.code = code
                track.missed = 0
            else:
                fresh.append(code)
                track = _CodeTrack(code)
            tracks.append(track)

        for old_index, track in enumerate(self._tracks):
            if old_index not in matched_old:
                track.missed += 1
                if track.missed <= self.max_missed_detections:
                    tracks.append(track)
        self._tracks = tracks
        return fresh
