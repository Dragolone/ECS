"""
IoT Gateway 安全 + 压力测试脚本

用法: python test_security.py [--host HOST] [--port PORT]
需要先启动 gateway: python main.py
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time

import requests

try:
    import websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False
    print("[WARN] websocket-client 未安装，WS 测试跳过。运行 pip install websocket-client")

PASS = 0
FAIL = 0
SKIP = 0

def report(name, ok, detail=""):
    global PASS, FAIL
    tag = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  [{tag}] {name}{suffix}")

def skip(name, reason=""):
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {name} — {reason}")

def make_signed_token(uid, secret, ttl_ms=3600000):
    expires_at = int(time.time() * 1000) + ttl_ms
    payload = f"{uid}:{expires_at}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{uid}:{expires_at}:{sig}"

def make_expired_token(uid, secret):
    expires_at = int(time.time() * 1000) - 60000
    payload = f"{uid}:{expires_at}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{uid}:{expires_at}:{sig}"


def test_healthz(base):
    print("\n=== 健康检查 ===")
    try:
        r = requests.get(f"{base}/healthz", timeout=5)
        data = r.json()
        report("GET /healthz 200", r.status_code == 200)
        report("返回 code=0", data.get("code") == 0)
        report("不暴露 basePrefix", "basePrefix" not in data.get("data", {}))
    except Exception as e:
        report("healthz 可达", False, str(e))


def test_sendcommand_auth(base, token):
    print("\n=== sendCommand 鉴权 ===")

    r = requests.post(f"{base}/sendCommand", json={"robotCode": "test", "cmd": "stop", "params": {}, "ts": int(time.time()*1000), "requestId": "t1"}, timeout=5)
    report("无 token → 401", r.status_code == 401)

    r = requests.post(f"{base}/sendCommand", headers={"x-command-token": "wrong_token_xxx"}, json={"robotCode": "test", "cmd": "stop", "params": {}, "ts": int(time.time()*1000), "requestId": "t2"}, timeout=5)
    report("错误 token → 401", r.status_code == 401)

    r = requests.post(f"{base}/sendCommand", headers={"x-command-token": token}, json={}, timeout=5)
    report("空 body → 400", r.status_code == 400)


def test_sendcommand_validation(base, token):
    print("\n=== sendCommand 输入校验 ===")
    headers = {"x-command-token": token, "Content-Type": "application/json"}

    r = requests.post(f"{base}/sendCommand", headers=headers, json={"robotCode": "12315", "cmd": "hack_cmd", "params": {}, "ts": int(time.time()*1000), "requestId": "t3"}, timeout=5)
    report("未知 cmd → 400", r.status_code == 400, f"code={r.json().get('code')}")

    r = requests.post(f"{base}/sendCommand", headers=headers, json={"robotCode": "", "cmd": "stop", "params": {}, "ts": int(time.time()*1000), "requestId": "t4"}, timeout=5)
    report("空 robotCode → 400", r.status_code == 400)

    r = requests.post(f"{base}/sendCommand", headers=headers, json={"robotCode": "12315", "cmd": "move", "params": {"direction": "hack_direction"}, "ts": int(time.time()*1000), "requestId": "t5"}, timeout=5)
    report("非法 direction → 400", r.status_code == 400)

    r = requests.post(f"{base}/sendCommand", headers=headers, data="not json", timeout=5)
    report("非 JSON body → 400", r.status_code == 400)


def test_sendcommand_large_payload(base, token):
    print("\n=== sendCommand 大 payload ===")
    headers = {"x-command-token": token, "Content-Type": "application/json"}
    big_body = {"robotCode": "12315", "cmd": "stop", "params": {"junk": "x" * 100000}, "ts": int(time.time()*1000), "requestId": "t6"}
    try:
        r = requests.post(f"{base}/sendCommand", headers=headers, json=big_body, timeout=10)
        report("100KB payload 不崩溃", r.status_code in (200, 400, 503), f"status={r.status_code}")
    except Exception as e:
        report("100KB payload 不崩溃", False, str(e))


def test_ws_auth(base_ws, ws_secret):
    if not HAS_WS:
        skip("WS 鉴权测试", "websocket-client 未安装")
        return
    print("\n=== WebSocket 鉴权 ===")

    # 无 token
    try:
        ws = websocket.create_connection(f"{base_ws}/ws", timeout=5)
        msg = json.loads(ws.recv())
        ws.close()
        report("无 token → error", msg.get("type") == "error")
    except Exception as e:
        report("无 token → 拒绝", True, str(e))

    # 错误 token
    try:
        ws = websocket.create_connection(f"{base_ws}/ws?token=wrong_token", timeout=5)
        msg = json.loads(ws.recv())
        ws.close()
        report("错误 token → error", msg.get("type") == "error")
    except Exception as e:
        report("错误 token → 拒绝", True, str(e))

    # 旧格式静态 token（不应被接受）
    try:
        ws = websocket.create_connection(f"{base_ws}/ws?token={ws_secret}", timeout=5)
        msg = json.loads(ws.recv())
        ws.close()
        report("旧格式静态 token → 拒绝", msg.get("type") == "error")
    except Exception as e:
        report("旧格式静态 token → 拒绝", True, str(e))

    # 过期 token
    expired = make_expired_token("test_uid", ws_secret)
    try:
        ws = websocket.create_connection(f"{base_ws}/ws?token={expired}", timeout=5)
        msg = json.loads(ws.recv())
        ws.close()
        report("过期 token → error", msg.get("type") == "error")
    except Exception as e:
        report("过期 token → 拒绝", True, str(e))

    # 有效签名 token
    valid = make_signed_token("test_uid", ws_secret)
    try:
        ws = websocket.create_connection(f"{base_ws}/ws?token={valid}", timeout=5)
        msg = json.loads(ws.recv())
        ws.close()
        report("有效签名 token → connected", msg.get("type") == "connected")
    except Exception as e:
        report("有效签名 token → connected", False, str(e))


def test_ws_heartbeat(base_ws, ws_secret):
    if not HAS_WS:
        skip("WS 心跳测试", "websocket-client 未安装")
        return
    print("\n=== WebSocket 心跳 ===")
    token = make_signed_token("heartbeat_uid", ws_secret)
    try:
        ws = websocket.create_connection(f"{base_ws}/ws?token={token}", timeout=5)
        ws.recv()  # connected
        ws.send(json.dumps({"type": "ping"}))
        msg = json.loads(ws.recv())
        report("ping → pong", msg.get("type") == "pong")
        ws.close()
    except Exception as e:
        report("ping/pong", False, str(e))


def test_ws_large_message(base_ws, ws_secret):
    if not HAS_WS:
        skip("WS 大消息测试", "websocket-client 未安装")
        return
    print("\n=== WebSocket 大消息 ===")
    token = make_signed_token("large_msg_uid", ws_secret)
    try:
        ws = websocket.create_connection(f"{base_ws}/ws?token={token}", timeout=5)
        ws.recv()  # connected
        big_msg = json.dumps({"type": "ping", "junk": "x" * 10000})
        ws.send(big_msg)
        time.sleep(0.5)
        ws.send(json.dumps({"type": "ping"}))
        msg = json.loads(ws.recv())
        report("大消息被丢弃后仍可通信", msg.get("type") == "pong")
        ws.close()
    except Exception as e:
        report("大消息处理", False, str(e))


def test_ws_concurrent(base_ws, ws_secret, count=50):
    if not HAS_WS:
        skip("WS 并发测试", "websocket-client 未安装")
        return
    print(f"\n=== WebSocket 并发 ({count} 连接) ===")
    connections = []
    successes = 0
    errors = 0

    def connect_one(idx):
        nonlocal successes, errors
        token = make_signed_token(f"concurrent_{idx}", ws_secret)
        try:
            ws = websocket.create_connection(f"{base_ws}/ws?token={token}", timeout=10)
            msg = json.loads(ws.recv())
            if msg.get("type") == "connected":
                successes += 1
                connections.append(ws)
            else:
                errors += 1
        except Exception:
            errors += 1

    threads = [threading.Thread(target=connect_one, args=(i,)) for i in range(count)]
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    elapsed = time.time() - start

    report(f"{count} 并发连接成功率", successes >= count * 0.9,
           f"{successes}/{count} 成功, 耗时 {elapsed:.2f}s")

    for ws in connections:
        try:
            ws.close()
        except Exception:
            pass
    time.sleep(1)


def test_ws_max_connections(base_ws, ws_secret, max_clients=200):
    if not HAS_WS:
        skip("WS 最大连接数测试", "websocket-client 未安装")
        return
    print(f"\n=== WebSocket 最大连接数限制 (尝试 {max_clients + 10}) ===")
    connections = []
    rejected = 0

    for i in range(max_clients + 10):
        token = make_signed_token(f"maxtest_{i}", ws_secret)
        try:
            ws = websocket.create_connection(f"{base_ws}/ws?token={token}", timeout=5)
            msg = json.loads(ws.recv())
            if msg.get("type") == "connected":
                connections.append(ws)
            elif msg.get("type") == "error" and "full" in msg.get("msg", ""):
                rejected += 1
        except Exception:
            rejected += 1

    report(f"超过上限被拒绝", rejected > 0, f"已连接 {len(connections)}, 被拒 {rejected}")

    for ws in connections:
        try:
            ws.close()
        except Exception:
            pass
    time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="IoT Gateway 安全测试")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--command-token", default=os.getenv("COMMAND_TOKEN", "abc123"))
    parser.add_argument("--ws-secret", default=os.getenv("WS_TOKEN", "abc123"))
    parser.add_argument("--skip-ws", action="store_true")
    parser.add_argument("--skip-stress", action="store_true")
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    base_ws = f"ws://{args.host}:{args.port}"

    print(f"目标: {base}")
    print(f"WS:   {base_ws}")
    print("=" * 60)

    # HTTP 测试
    test_healthz(base)
    test_sendcommand_auth(base, args.command_token)
    test_sendcommand_validation(base, args.command_token)
    test_sendcommand_large_payload(base, args.command_token)

    # WebSocket 测试
    if not args.skip_ws:
        test_ws_auth(base_ws, args.ws_secret)
        test_ws_heartbeat(base_ws, args.ws_secret)
        test_ws_large_message(base_ws, args.ws_secret)

        if not args.skip_stress:
            test_ws_concurrent(base_ws, args.ws_secret, count=50)
            test_ws_max_connections(base_ws, args.ws_secret)

    # 汇总
    print("\n" + "=" * 60)
    total = PASS + FAIL + SKIP
    print(f"结果: {PASS} PASS / {FAIL} FAIL / {SKIP} SKIP (共 {total})")
    if FAIL > 0:
        print("存在失败项，请检查！")
        sys.exit(1)
    else:
        print("全部通过！")


if __name__ == "__main__":
    main()
