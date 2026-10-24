"""配置项：摄像头、识别参数、记录文件等。"""

# 摄像头设备号（0 为默认摄像头）
CAMERA_INDEX = 0

# 摄像头采集分辨率，分辨率越高小码/远距离识别率越好
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720
# 连续读取失败达到此次数后以非零状态退出，短暂失败期间保留窗口响应
CAMERA_READ_FAILURE_LIMIT = 3
CAMERA_RETRY_DELAY = 0.1

# 最近识别结果为空时，每 N 个采集帧提交一帧到后台（待处理帧仅保留最新一帧）
DETECT_EVERY_N_FRAMES = 1

# 无码增强识别：至少间隔指定的识别次数与秒数
ENHANCE_EVERY_N_DETECTIONS = 5
ENHANCE_INTERVAL_SECONDS = 0.5

# 码实例跟踪：默认一次完整检测未发现该实例就视为离开，重现时重新上报
SCAN_MAX_MISSED_DETECTIONS = 0
# 连续检测中允许的中心移动距离，以前后包围框平均对角线长度为单位
SCAN_MAX_CENTER_DISTANCE = 0.75

# 后台结果超过此时间就不再绘制旧框（秒）
RESULT_MAX_AGE_SECONDS = 1.0
DETECTION_CLOSE_TIMEOUT = 1.0

# 是否识别到链接时自动打开浏览器
AUTO_OPEN_URL = True

# 记录文件路径
RECORD_CSV = "records/history.csv"

# CSV 表头
CSV_HEADER = ["timestamp", "type", "content"]

# MQTT 推送：把识别结果发布到 Broker，便于接入 IoT / 机器人（默认关闭）
MQTT_ENABLED = False
MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
MQTT_TOPIC = "qr_barcode/scans"
MQTT_CLIENT_ID = ""  # 留空则自动生成
MQTT_USERNAME = ""
MQTT_PASSWORD = ""
MQTT_QOS = 1
MQTT_OUTBOX_PATH = "records/mqtt_outbox.sqlite3"
MQTT_MAX_PENDING = 1000
# 退出时排空队列的等待预算；不包含网络线程的清理时间
MQTT_CLOSE_TIMEOUT = 3.0
MQTT_ACK_TIMEOUT = 10.0
MQTT_MAX_ATTEMPTS = 3

# 与 MQTT 待发队列共用 SQLite；即使 MQTT 关闭也保存扫描事件
EVENT_DATABASE_PATH = MQTT_OUTBOX_PATH
STORAGE_MAX_BUFFER = 1000
STORAGE_CLOSE_TIMEOUT = 3.0
