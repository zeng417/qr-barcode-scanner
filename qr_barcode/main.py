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
import sqlite3
import time
import webbrowser
import uuid

import cv2

from .config import (
    AUTO_OPEN_URL,
    CAMERA_HEIGHT,
    CAMERA_READ_FAILURE_LIMIT,
    CAMERA_RETRY_DELAY,
    CAMERA_INDEX,
    CAMERA_WIDTH,
    CSV_HEADER,
    DETECT_EVERY_N_FRAMES,
    DETECTION_CLOSE_TIMEOUT,
    EVENT_DATABASE_PATH,
    ENHANCE_EVERY_N_DETECTIONS,
    ENHANCE_INTERVAL_SECONDS,
    MQTT_ACK_TIMEOUT,
    MQTT_MAX_ATTEMPTS,
    MQTT_BROKER,
    MQTT_CLIENT_ID,
    MQTT_ENABLED,
    MQTT_CLOSE_TIMEOUT,
    MQTT_MAX_PENDING,
    MQTT_PASSWORD,
    MQTT_PORT,
    MQTT_QOS,
    MQTT_TOPIC,
    MQTT_USERNAME,
    RECORD_CSV,
    RESULT_MAX_AGE_SECONDS,
    SCAN_MAX_CENTER_DISTANCE,
    SCAN_MAX_MISSED_DETECTIONS,
    STORAGE_MAX_BUFFER,
    STORAGE_CLOSE_TIMEOUT,
)
from .detector import CodeDetector, DetectedCode
from .publisher import MqttPublisher
from .storage import EventRecorder
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
        outbox_path=EVENT_DATABASE_PATH,
        max_pending=MQTT_MAX_PENDING,
        close_timeout=MQTT_CLOSE_TIMEOUT,
        ack_timeout=MQTT_ACK_TIMEOUT,
        max_attempts=MQTT_MAX_ATTEMPTS,
    )
    if not publisher.available:
        print("[提示] MQTT 已启用但未安装 paho-mqtt，推送未启动，事件仍将保留在本地。"
              "可执行: pip install paho-mqtt")
        return None
    print(f"[信息] MQTT 推送: {MQTT_TOPIC} @ {MQTT_BROKER}:{MQTT_PORT}")
    return publisher


def save_snapshot(frame, directory: str) -> bool:
    path = os.path.join(directory, f"frame_{time.time_ns()}_{uuid.uuid4().hex[:8]}.png")
    try:
        os.makedirs(directory, exist_ok=True)
        if not cv2.imwrite(path, frame):
            raise OSError("图像编码器未能写入文件")
    except (OSError, cv2.error) as exc:
        print(f"[错误] 保存画面失败: {exc}")
        return False
    print(f"[调试] 已保存当前画面: {path}")
    return True


def main() -> int:
    detector = CodeDetector(ENHANCE_EVERY_N_DETECTIONS, ENHANCE_INTERVAL_SECONDS)
    deduplicator = ScanDeduplicator(SCAN_MAX_MISSED_DETECTIONS, SCAN_MAX_CENTER_DISTANCE)
    try:
        publisher = create_publisher()
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"[错误] MQTT 启动失败，事件将保留在本地等待下次启动: {exc}")
        publisher = None
    recorder = EventRecorder(EVENT_DATABASE_PATH, RECORD_CSV, CSV_HEADER,
                             topic=MQTT_TOPIC if MQTT_ENABLED else None, qos=MQTT_QOS,
                             max_pending=MQTT_MAX_PENDING, max_buffer=STORAGE_MAX_BUFFER,
                             on_record=publisher.wake if publisher is not None else None,
                             close_timeout=STORAGE_CLOSE_TIMEOUT)
    opened_links: set[str] = set()
    codes: list[DetectedCode] = []
    result_at = float("-inf")
    status = 0
    worker = cap = None
    window_ready = False

    def handle_result(result, open_urls=True):
        nonlocal codes, result_at, status
        if result.error is not None:
            print(f"[错误] 后台识别失败: {result.error}")
            status = 1
            return
        codes, result_at = result.codes, result.captured_at
        for code in deduplicator.update(codes):
            print(f"[{code.kind}] {code.data}")
            if not recorder.add(code.kind, code.data):
                print("[错误] 扫描事件未能进入存储缓冲，请检查磁盘和缓冲容量。")
                status = 2
            if open_urls and code.data not in opened_links:
                try:
                    if maybe_open_url(code.data):
                        opened_links.add(code.data)
                except (OSError, webbrowser.Error) as exc:
                    print(f"[错误] 打开链接失败: {exc}")

    try:
        print(f"[信息] 解码引擎: {' + '.join(detector.engines)}")
        if not detector.barcode_supported:
            print("[提示] 当前仅支持二维码，可安装 zxing-cpp 或 pyzbar 启用条码。")
        cap = cv2.VideoCapture(CAMERA_INDEX)
        if not cap.isOpened():
            print(f"[错误] 无法打开摄像头 {CAMERA_INDEX}")
            status = 1
        else:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
            print(f"[信息] 采集分辨率: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
                  f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
            worker = DetectionWorker(detector, DETECT_EVERY_N_FRAMES)
            print("按 Q / Esc 退出，按 S 保存当前画面用于排查。")
            read_failures = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    read_failures += 1
                    if read_failures >= max(1, CAMERA_READ_FAILURE_LIMIT):
                        print("[错误] 摄像头连续读取失败，停止采集。")
                        status = 1
                        break
                    print(f"[提示] 读取画面失败，重试 {read_failures}/{CAMERA_READ_FAILURE_LIMIT}")
                    if cv2.waitKey(max(1, int(CAMERA_RETRY_DELAY * 1000))) & 0xFF in (ord('q'), 27):
                        break
                    continue
                read_failures = 0
                if not window_ready:
                    h, w = frame.shape[:2]
                    win_w, win_h = window_size(w, h)
                    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO | cv2.WINDOW_GUI_NORMAL)
                    cv2.resizeWindow(WINDOW_NAME, win_w, win_h)
                    window_ready = True
                captured_at = time.monotonic()
                result = worker.poll()
                if result is not None:
                    handle_result(result)
                    if result.error is not None:
                        break
                worker.submit(frame, captured_at)
                if captured_at - result_at <= RESULT_MAX_AGE_SECONDS:
                    for code in codes:
                        draw_code(frame, code)
                cv2.imshow(WINDOW_NAME, frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord('s'):
                    if not save_snapshot(frame, os.path.dirname(RECORD_CSV) or '.'):
                        status = 2
                if key in (ord('q'), 27):
                    break
                if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                    break
    except (OSError, cv2.error) as exc:
        print(f"[错误] 采集或显示失败: {exc}")
        status = 1
    finally:
        if cap is not None:
            cap.release()
        cv2.destroyAllWindows()
        if worker is not None:
            for result in worker.close(timeout=DETECTION_CLOSE_TIMEOUT):
                handle_result(result, open_urls=False)
        if not recorder.close():
            status = 2
        if publisher is not None:
            try:
                publisher.close()
            except (OSError, sqlite3.Error) as exc:
                print(f"[错误] MQTT 清理失败，未确认事件保留在本地: {exc}")
                status = 2
    if status == 0:
        print(f"[完成] 事件已入库，CSV 已导出至 {RECORD_CSV}")
    else:
        print("[退出] 存在采集、保存或存储错误，请检查上方日志。")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
