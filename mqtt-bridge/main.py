import json
import os
import queue
import random
import string
import threading
import time
import hashlib
from collections import OrderedDict
from datetime import datetime
from typing import Any, Optional

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_ms() -> int:
    return int(time.time() * 1000)


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
        raise ValueError(f"环境变量 {name} 需要是整数，但当前为: {raw!r}") from e


def get_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except Exception as e:
        raise ValueError(f"环境变量 {name} 需要是数字，但当前为: {raw!r}") from e


def gen_client_id(prefix: str = "mqtt-bridge") -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{prefix}-{suffix}"


def reason_code_to_int(reason_code) -> int:
    # paho-mqtt 2.x: ReasonCode；1.x: int
    try:
        return int(getattr(reason_code, "value"))
    except Exception:
        try:
            return int(reason_code)
        except Exception:
            return -1


class Deduper:
    """
    进程内去重缓存（TTL + 最大容量）。
    用于避免 MQTT 重复投递 / 重连重放导致的重复写库。
    """

    def __init__(self, ttl_seconds: float, max_keys: int):
        self.ttl_ms = max(1000, int(float(ttl_seconds) * 1000))
        self.max_keys = max(1000, int(max_keys))
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, int]" = OrderedDict()  # key -> expires_at_ms

    def seen_recently(self, key: str, now: int) -> bool:
        """
        - True: 在 TTL 内已见过（应跳过）
        - False: 未见过/已过期（已记录，允许继续）
        """
        expires_at = now + self.ttl_ms
        with self._lock:
            # 清理过期（从最旧开始）
            while self._items:
                _, exp = next(iter(self._items.items()))
                if exp > now:
                    break
                self._items.popitem(last=False)

            prev_exp = self._items.get(key)
            if prev_exp is not None and prev_exp > now:
                # 刷新 TTL，保持活跃 key 在尾部
                self._items.move_to_end(key, last=True)
                self._items[key] = expires_at
                return True

            self._items[key] = expires_at
            self._items.move_to_end(key, last=True)

            # 控制容量（超了就丢最旧的）
            while len(self._items) > self.max_keys:
                self._items.popitem(last=False)

            return False


