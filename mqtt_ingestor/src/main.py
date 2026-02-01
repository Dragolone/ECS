import json
import os
import time
from datetime import datetime

import paho.mqtt.client as mqtt
from dotenv import load_dotenv


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_env(name: str, default: str | None = None) -> str:
    val = os.getenv(name, default)
    if val is None or str(val).strip() == "":
        raise ValueError(f"缺少环境变量 {name}（请在 .env 中配置）")
    return str(val)


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except Exception as e:
        raise ValueError(f"环境变量 {name} 需要是整数，但当前为: {raw!r}") from e


def _reason_code_to_int(reason_code) -> int:
    # paho-mqtt 2.x: ReasonCode；1.x: int
    try:
        return int(getattr(reason_code, "value"))
    except Exception:
        try:
            return int(reason_code)
        except Exception:
            return -1


last_ts_by_robotCode: dict[int, int] = {}


def normalize_telemetry(payload: dict) -> dict:
    """
    将 MQTT JSON payload 归一化为统一结构 normalized。

    normalized 必须包含：
    - robotCode: int
    - speed: float (缺失则 0)
    - vehicleBattery: float (缺失则 None)
    - packBattery: float (缺失则 None)
    - ts: int (缺失则当前时间毫秒)
    - receivedAt: int (当前时间毫秒)
    - raw: 原始 payload 对象（用于排查）
    """

    def _get_int(key: str) -> int:
        v = payload.get(key, None)
        if v is None:
            raise ValueError(f"缺少字段 {key}")
        if isinstance(v, bool):
            raise ValueError(f"字段 {key} 类型非法: bool")
        return int(v)

    def _get_float(key: str, default):
        v = payload.get(key, default)
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError(f"字段 {key} 类型非法: bool")
        return float(v)

    received_at = _now_ms()
    ts_val = payload.get("ts", None)
    ts = received_at if ts_val is None else int(ts_val)

    normalized = {
        "robotCode": _get_int("robotCode"),
        "speed": _get_float("speed", 0.0) or 0.0,
        "vehicleBattery": _get_float("vehicleBattery", None),
        "packBattery": _get_float("packBattery", None),
        "ts": ts,
        "receivedAt": received_at,
        "raw": payload,
    }
    return normalized


def build_client(client_id: str, topic: str) -> mqtt.Client:
    # 兼容 paho-mqtt 1.x / 2.x
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except Exception:
        client = mqtt.Client(client_id=client_id)

    client.user_data_set({"topic": topic})
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(_client, userdata, _flags, reason_code, properties=None):
        rc = _reason_code_to_int(reason_code)
        print(f"[{_now()}] MQTT 已连接，rc={rc}")
        t = (userdata or {}).get("topic")
        if not t:
            print(f"[{_now()}] 未配置订阅主题，跳过订阅")
            return
        try:
            result, mid = _client.subscribe(t)
            print(f"[{_now()}] 已订阅主题: {t} (result={result}, mid={mid})")
        except Exception as e:
            print(f"[{_now()}] 订阅失败: {e!r}")

    def on_disconnect(_client, _userdata, reason_code, properties=None):
        rc = _reason_code_to_int(reason_code)
        print(f"[{_now()}] MQTT 已断开，rc={rc}（将自动重连）")

    def on_message(_client, _userdata, msg: mqtt.MQTTMessage):
        payload_bytes = msg.payload or b""
        payload_text = payload_bytes.decode("utf-8", errors="replace")
        print(f"[{_now()}] 收到消息")
        print(f"  topic  : {msg.topic}")
        print(f"  payload: {payload_text}")

        try:
            data = json.loads(payload_text)
        except Exception as e:
            print(f"[{_now()}] JSON 解析失败: {e!r}（不中断）")
            return

        if not isinstance(data, dict):
            print(f"[{_now()}] JSON 不是对象(dict)，实际类型: {type(data).__name__}（不中断）")
            return

        try:
            normalized = normalize_telemetry(data)
        except Exception as e:
            print(f"[{_now()}] normalize_telemetry 失败: {e!r}（不中断）")
            return

        # 过滤：同一个 robotCode 的 ts 不变则跳过
        try:
            robot_code = int(normalized["robotCode"])
            ts = int(normalized["ts"])
            last_ts = last_ts_by_robotCode.get(robot_code)
            if last_ts - 1 <= ts <= last_ts + 1:
                print(f"[{_now()}] 去重: robotCode={robot_code} ts={ts} 未变化，跳过处理")
                return
            last_ts_by_robotCode[robot_code] = ts
        except Exception as e:
            print(f"[{_now()}] 去重过滤异常: {e!r}（不中断，继续处理）")

        # 控制台打印 normalized（格式化 JSON）
        try:
            print(f"[{_now()}] normalized:")
            print(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        except Exception as e:
            print(f"[{_now()}] 打印 normalized 失败: {e!r}（不中断）")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def main() -> None:
    load_dotenv()

    host = _get_env("MQTT_HOST")
    port = _parse_int("MQTT_PORT", 1883)
    topic = _get_env("MQTT_TOPIC")
    username = os.getenv("MQTT_USERNAME", "")
    password = os.getenv("MQTT_PASSWORD", "")
    client_id = _get_env("MQTT_CLIENT_ID")

    client = build_client(client_id=client_id, topic=topic)
    if username or password:
        client.username_pw_set(username=username, password=password)

    print(f"[{_now()}] 启动 MQTT ingestor")
    print(f"  - MQTT_HOST     : {host}")
    print(f"  - MQTT_PORT     : {port}")
    print(f"  - MQTT_TOPIC    : {topic}")
    print(f"  - MQTT_CLIENT_ID: {client_id}")

    while True:
        try:
            print(f"[{_now()}] 尝试连接 MQTT broker...")
            client.connect(host, port=port, keepalive=60)
            # loop_forever 会在断线时自动重连；retry_first_connection 确保首次连接失败也会重试
            client.loop_forever(retry_first_connection=True)
        except KeyboardInterrupt:
            print(f"\n[{_now()}] 收到退出信号，正在停止...")
            try:
                client.disconnect()
            except Exception:
                pass
            break
        except Exception as e:
            print(f"[{_now()}] 运行异常: {e!r}，3 秒后重试连接")
            time.sleep(3)


if __name__ == "__main__":
    main()
