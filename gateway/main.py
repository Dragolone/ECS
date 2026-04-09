"""
IoT Gateway — 统一物联网网关

合并原 command-bridge + mqtt-bridge，新增 WebSocket 实时推送：
1. MQTT 订阅：接收机器人遥测 → 写 uniCloud + 广播 WebSocket
2. HTTP POST /sendCommand：接收云函数指令 → 发布到 MQTT
3. WebSocket /ws：前端实时连接，推送遥测数据
4. HTTP GET /healthz：健康检查
"""

import hashlib
import hmac
import json
import os
import queue
import random
import string
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Optional

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_sock import Sock

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def now_ms() -> int:
    return int(time.time() * 1000)

def get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

def get_env_required(name: str) -> str:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        raise ValueError(f"缺少环境变量 {name}（请在 .env 中配置）")
    return str(v).strip()

def get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except Exception as e:
        raise ValueError(f"环境变量 {name} 需要是整数，当前为: {raw!r}") from e

def get_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except Exception as e:
        raise ValueError(f"环境变量 {name} 需要是数字，当前为: {raw!r}") from e

def gen_client_id(prefix: str = "iot-gateway") -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{prefix}-{suffix}"

def reason_code_to_int(rc: Any) -> int:
    try:
        return int(getattr(rc, "value"))
    except Exception:
        try:
            return int(rc)
        except Exception:
            return -1

# ---------------------------------------------------------------------------
# Deduplication (from mqtt-bridge)
# ---------------------------------------------------------------------------

class Deduper:
    def __init__(self, ttl_seconds: float, max_keys: int):
        self.ttl_ms = max(1000, int(float(ttl_seconds) * 1000))
        self.max_keys = max(1000, int(max_keys))
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, int]" = OrderedDict()

    def seen_recently(self, key: str, now: int) -> bool:
        expires_at = now + self.ttl_ms
        with self._lock:
            while self._items:
                _, exp = next(iter(self._items.items()))
                if exp > now:
                    break
                self._items.popitem(last=False)

            prev_exp = self._items.get(key)
            if prev_exp is not None and prev_exp > now:
                self._items.move_to_end(key, last=True)
                self._items[key] = expires_at
                return True

            self._items[key] = expires_at
            self._items.move_to_end(key, last=True)
            while len(self._items) > self.max_keys:
                self._items.popitem(last=False)
            return False

def make_dedup_key(topic: str, payload_obj: dict) -> str:
    canonical = json.dumps(payload_obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha1()
    h.update((topic or "").encode("utf-8"))
    h.update(b"\n")
    h.update(canonical.encode("utf-8"))
    return h.hexdigest()

# ---------------------------------------------------------------------------
# Ingestor — HTTP 写入 uniCloud (from mqtt-bridge)
# ---------------------------------------------------------------------------

class Ingestor:
    def __init__(self, url: str, token: str, timeout_seconds: float, max_retries: int):
        self.url = url
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, int(max_retries))
        self.session = requests.Session()

    def post(self, body: dict) -> tuple[bool, str]:
        headers = {"Content-Type": "application/json", "X-INGEST-TOKEN": self.token}
        last_err = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(self.url, headers=headers, json=body, timeout=self.timeout_seconds)
                if resp.status_code < 200 or resp.status_code >= 300:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:500]}"
                    raise RuntimeError(last_err)
                data = resp.json()
                if isinstance(data, dict):
                    if data.get("ok") is True:
                        return True, ""
                    if data.get("code") == 0 and isinstance(data.get("data"), dict) and data["data"].get("ok") is True:
                        return True, ""
                last_err = f"非 ok 响应: {json.dumps(data, ensure_ascii=False)[:500]}"
                raise RuntimeError(last_err)
            except Exception as e:
                last_err = str(e)
                if attempt >= self.max_retries:
                    break
                sleep_s = min(8.0, 1.0 * (2 ** (attempt - 1)))
                print(f"[{now_str()}] ingest 失败 ({attempt}/{self.max_retries}): {last_err}，{sleep_s:.1f}s 后重试")
                time.sleep(sleep_s)
        return False, last_err

# ---------------------------------------------------------------------------
# WebSocket Client Manager
# ---------------------------------------------------------------------------

