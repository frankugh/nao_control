# -*- coding: utf-8 -*-
import os
import re
import posixpath
from naoqi import ALProxy
import paramiko

# --- Py2/3 shims (houdt editors stil en werkt op Py2) ---
try:
    basestring
except NameError:
    basestring = str
try:
    unicode
except NameError:
    unicode = str
try:
    long
except NameError:
    long = int

try:
    import paramiko
except ImportError:
    paramiko = None

DEFAULT_REMOTE_AUDIO_DIR = "/home/nao/ugh_audio"


def _to_bytes(s):
    return s.encode("utf-8") if isinstance(s, unicode) else s


class NaoUtils(object):
    """
    Legacy Py2 util-klasse voor NAO.
    LET OP:
      - Deze laag blijft zoveel mogelijk ongewijzigd.
      - Nieuwe functionaliteit (Piper / Py3-transport) moet NIET hier worden toegevoegd.
      - Upload/audio-methodes hieronder zijn gemarkeerd als 'deprecated' voor toekomstige ontwikkeling.
    """

    def __init__(self, nao_ip, nao_port=9559, ssh_user="nao", ssh_pass=None,
                 ssh_port=22, remote_audio_dir=DEFAULT_REMOTE_AUDIO_DIR):
        self.nao_ip = nao_ip
        self.nao_port = nao_port
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass
        self.ssh_port = ssh_port
        self.remote_audio_dir = remote_audio_dir or DEFAULT_REMOTE_AUDIO_DIR

    # --- NAOqi proxy ---
    def get_proxy(self, name):
        return ALProxy(name, self.nao_ip, self.nao_port)

    # --- SSH/SFTP helpers ---
    def _connect_ssh(self):
        if paramiko is None:
            raise RuntimeError("paramiko is niet geïnstalleerd")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.nao_ip, port=self.ssh_port, username=self.ssh_user,
                       password=self.ssh_pass, allow_agent=True, look_for_keys=True, timeout=5)
        return client

    def _ensure_audio_dir(self, sftp, remote_dir):
        """
        Maak ALLEEN subdirs aan onder /home/nao. Doel: /home/nao/ugh_audio (default).
        Bestaand, werkend gedrag – niet aanpassen.
        """
        base = "/home/nao"
        target = (remote_dir or self.remote_audio_dir or DEFAULT_REMOTE_AUDIO_DIR)
        target = target.replace("\\", "/").strip().rstrip("/")
        if not target.startswith(base):
            target = DEFAULT_REMOTE_AUDIO_DIR

        # start in /home/nao en bouw het pad stap voor stap
        sftp.chdir(base)
        rel = target[len(base):].strip("/")
        cur = base
        for part in [p for p in rel.split("/") if p]:
            cur = posixpath.join(cur, part)
            try:
                sftp.chdir(cur)   # bestaat al
            except IOError:
                sftp.mkdir(cur)   # maak submap
                sftp.chdir(cur)
        return cur  # absoluut pad

    @staticmethod
    def sanitize_filename(name):
        base = os.path.basename(name or "audio.bin")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)
        return safe or "audio.bin"

    # --- Bestandsupload: lokale temp -> sftp.put (stabiel) ---

    # DEPRECATED VOOR NIEUWE ONTWIKKELING:
    # Deze Py2-upload zal in de toekomst vervangen worden door een Py3-NAO-transportlaag.
    # Bestaande code mag dit blijven gebruiken totdat de migratie compleet is.
    def upload_localpath(self, local_path, remote_filename=None, remote_dir=None):
        client = self._connect_ssh()
        sftp = client.open_sftp()
        try:
            target_dir = self._ensure_audio_dir(sftp, remote_dir)
            fname = self.sanitize_filename(remote_filename or os.path.basename(local_path))
            remote_path = posixpath.join(target_dir, fname)
            sftp.put(local_path, remote_path)
            return remote_path
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            client.close()

    # DEPRECATED VOOR NIEUWE ONTWIKKELING:
    # Convenience-wrapper rond upload_localpath. Niet uitbreiden; alleen legacy gebruiken.
    def upload_via_temp(self, file_like, original_filename, remote_filename=None, remote_dir=None):
        """
        Sla upload eerst lokaal op (temp), upload dan met sftp.put, ruim temp op.
        `file_like` mag Flask FileStorage zijn of elk file-achtig object met .read().
        """
        import tempfile
        import shutil
        suffix = os.path.splitext(original_filename or "")[1] or ".bin"
        fd, tmp_path = tempfile.mkstemp(prefix="nao_", suffix=suffix)
        os.close(fd)
        try:
            # schrijf upload naar temp
            src = getattr(file_like, "stream", file_like)
            try:
                src.seek(0)
            except Exception:
                pass
            with open(tmp_path, "wb") as out:
                shutil.copyfileobj(src, out)
            # upload via sftp.put (bewezen stabiel)
            return self.upload_localpath(tmp_path, remote_filename=remote_filename or original_filename,
                                         remote_dir=remote_dir or self.remote_audio_dir)
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    # --- Afspelen ---

    def play_remote_file(self, remote_path):
        """
        Speelt een bestaand remote audiobestand af via ALAudioPlayer.
        """
        audio = self.get_proxy("ALAudioPlayer")
        # Py2 NAOqi verwacht bytes, geen unicode
        return audio.playFile(_to_bytes(remote_path))

    # DEPRECATED VOOR NIEUWE ONTWIKKELING:
    # Upload + play in één stap; in de toekomst verplaatst naar Py3-service (Piper/transport).
    def upload_and_play(self, file_like, original_filename, remote_filename=None, remote_dir=None):
        remote_path = self.upload_via_temp(file_like, original_filename, remote_filename, remote_dir)
        # playFile met bytes; JSON response met string
        self.play_remote_file(remote_path)
        return {"remote_path": remote_path}

    # DEPRECATED VOOR NIEUWE ONTWIKKELING:
    # Streamingfunctionaliteit blijft voorlopig hier voor legacy-tests,
    # maar wordt in toekomstige architectuur door Py3 overgenomen.
    def stream_and_play(self, audio_bytes, sample_rate=22050):
        # guard tegen unicode in Py2
        try:
            unicode_type = unicode
        except NameError:
            unicode_type = str
        if isinstance(audio_bytes, unicode_type):
            raise TypeError("audio_bytes moet bytes zijn (geen unicode).")

        client = self._connect_ssh()
        try:
            cmd = "aplay -q -t raw -r {} -f S16_LE -c 1 -".format(int(sample_rate))
            stdin, stdout, stderr = client.exec_command(cmd)
            try:
                stdin.write(audio_bytes)
            finally:
                try:
                    stdin.close()
                except Exception:
                    pass
            try:
                stdout.channel.recv_exit_status()
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass
        return {"played": True, "sample_rate": int(sample_rate)}


