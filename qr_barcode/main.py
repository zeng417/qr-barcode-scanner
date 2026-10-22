"""二维码 & 条形码实时识别器 主程序。

功能：
- 摄像头实时画面
- 自动识别二维码(OpenCV QRCodeDetector) 与一维条码(pyzbar)
- 在画面绘制包围框并显示解码文本
- 识别结果打印并写入 CSV
- 可选：识别到链接自动打开浏览器
"""

from __future__ import annotations

import webbrowser

import cv2

from .config import (
    AUTO_OPEN_URL,
    CAMERA_INDEX,
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


def draw_code(frame, code: DetectedCode) -> None:
    """在画面上绘制包围框与解码文本。"""
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

    if not detector.barcode_supported:
        print("[提示] pyzbar 未安装，仅支持二维码识别。"
              "可执行: pip install pyzbar (需系统 zbar 库)")

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print(f"[错误] 无法打开摄像头 {CAMERA_INDEX}")
        return 1

    print("按 Q / Esc 退出。")

    codes: list[DetectedCode] = []
    active: set[tuple[str, str]] = set()  # 当前画面中的码，用于去重
    detect_cooldown = 0

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("[错误] 读取摄像头画面失败")
                break

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

            cv2.imshow("QR / Barcode Scanner", frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):  # q 或 Esc
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()

    print(f"[完成] 识别历史已保存至 {RECORD_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())