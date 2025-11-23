# nao_actions.py
import requests

class NaoActions(object):
    """
    Dunne Py3-wrapper om de Py2-NAO-API aan te roepen.
    Nu alleen say_native(), later uit te breiden met wake_up, rest, behaviors, etc.
    """
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def say_native(self, text, timeout=5):
        """
        Roept de Py2 /tts-endpoint aan met JSON {"text": ...}.
        Verwacht dezelfde JSON-structuur terug als nao_api.py.
        """
        payload = {"text": text}
        resp = requests.post(f"{self.base_url}/tts", json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
