# 二维码 & 条形码实时识别器

基于 OpenCV 的摄像头实时扫码工具：自动识别 **QR 二维码** 与 **一维条形码**，
在画面绘制包围框显示解码内容，识别历史自动保存到本地 CSV。

## 功能

- 实时摄像头画面，自动检测码区域
- 二维码与一维条码识别，合并 zxing-cpp、pyzbar 与 OpenCV 的结果，按内容和位置消除重复框
- 在画面绘制多边形包围框并展示解码文本
- 识别失败时自动放大画面并锐化/二值化后重试，提升小码、模糊、远距离场景的识别率
- 识别成功打印结果并写入 CSV 持久化存储
- 后台线程识别，待处理帧只保留最新一帧，增强识别按时间和次数限频
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
- 新出现的每个码实例会分别追加到 `records/history.csv` 并产生 MQTT 事件；同内容在不同位置出现时分别上报，连续可见或小幅移动时不重复上报
- 按“类型 + 内容 + 相邻检测中的位置”关联实例。默认一次完整检测结果中未发现该实例就视为离开，再出现立即上报，不再等待 2 秒。后台跳帧或尚未返回检测结果不算离开。
- 此规则无法区分实际离开与识别漏检，因此漏检后重现也会重新上报；需要容忍漏检时可增加 `SCAN_MAX_MISSED_DETECTIONS`。同内容码快速移动、交叉或缺少坐标时，实例关联存在歧义，不能作为严格的物品计数保证。
- 按 `Q` 或 `Esc` 退出；按 `S` 保存当前画面到 `records/`，用于排查识别失败

## 配置

修改 [qr_barcode/config.py](qr_barcode/config.py)：

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `CAMERA_INDEX` | 摄像头设备号 | `0` |
| `CAMERA_WIDTH` / `CAMERA_HEIGHT` | 摄像头采集分辨率 | `1280` / `720` |
| `DETECT_EVERY_N_FRAMES` | 最近识别结果为空时，每 N 个采集帧提交一帧到后台；待处理帧仅保留最新一帧 | `1` |
| `ENHANCE_EVERY_N_DETECTIONS` / `ENHANCE_INTERVAL_SECONDS` | 无码时增强识别的最小次数与时间间隔（两者均需满足） | `5` / `0.5` 秒 |
| `SCAN_MAX_MISSED_DETECTIONS` | 允许实例连续缺失的完整检测次数；默认缺失一次就结束跟踪 | `0` |
| `SCAN_MAX_CENTER_DISTANCE` | 相邻检测中同内容实例允许移动的中心距离，以前后框的平均对角线长度为单位 | `0.75` |
| `RESULT_MAX_AGE_SECONDS` | 识别框超过此时间不再绘制，避免旧框长时间残留 | `1.0` 秒 |
| `AUTO_OPEN_URL` | 识别到链接时自动打开浏览器 | `True` |
| `RECORD_CSV` | 历史记录文件路径 | `records/history.csv` |
| `MQTT_ENABLED` | 是否启用 MQTT 推送 | `False` |
| `MQTT_BROKER` / `MQTT_PORT` | MQTT Broker 地址与端口 | `127.0.0.1` / `1883` |
| `MQTT_TOPIC` | 识别结果发布主题 | `qr_barcode/scans` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | Broker 认证信息，留空表示匿名 | 空 |
| `MQTT_OUTBOX_PATH` | 本地待发队列，重启后恢复发送 | `records/mqtt_outbox.sqlite3` |
| `MQTT_MAX_PENDING` | 待发队列上限，达到上限时明确报错，原有事件保留 | `1000` |
| `MQTT_CLOSE_TIMEOUT` | 退出时排空队列的等待预算，不包含网络线程清理时间 | `3.0` 秒 |

## 项目结构

```
qr_barcode/
  config.py      # 配置项
  detector.py    # QR + 一维条码检测
  scanning.py    # 后台识别、最新帧调度与事件去重
  recorder.py    # CSV 持久化记录
  publisher.py   # MQTT 结果推送
  main.py        # 摄像头实时主程序
records/         # 运行时生成的识别历史
```

## MQTT 推送（IoT / 机器人接入）

在 [config.py](qr_barcode/config.py) 中设置 `MQTT_ENABLED = True` 并填写 Broker 地址，
每次识别到新码时先写入本地待发队列，再向 `MQTT_TOPIC` 发布一条 JSON：

```json
{"event_id": "a970f45c3ce64d0fba3b7935afffa6d9", "timestamp": "2026-10-23T17:30:00", "type": "QR", "content": "https://example.com"}
```

本地可用 mosquitto 验证：

```bash
sudo apt install mosquitto mosquitto-clients
mosquitto_sub -h 127.0.0.1 -t qr_barcode/scans
```

Broker 断开时自动重连，断线期间的事件保留在 SQLite 待发队列中，恢复连接后按顺序补发，不需要重新扫码。
默认 QoS 1，收到确认后才删除本地事件；退出时最多等待 `MQTT_CLOSE_TIMEOUT` 秒，仍未确认的事件留到下次启用 MQTT 时继续发送。
采用至少一次发送语义，重启时可能重发已经送达但尚未删除的消息，接收端可按 `event_id` 去重。QoS 0 只能确认客户端发送，不能确认 Broker 收到，重要事件应使用 QoS 1 或 2。
队列满或写入失败会输出错误，该次识别仍保存在 CSV 中，需要人工处理。一个待发队列文件供一个运行中的扫码进程使用。
未安装 `paho-mqtt` 时程序会提示并自动关闭推送。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖多引擎合并、多码增强识别、OpenCV 降级、去重、后台帧调度、MQTT 断线/重启补发与确认；不需要摄像头或真实 Broker。
