# IoT Gateway — 统一物联网网关

合并原 `command-bridge` + `mqtt-bridge` 为一个服务，新增 WebSocket 实时推送。

**一个进程，三个职责：**

1. **MQTT 订阅** — 接收机器人遥测 → 写入 uniCloud 数据库 + WebSocket 广播
2. **HTTP 指令下发** — 接收 uniCloud 后端命令 → 发布到 MQTT → 机器人执行
3. **WebSocket 推送** — 小程序实时连接，毫秒级接收遥测数据

## 架构图

```
                         ┌─────────────────────────────────┐
  机器人 ── MQTT ──────→ │         IoT Gateway              │ ←── WebSocket ──→ 小程序前端
                         │                                   │
  uniCloud ── HTTP ────→ │  POST /sendCommand → MQTT 发布   │ ──→ 机器人
  userService            │  GET  /healthz     → 状态检查     │
                         │  WS   /ws          → 实时推送     │
                         │                                   │
                         │  MQTT 订阅 → 去重 → 写库 + 广播   │
                         └───────────────┬───────────────────┘
                                         │ HTTP POST
                                         ↓
                                   uniCloud 云函数
                                  telemetryIngest
                                         │
                              ┌──────────┴──────────┐
                              ↓                     ↓
                       telemetry_latest      telemetry_history
                       (robotCode 唯一)      (每条插入一行)
```

## 目录结构

```
gateway/
  main.py            # 主程序（合并全部功能）
  requirements.txt   # Python 依赖
  .env.example       # 配置模板
  .env               # 实际配置（需自建，不提交 git）
  Procfile           # Render 部署启动命令
  README.md
```

## 数据库写入详情

当 gateway 收到一条 MQTT 遥测消息后，经过去重检查，会 HTTP POST 到 uniCloud 云函数 `telemetryIngest`。

### 写入的数据字段

gateway 发送给 telemetryIngest 的请求体：

```json
{
  "robotCode": "12315",
  "speed": 0.0,
  "vehicleBattery": 85.5,
  "packBattery": 92.3,
  "ts": 1775574457193,
  "receivedAt": 1775574457300,
  "raw": { "robotCode": 12315, "speed": 0.0, "vehicleBattery": 85.5, "packBattery": 92.3, "ts": 1775574457193 }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `robotCode` | string | 机器人编号（来自 MQTT payload） |
| `speed` | float | 当前速度（缺失则 0） |
| `vehicleBattery` | float | 车体电量百分比 |
| `packBattery` | float | 电池包电量百分比 |
| `ts` | int | 机器人端时间戳（ms），缺失则用当前时间 |
| `receivedAt` | int | gateway 收到消息的时间戳（ms） |
| `raw` | object | MQTT 原始 JSON payload（用于排查） |

### telemetryIngest 云函数处理逻辑

收到上述数据后，云函数执行：

1. **鉴权**：校验 `X-INGEST-TOKEN` Header
2. **频率限制**：同一 `robotCode` 两次写入间隔 < 3 秒则跳过
3. **去重**：同一 `robotCode` 在 10 秒窗口内 speed/vehicleBattery/packBattery/rawJson 完全一致则跳过
4. **写入 `telemetry_history`**：每条消息插入一行（历史记录）
5. **写入/更新 `telemetry_latest`**：按 `robotCode` upsert，始终保持最新一条

### 最终写入数据库的文档

```json
{
  "robotCode": "12315",
  "speed": 0.0,
  "vehicleBattery": 85.5,
  "packBattery": 92.3,
  "ts": 1775574457193,
  "receivedAt": 1775574457300,
  "rawJson": "{\"robotCode\":12315,\"speed\":0.0,...}",
  "createdAt": 1775574457350,
  "updatedAt": 1775574457350
}
```

- `telemetry_history`：包含 `createdAt`，不含 `updatedAt`
- `telemetry_latest`：包含 `createdAt` + `updatedAt`

## 配置说明（.env）

```bash
cp .env.example .env
```

### 必须配置

| 变量 | 说明 | 示例 |
|------|------|------|
| `MQTT_HOST` | EMQX Broker 地址 | `43.139.45.139` |
| `COMMAND_TOKEN` | HTTP 指令鉴权令牌 | `abc123` |
| `INGEST_URL` | uniCloud telemetryIngest HTTP 地址 | `https://fc-mp-xxx.bspapp.com/telemetryIngest` |
| `INGEST_TOKEN` | 云函数鉴权令牌 | `abc123` |

