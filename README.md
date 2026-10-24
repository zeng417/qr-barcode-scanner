# 二维码 & 条形码实时识别器

基于 OpenCV 的摄像头实时扫码工具：自动识别 **QR 二维码** 与 **一维条形码**，
在画面绘制包围框显示解码内容，扫描事件持久化到 SQLite，并自动导出本地 CSV。

## 功能

- 实时摄像头画面，自动检测码区域
- 支持直接识别图片文件，并可另存识别框标注图
- 二维码与一维条码识别，合并 zxing-cpp、pyzbar 与 OpenCV 的结果，按内容和位置消除重复框
- 原图解码失败时，根据三个定位内环提取二维码、校正透视并在码区域周围补白，改善码外沿与手机深色背景相连时的识别；单帧最多解码 4 个候选区域
- 在画面绘制多边形包围框并展示解码文本
- 识别失败时补白边重试，兼容紧贴图像边缘、缺少留白的二维码；同时合并放大、锐化/二值化后的识别结果，改善小码、模糊、远距离场景
- 识别成功打印结果；后台保存事件、导出 CSV，存储临时失败时缓存并重试
- 非 UTF-8 内容显示为可逆的 `hex:...` 字节表示，不因 OpenCV 编码异常退出
- 一维码默认需同一位置连续 3 次完整检测内容一致才显示、打印、保存和推送；单个引擎成功也可进入确认。双引擎同内容时合并不同扫描范围的框，内容冲突时暂缓；可选开启严格双引擎模式
- 同内容的一维码事件在终端默认每 2 秒最多打印一次，减少漏检重现时刷屏；不同内容照常打印，事件保存和 MQTT 上报仍按实例规则执行
- 同一位置的一维码出现内容冲突时暂不接受该组结果；二维码内容冲突仍按 zxing-cpp、pyzbar、OpenCV 的顺序选择结果，避免编码差异导致重复上报
- 后台线程识别，待处理帧只保留最新一帧，增强识别按时间和次数限频
- 可选：识别结果通过 MQTT 推送到 Broker，便于接入 IoT / 机器人
- 可选：识别到 `http(s)` 链接自动打开浏览器

## 环境要求

- Python 3.9+
- 摄像头设备（仅实时模式需要）

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

直接识别图片文件：

```bash
bash run.sh --image tests/fixtures/code128_abc_2024_001.png
# 或指定自己的图片，并保存标注结果
python -m qr_barcode.main --image /path/to/image.png --output /path/to/annotated.png
```

图片模式立即打印解码内容，不使用摄像头模式的连续三次确认，也不打开窗口、浏览器或写入事件库、MQTT。上述样本应输出 `[BARCODE] ABC-2024-001`。`--output` 可省略，指定时必须与原图路径不同。退出状态：`0` 成功、`1` 读取或识别出错、`2` 参数或标注图保存错误、`3` 未识别到码。

摄像头模式：

- 对准二维码 / 条形码，识别结果实时显示在画面左上角并打印到终端
- 窗口按当前显示器分辨率自动选择合适的初始大小（可用鼠标拖拽缩放）
- 新出现的每个码实例会分别追加到 `records/history.csv` 并产生 MQTT 事件；同内容在不同位置出现时分别上报，连续可见或小幅移动时不重复上报
- 按“类型 + 内容 + 相邻检测中的位置”关联实例。默认一次完整检测结果中未发现该实例就视为离开，再出现时二维码立即上报，一维码重新完成连续确认后上报。后台跳帧或尚未返回检测结果不算离开，也不增加确认次数。
- 一维码确认期间如果内容变化、漏检或同位置各引擎出现冲突，连续计数重新开始。停留不足确认次数的一维码会被舍弃；连续确认可降低偶发误读，但不能保证清晰度不足时仍正确。EAN 校验位也不能排除所有误读。
- 此规则无法区分实际离开与识别漏检，因此漏检后重现也会重新上报；需要容忍漏检时可增加 `SCAN_MAX_MISSED_DETECTIONS`。同内容码快速移动、交叉或缺少坐标时，实例关联存在歧义，不能作为严格的物品计数保证。
- 按 `Q` 或 `Esc` 退出；按 `S` 保存未经识别框、文字绘制的原始摄像头画面到 `records/`，用于排查识别失败
- 关闭时先收集已完成与正在处理的识别结果（默认等待最多 1 秒），再排空事件存储缓冲和 MQTT；尚未开始识别的待处理帧会放弃，超时未完成的解码会输出提示
- 摄像头连续读取失败默认重试到 3 次后以非零状态退出；截图检查实际写入结果，使用唯一文件名避免覆盖

## 配置