class WSManager:
    def __init__(self):
        self._clients = set()
        self._lock = threading.Lock()

    def add(self, ws):
        with self._lock:
            self._clients.add(ws)
        print(f"[{now_str()}] WS 客户端连接，当前 {len(self._clients)} 个")

    def remove(self, ws):
        with self._lock:
            self._clients.discard(ws)
        print(f"[{now_str()}] WS 客户端断开，当前 {len(self._clients)} 个")

    @property
    def count(self) -> int:
        return len(self._clients)

    def broadcast(self, data: dict):
        msg = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            targets = list(self._clients)
        if not targets:
            return
        dead = []
        for ws in targets:
            try:
                ws.send(msg)
            except Exception:
                dead.append(ws)
        if dead:
            with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

# ---------------------------------------------------------------------------
# Command helpers (from command-bridge)
# ---------------------------------------------------------------------------

STATUS_COMMANDS = {"start", "stop", "clear_error", "emergency_stop"}
MOVE_COMMANDS = {"move"}

ERROR_INVALID_TOKEN = 4001
ERROR_INVALID_JSON = 4002
ERROR_INVALID_FIELD = 4003
ERROR_UNSUPPORTED_COMMAND = 4004
ERROR_MQTT_NOT_CONNECTED = 5001
ERROR_MQTT_PUBLISH_FAILED = 5002

def build_error(code: int, msg: str, http_status: int):
    return jsonify({"code": code, "msg": msg}), http_status

def resolve_topic(base_prefix: str, cmd: str) -> str:
    if cmd in STATUS_COMMANDS:
        return f"{base_prefix}/status"
    if cmd in MOVE_COMMANDS:
        return f"{base_prefix}/move"
    raise ValueError(f"暂不支持的 cmd: {cmd}")

def normalize_move_params(params: dict) -> dict:
    def is_number(v):
        if isinstance(v, bool): return False
        if isinstance(v, (int, float)): return True
        if isinstance(v, str):
            try: float(v.strip()); return True
            except: return False
        return False

    def to_float(v):
        return float(v.strip() if isinstance(v, str) else v)

    def clamp(v, lo=-1.0, hi=1.0):
        return max(lo, min(hi, v))

    x, y = params.get("x"), params.get("y")
    if is_number(x) and is_number(y):
        return {"x": clamp(to_float(x)), "y": clamp(to_float(y))}
    direction = params.get("direction")
    if isinstance(direction, str) and direction.strip():
        return {"direction": direction.strip()}
    raise ValueError("move 缺少有效的 direction 或 x/y")

def normalize_command_payload(body: Any) -> dict:
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    robot_code = str(body.get("robotCode", "")).strip()
    cmd = str(body.get("cmd", "")).strip()
    request_id = str(body.get("requestId", "")).strip()
    params = body.get("params") or {}
    ts = body.get("ts")
    if not robot_code: raise ValueError("robotCode 不能为空")
    if not cmd: raise ValueError("cmd 不能为空")
    if not request_id: raise ValueError("requestId 不能为空")
    if cmd not in STATUS_COMMANDS and cmd not in MOVE_COMMANDS:
        raise LookupError(f"不支持的 cmd: {cmd}")
    if not isinstance(params, dict): raise ValueError("params 必须是对象")
    if isinstance(ts, bool) or ts is None: raise ValueError("ts 不能为空")
    try: ts_value = int(ts)
    except: raise ValueError("ts 必须是整数时间戳")
    return {"robotCode": robot_code, "cmd": cmd, "params": params, "ts": ts_value, "requestId": request_id}

# ---------------------------------------------------------------------------
# MQTT Client (subscribe telemetry + publish commands)
# ---------------------------------------------------------------------------

