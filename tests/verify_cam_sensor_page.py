#!/usr/bin/env python3
"""CAMPAGE/MSENSOR 传感器集成的轻量静态与协议回归测试。

不导入 console3.board.py（避免部署环境依赖），不访问设备或网络。
"""
import ast
import math
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
_CANDIDATES = (ROOT / "host" / "console3.board.py", Path("/root/console3.py"))
BOARD = next((p for p in _CANDIDATES if p.is_file()), _CANDIDATES[0])
SOURCE = BOARD.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE, filename=str(BOARD))


def page_literal(name):
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("missing page literal: " + name)


def packet_order():
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == "sensor_packet_order":
            module = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(module)
            namespace = {"math": math}
            exec(compile(module, str(BOARD), "exec"), namespace)
            return namespace[node.name]
    raise AssertionError("missing sensor_packet_order")


class PacketOrderTest(unittest.TestCase):
    def test_legacy_packets_and_ordering(self):
        order = packet_order()
        legacy = {"on": ["acc"], "v": {"acc": [1, 2, 3]}, "t": 123}
        self.assertEqual(order(legacy), (None, None))
        self.assertEqual(order({"seq": 1, "mono": 10.5}), (1, 10.5))
        self.assertEqual(order({"seq": 2, "mono": 11}, 1, 10.5), (2, 11.0))
        self.assertIsNone(order({"seq": 1, "mono": 11}, 1, 10.5))
        self.assertIsNone(order({"seq": 0, "mono": 11}, 1, 10.5))
        self.assertIsNone(order({"seq": 2, "mono": 9}, 1, 10.5))
        # 新序号配相同单调时间仍新鲜，兼容低分辨率 performance.now()。
        self.assertEqual(order({"seq": 2, "mono": 10.5}, 1, 10.5), (2, 10.5))
        self.assertEqual(order({"seq": 3}, 1, 10.5), (3, None))
        self.assertEqual(order({"mono": 12}, None, 10.5), (None, 12.0))
        self.assertIsNone(order({"mono": 9}, None, 10.5))

    def test_invalid_new_fields_do_not_poison_legacy_shape(self):
        order = packet_order()
        for bad in (True, False, -1, 1.5, "2", None):
            if bad is None:
                continue
            self.assertIsNone(order({"seq": bad}), repr(bad))
        for bad in (True, False, -1, "1", float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(order({"seq": 1, "mono": bad}), repr(bad))
        self.assertEqual(order({"seq": 2.0, "mono": 3.0}), (2, 3.0))

    def test_live_file_and_ack_remain_compatible(self):
        self.assertEqual(struct.calcsize("<iiIq40f"), 180)
        self.assertIn('struct.pack("<iiIq40f"', SOURCE)
        self.assertIn("int(time.time() * 1000)", SOURCE)
        self.assertIn('ack = {"type": "ack", "mask": m, "n": len(on)}', SOURCE)
        check = SOURCE.index("order = sensor_packet_order(msg, last_seq, last_mono)")
        write = SOURCE.index("m = sensor_write_live(name, on, vals)", check)
        self.assertLess(check, write)
        self.assertIn("if seq is not None:", SOURCE[check:])
        self.assertIn("if mono is not None:", SOURCE[check:])


class CamPageStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cam = page_literal("CAMPAGE")
        cls.msensor = page_literal("MSENSOR")
        start = cls.cam.index("// 相机页传感器同步")
        cls.sensor = cls.cam[start:cls.cam.index("var CAPTURE_FPS", start)]

    def test_real_sources_consent_and_visible_state(self):
        for token in (
            "sensorAuthorized=false", "ev&&ev.isTrusted",
            "sensorConsent.addEventListener('change'",
            "sensorAsk.addEventListener('click'",
            "window.DeviceMotionEvent.requestPermission()",
            "window.DeviceOrientationEvent.requestPermission()",
            "sensor-acc", "sensor-gyro", "sensor-rv", "sensorMeta",
            "source:source", "now-r.mono>1500", "尚无真实更新",
        ):
            self.assertIn(token, self.cam, token)
        self.assertIn("if(!sensorAuthorized)", self.sensor)
        self.assertIn("sensorSources[k]!==source", self.sensor)
        self.assertIn("Generic Sensor", self.sensor)
        self.assertIn("DeviceMotion", self.sensor)
        self.assertIn("DeviceOrientation", self.sensor)
        self.assertNotIn("function fake(", self.sensor)
        self.assertNotIn("Math.random", self.sensor)

    def test_protocol_lifecycle_and_listener_ownership(self):
        self.assertIn("p={on:[],v:{},t:Date.now(),seq:++sensorSeq,mono:now}", self.sensor)
        self.assertIn("setInterval(sensorSend,40)", self.sensor)
        self.assertIn("/ws/sensor?name=", self.sensor)
        self.assertIn("stopPump(); stopJpegEarly(); sensorStop('已停止同步')", self.cam)
        self.assertIn("showTrackSettings(stream);document.getElementById('quality')", self.cam)
        self.assertIn("sensorStart();", self.cam)
        self.assertIn("window.addEventListener('pagehide'", self.cam)
        for event, handler in (("devicemotion", "sensorOnMotion"),
                               ("deviceorientation", "sensorOnOrientation")):
            self.assertEqual(self.cam.count(
                "addEventListener('%s',%s,true)" % (event, handler)), 1)
            self.assertEqual(self.cam.count(
                "removeEventListener('%s',%s,true)" % (event, handler)), 1)
        # 屏幕方向 HTTP 上报保持独立，不另开 DeviceOrientation 监听。
        self.assertIn("function orientReport()", self.cam)
        self.assertIn("fetch('/api/cam/orient'", self.cam)
        self.assertIn("window.addEventListener('orientationchange'", self.cam)

    def test_msensor_still_works_and_cleans_up(self):
        self.assertIn("return {on:on, v:v, t:Date.now(), seq:++seq, mono:monoNow()};", self.msensor)
        self.assertIn("function stopMotion()", self.msensor)
        self.assertIn("if(motionOn)return;", self.msensor)
        self.assertIn("stopMotion();stopSensors();", self.msensor)
        self.assertIn("setInterval(function(){ send(false); },40)", self.msensor)
        self.assertIn("自测模式（合成数据）", self.msensor)
        for event, handler in (("devicemotion", "onMotion"),
                               ("deviceorientation", "onOrientation")):
            self.assertEqual(self.msensor.count(
                "addEventListener('%s',%s,true)" % (event, handler)), 1)
            self.assertEqual(self.msensor.count(
                "removeEventListener('%s',%s,true)" % (event, handler)), 1)

    def test_video_metrics_are_measured_not_claimed(self):
        self.assertIn("t.getSettings?t.getSettings():{}", self.cam)
        self.assertIn("r.framesEncoded-lastFrames", self.cam)
        self.assertIn("qualityLimitationReason", self.cam)
        self.assertIn("framesEncoded/时间差", self.cam)


if __name__ == "__main__":
    unittest.main(verbosity=2)
