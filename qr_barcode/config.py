"""配置项：摄像头、识别参数、记录文件等。"""

# 摄像头设备号（0 为默认摄像头）
CAMERA_INDEX = 0

# 摄像头采集分辨率，分辨率越高小码/远距离识别率越好
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720

# 识别失败（无码）时的重试间隔，单位：帧
DETECT_EVERY_N_FRAMES = 1

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