### 可选配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HTTP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `HTTP_PORT` / `PORT` | `8090` | HTTP 端口（Render 自动设置 `PORT`） |
| `MQTT_PORT` | `1883` | MQTT 端口 |
| `MQTT_USERNAME` | (空) | MQTT 用户名 |
| `MQTT_PASSWORD` | (空) | MQTT 密码 |
| `MQTT_CLIENT_ID` | (自动生成) | MQTT 客户端 ID |
| `MQTT_BASE_PREFIX` | `01_test` | 命令发布 topic 前缀 |
| `MQTT_SUB_TOPIC` | `{PREFIX}/Servers` | 遥测订阅 topic |
| `WS_TOKEN` | (同 COMMAND_TOKEN) | WebSocket 连接鉴权令牌 |
| `DEDUP_ENABLED` | `1` | 是否启用消息去重 |
| `DEDUP_TTL_SECONDS` | `30` | 去重窗口（秒） |

## 本地启动

需要 Python 3.10+。

```bash
cd gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入你的实际配置
python3 main.py
```

启动成功后输出：

```
[2026-04-08 12:00:00] MQTT 连接 43.139.45.139:1883
  订阅主题: 01_test/Servers
  命令前缀: 01_test
[2026-04-08 12:00:00] 遥测写库已启用: https://fc-mp-xxx.bspapp.com/telemetryIngest
[2026-04-08 12:00:00] IoT Gateway 启动
  HTTP  : 0.0.0.0:8090
  路由  : POST /sendCommand | GET /healthz | WS /ws
[2026-04-08 12:00:01] MQTT 已连接 rc=0
[2026-04-08 12:00:01] 已订阅: 01_test/Servers (result=0)
```

## 验证测试

### 1. 健康检查

```bash
curl http://localhost:8090/healthz
```

返回：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "mqttConnected": true,
    "wsClients": 0,
    "basePrefix": "01_test"
  }
}
```

### 2. 测试 WebSocket

#### 方法 A：用 wscat（推荐）

安装：

```bash
npm install -g wscat
```

连接：

```bash
wscat -c "ws://localhost:8090/ws?token=abc123"
```

连接成功后会收到：

```json
{"type":"connected","msg":"ok"}
```

然后保持终端开着。当机器人发送 MQTT 遥测时，你会实时看到推送：

```json
{"type":"telemetry","robotCode":"12315","data":{"speed":0.0,"vehicleBattery":85.5,"packBattery":92.3,"ts":1775574457193}}
```

你也可以手动发送心跳测试：

```
> {"type":"ping"}
< {"type":"pong"}
```

按 `Ctrl+C` 断开。

#### 方法 B：用 Python 脚本

```python
import asyncio
import websockets
import json

async def test():
    uri = "ws://localhost:8090/ws?token=abc123"
    async with websockets.connect(uri) as ws:
        print("已连接，等待遥测推送...")
        # 发送心跳
        await ws.send(json.dumps({"type": "ping"}))
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            print(f"收到: {json.dumps(data, ensure_ascii=False, indent=2)}")