class MQTTClient:
    def __init__(self, host, port, username, password, client_id,
                 sub_topic, base_prefix, publish_timeout, msg_queue):
        self.host = host
        self.port = port
        self.sub_topic = sub_topic
        self.base_prefix = base_prefix
        self.publish_timeout = max(1.0, publish_timeout)
        self.msg_queue = msg_queue
        self.connected = threading.Event()
        self.publish_lock = threading.Lock()

        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except Exception:
            client = mqtt.Client(client_id=client_id)

        if username or password:
            client.username_pw_set(username=username, password=password)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self.client = client

    def _on_connect(self, _client, _userdata, _flags, reason_code, properties=None):
        rc = reason_code_to_int(reason_code)
        if rc == 0:
            self.connected.set()
            print(f"[{now_str()}] MQTT 已连接 rc={rc}")
            if self.sub_topic:
                try:
                    result, mid = _client.subscribe(self.sub_topic)
                    print(f"[{now_str()}] 已订阅: {self.sub_topic} (result={result})")
                except Exception as e:
                    print(f"[{now_str()}] 订阅失败: {e!r}")
        else:
            self.connected.clear()
            print(f"[{now_str()}] MQTT 连接失败 rc={rc}")

    def _on_disconnect(self, _client, _userdata, reason_code, properties=None):
        self.connected.clear()
        print(f"[{now_str()}] MQTT 断开 rc={reason_code_to_int(reason_code)}（自动重连中）")

    def _on_message(self, _client, _userdata, msg):
        payload_text = (msg.payload or b"").decode("utf-8", errors="replace")
        try:
            parsed = json.loads(payload_text)
        except Exception as e:
            print(f"[{now_str()}] MQTT JSON 解析失败: {e!r}")
            return
        if not isinstance(parsed, dict):
            return
        try:
            self.msg_queue.put_nowait((msg.topic, payload_text, parsed))
        except queue.Full:
            print(f"[{now_str()}] 消息队列已满，丢弃")

    def start(self):
        print(f"[{now_str()}] MQTT 连接 {self.host}:{self.port}")
        print(f"  订阅主题: {self.sub_topic or '(无)'}")
        print(f"  命令前缀: {self.base_prefix}")
        self.client.connect_async(self.host, port=self.port, keepalive=60)
        self.client.loop_start()

    def is_connected(self) -> bool:
        return self.connected.is_set()

    def publish_json(self, topic: str, payload: dict) -> tuple[bool, str]:
        if not self.is_connected():
            return False, "MQTT 未连接"
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.publish_lock:
            info = self.client.publish(topic, text, qos=1, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False, f"publish 失败 rc={info.rc}"
        deadline = time.monotonic() + self.publish_timeout
        while time.monotonic() < deadline:
            if info.is_published():
                return True, ""
            time.sleep(0.05)
        return False, "publish 确认超时"

# ---------------------------------------------------------------------------
# Worker — 处理 MQTT 遥测消息
# ---------------------------------------------------------------------------

def telemetry_worker(stop_event, msg_queue, ingestor, deduper, ws_manager):
    while not stop_event.is_set():
        try:
            topic, payload_text, parsed = msg_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        robot_code = parsed.get("robotCode")
        try:
            if deduper:
                key = make_dedup_key(topic, parsed)
                if deduper.seen_recently(key, now_ms()):
                    continue

            body = dict(parsed)
            body["receivedAt"] = now_ms()
            body["raw"] = parsed

            ok, err = ingestor.post(body)
            if ok:
                print(f"[{now_str()}] 遥测写库成功 robotCode={robot_code}")
            else:
                print(f"[{now_str()}] 遥测写库失败 robotCode={robot_code} err={err}")

            ws_data = {
                "type": "telemetry",
                "robotCode": str(robot_code) if robot_code is not None else "",
                "data": {
                    "speed": parsed.get("speed", 0),
                    "vehicleBattery": parsed.get("vehicleBattery"),
                    "packBattery": parsed.get("packBattery"),
                    "ts": parsed.get("ts", now_ms()),
                },
            }
            ws_manager.broadcast(ws_data)
        except Exception as e:
            print(f"[{now_str()}] worker 异常: {e!r}")
        finally:
            msg_queue.task_done()

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

def create_app():
    load_dotenv()

    mqtt_host = get_env_required("MQTT_HOST")
    mqtt_port = get_env_int("MQTT_PORT", 1883)
    mqtt_username = get_env("MQTT_USERNAME")
    mqtt_password = get_env("MQTT_PASSWORD")
    mqtt_client_id = get_env("MQTT_CLIENT_ID") or gen_client_id()
    mqtt_base_prefix = get_env("MQTT_BASE_PREFIX") or "01_test"
    mqtt_sub_topic = get_env("MQTT_SUB_TOPIC") or f"{mqtt_base_prefix}/Servers"
    publish_timeout = get_env_float("MQTT_PUBLISH_TIMEOUT_SECONDS", 3.0)

    command_token = get_env_required("COMMAND_TOKEN")
    ws_token = get_env("WS_TOKEN") or command_token

    ingest_url = get_env("INGEST_URL")
    ingest_token = get_env("INGEST_TOKEN")
    http_timeout = get_env_float("HTTP_TIMEOUT_SECONDS", 8.0)
    http_max_retries = get_env_int("HTTP_MAX_RETRIES", 3)

    dedup_enabled = get_env("DEDUP_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
    dedup_ttl = get_env_float("DEDUP_TTL_SECONDS", 30.0)
    dedup_max = get_env_int("DEDUP_MAX_KEYS", 50000)

    msg_queue = queue.Queue(maxsize=1000)
    ws_manager = WSManager()

    mqtt_client = MQTTClient(
        host=mqtt_host, port=mqtt_port, username=mqtt_username,
        password=mqtt_password, client_id=mqtt_client_id,
        sub_topic=mqtt_sub_topic, base_prefix=mqtt_base_prefix,
        publish_timeout=publish_timeout, msg_queue=msg_queue,
    )
    mqtt_client.start()

    ingestor = None
    if ingest_url:
        ingestor = Ingestor(url=ingest_url, token=ingest_token, timeout_seconds=http_timeout, max_retries=http_max_retries)
        print(f"[{now_str()}] 遥测写库已启用: {ingest_url}")
    else:
        print(f"[{now_str()}] INGEST_URL 未配置，遥测仅通过 WebSocket 推送，不写库")

    deduper = Deduper(ttl_seconds=dedup_ttl, max_keys=dedup_max) if dedup_enabled else None

    class NoopIngestor:
        def post(self, body): return True, ""

    stop_event = threading.Event()
    worker = threading.Thread(
        target=telemetry_worker,
        args=(stop_event, msg_queue, ingestor or NoopIngestor(), deduper, ws_manager),
        daemon=True,
    )
    worker.start()

    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    sock = Sock(app)

    # -- HTTP: 指令下发 --
    @app.post("/sendCommand")
    def send_command():
        header_token = request.headers.get("x-command-token", "").strip()
        if not header_token or not hmac.compare_digest(header_token, command_token):
            return build_error(ERROR_INVALID_TOKEN, "invalid command token", 401)

        body = request.get_json(silent=True)
        if body is None:
            return build_error(ERROR_INVALID_JSON, "request body must be valid JSON", 400)

        try:
            payload = normalize_command_payload(body)
        except LookupError as e:
            return build_error(ERROR_UNSUPPORTED_COMMAND, str(e), 400)
        except ValueError as e:
            return build_error(ERROR_INVALID_FIELD, str(e), 400)

        if payload["cmd"] == "move":
            try:
                payload["params"] = normalize_move_params(payload.get("params", {}))
            except ValueError as e:
                return build_error(ERROR_INVALID_FIELD, str(e), 400)

        topic = resolve_topic(mqtt_base_prefix, payload["cmd"])
        print(f"[{now_str()}] 下行命令 robot={payload['robotCode']} cmd={payload['cmd']} topic={topic}")

        if not mqtt_client.is_connected():
            return build_error(ERROR_MQTT_NOT_CONNECTED, "mqtt is not connected", 503)

        ok, err = mqtt_client.publish_json(topic=topic, payload=payload)
        if not ok:
            return build_error(ERROR_MQTT_PUBLISH_FAILED, err, 500)

        return jsonify({"code": 0, "msg": "ok"})

    # -- HTTP: 健康检查 --
    @app.get("/healthz")
    def healthz():
        return jsonify({
            "code": 0, "msg": "ok",
            "data": {
                "mqttConnected": mqtt_client.is_connected(),
                "wsClients": ws_manager.count,
                "basePrefix": mqtt_base_prefix,
            },
        })

    # -- WebSocket: 实时推送 --
    @sock.route("/ws")
    def ws_handler(ws):
        token = request.args.get("token", "").strip()
        if not token or not hmac.compare_digest(token, ws_token):
            try:
                ws.send(json.dumps({"type": "error", "msg": "invalid token"}))
            except Exception:
                pass
            return

        ws.send(json.dumps({"type": "connected", "msg": "ok"}))
        ws_manager.add(ws)
        try:
            while True:
                try:
                    data = ws.receive(timeout=60)
                except Exception:
                    break
                if data is None:
                    break
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "ping":
                        ws.send(json.dumps({"type": "pong"}))
                except Exception:
                    pass
        finally:
            ws_manager.remove(ws)

    return app


def main():
    app = create_app()
    host = get_env("HTTP_HOST") or "0.0.0.0"
    port = int(os.getenv("PORT", os.getenv("HTTP_PORT", "8090")))

    print(f"[{now_str()}] IoT Gateway 启动")
    print(f"  HTTP  : {host}:{port}")
    print(f"  路由  : POST /sendCommand | GET /healthz | WS /ws")

    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
