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