修改 [qr_barcode/config.py](qr_barcode/config.py)：

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `CAMERA_INDEX` | 摄像头设备号 | `0` |
| `CAMERA_WIDTH` / `CAMERA_HEIGHT` | 摄像头采集分辨率 | `1280` / `720` |
| `CAMERA_READ_FAILURE_LIMIT` / `CAMERA_RETRY_DELAY` | 连续读取失败上限与重试间隔 | `3` / `0.1` 秒 |
| `DETECT_EVERY_N_FRAMES` | 最近识别结果为空时，每 N 个采集帧提交一帧到后台；待处理帧仅保留最新一帧 | `1` |
| `ENHANCE_EVERY_N_DETECTIONS` / `ENHANCE_INTERVAL_SECONDS` | 无码时增强识别的最小次数与时间间隔（两者均需满足） | `5` / `0.5` 秒 |
| `SCAN_MAX_MISSED_DETECTIONS` | 允许实例连续缺失的完整检测次数；默认缺失一次就结束跟踪 | `0` |
| `SCAN_MAX_CENTER_DISTANCE` | 相邻检测中同内容实例允许移动的中心距离，以前后框的平均对角线长度为单位 | `0.75` |
| `BARCODE_CONFIRM_DETECTIONS` | 一维码首次出现及离开后重现时，需要的连续同位置、同内容检测次数 | `3` |
| `BARCODE_REQUIRE_ENGINE_AGREEMENT` | 可选严格模式：ZXing、pyzbar 均可用时要求两者同时成功且内容一致；默认允许单引擎结果进入连续确认，避免漏检屏蔽有效条码 | `False` |
| `BARCODE_CONSOLE_INTERVAL_SECONDS` | 同内容一维码的终端打印最小间隔，仅影响日志；`0` 表示打印每个新事件 | `2.0` 秒 |
| `RESULT_MAX_AGE_SECONDS` | 识别框超过此时间不再绘制，避免旧框长时间残留 | `1.0` 秒 |
| `AUTO_OPEN_URL` | 识别到链接时自动打开浏览器 | `True` |
| `RECORD_CSV` | 历史记录文件路径 | `records/history.csv` |
| `EVENT_DATABASE_PATH` | 扫描事件和 MQTT 队列共用的 SQLite；保留原队列文件名便于迁移 | `records/mqtt_outbox.sqlite3` |
| `STORAGE_MAX_BUFFER` / `STORAGE_CLOSE_TIMEOUT` | 存储故障时的内存缓冲上限与退出排空等待预算 | `1000` / `3.0` 秒 |
| `DETECTION_CLOSE_TIMEOUT` | 退出时等待正在处理的识别完成的时间 | `1.0` 秒 |
| `MQTT_ENABLED` | 是否启用 MQTT 推送 | `False` |
| `MQTT_BROKER` / `MQTT_PORT` | MQTT Broker 地址与端口 | `127.0.0.1` / `1883` |
| `MQTT_TOPIC` | 识别结果发布主题 | `qr_barcode/scans` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | Broker 认证信息，留空表示匿名 | 空 |
| `MQTT_OUTBOX_PATH` | 旧路径配置，作为 `EVENT_DATABASE_PATH` 的默认值；主程序实际使用后者 | `records/mqtt_outbox.sqlite3` |
| `MQTT_MAX_PENDING` | 活跃待发队列上限，主程序超出部分留在事件库中延后入队 | `1000` |
| `MQTT_CLOSE_TIMEOUT` | 退出时排空队列的等待预算，不包含网络线程清理时间 | `3.0` 秒 |
| `MQTT_ACK_TIMEOUT` / `MQTT_MAX_ATTEMPTS` | 已连接状态下等待确认的超时与发送次数上限 | `10.0` 秒 / `3` |

增强识别读出一维码后，后续帧会继续尝试增强，以便完成连续确认；每次均重新解码，增强不再读出一维码时恢复次数和时间限频。图片模式首次检测就允许增强，不等待摄像头重试间隔。

## 项目结构

```
qr_barcode/
  config.py      # 配置项
  detector.py    # QR + 一维条码检测
  qr_regions.py  # 从定位内环提取码区域、透视校正并恢复白色静区
  scanning.py    # 后台识别、最新帧调度与事件去重
  storage.py     # 事件与待发项原子提交、后台重试、CSV 导出
  recorder.py    # 旧 CSV 工具接口（主程序使用 storage.py）
  publisher.py   # MQTT 结果推送
  main.py        # 摄像头实时主程序与图片识别入口
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
默认 QoS 1，收到确认后才删除本地待发项；退出时最多等待 `MQTT_CLOSE_TIMEOUT` 秒，仍未确认的事件留到下次启用 MQTT 时继续发送。
采用至少一次发送语义，重启时可能重发已经送达但尚未删除的消息，接收端可按 `event_id` 去重。QoS 0 只能确认客户端发送，不能确认 Broker 收到，重要事件应使用 QoS 1 或 2。
确认回调立即唤醒发送线程；已连接但确认超时时重建连接，使用相同 `event_id` 重试。达到发送次数上限的事件保存在 `mqtt_failures` 表中并输出错误，后续事件继续发送；这些失败事件需人工检查和处理。
事件与 MQTT 待发项在同一 SQLite 事务中提交。活跃队列满时，新扫描仍保存在 `events` 表中，等队列腾出空间再自动入队。一个事件库供一个运行中的扫码进程使用。
未安装 `paho-mqtt` 或 MQTT 初始化失败时，采集继续，启用 MQTT 的扫描事件仍入库等待下次正常启动发送。

## 存储与恢复

SQLite 中的 `events` 表是扫描历史的来源；MQTT 确认后只移除待发项，历史事件继续保留。CSV 最多每秒导出一次，采用临时文件加原子替换，退出时再次导出。已有三列 CSV 首次使用时导入一次，不会把旧历史重新推送；旧版 MQTT 待发队列原地升级并保留原事件。

CSV 写入失败不会阻止新事件入库；下次启动可从数据库重建 CSV。旧 CSV 无法读取或格式错误时保留原文件，新事件仍能入库，修复旧文件后再导出。

数据库临时写入失败时，新事件暂存在有界内存缓冲中，恢复后重试。缓冲满、关闭时仍有未提交事件或 CSV 导出失败都会明确报错并以状态 `2` 退出。持续磁盘故障期间尚未入库的内存事件无法保证在进程退出或断电后恢复。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖多引擎合并、二进制二维码、实例关联、后台帧调度与关闭回收、原子事件提交、CSV 恢复、摄像头错误，以及 MQTT 断线/重启补发、确认超时与失败隔离；不需要摄像头或真实 Broker。
