"""记录持久化：识别历史写入 CSV 文件。"""

from __future__ import annotations

import csv
import datetime
import os
import threading


class CsvRecorder:
    """线程安全的 CSV 记录器，写入识别历史。"""

    def __init__(self, path: str, header: list[str]) -> None:
        self.path = path
        self.header = header
        self._lock = threading.Lock()
        self._ensure_file()

    def _ensure_file(self) -> None:
        dirname = os.path.dirname(self.path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            with self._lock, open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.header)

    def add(self, kind: str, content: str) -> None:
        """新增一条记录。"""
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with self._lock, open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([ts, kind, content])

    def load(self) -> list[dict]:
        """读取全部历史记录（供展示/回显用）。"""
        if not os.path.exists(self.path):
            return []
        with self._lock, open(self.path, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))