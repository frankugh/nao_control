# -*- coding: utf-8 -*-
import unittest
import posixpath
from mock import patch, MagicMock
import nao_utils as nu
from nao_utils import NaoUtils, parse_color, _rgb_tuple_to_int, group_behaviors


class TestNaoUtilsUpload(unittest.TestCase):
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
            player.playFile.assert_called_once_with("/home/nao/ugh_audio/test.wav")


class TestColorParsing(unittest.TestCase):
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


class TestSetEyeColor(unittest.TestCase):
    @patch("nao_utils.ALProxy")
    def test_set_eye_color(self, mock_proxy):
        inst = MagicMock()
        mock_proxy.return_value = inst
        rgb = nu.set_eye_color("1.2.3.4", 9559, "#112233", 0.2)
        self.assertEqual(rgb, 0x112233)
        inst.fadeRGB.assert_called_with("FaceLeds", 0x112233, 0.2)


if __name__ == "__main__":
    unittest.main()
