# mqtt-bridge（ECS MQTT -> uniCloud HTTP）

Python services running on Linux/ECS:
-Subscribe to EMQX MQTT topic (plaintext 1883)
-After receiving the message, parse the JSON (only log if parsing fails, not crash)
-Assemble body (transparent field+` acceptAt `+` raw `)
-Call the uniCloud cloud function 'telemetriyIgest' (with 'X-INGEST-TOKEN') via HTTP
-HTTP failure automatic retry (at least 3 times)

## Project Structure

```
mqtt-bridge/
  main.py
  requirements.txt
  .env.example
  README.md
```

## operation steps（Linux / ECS）

Recommended to use Python 3.10+。

Using a virtual environment (recommended)

```bash
cd mqtt-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
vim .env
python3 main.py
```

### Method B: Do not use virtual environment (simple but not recommended)

```bash
cd mqtt-bridge
pip3 install -r requirements.txt
cp .env.example .env
vim .env
python3 main.py
```

## 配置说明（.env）

必须配置：

- `INGEST_URL`: uniCloud 云函数 HTTP URL
- `INGEST_TOKEN`: 你的 `X-INGEST-TOKEN` 值

已给默认/示例值：

- `MQTT_HOST=43.139.45.139`
- `MQTT_PORT=1883`
- `MQTT_TOPIC=01_test/Servers`（注意大小写）

可选配置：

- `HTTP_TIMEOUT_SECONDS`：HTTP 超时（默认 8 秒）
- `HTTP_MAX_RETRIES`：HTTP 最大重试次数（默认 3；代码里会强制至少 3）
- `MQTT_CLIENT_ID`：不填则自动生成

## 验收方式

你在 ECS 上运行 `python3 main.py` 后：

1. 控制台能看到持续打印：`收到 MQTT 消息`
2. 每收到一条 MQTT 消息，会触发一次 HTTP 调用
   - 成功日志类似：`HTTP 写库成功 ... resp=...`
   - 若云函数返回 `{code:0, data:{ok:true}}` 或 `{ok:true}`，都会被识别为成功
3. uniCloud MySQL：
   - `telemetry_history` 每条消息新增一行
   - `telemetry_latest` 对同一 `robotCode` 持续更新最新记录

## 常见错误排查

### 1) 运行时报 “No module named ...”

说明依赖没装好：

```bash
pip3 install -r requirements.txt
```

### 2) venv 创建失败（Ubuntu 常见）

可能缺 `python3-venv`：

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip
```

### 3) MQTT 收不到消息

- 确认 topic 大小写完全一致：`01_test/Servers`
- 确认 EMQX 有消息发布到该 topic
- 确认 ECS 到 `43.139.45.139:1883` 网络可达（安全组/防火墙）

### 4) HTTP 调用失败 / 401 / 鉴权失败

- 确认 `.env` 里 `INGEST_URL` 正确（部署后的 HTTP 触发器地址）
- 确认 `INGEST_TOKEN` 与云函数校验的 token 一致
- 看日志里的 `HTTP xxx:` 返回体截断信息定位原因

