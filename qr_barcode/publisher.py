"""MQTT 推送：把识别结果发布到 MQTT Broker，便于接入 IoT / 机器人场景。"""

from __future__ import annotations

import datetime
import json
import os

try:
    import paho.mqtt.client as mqtt
except ImportError:  # 未安装 paho-mqtt 时自动关闭推送
    mqtt = None


class MqttPublisher:
    """非阻塞 MQTT 发布器，Broker 未连接时静默丢弃消息并自动重连。"""

    def __init__(
        self,
        broker: str,
        port: int,
        topic: str,
        client_id: str = "",
        username: str = "",
        password: str = "",
        qos: int = 1,
    ) -> None:
        self.broker = broker
        self.port = port
        self.topic = topic
        self.qos = qos
        self._client = None

        if mqtt is None:
            return

        client_id = client_id or f"qr-barcode-{os.getpid()}"
        if hasattr(mqtt, "CallbackAPIVersion"):
            # paho-mqtt 2.x 需要显式指定回调 API 版本
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
            )
        else:
            self._client = mqtt.Client(client_id=client_id)

        if username:
            self._client.username_pw_set(username, password)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        # connect_async 不阻塞主循环，连接失败由后台线程自动重试
        self._client.connect_async(broker, port)
        self._client.loop_start()

    @property
    def available(self) -> bool:
        """paho-mqtt 是否可用（已安装并创建客户端）。"""
        return self._client is not None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    def publish(self, kind: str, content: str) -> bool:
        """发布一条识别结果；Broker 未连接时返回 False，避免消息无限堆积。"""
        if self._client is None or not self._client.is_connected():
            return False
        payload = json.dumps(
            {
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "type": kind,
                "content": content,
            },
            ensure_ascii=False,
        )
        return self._client.publish(
            self.topic, payload, qos=self.qos,
        ).rc == mqtt.MQTT_ERR_SUCCESS

    def close(self) -> None:
        if self._client is None:
            return
        self._client.loop_stop()
        self._client.disconnect()
