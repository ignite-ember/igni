"""CLI authentication — browser-based login with local callback.

Flow:
1. Start a local HTTP server on a random port
2. Open browser to portal login with callback URL
3. User authenticates in the portal
4. Portal redirects to localhost callback with token
5. Local server receives token, returns it to caller
"""

import asyncio
import logging
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.ignite-ember.sh"
DEFAULT_PORTAL_URL = "https://ignite-ember.sh"
_TIMEOUT = 15.0


_SUCCESS_PAGE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>CLI Authenticated</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  :root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--border:#d1d9e0;--surface:#f6f8fa}
  @media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#7d8590;--border:#30363d;--surface:#161b22}}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--fg);overflow:hidden}
  canvas{position:fixed;top:0;left:0;z-index:0}
  .content{position:relative;z-index:1;min-height:100vh;display:flex;align-items:center;justify-content:center}
  .card{text-align:center;padding:48px 24px}
  .icon{width:64px;height:64px;display:flex;align-items:center;justify-content:center;border-radius:50%;background:linear-gradient(135deg,rgba(249,115,22,.12),rgba(220,38,38,.08));margin:0 auto 24px}
  h2{font-size:24px;font-weight:700;margin-bottom:8px}
  p{font-size:15px;color:var(--muted);margin-bottom:32px}
  .hint{display:inline-flex;align-items:center;gap:8px;padding:12px 24px;border:1px solid var(--border);border-radius:12px;background:var(--surface);font-family:ui-monospace,monospace;font-size:14px;color:var(--muted)}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div class="content">
  <div class="card">
    <div class="icon">
      <svg width="32" height="32" viewBox="0 0 16 16" fill="#F97316"><path d="M8 16A8 8 0 1 1 8 0a8 8 0 0 1 0 16Zm3.78-9.72a.751.751 0 0 0-.018-1.042.751.751 0 0 0-1.042-.018L6.75 9.19 5.28 7.72a.751.751 0 0 0-1.042.018.751.751 0 0 0-.018 1.042l2 2a.75.75 0 0 0 1.06 0Z"/></svg>
    </div>
    <h2>CLI Authenticated</h2>
    <p>You're all set. Return to your terminal to start using igni.</p>
    <div class="hint">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M0 2.75C0 1.784.784 1 1.75 1h12.5c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0 1 14.25 15H1.75A1.75 1.75 0 0 1 0 13.25Zm1.75-.25a.25.25 0 0 0-.25.25v10.5c0 .138.112.25.25.25h12.5a.25.25 0 0 0 .25-.25V2.75a.25.25 0 0 0-.25-.25ZM7.25 8a.749.749 0 0 1-.22.53l-2.25 2.25a.749.749 0 0 1-1.275-.326.749.749 0 0 1 .215-.734L5.44 8 3.72 6.28a.749.749 0 0 1 .326-1.275.749.749 0 0 1 .734.215l2.25 2.25c.141.14.22.331.22.53Zm1.5 1.5h3a.75.75 0 0 1 0 1.5h-3a.75.75 0 0 1 0-1.5Z"/></svg>
      You can close this window
    </div>
  </div>
