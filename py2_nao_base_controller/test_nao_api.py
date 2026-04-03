# -*- coding: utf-8 -*-
import unittest
import json
import sys

try:
    from unittest.mock import patch, MagicMock
except ImportError:  # pragma: no cover
    from mock import patch, MagicMock

try:
    import builtins
except ImportError:  # pragma: no cover
    import __builtin__ as builtins

try:
    import configparser as _configparser
except ImportError:  # pragma: no cover
    import ConfigParser as _configparser


if not hasattr(builtins, "unicode"):
    builtins.unicode = str
if not hasattr(builtins, "basestring"):
    builtins.basestring = str
if not hasattr(builtins, "long"):
    builtins.long = int
if "ConfigParser" not in sys.modules:
    sys.modules["ConfigParser"] = _configparser
if "naoqi" not in sys.modules:
    class _NaoqiStub(object):
        ALProxy = object
    sys.modules["naoqi"] = _NaoqiStub()
if "paramiko" not in sys.modules:
    class _ParamikoStub(object):
        SSHClient = object

        @staticmethod
        def AutoAddPolicy():
            return object()
    sys.modules["paramiko"] = _ParamikoStub()

import nao_api
import nao_proxy_manager

# Py2/3 compatibele unicode-alias
try:
    unicode
except NameError:
    unicode = str


class TestTtsRoute(unittest.TestCase):

    def setUp(self):
        nao_proxy_manager.clear_proxy_cache()
        nao_api.app.config["NAO_IP"] = "192.168.68.102"
        nao_api.app.config["NAO_PORT"] = 9559

    @patch("nao_api.get_proxy")
    def test_tts_say_unicode_text(self, mock_get_proxy):
        # Mock ALTextToSpeech
        tts = MagicMock()
        mock_get_proxy.return_value = tts

        app = nao_api.app
        client = app.test_client()

        payload = {
            "text": u"Hoi NAO, ik zei: \u201czo\u2019n test\u201d met caf\u00e9, na\u00efef en fa\u00e7ade."
        }
        resp = client.post(
            "/tts",
            data=json.dumps(payload),
            content_type="application/json"
        )

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertIn("data", data)
        self.assertEqual(data["data"]["text"], payload["text"])

        # Controleer wat er naar NAOqi gaat
        tts.say.assert_called_once()
        arg = tts.say.call_args[0][0]
        if not isinstance(arg, unicode):
            arg = arg.decode("ascii")
        expected = u"Hoi NAO, ik zei: \"zo'n test\" met cafe, naief en facade."
        self.assertEqual(arg, expected)

    @patch("nao_api.get_proxy")
    def test_tts_say_missing_text_defaults_empty(self, mock_get_proxy):
        tts = MagicMock()
        mock_get_proxy.return_value = tts

        app = nao_api.app
        client = app.test_client()

        # Geen "text" veld
        resp = client.post(
            "/tts",
            data=json.dumps({}),
            content_type="application/json"
        )

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["data"]["text"], u"")

        tts.say.assert_called_once()
        arg = tts.say.call_args[0][0]
        if not isinstance(arg, unicode):
            arg = arg.decode("ascii")
        self.assertEqual(arg, u"")