# === Kleurhelpers ===
def _rgb_tuple_to_int(rgb):
    r, g, b = rgb
    r = max(0, min(int(r), 255))
    g = max(0, min(int(g), 255))
    b = max(0, min(int(b), 255))
    return (r << 16) | (g << 8) | b


def parse_color(value):
    """
    '#RRGGBB', 'RRGGBB', 'rgb(R,G,B)', [R,G,B], (R,G,B), of int 0xRRGGBB.
    Retourneert 24-bit int 0xRRGGBB.
    """
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return _rgb_tuple_to_int(value)
    if isinstance(value, (int, long)):
        return int(value) & 0xFFFFFF
    if isinstance(value, basestring):
        s = value.strip()
        if s.startswith("#"):
            s = s[1:]
        m = re.match(r"^([0-9A-Fa-f]{6})$", s)
        if m:
            return int(m.group(1), 16)
        m2 = re.match(r"^rgb\s*\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)$", s)
        if m2:
            return _rgb_tuple_to_int((int(m2.group(1)), int(m2.group(2)), int(m2.group(3))))
    raise ValueError("Unsupported color format")


def set_eye_color(nao_ip, nao_port, color, duration):
    leds = ALProxy("ALLeds", nao_ip, nao_port)
    rgb = parse_color(color)
    leds.fadeRGB("FaceLeds", int(rgb), float(duration))
    return rgb


def group_behaviors(behaviors):
    import os as _os
    grouped = {}
    for b in behaviors:
        folder = _os.path.dirname(b)
        name = _os.path.basename(b)
        if folder not in grouped:
            grouped[folder] = []
        grouped[folder].append(name)
        grouped[folder].sort()
    return grouped
