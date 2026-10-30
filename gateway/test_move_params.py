"""
move 指令参数校验测试（离线，不连接 MQTT broker，也不读取 .env）

用法: cd gateway && python -m unittest test_move_params -v
"""

import json
import os
import unittest
from unittest import mock

import main as gateway

COMMAND_TOKEN = "test-command-token"


class FakeMQTTClient:
    """替换 MQTTClient：不建立连接，只记录 publish。"""

    instances = []

    def __init__(self, **kwargs):
        self.published = []
        FakeMQTTClient.instances.append(self)

    def start(self):
        pass

    def is_connected(self):
        return True

    def publish_json(self, topic, payload):
        self.published.append((topic, payload))
        return True, ""


def bridge_body(cmd, params):
    """与小程序 userService 的 bridgeRequestBody 字段一致。"""
    return {
        "requestId": "req-1",
        "commandId": "cmd-1",
        "robotCode": "12315",
        "cmd": cmd,
        "params": params,
        "ts": 1775574457193,
    }


class NormalizeVelocityTest(unittest.TestCase):
    def test_passes_vector_through(self):
        self.assertEqual(
            gateway.normalize_move_params({"vx": 0.5, "vy": -0.25, "wz": 0, "enable": True}),
            {"vx": 0.5, "vy": -0.25, "wz": 0.0, "enable": True},
        )

    def test_clamps_to_unit_range(self):
        self.assertEqual(
            gateway.normalize_move_params({"vx": 3, "vy": -3, "wz": 1.5, "enable": True}),
            {"vx": 1.0, "vy": -1.0, "wz": 1.0, "enable": True},
        )

    def test_enable_defaults_to_true(self):
        self.assertIs(gateway.normalize_move_params({"vx": 0.1, "vy": 0, "wz": 0})["enable"], True)

    def test_enable_false_is_kept(self):
        self.assertIs(gateway.normalize_move_params({"vx": 0, "vy": 0, "wz": 0, "enable": False})["enable"], False)

    def test_enable_must_be_boolean(self):
        for enable in ("false", "true", 0, 1, None, []):
            with self.subTest(enable=enable):
                with self.assertRaises(ValueError):
                    gateway.normalize_move_params({"vx": 0, "vy": 0, "wz": 0, "enable": enable})

    def test_missing_axis_is_zero(self):
        self.assertEqual(
            gateway.normalize_move_params({"wz": 0.3}),
            {"vx": 0.0, "vy": 0.0, "wz": 0.3, "enable": True},
        )

    def test_numeric_string_matches_legacy_xy(self):
        self.assertEqual(gateway.normalize_move_params({"vx": "0.5", "vy": 0, "wz": 0})["vx"], 0.5)

    def test_rejects_invalid_speed(self):
        for bad in (None, "", "fast", True, [], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    gateway.normalize_move_params({"vx": bad, "vy": 0, "wz": 0})

    def test_rejects_non_finite_instead_of_full_speed(self):
        for bad in (float("nan"), float("inf"), float("-inf"), "nan", "inf", "1e999", 10 ** 400):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    gateway.normalize_move_params({"vx": bad, "vy": 0, "wz": 0})

    def test_drops_unknown_fields(self):
        self.assertEqual(
            set(gateway.normalize_move_params({"vx": 0, "vy": 0, "wz": 0, "enable": True, "junk": 1})),
            {"vx", "vy", "wz", "enable"},
        )


class NormalizeLegacyTest(unittest.TestCase):
    def test_xy_still_works(self):
        self.assertEqual(gateway.normalize_move_params({"x": 2, "y": -0.5}), {"x": 1.0, "y": -0.5})

    def test_direction_still_works(self):
        self.assertEqual(gateway.normalize_move_params({"direction": " Forward "}), {"direction": "forward"})

    def test_invalid_direction_rejected(self):
        with self.assertRaises(ValueError):
            gateway.normalize_move_params({"direction": "hack_direction"})

    def test_empty_params_rejected(self):
        with self.assertRaises(ValueError):
            gateway.normalize_move_params({})


class SendCommandTest(unittest.TestCase):
    """走完整的 POST /sendCommand，MQTT 替换为假实现。"""

    def setUp(self):
        FakeMQTTClient.instances.clear()
        env = {
            "MQTT_HOST": "mqtt.invalid",
            "MQTT_BASE_PREFIX": "01_test",
            "COMMAND_TOKEN": COMMAND_TOKEN,
            "WS_TOKEN": "",
            "INGEST_URL": "",
            "INGEST_TOKEN": "",
        }
        patches = [
            mock.patch.dict(os.environ, env),
            mock.patch.object(gateway, "load_dotenv"),
            mock.patch.object(gateway, "MQTTClient", FakeMQTTClient),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = gateway.create_app().test_client()
        self.mqtt = FakeMQTTClient.instances[-1]

    def post(self, body, token=COMMAND_TOKEN):
        return self.client.post(
            "/sendCommand",
            headers={"x-command-token": token, "Content-Type": "application/json"},
            data=json.dumps(body),
        )

    def test_real_velocity_body_is_published(self):
        resp = self.post(bridge_body("move", {"vx": 0.42, "vy": -0.1, "wz": 0.75, "enable": True}))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()["code"], 0)
        topic, payload = self.mqtt.published[-1]
        self.assertEqual(topic, "01_test/move")
        self.assertEqual(payload["params"], {"vx": 0.42, "vy": -0.1, "wz": 0.75, "enable": True})
        self.assertEqual(payload["robotCode"], "12315")
        self.assertEqual(payload["requestId"], "req-1")

    def test_real_release_body_keeps_enable_false(self):
        resp = self.post(bridge_body("move", {"vx": 0, "vy": 0, "wz": 0, "enable": False}))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertIs(self.mqtt.published[-1][1]["params"]["enable"], False)

    def test_out_of_range_is_clamped_before_publish(self):
        resp = self.post(bridge_body("move", {"vx": 9, "vy": -9, "wz": 0, "enable": True}))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        params = self.mqtt.published[-1][1]["params"]
        self.assertEqual((params["vx"], params["vy"]), (1.0, -1.0))

    def test_invalid_vector_is_rejected_and_not_published(self):
        for params in (
            {"vx": "fast", "vy": 0, "wz": 0, "enable": True},
            {"vx": 0.5, "vy": 0, "wz": 0, "enable": "false"},
        ):
            with self.subTest(params=params):
                resp = self.post(bridge_body("move", params))
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["code"], gateway.ERROR_INVALID_FIELD)
        self.assertEqual(self.mqtt.published, [])

    def test_json_nan_is_rejected(self):
        # Python 的 json 模块接受裸 NaN，所以这个请求体能到达校验逻辑
        resp = self.client.post(
            "/sendCommand",
            headers={"x-command-token": COMMAND_TOKEN, "Content-Type": "application/json"},
            data='{"requestId":"r","robotCode":"12315","cmd":"move","ts":1,'
                 '"params":{"vx":NaN,"vy":0,"wz":0,"enable":true}}',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.mqtt.published, [])

    def test_legacy_xy_and_direction_still_published(self):
        self.assertEqual(self.post(bridge_body("move", {"x": 0.5, "y": 0.5})).status_code, 200)
        self.assertEqual(self.post(bridge_body("move", {"direction": "left"})).status_code, 200)
        self.assertEqual(self.mqtt.published[-2][1]["params"], {"x": 0.5, "y": 0.5})
        self.assertEqual(self.mqtt.published[-1][1]["params"], {"direction": "left"})

    def test_real_stop_body_goes_to_status_topic(self):
        resp = self.post(bridge_body("stop", {"vx": 0, "vy": 0, "wz": 0, "enable": False}))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(self.mqtt.published[-1][0], "01_test/status")

    def test_goto_stays_unsupported_until_topic_is_agreed(self):
        resp = self.post(bridge_body("goto", {"x": 1, "y": 2}))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], gateway.ERROR_UNSUPPORTED_COMMAND)
        self.assertEqual(self.mqtt.published, [])

    def test_wrong_token_rejected_before_validation(self):
        resp = self.post(bridge_body("move", {"vx": 0, "vy": 0, "wz": 0}), token="wrong")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