asyncio.run(test())
```

需要先 `pip install websockets`。

#### 方法 C：用浏览器控制台

打开任意网页，按 F12 → Console，粘贴：

```javascript
const ws = new WebSocket("ws://localhost:8090/ws?token=abc123");
ws.onopen = () => console.log("已连接");
ws.onmessage = (e) => console.log("收到:", JSON.parse(e.data));
ws.onclose = () => console.log("已断开");
```

当有遥测数据时，控制台会打印收到的 JSON。

### 3. 测试指令下发

```bash
curl -X POST http://localhost:8090/sendCommand \
  -H "Content-Type: application/json" \
  -H "x-command-token: abc123" \
  -d '{
    "robotCode": "12315",
    "cmd": "stop",
    "params": {},
    "ts": 1705291825000,
    "requestId": "test_stop_001"
  }'
```

返回 `{"code":0,"msg":"ok"}` 表示命令已发布到 MQTT。

### 4. 端到端完整验证

同时打开两个终端：

**终端 1** — 启动 gateway：
```bash
python3 main.py
```

**终端 2** — 连接 WebSocket：
```bash
wscat -c "ws://localhost:8090/ws?token=abc123"
```

然后让机器人发送一条 MQTT 遥测消息（或用 MQTT 客户端手动发布）：

```bash
# 用 mosquitto_pub 模拟机器人遥测
mosquitto_pub -h 43.139.45.139 -t "01_test/Servers" \
  -m '{"robotCode":12315,"speed":0.5,"vehicleBattery":80.0,"packBattery":90.0,"ts":1775574460000}'
```

预期结果：

- **终端 1 日志**：`遥测写库成功 robotCode=12315`
- **终端 2 WebSocket**：实时收到 `{"type":"telemetry","robotCode":"12315","data":{...}}`
- **uniCloud 数据库**：`telemetry_latest` 中 robotCode=12315 的记录已更新

## 部署到 Render

1. 在 Render Dashboard 创建新的 Web Service
2. 关联 Git 仓库，Root Directory 设置为 `gateway`
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python main.py`
5. 在 Environment 中添加 `.env.example` 里的所有变量
6. 部署完成后，更新小程序 `utils/ws-client.js` 中的 `WS_BASE_URL`

注意：Render 会自动设置 `PORT` 环境变量，gateway 会自动读取。

### Render 免费版限制

- 15 分钟无请求自动休眠（有 WS 客户端连着时不会休眠）
- 休眠后首次连接需 30-50 秒冷启动
- 冷启动期间 MQTT 连接也断开，遥测数据会丢失

生产环境建议使用 Render 付费版（$7/月）或迁移到阿里云 ECS。

## 错误码

| 错误码 | 含义 |
|--------|------|
| `4001` | `x-command-token` 无效 |
| `4002` | 请求体不是合法 JSON |
| `4003` | 字段校验失败（缺少必填字段、类型错误等） |
| `4004` | 不支持的 cmd |
| `5001` | MQTT 未连接 |
| `5002` | MQTT publish 失败 |

## MQTT Topic 约定

| 方向 | Topic | 说明 |
|------|-------|------|
| 上行（机器人→gateway） | `{PREFIX}/Servers` | 遥测数据，gateway 订阅 |
| 下行（gateway→机器人） | `{PREFIX}/status` | start / stop / clear_error / emergency_stop |
| 下行（gateway→机器人） | `{PREFIX}/move` | move（x/y 或 direction） |

默认 `PREFIX = 01_test`，由 `MQTT_BASE_PREFIX` 环境变量控制。

## 从旧服务迁移

本 gateway 完全兼容原 `command-bridge` 和 `mqtt-bridge` 的所有功能：

- `/sendCommand` 接口签名完全不变（uniCloud 后端无需修改）
- `/healthz` 接口保持兼容（新增 `wsClients` 字段）
- MQTT 遥测处理逻辑与 mqtt-bridge 一致（去重 + HTTP 写库）
- 新增 WebSocket `/ws` 端点（纯增量功能）

迁移步骤：

1. 部署 gateway 到 Render
2. 更新 uniCloud 配置中的 `commandBridge.url` 指向新服务
3. 确认指令下发和遥测接收正常
4. 删除旧的 command-bridge 服务
5. 本地 mqtt-bridge 不再需要（gateway 已包含）
