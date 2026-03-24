import hmac
import json
import os
import random
import string
import threading
import time
from datetime import datetime
from typing import Any

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from flask import Flask, jsonify, request


STATUS_COMMANDS = {"start", "stop", "clear_error", "emergency_stop"}
MOVE_COMMANDS = {"move"}

ERROR_INVALID_TOKEN = 4001
ERROR_INVALID_JSON = 4002
ERROR_INVALID_FIELD = 4003
ERROR_UNSUPPORTED_COMMAND = 4004
ERROR_MQTT_NOT_CONNECTED = 5001
ERROR_MQTT_PUBLISH_FAILED = 5002


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_env_required(name: str) -> str:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        raise ValueError(f"缺少环境变量 {name}（请在 .env 中配置）")
    return str(value).strip()


def get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except Exception as exc:
        raise ValueError(f"环境变量 {name} 需要是整数，但当前为: {raw!r}") from exc


def get_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except Exception as exc:
        raise ValueError(f"环境变量 {name} 需要是数字，但当前为: {raw!r}") from exc


def gen_client_id(prefix: str = "command-bridge") -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{prefix}-{suffix}"


def reason_code_to_int(reason_code: Any) -> int:
    try:
        return int(getattr(reason_code, "value"))
    except Exception:
        try:
            return int(reason_code)
        except Exception:
            return -1


def build_error(code: int, msg: str, http_status: int):
    return jsonify({"code": code, "msg": msg}), http_status


def resolve_topic(base_prefix: str, cmd: str) -> str:
    if cmd in STATUS_COMMANDS:
        return f"{base_prefix}/status"
    if cmd in MOVE_COMMANDS:
        return f"{base_prefix}/move"
    raise ValueError(f"暂不支持的 cmd: {cmd}")


def normalize_payload(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")

    robot_code = str(body.get("robotCode", "")).strip()
    cmd = str(body.get("cmd", "")).strip()
    request_id = str(body.get("requestId", "")).strip()
    params = body.get("params", {})
    ts = body.get("ts")

    if not robot_code:
        raise ValueError("robotCode 不能为空")
    if not cmd:
        raise ValueError("cmd 不能为空")
    if not request_id:
        raise ValueError("requestId 不能为空")
    if cmd not in STATUS_COMMANDS and cmd not in MOVE_COMMANDS:
        raise LookupError(f"不支持的 cmd: {cmd}")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")
    if isinstance(ts, bool) or ts is None:
        raise ValueError("ts 不能为空，且必须是时间戳")

    try:
        ts_value = int(ts)
    except Exception as exc:
        raise ValueError("ts 必须是整数时间戳") from exc

    return {
        "robotCode": robot_code,
        "cmd": cmd,
        "params": params,
        "ts": ts_value,
        "requestId": request_id,
    }


class MQTTCommandBridge:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        client_id: str,
        publish_timeout_seconds: float,
    ):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.publish_timeout_seconds = max(1.0, publish_timeout_seconds)
        self.connected_event = threading.Event()
        self.publish_lock = threading.Lock()

        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except Exception:
            client = mqtt.Client(client_id=client_id)

        if username or password:
            client.username_pw_set(username=username, password=password)

        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self.on_connect
        client.on_disconnect = self.on_disconnect
        self.client = client

    def on_connect(self, _client, _userdata, _flags, reason_code, properties=None):
        rc = reason_code_to_int(reason_code)
        if rc == 0:
            self.connected_event.set()
            print(f"[{now_str()}] MQTT 已连接，rc={rc}")
        else:
            self.connected_event.clear()
            print(f"[{now_str()}] MQTT 连接失败，rc={rc}")

    def on_disconnect(self, _client, _userdata, reason_code, properties=None):
        rc = reason_code_to_int(reason_code)
        self.connected_event.clear()
        print(f"[{now_str()}] MQTT 已断开，rc={rc}（将自动重连）")

    def start(self) -> None:
        print(f"[{now_str()}] 启动 MQTT 发布连接")
        print(f"  - MQTT_HOST   : {self.host}")
        print(f"  - MQTT_PORT   : {self.port}")
        print(f"  - MQTT_CLIENT : {self.client_id}")
        self.client.connect_async(self.host, port=self.port, keepalive=60)
        self.client.loop_start()

    def is_connected(self) -> bool:
        return self.connected_event.is_set()

    def publish_json(self, topic: str, payload: dict[str, Any]) -> tuple[bool, str]:
        if not self.is_connected():
            return False, "MQTT 当前未连接"

        payload_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        with self.publish_lock:
            info = self.client.publish(topic, payload_text, qos=1, retain=False)

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False, f"publish 调用失败，rc={info.rc}"

        deadline = time.monotonic() + self.publish_timeout_seconds
        while time.monotonic() < deadline:
            if info.is_published():
                return True, ""
            time.sleep(0.05)

        return False, "等待 MQTT publish 确认超时"