def make_dedup_key(topic: str, payload_obj: dict) -> str:
    """
    基于 topic + payload（排序后 JSON）生成稳定指纹。 
    注意：不要把 receivedAt 这种每次都会变的字段放进来。
    """
    canonical = json.dumps(payload_obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha1()
    h.update((topic or "").encode("utf-8"))
    h.update(b"\n")
    h.update(canonical.encode("utf-8"))
    return h.hexdigest()


class Ingestor:
    def __init__(self, url: str, token: str, timeout_seconds: float, max_retries: int):
        self.url = url
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(3, int(max_retries))
        self.session = requests.Session()

    def post_with_retry(self, body: dict) -> tuple[bool, Optional[dict], str]:
        headers = {
            "Content-Type": "application/json",
            "X-INGEST-TOKEN": self.token,
        }

        last_err = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(
                    self.url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout_seconds,
                )
                text = resp.text
                if resp.status_code < 200 or resp.status_code >= 300:
                    last_err = f"HTTP {resp.status_code}: {text[:500]}"
                    raise RuntimeError(last_err)

                try:
                    data = resp.json()
                except Exception as e:
                    last_err = f"响应不是 JSON: {e!r}, body={text[:500]!r}"
                    raise RuntimeError(last_err)

                # 兼容你描述的两种：{code:0,data:{ok:true}} 或 {ok:true}
                ok = False
                if isinstance(data, dict):
                    if data.get("ok") is True:
                        ok = True
                    elif data.get("code") == 0 and isinstance(data.get("data"), dict) and data["data"].get("ok") is True:
                        ok = True

                if ok:
                    return True, data, ""

                last_err = f"云函数返回非 ok: {json.dumps(data, ensure_ascii=False)[:500]}"
                raise RuntimeError(last_err)
            except Exception as e:
                last_err = str(e)
                if attempt >= self.max_retries:
                    break
                sleep_s = min(8.0, 1.0 * (2 ** (attempt - 1)))  # 1s,2s,4s... capped
                print(f"[{now_str()}] HTTP 调用失败（第 {attempt}/{self.max_retries} 次）：{last_err}，{sleep_s:.1f}s 后重试")
                time.sleep(sleep_s)

        return False, None, last_err


def build_ingest_body(parsed: dict, raw_payload: Any) -> dict:
    body = dict(parsed)  # 透传字段
    body["receivedAt"] = now_ms()
    body["raw"] = raw_payload
    return body


def build_mqtt_client(client_id: str, topic: str, msg_queue: "queue.Queue[tuple[str, str, Any]]") -> mqtt.Client:
    # 兼容 paho-mqtt 1.x / 2.x
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except Exception:
        client = mqtt.Client(client_id=client_id)

    client.user_data_set({"topic": topic, "queue": msg_queue})
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(_client, userdata, _flags, reason_code, properties=None):
        rc = reason_code_to_int(reason_code)
        print(f"[{now_str()}] MQTT 已连接，rc={rc}")
        t = (userdata or {}).get("topic")
        try:
            result, mid = _client.subscribe(t)
            print(f"[{now_str()}] 已订阅主题: {t} (result={result}, mid={mid})")
        except Exception as e:
            print(f"[{now_str()}] 订阅失败: {e!r}")

    def on_disconnect(_client, _userdata, reason_code, properties=None):
        rc = reason_code_to_int(reason_code)
        print(f"[{now_str()}] MQTT 已断开，rc={rc}（将自动重连）")

    def on_message(_client, userdata, msg: mqtt.MQTTMessage):
        payload_bytes = msg.payload or b""
        payload_text = payload_bytes.decode("utf-8", errors="replace")

        print(f"[{now_str()}] 收到 MQTT 消息")
        print(f"  topic  : {msg.topic}")
        print(f"  payload: {payload_text}")

        try:
            parsed = json.loads(payload_text)
        except Exception as e:
            print(f"[{now_str()}] payload JSON 解析失败: {e!r}（跳过本条，不崩溃）")
            return

        if not isinstance(parsed, dict):
            print(f"[{now_str()}] payload JSON 不是对象(dict)，实际: {type(parsed).__name__}（跳过本条）")
            return

        q: "queue.Queue[tuple[str, str, Any]]" = (userdata or {}).get("queue")
        if q is None:
            print(f"[{now_str()}] 内部队列未初始化（跳过本条）")
            return
        try:
            q.put_nowait((msg.topic, payload_text, parsed))
        except queue.Full:
            print(f"[{now_str()}] 队列已满，丢弃本条消息（避免阻塞 MQTT 网络线程）")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def worker_loop(
    stop_event: threading.Event,
    msg_queue: "queue.Queue[tuple[str, str, Any]]",
    ingestor: Ingestor,
    deduper: Optional[Deduper],
):
    while not stop_event.is_set():
        try:
            topic, payload_text, parsed = msg_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        try:
            if deduper is not None:
                now = now_ms()
                try:
                    key = make_dedup_key(topic=topic, payload_obj=parsed)
                    if deduper.seen_recently(key, now=now):
                        print(
                            f"[{now_str()}] 去重: MQTT payload 重复，跳过写库 "
                            f"robotCode={parsed.get('robotCode', None)} ts={parsed.get('ts', None)} key={key[:12]}"
                        )
                        continue
                except Exception as e:
                    # 去重失败不能影响主流程
                    print(f"[{now_str()}] 去重异常: {e!r}（忽略，继续写库）")

            body = build_ingest_body(parsed=parsed, raw_payload=parsed)
            ok, resp_json, err = ingestor.post_with_retry(body)
            robot_code = parsed.get("robotCode", None)
            if ok:
                print(f"[{now_str()}] HTTP 写库成功 robotCode={robot_code} resp={json.dumps(resp_json, ensure_ascii=False)[:500]}")
            else:
                print(f"[{now_str()}] HTTP 写库失败 robotCode={robot_code} err={err}")
        except Exception as e:
            print(f"[{now_str()}] 处理/上报异常: {e!r}（不中断）")
        finally:
            msg_queue.task_done()


def main() -> None:
    load_dotenv()

    mqtt_host = os.getenv("MQTT_HOST", "43.139.45.139").strip()
    mqtt_port = get_env_int("MQTT_PORT", 1883)
    mqtt_topic = os.getenv("MQTT_TOPIC", "01_test/Servers").strip()
    ingest_url = get_env_required("INGEST_URL")
    ingest_token = get_env_required("INGEST_TOKEN")

    http_timeout = get_env_float("HTTP_TIMEOUT_SECONDS", 8.0)
    http_max_retries = get_env_int("HTTP_MAX_RETRIES", 3)

    dedup_enabled = os.getenv("DEDUP_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    dedup_ttl_seconds = get_env_float("DEDUP_TTL_SECONDS", 30.0)
    dedup_max_keys = get_env_int("DEDUP_MAX_KEYS", 50000)

    client_id = os.getenv("MQTT_CLIENT_ID", "").strip() or gen_client_id()

    print(f"[{now_str()}] 启动 mqtt-bridge（MQTT -> uniCloud HTTP）")
    print(f"  - MQTT_HOST   : {mqtt_host}")
    print(f"  - MQTT_PORT   : {mqtt_port}")
    print(f"  - MQTT_TOPIC  : {mqtt_topic}")
    print(f"  - MQTT_CLIENT : {client_id}")
    print(f"  - INGEST_URL  : {ingest_url}")
    print(f"  - HTTP_TIMEOUT: {http_timeout}s")
    print(f"  - HTTP_RETRIES: {http_max_retries}")
    print(f"  - DEDUP_ENABLED: {dedup_enabled}")
    if dedup_enabled:
        print(f"  - DEDUP_TTL   : {dedup_ttl_seconds}s")
        print(f"  - DEDUP_MAX   : {dedup_max_keys}")

    msg_queue: "queue.Queue[tuple[str, str, Any]]" = queue.Queue(maxsize=1000)
    ingestor = Ingestor(
        url=ingest_url,
        token=ingest_token,
        timeout_seconds=http_timeout,
        max_retries=http_max_retries,
    )

    deduper = Deduper(ttl_seconds=dedup_ttl_seconds, max_keys=dedup_max_keys) if dedup_enabled else None

    stop_event = threading.Event()
    t = threading.Thread(target=worker_loop, args=(stop_event, msg_queue, ingestor, deduper), daemon=True)
    t.start()

    client = build_mqtt_client(client_id=client_id, topic=mqtt_topic, msg_queue=msg_queue)

    while True:
        try:
            print(f"[{now_str()}] 尝试连接 MQTT broker...")
            client.connect(mqtt_host, port=mqtt_port, keepalive=60)
            # 断线后自动重连；首次连接失败也会重试
            client.loop_forever(retry_first_connection=True)
        except KeyboardInterrupt:
            print(f"\n[{now_str()}] 收到退出信号，正在停止...")
            stop_event.set()
            try:
                client.disconnect()
            except Exception:
                pass
            break
        except Exception as e:
            print(f"[{now_str()}] MQTT 主循环异常: {e!r}，3 秒后重试")
            time.sleep(3)


if __name__ == "__main__":
    main()

