# -*- coding: utf-8 -*-
import unittest
import json

from mock import patch, MagicMock
import nao_api

# Py2/3 compatibele unicode-alias
try:
    unicode
except NameError:
    unicode = str


class TestTtsRoute(unittest.TestCase):

    @patch("nao_api.get_proxy")
    def test_tts_say_unicode_text(self, mock_get_proxy):
        # Mock ALTextToSpeech
        tts = MagicMock()
        mock_get_proxy.return_value = tts

        app = nao_api.app
        client = app.test_client()

        payload = {"text": u"hé NAO"}
        resp = client.post(
            "/tts",
            data=json.dumps(payload),
            content_type="application/json"
        )

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertIn("data", data)
        self.assertEqual(data["data"]["text"], u"hé NAO")

        # Controleer wat er naar NAOqi gaat
        tts.say.assert_called_once()
        arg = tts.say.call_args[0][0]
        self.assertTrue(isinstance(arg, unicode))
        self.assertEqual(arg, u"hé NAO")

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
        self.assertTrue(isinstance(arg, unicode))
        self.assertEqual(arg, u"")


class TestDoBehaviorRoute(unittest.TestCase):

    @patch("nao_api.is_awake", return_value=True)
    @patch("nao_api.get_proxy")
    def test_do_behavior_unicode_name(self, mock_get_proxy, mock_is_awake):
        behavior = MagicMock()
        behavior.isBehaviorInstalled.return_value = True
        mock_get_proxy.return_value = behavior

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

        behavior.isBehaviorInstalled.assert_called_once()
        behavior.runBehavior.assert_called_once()

        arg = behavior.runBehavior.call_args[0][0]
        self.assertTrue(isinstance(arg, unicode))
        self.assertEqual(arg, behavior_name)

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


if __name__ == "__main__":
    unittest.main()
