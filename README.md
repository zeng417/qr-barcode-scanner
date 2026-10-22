# 二维码 & 条形码实时识别器

基于 OpenCV 的摄像头实时扫码工具：自动识别 **QR 二维码** 与 **一维条形码**，
在画面绘制包围框显示解码内容，识别历史自动保存到本地 CSV。

## 功能

- 实时摄像头画面，自动检测码区域
- 二维码识别（OpenCV 内置 `QRCodeDetector`）
- 一维条形码识别（`pyzbar`，需系统 zbar 库）
- 在画面绘制多边形包围框并展示解码文本
- 识别成功打印结果并写入 CSV 持久化存储
- 可选：识别到 `http(s)` 链接自动打开浏览器

## 环境要求

- Python 3.9+
- 摄像头设备

## 安装

```bash
# 推荐使用虚拟环境
python3 -m venv venv && source venv/bin/activate

pip install -r requirements.txt

# 一维条形码需要 pyzbar 与系统 zbar 库（可选，仅二维码可跳过）
sudo apt install libzbar0
pip install pyzbar
```

> 未安装 pyzbar 时程序仍可运行，仅降级为二维码识别。

## 使用

```bash
python -m qr_barcode.main
```

- 对准二维码 / 条形码，识别结果实时显示在画面左上角并打印到终端
- 每帧识别到的码会自动追加到 `records/history.csv`
- 按 `Q` 或 `Esc` 退出

## 配置

修改 [qr_barcode/config.py](qr_barcode/config.py)：

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `CAMERA_INDEX` | 摄像头设备号 | `0` |
| `AUTO_OPEN_URL` | 识别到链接时自动打开浏览器 | `True` |
| `RECORD_CSV` | 历史记录文件路径 | `records/history.csv` |

## 项目结构

```
qr_barcode/
  config.py      # 配置项
  detector.py    # QR + 一维条码检测
  recorder.py    # CSV 持久化记录
  main.py        # 摄像头实时主程序
records/         # 运行时生成的识别历史
```

## 扩展方向

- 倾斜 / 模糊码图像预处理（灰度、二值化、形态学）提升识别率
- 识别结果通过 MQTT 推送，嵌入 IoT / 机器人场景