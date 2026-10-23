#!/usr/bin/env bash
# 一键启动：自动准备虚拟环境、安装依赖并运行识别器
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
    echo "[1/2] 创建虚拟环境..."
    if ! python3 -m venv "$VENV"; then
        echo "[错误] 创建虚拟环境失败，请先执行: sudo apt install python3.12-venv"
        exit 1
    fi
fi

if ! "$PY" -c "import cv2, numpy, zxingcpp" >/dev/null 2>&1; then
    echo "[2/2] 安装依赖..."
    "$PY" -m pip install -r requirements.txt
fi

exec "$PY" -m qr_barcode.main
