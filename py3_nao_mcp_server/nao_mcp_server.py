import os
import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import time

last_call_time = 0.0   # vervangt last_text en last_time
DEBOUNCE_SECONDS = 15.0


NAO_API_BASE_URL = os.environ.get("NAO_API_BASE_URL", "http://localhost:5001")



# MCP server configuratie
mcp = FastMCP(
    name="nao_tts_server",
    stateless_http=True,        # HTTP i.p.v. stdio
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False  # nodig voor ngrok
    ),
)

# Tool: alleen TTS
@mcp.tool()
def nao_tts(text: str) -> str:
    global last_call_time
    now = time.time()

    # Tijd-based debounce, tekst maakt niet uit
    if now - last_call_time < DEBOUNCE_SECONDS:
        print(f"[nao_tts] Ignored due to debounce ({DEBOUNCE_SECONDS}s)", flush=True)
        return f"(genegeerd: debounce {DEBOUNCE_SECONDS}s actief)"

    last_call_time = now

    print(f"[nao_tts] incoming text={text!r}", flush=True)
    try:
        r = requests.post(f"{NAO_API_BASE_URL}/nao/tts",
                          json={"text": text},
                          timeout=10)
        print(f"[nao_tts] status={r.status_code}, body={r.text}", flush=True)
        r.raise_for_status()
        return f"Alex zegt (als het goed is): {text}"
    except Exception as e:
        print(f"[nao_tts] ERROR: {e}", flush=True)
        return f"Fout bij NAO API: {e}"




if __name__ == "__main__":
    # poort instellen vóór run()
    mcp.http_port = 8000            # correcte manier
    mcp.http_host = "0.0.0.0"       # zodat ngrok hem kan bereiken

    # Start MCP server (zonder 'port=' argument)
    mcp.run(transport="streamable-http")