class TestDoBehaviorRoute(unittest.TestCase):

    def setUp(self):
        nao_proxy_manager.clear_proxy_cache()
        nao_api.app.config["NAO_IP"] = "192.168.68.102"
        nao_api.app.config["NAO_PORT"] = 9559

    @patch("nao_api.is_awake", return_value=True)
    @patch("nao_api.call_proxy_write")
    @patch("nao_api.call_proxy_read")
    def test_do_behavior_unicode_name_encoded_for_naoqi(self, mock_call_proxy_read, mock_call_proxy_write, mock_is_awake):
        mock_call_proxy_read.return_value = True

        app = nao_api.app
        client = app.test_client()

        behavior_name = u"animations/Stand/Gestures/You_1"
        payload = {"behavior": behavior_name}

        resp = client.post(
            "/do_behavior",
            data=json.dumps(payload),
            content_type="application/json"
        )

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "ok")

        mock_call_proxy_read.assert_called_once()
        mock_call_proxy_write.assert_called_once()

        arg_installed = mock_call_proxy_read.call_args[0][2]
        arg_run = mock_call_proxy_write.call_args[0][2]
        self.assertIsInstance(arg_installed, (str, bytes))
        self.assertIsInstance(arg_run, (str, bytes))
        self.assertEqual(arg_installed.decode("utf-8"), behavior_name)
        self.assertEqual(arg_run.decode("utf-8"), behavior_name)

        # Response should still be unicode in JSON.
        self.assertEqual(data["data"]["behavior"], behavior_name)

    def test_do_behavior_missing_name_gives_error(self):
        app = nao_api.app
        client = app.test_client()

        resp = client.post(
            "/do_behavior",
            data=json.dumps({}),
            content_type="application/json"
        )

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "error")
        self.assertIn("Missing 'behavior'", data.get("error", ""))

    @patch("nao_api.get_read_proxy")
    @patch("nao_api.call_proxy_write")
    @patch("nao_api.call_proxy_read")
    def test_stop_behavior_unicode_name_encoded_for_naoqi(self, mock_call_proxy_read, mock_call_proxy_write, mock_get_read_proxy):
        mock_call_proxy_read.return_value = True
        behavior_read = MagicMock()
        behavior_read.isBehaviorRunning.return_value = True
        mock_get_read_proxy.return_value = behavior_read

        app = nao_api.app
        client = app.test_client()

        behavior_name = u"animations/Stand/Gestures/You_1"
        resp = client.post(
            "/stop_behavior",
            data=json.dumps({"behavior": behavior_name}),
            content_type="application/json"
        )

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "ok")

        arg_installed = mock_call_proxy_read.call_args[0][2]
        arg_running = behavior_read.isBehaviorRunning.call_args[0][0]
        arg_stop = mock_call_proxy_write.call_args[0][2]
        self.assertIsInstance(arg_installed, (str, bytes))
        self.assertIsInstance(arg_running, (str, bytes))
        self.assertIsInstance(arg_stop, (str, bytes))
        self.assertEqual(arg_installed.decode("utf-8"), behavior_name)
        self.assertEqual(arg_running.decode("utf-8"), behavior_name)
        self.assertEqual(arg_stop.decode("utf-8"), behavior_name)
        self.assertEqual(data["data"]["behavior"], behavior_name)


