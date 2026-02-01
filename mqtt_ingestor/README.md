# mqtt_ingestor

一个最小可运行的 Python MQTT 订阅程序：连接 EMQX，订阅指定主题，收到消息后打印 `topic` 与 `payload`，并尝试解析 JSON（解析失败不中断）。  
当前阶段**只做接收+打印**，不写 uniCloud / MySQL。

## 目录结构

```
mqtt_ingestor/
  src/main.py
  requirements.txt
  .env.example
  README.md
```

## macOS 运行步骤

在项目根目录（`iot-bridge/`）下执行：

```bash
cd mqtt_ingestor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python src/main.py
```

然后在 `.env` 中填写以下配置：

- `MQTT_HOST`: MQTT Broker 地址（例如 EMQX）
- `MQTT_PORT`: 端口（默认 1883）
- `MQTT_TOPIC`: 需要订阅的主题
- `MQTT_USERNAME` / `MQTT_PASSWORD`: 账号密码（可为空）
- `MQTT_CLIENT_ID`: 客户端 ID（建议唯一）

## 输出内容

收到消息后会打印：

- `topic`
- `payload` 原文
- JSON 解析结果（若解析成功）：`robotCode`, `speed`, `vehicleBattery`, `packBattery`, `ts`