</div>
<script>
const c=document.getElementById('c'),x=c.getContext('2d');c.width=innerWidth;c.height=innerHeight;
const cols=['#F97316','#DC2626','#FBBF24','#f85149','#f0883e','#FF6B35'],sp=[];
function burst(bx,by){for(let i=0;i<80;i++){const a=Math.random()*Math.PI*2,s=2+Math.random()*5;sp.push({x:bx,y:by,vx:Math.cos(a)*s,vy:Math.sin(a)*s,life:80+Math.random()*40,age:0,r:1.5+Math.random()*2,color:cols[Math.floor(Math.random()*6)]})}}
const L=[];for(let i=0;i<6;i++)L.push({x:c.width*.15+Math.random()*c.width*.7,y:c.height,tY:c.height*.1+Math.random()*c.height*.3,vy:-12-Math.random()*4,d:i*25,done:false});
let f=0;(function loop(){const m=(getComputedStyle(document.body).backgroundColor||'').match(/\\d+/g),t=m?`rgba(${m[0]},${m[1]},${m[2]},0.25)`:'rgba(255,255,255,0.25)';x.fillStyle=t;x.fillRect(0,0,c.width,c.height);for(const l of L){if(f<l.d||l.done)continue;l.y+=l.vy;l.vy+=.15;x.fillStyle='#FBBF24';x.globalAlpha=1;x.beginPath();x.arc(l.x,l.y,3,0,Math.PI*2);x.fill();if(l.y<=l.tY||l.vy>=0){burst(l.x,l.y);l.done=true}}for(const s of sp){if(s.age>s.life)continue;s.age++;s.x+=s.vx;s.y+=s.vy;s.vy+=.1;s.vx*=.98;const t=s.age/s.life;x.globalAlpha=1-t;x.fillStyle=t<.15?'#FBBF24':s.color;x.beginPath();x.arc(s.x,s.y,s.r*(1-t*.5),0,Math.PI*2);x.fill()}x.globalAlpha=1;f++;requestAnimationFrame(loop)})();
</script>
</body>
</html>"""


def _find_free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the token from the callback redirect."""

    token: str | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        token = params.get("token", [None])[0]

        if token:
            _CallbackHandler.token = token
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_SUCCESS_PAGE.encode())
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Missing token</h2></body></html>")

    def log_message(self, *args: object) -> None:
        """Silence HTTP request logs during OAuth callback.

        Overrides BaseHTTPRequestHandler.log_message to prevent
        incoming HTTP request logs from cluttering the console output
        while waiting for the OAuth callback redirection from the portal.
        """
        pass


def get_login_url(port: int, portal_url: str = DEFAULT_PORTAL_URL) -> str:
    """Build the portal CLI authentication URL.

    Args:
        port: Local port for the callback server.
        portal_url: Base URL of the portal. Defaults to production portal.

    Returns:
        The full authentication URL, e.g., "https://ignite-ember.sh/cli-auth?port=53842"
    """
    return f"{portal_url.rstrip('/')}/cli-auth?port={port}"


def start_callback_server() -> tuple[HTTPServer, str]:
    """Start a local HTTP server for receiving the auth callback.

    Returns (server, callback_url).
    The caller is responsible for running the server and closing it.
    """
    port = _find_free_port()
    callback_url = f"http://localhost:{port}/callback"

    _CallbackHandler.token = None
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 1.0

    def _serve():
        while _CallbackHandler.token is None:
            try:
                server.handle_request()
            except (ValueError, OSError):
                break  # server closed

    thread = Thread(target=_serve, daemon=True)
    thread.start()

    return server, callback_url


async def wait_for_token(server: HTTPServer, timeout: float = 300.0) -> str:
    """Wait for the callback handler to receive a token.

    Raises TimeoutError if no callback within timeout.
    """
    elapsed = 0.0
    while _CallbackHandler.token is None and elapsed < timeout:
        await asyncio.sleep(0.5)
        elapsed += 0.5

    server.server_close()

    if _CallbackHandler.token is None:
        raise TimeoutError("Login timed out — no callback received")

    return _CallbackHandler.token


async def wait_for_callback(timeout: float = 300.0) -> tuple[str, str]:
    """Start a local HTTP server and wait for the portal to redirect with a token.

    Returns (token, callback_url).
    Raises TimeoutError if no callback within timeout.
    """
    server, callback_url = start_callback_server()
    token = await wait_for_token(server, timeout)
    return token, callback_url


async def validate_token(token: str, api_url: str = DEFAULT_API_URL) -> dict | None:
    """Validate a CLI token by calling the server.

    Returns user info dict on success, None on failure.
    """
    import httpx

    url = f"{api_url.rstrip('/')}/v1/portal/me"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.debug("Token validation failed: %s", e)
    return None
