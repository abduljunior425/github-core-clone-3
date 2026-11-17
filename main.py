"""github-core-clone-3 entrypoint.

Two modes:

- Trial mode (MAKEFUN_ENV_URL is set): bind an ephemeral port on 0.0.0.0,
  POST {"port": N} to $MAKEFUN_ENV_URL/register, then poll GET
  $MAKEFUN_ENV_URL/done until {"done": true} and exit 0. The trial pod's
  /bot is read-only, so the database lives under FORGE_DATA_DIR or a fresh
  temp dir.
- Standalone mode: serve on FORGE_PORT (default 18080) until interrupted.
  Never bind 8080 — the suite owns it.
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

from forge.server import make_server
from forge.store import ForgeStore


def _data_dir() -> str:
    configured = os.environ.get("FORGE_DATA_DIR")
    if configured:
        return configured
    return tempfile.mkdtemp(prefix="forge3-")


def _http_json(method: str, url: str, payload=None, timeout: float = 30.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _trial(env_url: str, server, port: int) -> int:
    base = env_url.rstrip("/")
    # Register this entry: the first register wins; a 409 means another
    # entry already registered, which still counts as registered.
    deadline = time.time() + 60
    registered = False
    delay = 0.25
    while time.time() < deadline and not registered:
        try:
            _http_json("POST", base + "/register", {"port": port}, timeout=5)
            registered = True
        except urllib.error.HTTPError as err:
            if err.code in (200, 409):
                registered = True
            elif err.code in (400, 405):
                print("register rejected with HTTP %d" % err.code,
                      file=sys.stderr)
                return 1
        except Exception:
            pass
        if not registered:
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
    if not registered:
        print("could not register with the trial environment", file=sys.stderr)
        return 1
    # Poll until the environment is done, then shut down cleanly.
    while True:
        try:
            state = _http_json("GET", base + "/done", timeout=30)
        except Exception:
            state = {}
        if isinstance(state, dict) and state.get("done"):
            break
        time.sleep(0.5)
    server.shutdown()
    return 0


def main() -> int:
    store = ForgeStore(_data_dir())
    env_url = os.environ.get("MAKEFUN_ENV_URL")
    if env_url:
        server = make_server(store, "0.0.0.0", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return _trial(env_url, server, port)
        finally:
            server.server_close()
            store.close()
    port = int(os.environ.get("FORGE_PORT", "18080"))
    server = make_server(store, "127.0.0.1", port)
    print("forge listening on 127.0.0.1:%d (data dir %s)"
          % (port, store.data_dir), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