class TestProxyReconnectHardening(unittest.TestCase):

    def setUp(self):
        nao_proxy_manager.clear_proxy_cache()
        self.app = nao_api.app
        self._prev_ip = self.app.config.get("NAO_IP")
        self._prev_port = self.app.config.get("NAO_PORT")
        self.app.config["NAO_IP"] = "192.168.68.102"
        self.app.config["NAO_PORT"] = 9559

    def tearDown(self):
        nao_proxy_manager.clear_proxy_cache()
        self.app.config["NAO_IP"] = self._prev_ip
        self.app.config["NAO_PORT"] = self._prev_port

    @patch("nao_proxy_manager.ALProxy")
    def test_repeated_reads_reuse_cached_proxy(self, mock_alproxy):
        motion = MagicMock()
        motion.robotIsWakeUp.return_value = True
        mock_alproxy.return_value = motion

        self.assertTrue(nao_api.is_awake())
        self.assertTrue(nao_api.is_awake())

        self.assertEqual(mock_alproxy.call_count, 1)
        self.assertEqual(motion.robotIsWakeUp.call_count, 2)

    @patch("nao_proxy_manager.ALProxy")
    def test_read_reconnects_once_after_connection_error(self, mock_alproxy):
        stale_motion = MagicMock()
        stale_motion.robotIsWakeUp.side_effect = RuntimeError("ALBroker::createBroker Cannot connect to tcp://192.168.68.102:9559")
        fresh_motion = MagicMock()
        fresh_motion.robotIsWakeUp.return_value = True
        mock_alproxy.side_effect = [stale_motion, fresh_motion]

        self.assertTrue(nao_api.is_awake())

        self.assertEqual(mock_alproxy.call_count, 2)
        stale_motion.robotIsWakeUp.assert_called_once_with()
        fresh_motion.robotIsWakeUp.assert_called_once_with()

    @patch("nao_proxy_manager.ALProxy")
    def test_read_failure_stays_error_after_single_retry(self, mock_alproxy):
        bad_motion_1 = MagicMock()
        bad_motion_1.robotIsWakeUp.side_effect = RuntimeError("Cannot connect to tcp://192.168.68.102:9559")
        bad_motion_2 = MagicMock()
        bad_motion_2.robotIsWakeUp.side_effect = RuntimeError("Connection refused")
        mock_alproxy.side_effect = [bad_motion_1, bad_motion_2]

        client = self.app.test_client()
        resp = client.get("/is_awake")

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "error")
        self.assertEqual(mock_alproxy.call_count, 2)

    @patch("nao_proxy_manager.ALProxy")
    def test_custom_life_state_recovers_from_module_destroyed_reads(self, mock_alproxy):
        stale_awareness = MagicMock()
        stale_awareness.isAwarenessRunning.side_effect = RuntimeError(
            "ALBasicAwareness::isAwarenessRunning module destroyed"
        )
        fresh_awareness = MagicMock()
        fresh_awareness.isAwarenessRunning.return_value = False

        stale_bg = MagicMock()
        stale_bg.isEnabled.side_effect = RuntimeError(
            "ALBackgroundMovement::isEnabled module destroyed"
        )
        fresh_bg = MagicMock()
        fresh_bg.isEnabled.return_value = False

        motion = MagicMock()
        motion.getBreathEnabled.return_value = True
        motion.robotIsWakeUp.return_value = True

        proxy_calls = {}

        def _make_proxy(module_name, ip, port):
            if isinstance(module_name, bytes):
                module_name = module_name.decode("utf-8")
            elif isinstance(module_name, str) and module_name.startswith("b'") and module_name.endswith("'"):
                module_name = module_name[2:-1]
            proxy_calls[module_name] = proxy_calls.get(module_name, 0) + 1
            if module_name == "ALBasicAwareness":
                if proxy_calls[module_name] == 1:
                    return stale_awareness
                return fresh_awareness
            if module_name == "ALBackgroundMovement":
                if proxy_calls[module_name] == 1:
                    return stale_bg
                return fresh_bg
            if module_name == "ALMotion":
                return motion
            raise AssertionError("Unexpected module_name %r" % (module_name,))

        mock_alproxy.side_effect = _make_proxy

        client = self.app.test_client()
        resp = client.get("/custom_life_state")

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertGreaterEqual(proxy_calls.get("ALBasicAwareness", 0), 2)
        self.assertGreaterEqual(proxy_calls.get("ALBackgroundMovement", 0), 2)
        self.assertGreaterEqual(proxy_calls.get("ALMotion", 0), 1)
        self.assertTrue(data["data"]["is_awake"])
        self.assertEqual(
            data["data"]["modules"],
            {
                "basic_awareness": False,
                "background_movement": False,
                "breathing": True,
            },
        )
        stale_awareness.isAwarenessRunning.assert_called_once_with()
        fresh_awareness.isAwarenessRunning.assert_called_once_with()
        stale_bg.isEnabled.assert_called_once_with()
        fresh_bg.isEnabled.assert_called_once_with()
        motion.getBreathEnabled.assert_called_once_with("Body")
        motion.robotIsWakeUp.assert_called_once_with()

    @patch("nao_proxy_manager.ALProxy")
    def test_custom_life_apply_disables_stock_life_and_updates_real_modules(self, mock_alproxy):
        awareness = MagicMock()
        awareness.isAwarenessRunning.return_value = True

        background = MagicMock()
        background.isEnabled.return_value = True

        motion = MagicMock()
        motion.getBreathEnabled.return_value = False

        proxies = {
            "ALBasicAwareness": awareness,
            "ALBackgroundMovement": background,
            "ALMotion": motion,
        }

        def _make_proxy(module_name, ip, port):
            if isinstance(module_name, bytes):
                module_name = module_name.decode("utf-8")
            elif isinstance(module_name, str) and module_name.startswith("b'") and module_name.endswith("'"):
                module_name = module_name[2:-1]
            proxy = proxies.get(module_name)
            if proxy is None:
                raise AssertionError("Unexpected module_name %r" % (module_name,))
            return proxy

        mock_alproxy.side_effect = _make_proxy

        client = self.app.test_client()
        resp = client.post(
            "/custom_life_apply",
            data=json.dumps(
                {
                    "settings": {
                        "basic_awareness": False,
                        "background_movement": False,
                        "breathing": True,
                    }
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(
            data["data"]["prev_state"]["modules"],
            {
                "basic_awareness": True,
                "background_movement": True,
                "breathing": False,
            },
        )
        awareness.stopAwareness.assert_called_once_with()
        background.setEnabled.assert_called_once_with(False)
        motion.setBreathEnabled.assert_called_once_with("Body", True)

    @patch("nao_api.is_awake", return_value=False)
    @patch("nao_proxy_manager.ALProxy")
    def test_write_does_not_retry_on_connection_error(self, mock_alproxy, mock_is_awake):
        motion = MagicMock()
        motion.wakeUp.side_effect = RuntimeError("Connection failed")
        mock_alproxy.return_value = motion

        client = self.app.test_client()
        resp = client.post("/wake_up", data=json.dumps({}), content_type="application/json")

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "error")
        self.assertEqual(mock_alproxy.call_count, 1)
        motion.wakeUp.assert_called_once_with()

    @patch("nao_proxy_manager.ALProxy")
    def test_endpoint_change_clears_cached_proxy(self, mock_alproxy):
        first_motion = MagicMock()
        first_motion.robotIsWakeUp.return_value = True
        second_motion = MagicMock()
        second_motion.robotIsWakeUp.return_value = True
        mock_alproxy.side_effect = [first_motion, second_motion]

        self.assertTrue(nao_api.is_awake())
        self.app.config["NAO_IP"] = "192.168.68.103"
        self.assertTrue(nao_api.is_awake())

        self.assertEqual(mock_alproxy.call_count, 2)


if __name__ == "__main__":
    unittest.main()
