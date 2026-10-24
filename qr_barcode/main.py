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
    ENHANCE_EVERY_N_DETECTIONS,
    ENHANCE_INTERVAL_SECONDS,
    MQTT_BROKER,
    MQTT_CLIENT_ID,
    MQTT_ENABLED,
    MQTT_CLOSE_TIMEOUT,
    MQTT_MAX_PENDING,
    MQTT_OUTBOX_PATH,
    MQTT_PASSWORD,
    MQTT_PORT,
    MQTT_QOS,
    MQTT_TOPIC,
    MQTT_USERNAME,
    RECORD_CSV,
    RESULT_MAX_AGE_SECONDS,
    SCAN_MAX_CENTER_DISTANCE,
    SCAN_MAX_MISSED_DETECTIONS,
)
from .detector import CodeDetector, DetectedCode
from .publisher import MqttPublisher
from .recorder import CsvRecorder
from .scanning import DetectionWorker, ScanDeduplicator

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


def create_publisher() -> MqttPublisher | None:
    """按配置创建 MQTT 发布器；未启用或缺依赖时返回 None。"""
    if not MQTT_ENABLED:
        return None
    publisher = MqttPublisher(
        MQTT_BROKER,
        MQTT_PORT,
        MQTT_TOPIC,
        client_id=MQTT_CLIENT_ID,
        username=MQTT_USERNAME,
        password=MQTT_PASSWORD,
        qos=MQTT_QOS,
        outbox_path=MQTT_OUTBOX_PATH,
        max_pending=MQTT_MAX_PENDING,
        close_timeout=MQTT_CLOSE_TIMEOUT,
    )
    if not publisher.available:
        print("[提示] MQTT 已启用但未安装 paho-mqtt，推送已关闭。"
              "可执行: pip install paho-mqtt")
        return None
    print(f"[信息] MQTT 推送: {MQTT_TOPIC} @ {MQTT_BROKER}:{MQTT_PORT}")
    return publisher


def main() -> int:
    detector = CodeDetector(ENHANCE_EVERY_N_DETECTIONS, ENHANCE_INTERVAL_SECONDS)
    recorder = CsvRecorder(RECORD_CSV, CSV_HEADER)
    publisher = create_publisher()
    opened_links: set[str] = set()

    print(f"[信息] 解码引擎: {' + '.join(detector.engines)}")
    if not detector.barcode_supported:
        print("[提示] zxing-cpp/pyzbar 均未安装，仅支持二维码识别。"
              "可执行: pip install -r requirements.txt")

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print(f"[错误] 无法打开摄像头 {CAMERA_INDEX}")
        if publisher is not None:
            publisher.close()
        return 1

    # 尽量使用高分辨率采集，提升小码/远距离的识别率
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    print(f"[信息] 采集分辨率: "
          f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")

    print("按 Q / Esc 退出，按 S 保存当前画面用于排查。")

    codes: list[DetectedCode] = []
    deduplicator = ScanDeduplicator(SCAN_MAX_MISSED_DETECTIONS, SCAN_MAX_CENTER_DISTANCE)
    result_at = float("-inf")
    worker = None
    window_ready = False

    try:
        worker = DetectionWorker(detector, DETECT_EVERY_N_FRAMES)
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

            captured_at = time.monotonic()
            result = worker.poll()
            if result is not None:
                if result.error is not None:
                    raise RuntimeError("后台识别失败") from result.error
                codes, result_at = result.codes, result.captured_at
                for code in deduplicator.update(codes):
                    print(f"[{code.kind}] {code.data}")
                    recorder.add(code.kind, code.data)
                    if publisher is not None and not publisher.publish(code.kind, code.data):
                        print("[错误] MQTT 事件未能入队，识别结果已保存在 CSV，请检查待发队列。")
                    if code.data not in opened_links and maybe_open_url(code.data):
                        opened_links.add(code.data)
            worker.submit(frame, captured_at)

            if captured_at - result_at <= RESULT_MAX_AGE_SECONDS:
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
        if worker is not None:
            worker.close()
        cap.release()
        cv2.destroyAllWindows()
        if publisher is not None:
            publisher.close()

    print(f"[完成] 识别历史已保存至 {RECORD_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
