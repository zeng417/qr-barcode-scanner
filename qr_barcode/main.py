"""二维码 & 条形码实时识别器 主程序。

功能：
- 摄像头实时画面
- 自动识别二维码与一维条码（zxing-cpp / pyzbar / OpenCV）
- 在画面绘制包围框并显示解码文本
- 识别结果打印并写入 CSV
- 可选：识别到链接自动打开浏览器
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import webbrowser

import cv2

from .config import (
    AUTO_OPEN_URL,
    CAMERA_HEIGHT,
    CAMERA_INDEX,
    CAMERA_WIDTH,
    CSV_HEADER,
    DETECT_EVERY_N_FRAMES,
    RECORD_CSV,
)
from .detector import CodeDetector, DetectedCode
from .recorder import CsvRecorder

BOX_COLORS = {
    "QR": (0, 255, 0),        # 绿色
    "BARCODE": (255, 200, 0), # 蓝色
}

WINDOW_NAME = "QR / Barcode Scanner"

# 窗口初始大小占屏幕的比例，避免一打开就过小或超出屏幕
WINDOW_SCREEN_RATIO = 0.85
WINDOW_MAX_WIDTH = 1600


def screen_size() -> tuple[int, int]:
    """获取主显示器分辨率，获取失败时返回 1920x1080。"""
    # xrandr 适用于 X11；部分 Wayland 会话下取不到时退回 xdpyinfo
    for cmd, pattern in (
        (["xrandr", "--current"], r"current (\d+) x (\d+)"),
        (["xdpyinfo"], r"dimensions:\s+(\d+)x(\d+)"),
    ):
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=2,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(pattern, out)
        if match:
            return int(match.group(1)), int(match.group(2))
    return 1920, 1080


def window_size(frame_w: int, frame_h: int) -> tuple[int, int]:
    """按画面比例计算合适的初始窗口尺寸。"""
    screen_w, screen_h = screen_size()
    width = min(int(screen_w * WINDOW_SCREEN_RATIO), WINDOW_MAX_WIDTH)
    height = round(width * frame_h / frame_w)
    if height > int(screen_h * WINDOW_SCREEN_RATIO):
        height = int(screen_h * WINDOW_SCREEN_RATIO)
        width = round(height * frame_w / frame_h)
    return width, height


def draw_code(frame, code: DetectedCode) -> None:
    """在画面上绘制包围框与解码文本。"""
    if code.points is None:
        return
    color = BOX_COLORS.get(code.kind, (0, 0, 255))
    poly = code.points.reshape(-1, 2)
    cv2.polylines(frame, [poly], isClosed=True, color=color, thickness=2)

    label = f"{code.kind}: {code.data}"
    (x, y) = poly.reshape(-1, 2).min(axis=0).astype(int)
    org = (x, max(y - 5, 20))
    cv2.putText(frame, label, org, cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                color, 2, cv2.LINE_AA)


def maybe_open_url(data: str) -> bool:
    """若是合法 http(s) 链接，打开浏览器并返回 True。"""
    if not AUTO_OPEN_URL:
        return False
    stripped = data.strip()
    if stripped.lower().startswith(("http://", "https://")):
        webbrowser.open(stripped)
        return True
    return False


def main() -> int:
    detector = CodeDetector()
    recorder = CsvRecorder(RECORD_CSV, CSV_HEADER)
    opened_links: set[str] = set()

    print(f"[信息] 解码引擎: {' + '.join(detector.engines)}")
    if not detector.barcode_supported:
        print("[提示] zxing-cpp/pyzbar 均未安装，仅支持二维码识别。"
              "可执行: pip install -r requirements.txt")

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print(f"[错误] 无法打开摄像头 {CAMERA_INDEX}")
        return 1

    # 尽量使用高分辨率采集，提升小码/远距离的识别率
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    print(f"[信息] 采集分辨率: "
          f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")

    print("按 Q / Esc 退出，按 S 保存当前画面用于排查。")

    codes: list[DetectedCode] = []
    active: set[tuple[str, str]] = set()  # 当前画面中的码，用于去重
    detect_cooldown = 0
    window_ready = False

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("[错误] 读取摄像头画面失败")
                break

            # 首帧到达后再按画面比例确定窗口初始尺寸
            if not window_ready:
                h, w = frame.shape[:2]
                win_w, win_h = window_size(w, h)
                # 使用精简窗口（隐藏 OpenCV 自带工具栏按钮），并保持画面比例
                cv2.namedWindow(
                    WINDOW_NAME,
                    cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO | cv2.WINDOW_GUI_NORMAL,
                )
                cv2.resizeWindow(WINDOW_NAME, win_w, win_h)
                print(f"[信息] 窗口尺寸: {win_w}x{win_h}")
                window_ready = True

            # 无码时才按 DETECT_EVERY_N_FRAMES 间隔重试，避免空转消耗 CPU
            if detect_cooldown > 0:
                detect_cooldown -= 1
            else:
                codes = detector.detect(frame)
                if not codes and DETECT_EVERY_N_FRAMES > 1:
                    detect_cooldown = DETECT_EVERY_N_FRAMES - 1

                current = {(code.kind, code.data) for code in codes}
                for code in codes:
                    # 同一码持续出现在画面中时只记录一次
                    if (code.kind, code.data) in active:
                        continue
                    print(f"[{code.kind}] {code.data}")
                    recorder.add(code.kind, code.data)
                    if code.data not in opened_links and maybe_open_url(code.data):
                        opened_links.add(code.data)
                active = current

            for code in codes:
                draw_code(frame, code)

            cv2.imshow(WINDOW_NAME, frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("s"):
                out_dir = os.path.dirname(RECORD_CSV) or "."
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(out_dir, f"frame_{int(time.time())}.png")
                cv2.imwrite(path, frame)
                print(f"[调试] 已保存当前画面: {path}")
            if key in (ord("q"), 27):  # q 或 Esc
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()

    print(f"[完成] 识别历史已保存至 {RECORD_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())