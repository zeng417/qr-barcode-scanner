# 二维码 & 条形码实时识别器

基于 OpenCV 的摄像头实时扫码工具：自动识别 **QR 二维码** 与 **一维条形码**，
在画面绘制包围框显示解码内容，识别历史自动保存到本地 CSV。

## 功能

- 实时摄像头画面，自动检测码区域
- 二维码与一维条码识别（zxing-cpp 主引擎，pyzbar 与 OpenCV `QRCodeDetector` 补充）
- 在画面绘制多边形包围框并展示解码文本
- 识别失败时自动放大画面并锐化/二值化后重试，提升小码、模糊、远距离场景的识别率
- 识别成功打印结果并写入 CSV 持久化存储
- 可选：识别结果通过 MQTT 推送到 Broker，便于接入 IoT / 机器人
- 可选：识别到 `http(s)` 链接自动打开浏览器

## 环境要求

- Python 3.9+
- 摄像头设备

## 安装

```bash
# 推荐使用虚拟环境
python3 -m venv venv && source venv/bin/activate

pip install -r requirements.txt

# pyzbar 为备用解码引擎，需要系统 zbar 库（可选，缺少时自动降级为 zxing-cpp + OpenCV）
sudo apt install libzbar0
```

> 未安装 pyzbar 时程序仍可运行，由 zxing-cpp 与 OpenCV 完成识别。

## 使用

```bash
python -m qr_barcode.main
```

- 对准二维码 / 条形码，识别结果实时显示在画面左上角并打印到终端
- 窗口按当前显示器分辨率自动选择合适的初始大小（可用鼠标拖拽缩放）
- 每帧识别到的码会自动追加到 `records/history.csv`
- 按 `Q` 或 `Esc` 退出；按 `S` 保存当前画面到 `records/`，用于排查识别失败

## 配置

修改 [qr_barcode/config.py](qr_barcode/config.py)：

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `CAMERA_INDEX` | 摄像头设备号 | `0` |
| `CAMERA_WIDTH` / `CAMERA_HEIGHT` | 摄像头采集分辨率 | `1280` / `720` |
| `DETECT_EVERY_N_FRAMES` | 未检测到码时，间隔多少帧再重试 | `1` |
| `AUTO_OPEN_URL` | 识别到链接时自动打开浏览器 | `True` |
| `RECORD_CSV` | 历史记录文件路径 | `records/history.csv` |
| `MQTT_ENABLED` | 是否启用 MQTT 推送 | `False` |
| `MQTT_BROKER` / `MQTT_PORT` | MQTT Broker 地址与端口 | `127.0.0.1` / `1883` |
| `MQTT_TOPIC` | 识别结果发布主题 | `qr_barcode/scans` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | Broker 认证信息，留空表示匿名 | 空 |

## 项目结构

```
qr_barcode/
  config.py      # 配置项
  detector.py    # QR + 一维条码检测
  recorder.py    # CSV 持久化记录
  publisher.py   # MQTT 结果推送
  main.py        # 摄像头实时主程序
records/         # 运行时生成的识别历史
```

## MQTT 推送（IoT / 机器人接入）

在 [config.py](qr_barcode/config.py) 中设置 `MQTT_ENABLED = True` 并填写 Broker 地址，
每次识别到新码时会向 `MQTT_TOPIC` 发布一条 JSON：

```json
{"timestamp": "2026-10-23T17:30:00", "type": "QR", "content": "https://example.com"}
```

本地可用 mosquitto 验证：

```bash
sudo apt install mosquitto mosquitto-clients
mosquitto_sub -h 127.0.0.1 -t qr_barcode/scans
```

Broker 断开时推送会自动重连，期间识别与记录功能不受影响；
未安装 `paho-mqtt` 时程序会提示并自动关闭推送。