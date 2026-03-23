# command-bridge（uniCloud HTTP -> EMQX MQTT）

`command-bridge` 是一个独立的下行命令桥服务，职责很简单：

- 接收 uniCloud 后端发来的 HTTP 命令请求
- 校验 `x-command-token`
- 根据 `cmd` 映射到 MQTT topic
- 组装并发布 JSON payload 到 EMQX
- 返回发布成功 / 失败结果

它**不负责**用户权限、机器人在线状态、业务规则判断，这些都应在 uniCloud 后端先处理完成。

## 目录结构

```text
command-bridge/
  main.py
  requirements.txt
  .env.example
  README.md
```

## 当前 topic 约定

当前测试阶段统一使用前缀 `01_test`，默认由环境变量 `MQTT_BASE_PREFIX` 控制。

命令映射规则如下：

- `start` / `stop` / `clear_error` / `emergency_stop` -> `01_test/status`
- `move` -> `01_test/move`

## 请求接口

### 1. 发送命令

`POST /sendCommand`

请求头：

```http
x-command-token: your_token
Content-Type: application/json
```

请求体示例：

```json
{
  "robotCode": "12315",
  "cmd": "move",
  "params": {
    "x": -1,
    "y": 0
  },
  "ts": 1705291825000,
  "requestId": "cmd_move_001"
}
```

成功返回：

```json
{
  "code": 0,
  "msg": "ok"
}
```

失败时会返回明确错误码与错误信息，例如：

```json
{
  "code": 4004,
  "msg": "不支持的 cmd: xxx"
}
```

### 2. 健康检查

`GET /healthz`

可用于查看服务是否启动，以及 MQTT 当前是否已连接。

## 环境变量说明

至少需要配置：

- `MQTT_HOST`
- `MQTT_PORT`
- `MQTT_USERNAME`
- `MQTT_PASSWORD`
- `MQTT_CLIENT_ID`
- `COMMAND_TOKEN`

另外建议配置：

- `HTTP_HOST`
- `HTTP_PORT`
- `MQTT_BASE_PREFIX`

## 启动方法

推荐 Python 3.10+。

### 方式一：使用虚拟环境

```bash
cd command-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 main.py
```

然后按你的实际环境修改 `.env`。

### 方式二：不使用虚拟环境

```bash
cd command-bridge
pip3 install -r requirements.txt
cp .env.example .env
python3 main.py
```

## 运行日志

服务启动后，终端会打印以下关键信息，方便排查问题：

- 收到了什么命令
- 映射到了哪个 topic
- publish 是否成功
- 失败原因是什么

典型日志类似：

```text
[2026-03-23 12:00:00] 收到下行命令
  robotCode : 12315
  cmd       : move
  topic     : 01_test/move
  requestId : cmd_move_001
  payload   : {"robotCode":"12315","cmd":"move","params":{"x":-1,"y":0},"ts":1705291825000,"requestId":"cmd_move_001"}
[2026-03-23 12:00:00] publish 成功：topic=01_test/move requestId=cmd_move_001
```

## 手动验收步骤

### 1. 启动服务

```bash
cd command-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 main.py
```

### 2. 检查健康状态

```bash
curl "http://127.0.0.1:8090/healthz"
```

如果 MQTT 已连接，返回中 `data.mqttConnected` 应为 `true`。

### 3. 手动发送状态类命令

```bash
curl -X POST "http://127.0.0.1:8090/sendCommand" \
  -H "Content-Type: application/json" \
  -H "x-command-token: replace_with_your_secure_token" \
  -d '{
    "robotCode": "12315",
    "cmd": "start",
    "params": {},
    "ts": 1705291825000,
    "requestId": "cmd_start_001"
  }'
```

预期：

- HTTP 返回 `{"code":0,"msg":"ok"}`
- 服务日志显示 topic 为 `01_test/status`
- 机器人或 MQTT 客户端能在 `01_test/status` 收到消息

### 4. 手动发送移动类命令

```bash
curl -X POST "http://127.0.0.1:8090/sendCommand" \
  -H "Content-Type: application/json" \
  -H "x-command-token: replace_with_your_secure_token" \
  -d '{
    "robotCode": "12315",
    "cmd": "move",
    "params": {
      "x": -1,
      "y": 0
    },
    "ts": 1705291825000,
    "requestId": "cmd_move_001"
  }'
```

预期：

- HTTP 返回 `{"code":0,"msg":"ok"}`
- 服务日志显示 topic 为 `01_test/move`
- 机器人或 MQTT 客户端能在 `01_test/move` 收到消息

### 5. 校验错误场景

可以分别测试：

- 不带 `x-command-token`
- token 错误
- `cmd` 传未知值
- `params` 不是对象
- MQTT Broker 未连接

这样可以验证错误码和错误信息是否符合预期。

## uniCloud 后端如何调用

你的 uniCloud 后端在完成以下逻辑后再调用本服务：

1. 校验当前用户是否绑定机器人
2. 校验机器人是否在线
3. 写入 `commands` 表日志
4. 调用 `command-bridge`

调用方式建议：

- 方法：`POST`
- URL：`http://<command-bridge-host>:8090/sendCommand`
- Header：
  - `Content-Type: application/json`
  - `x-command-token: <COMMAND_TOKEN>`
- Body：直接传命令对象

请求体建议直接使用：

```json
{
  "robotCode": "12315",
  "cmd": "move",
  "params": {
    "x": -1,
    "y": 0
  },
  "ts": 1705291825000,
  "requestId": "cmd_move_001"
}
```

如果你在 uniCloud 云函数里发请求，伪代码可以类似：

```js
const res = await uniCloud.httpclient.request(commandBridgeUrl, {
  method: "POST",
  dataType: "json",
  contentType: "json",
  headers: {
    "x-command-token": commandToken
  },
  data: {
    robotCode,
    cmd,
    params,
    ts: Date.now(),
    requestId
  }
})
```

然后根据返回值判断：

- `code === 0`：说明命令已成功 publish 到 EMQX
- 非 `0`：按 `msg` 记录错误日志，并决定是否提示前端“下发失败”

## 当前版本边界

当前版本只做最小可跑通链路：

- HTTP 收命令
- MQTT publish
- 返回结果

暂未实现：

- 机器人 ACK 回执
- 重试队列
- 命令状态回写
- 更复杂的命令参数校验