def create_app() -> Flask:
    load_dotenv()

    mqtt_host = get_env_required("MQTT_HOST")
    mqtt_port = get_env_int("MQTT_PORT", 1883)
    mqtt_username = os.getenv("MQTT_USERNAME", "").strip()
    mqtt_password = os.getenv("MQTT_PASSWORD", "").strip()
    mqtt_client_id = os.getenv("MQTT_CLIENT_ID", "").strip() or gen_client_id()
    command_token = get_env_required("COMMAND_TOKEN")
    mqtt_base_prefix = os.getenv("MQTT_BASE_PREFIX", "01_test").strip() or "01_test"
    publish_timeout_seconds = get_env_float("MQTT_PUBLISH_TIMEOUT_SECONDS", 3.0)

    bridge = MQTTCommandBridge(
        host=mqtt_host,
        port=mqtt_port,
        username=mqtt_username,
        password=mqtt_password,
        client_id=mqtt_client_id,
        publish_timeout_seconds=publish_timeout_seconds,
    )
    bridge.start()

    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.config["COMMAND_TOKEN"] = command_token
    app.config["MQTT_BASE_PREFIX"] = mqtt_base_prefix
    app.config["MQTT_BRIDGE"] = bridge

    @app.post("/sendCommand")
    def send_command():
        header_token = request.headers.get("x-command-token", "").strip()
        expected_token = app.config["COMMAND_TOKEN"]
        if not header_token or not hmac.compare_digest(header_token, expected_token):
            print(f"[{now_str()}] 鉴权失败：x-command-token 无效")
            return build_error(ERROR_INVALID_TOKEN, "invalid command token", 401)

        body = request.get_json(silent=True)
        if body is None:
            print(f"[{now_str()}] 请求体不是合法 JSON")
            return build_error(ERROR_INVALID_JSON, "request body must be valid JSON", 400)

        try:
            payload = normalize_payload(body)
        except LookupError as exc:
            print(f"[{now_str()}] 命令校验失败：{exc}")
            return build_error(ERROR_UNSUPPORTED_COMMAND, str(exc), 400)
        except ValueError as exc:
            print(f"[{now_str()}] 参数校验失败：{exc}")
            return build_error(ERROR_INVALID_FIELD, str(exc), 400)

        topic = resolve_topic(app.config["MQTT_BASE_PREFIX"], payload["cmd"])

        print(f"[{now_str()}] 收到下行命令")
        print(f"  robotCode : {payload['robotCode']}")
        print(f"  cmd       : {payload['cmd']}")
        print(f"  topic     : {topic}")
        print(f"  requestId : {payload['requestId']}")
        print(f"  payload   : {json.dumps(payload, ensure_ascii=False)}")

        mqtt_bridge: MQTTCommandBridge = app.config["MQTT_BRIDGE"]
        if not mqtt_bridge.is_connected():
            print(f"[{now_str()}] publish 失败：MQTT 未连接")
            return build_error(ERROR_MQTT_NOT_CONNECTED, "mqtt is not connected", 503)

        ok, err = mqtt_bridge.publish_json(topic=topic, payload=payload)
        if not ok:
            print(f"[{now_str()}] publish 失败：topic={topic} err={err}")
            return build_error(ERROR_MQTT_PUBLISH_FAILED, err, 500)

        print(f"[{now_str()}] publish 成功：topic={topic} requestId={payload['requestId']}")
        return jsonify({"code": 0, "msg": "ok"})

    @app.get("/healthz")
    def healthz():
        mqtt_bridge: MQTTCommandBridge = app.config["MQTT_BRIDGE"]
        return jsonify(
            {
                "code": 0,
                "msg": "ok",
                "data": {
                    "mqttConnected": mqtt_bridge.is_connected(),
                    "basePrefix": app.config["MQTT_BASE_PREFIX"],
                },
            }
        )

    return app


def main() -> None:
    app = create_app()
    http_host = os.getenv("HTTP_HOST", "0.0.0.0").strip() or "0.0.0.0"
    http_port = int(os.getenv("PORT", os.getenv("HTTP_PORT", "8090")))

    print(f"[{now_str()}] 启动 command-bridge（uniCloud HTTP -> MQTT）")
    print(f"  - HTTP_HOST   : {http_host}")
    print(f"  - HTTP_PORT   : {http_port}")
    print(f"  - BASE_PREFIX : {app.config['MQTT_BASE_PREFIX']}")

    app.run(host=http_host, port=http_port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
