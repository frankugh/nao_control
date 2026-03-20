# -*- coding: utf-8 -*-
import unittest
import posixpath
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

import nao_utils as nu
import nao_api
import nao_proxy_manager
from nao_utils import NaoUtils, parse_color, _rgb_tuple_to_int, group_behaviors, _to_bytes


class TestNaoUtilsUpload(unittest.TestCase):
    def setUp(self):
        nao_proxy_manager.clear_proxy_cache()

    @patch("nao_utils.paramiko.SSHClient")
    def test_upload_localpath(self, mock_ssh):
        inst = MagicMock()
        mock_ssh.return_value = inst
        sftp = MagicMock()
        inst.open_sftp.return_value = sftp

        utils = NaoUtils("1.2.3.4", ssh_user="nao", ssh_pass="x", ssh_port=22, remote_audio_dir="/home/nao/ugh_audio")
        # minimal test: we willen alleen zien dat sftp.put wordt aangeroepen met een pad in de goede dir
        remote = utils.upload_localpath("/tmp/local.wav", remote_filename="test.wav", remote_dir="/home/nao/custom")
        self.assertTrue(remote.startswith("/home/nao/custom"))
        sftp.put.assert_called_once()

    @patch("nao_utils.paramiko.SSHClient")
    def test_upload_and_play(self, mock_ssh):
        inst = MagicMock()
        mock_ssh.return_value = inst
        sftp = MagicMock()
        inst.open_sftp.return_value = sftp

        utils = NaoUtils("1.2.3.4", ssh_user="nao", ssh_pass="x", ssh_port=22, remote_audio_dir="/home/nao/ugh_audio")

        class DummyFile(object):
            def __init__(self, data):
                self._data = data
                self._pos = 0

            def read(self, n=-1):
                if self._pos >= len(self._data):
                    return b""
                if n < 0:
                    chunk = self._data[self._pos:]
                    self._pos = len(self._data)
                    return chunk
                chunk = self._data[self._pos:self._pos+n]
                self._pos += len(chunk)
                return chunk

        dummy = DummyFile(b"12345678" * 3)
        with patch.object(utils, "upload_localpath", return_value="/home/nao/ugh_audio/test.wav") as mock_up, \
                patch.object(utils, "get_proxy") as mock_proxy:
            player = MagicMock()
            mock_proxy.return_value = player
            remote = utils.upload_and_play(dummy, "test.wav", remote_dir="/home/nao/ugh_audio")
            self.assertIsInstance(remote, dict)
            self.assertIn("remote_path", remote)
            self.assertEqual(remote["remote_path"], "/home/nao/ugh_audio/test.wav")
            mock_up.assert_called_once()
            player.playFile.assert_called_once()
            called_path = player.playFile.call_args[0][0]
            if isinstance(called_path, bytes):
                called_path = called_path.decode("utf-8")
            self.assertEqual(called_path, "/home/nao/ugh_audio/test.wav")


class TestColorParsing(unittest.TestCase):
    def setUp(self):
        nao_proxy_manager.clear_proxy_cache()

    def test_rgb_tuple_to_int(self):
        self.assertEqual(_rgb_tuple_to_int((255, 0, 0)), 0xFF0000)
        self.assertEqual(_rgb_tuple_to_int((0, 255, 0)), 0x00FF00)
        self.assertEqual(_rgb_tuple_to_int((0, 0, 255)), 0x0000FF)

    def test_parse_color_hex(self):
        self.assertEqual(parse_color("#FF0000"), 0xFF0000)
        self.assertEqual(parse_color("00FF00"), 0x00FF00)

    def test_parse_color_rgb_func(self):
        self.assertEqual(parse_color("rgb(255,0,0)"), 0xFF0000)
        self.assertEqual(parse_color("rgb(0,255,0)"), 0x00FF00)

    def test_parse_color_tuple_list(self):
        self.assertEqual(parse_color((255, 0, 0)), 0xFF0000)
        self.assertEqual(parse_color([0, 255, 0]), 0x00FF00)

    def test_parse_color_int(self):
        self.assertEqual(parse_color(0xFF0000), 0xFF0000)
        self.assertEqual(parse_color(0x00FF00), 0x00FF00)

    def test_parse_color_invalid(self):
        with self.assertRaises(ValueError):
            parse_color("not-a-color")


class TestGroupBehaviors(unittest.TestCase):
    def setUp(self):
        nao_proxy_manager.clear_proxy_cache()

    def test_group_behaviors(self):
        behaviors = [
            "dances/happy",
            "dances/sad",
            "stories/intro",
            "stories/outro",
            "just_one",
        ]
        grouped = group_behaviors(behaviors)
        self.assertEqual(sorted(grouped.keys()), ["", "dances", "stories"])
        self.assertEqual(grouped["dances"], ["happy", "sad"])
        self.assertEqual(grouped["stories"], ["intro", "outro"])
        self.assertEqual(grouped[""], ["just_one"])


class TestToBytes(unittest.TestCase):
    def setUp(self):
        nao_proxy_manager.clear_proxy_cache()

    def test_to_bytes_from_unicode(self):
        try:
            unicode
        except NameError:
            unicode = str

        value = u"hé_audio.wav"
        b = _to_bytes(value)

        self.assertEqual(b, value.encode("utf-8"))
        if sys.version_info[0] < 3:
            self.assertIsInstance(b, str)
        else:
            self.assertIsInstance(b, bytes)

    def test_to_bytes_passthrough_bytes(self):
        b_in = "plain-bytes.wav"
        b_out = _to_bytes(b_in)
        self.assertEqual(b_out, b"plain-bytes.wav")
        if sys.version_info[0] < 3:
            self.assertIs(b_out, b_in)


class TestSetEyeColor(unittest.TestCase):
    def setUp(self):
        nao_proxy_manager.clear_proxy_cache()
        self._prev_ip = nao_api.app.config.get("NAO_IP")
        self._prev_port = nao_api.app.config.get("NAO_PORT")
        nao_api.app.config["NAO_IP"] = "1.2.3.4"
        nao_api.app.config["NAO_PORT"] = 9559

    def tearDown(self):
        nao_proxy_manager.clear_proxy_cache()
        nao_api.app.config["NAO_IP"] = self._prev_ip
        nao_api.app.config["NAO_PORT"] = self._prev_port

    @patch("nao_proxy_manager.ALProxy")
    def test_set_eye_color(self, mock_proxy):
        inst = MagicMock()
        mock_proxy.return_value = inst
        rgb = nu.set_eye_color("1.2.3.4", 9559, "#112233", 0.2)
        self.assertEqual(rgb, 0x112233)
        inst.fadeRGB.assert_called_with("FaceLeds", 0x112233, 0.2)

    @patch("nao_proxy_manager.ALProxy")
    def test_nao_utils_and_nao_api_share_proxy_cache(self, mock_proxy):
        player = MagicMock()
        mock_proxy.return_value = player
        utils = NaoUtils("1.2.3.4", nao_port=9559)

        utils.get_proxy("ALAudioPlayer").playFile("/tmp/a.wav")
        nao_api.get_proxy("ALAudioPlayer").playFile("/tmp/b.wav")

        self.assertEqual(mock_proxy.call_count, 1)
        self.assertEqual(player.playFile.call_count, 2)


if __name__ == "__main__":
    unittest.main()
