"""
Roblox Studio <-> Blender Live-Link: HTTP Server
Runs in a background thread and queues incoming scene updates for main-thread processing.
"""

import json
import logging
import os
import queue
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

logger = logging.getLogger("RobloxLiveLinkServer")

# In-memory queue for passing data from the server thread to Blender's main thread
sync_queue = queue.Queue(maxsize=500)

_server_instance = None
_server_thread = None
_server_port = 28888
_is_running = False
# Unique per server start. Studio compares it and re-sends a full sync when Blender's server restarts.
_server_session = ""

ADDON_DIR = os.path.dirname(os.path.realpath(__file__))
PLUGIN_SOURCE_CANDIDATES = (
    # Release package: the Studio plugin ships inside the addon folder
    os.path.join(ADDON_DIR, "roblox_plugin", "RobloxLiveLink.lua"),
    # Source checkout: blender_addon/ next to roblox_plugin/
    os.path.join(ADDON_DIR, "..", "roblox_plugin", "RobloxLiveLink.server.luau"),
)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind the same port silently
    # (e.g. two Blender instances) and Studio requests then go to a random one.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


class LiveLinkRequestHandler(BaseHTTPRequestHandler):
    # A client that sends a short body or never finishes its request must not hold a thread forever
    timeout = 30

    def log_message(self, format, *args):
        # Suppress default stdout logging for high-frequency requests
        pass

    def _send_response_json(self, status_code, payload):
        response_bytes = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        # No CORS headers on purpose: Roblox Studio doesn't need them, and without them
        # web pages open in a browser can't read from / drive the Blender scene.
        self.end_headers()
        self.wfile.write(response_bytes)

    def _is_browser_request(self):
        # Browsers send an Origin header on cross-site POSTs; Roblox Studio never does
        return self.headers.get("Origin") is not None

    def _pop_outgoing(self):
        try:
            from . import sync_manager
            return sync_manager.pop_outgoing_changes()
        except Exception:
            return []

    def do_GET(self):
        try:
            if self.path == "/status":
                self._send_response_json(200, {
                    "status": "running",
                    "port": _server_port,
                    "session": _server_session,
                    "queue_size": sync_queue.qsize()
                })
            elif self.path == "/poll_blender_changes":
                self._send_response_json(200, {
                    "status": "ok",
                    "session": _server_session,
                    "blender_changes": self._pop_outgoing()
                })
            elif self.path == "/plugin_source":
                plugin_path = next((p for p in PLUGIN_SOURCE_CANDIDATES if os.path.exists(p)), "")
                if plugin_path:
                    with open(plugin_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    self._send_response_json(200, {"source": content})
                else:
                    self._send_response_json(404, {"error": "Plugin file not found"})
            else:
                self._send_response_json(404, {"error": "Not found"})
        except Exception as e:
            print(f"[RobloxLiveLinkServer] Error in do_GET: {e}")
            self._send_response_json(500, {"error": str(e)})

    def do_POST(self):
        if self._is_browser_request():
            self._send_response_json(403, {"error": "Browser requests are not allowed"})
            return

        if self.path == "/sync":
            try:
                content_length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                content_length = -1
            if content_length <= 0:
                self._send_response_json(400, {"error": "Empty body"})
                return

            try:
                body = self.rfile.read(content_length)
                data = json.loads(body.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Payload must be a JSON object")

                event_type = data.get("event")
                if event_type in ("full_sync", "clear_all"):
                    # Clear stale pending items so full_sync or clear executes immediately
                    while not sync_queue.empty():
                        try:
                            sync_queue.get_nowait()
                        except queue.Empty:
                            break
                # Never silently drop updates/removals: if Blender is busy, this request thread
                # waits (Studio waits for the reply) instead of losing data.
                sync_queue.put(data, timeout=20.0)

                self._send_response_json(200, {
                    "status": "queued",
                    "session": _server_session,
                    "blender_changes": self._pop_outgoing()
                })
            except queue.Full:
                self._send_response_json(503, {"error": "Blender is busy, retry later"})
            except Exception as e:
                self._send_response_json(400, {"error": str(e)})

        elif self.path == "/clear":
            # Command to clear all live-linked objects in Blender
            sync_queue.put({"event": "clear_all"})
            self._send_response_json(200, {"status": "clear_queued"})
        else:
            self._send_response_json(404, {"error": "Not found"})


def start_server(port=28888):
    global _server_instance, _server_thread, _server_port, _is_running, _server_session

    if _is_running:
        return True, f"Server already running on port {_server_port}"

    _server_port = port
    try:
        _server_instance = ThreadedHTTPServer(("127.0.0.1", _server_port), LiveLinkRequestHandler)
    except Exception as e:
        return False, f"Failed to bind port {_server_port} (another Blender already running Live Link?): {e}"

    _is_running = True
    _server_session = uuid.uuid4().hex
    _server_thread = threading.Thread(target=_server_instance.serve_forever, daemon=True)
    _server_thread.start()
    return True, f"Server started on http://127.0.0.1:{_server_port}"


def stop_server():
    global _server_instance, _server_thread, _is_running

    if not _is_running or _server_instance is None:
        _is_running = False
        return True, "Server not running"

    try:
        _server_instance.shutdown()
        _server_instance.server_close()
    except Exception as e:
        logger.error(f"Error shutting down server: {e}")
    finally:
        _is_running = False
        _server_instance = None
        _server_thread = None

    # Clear pending queue items
    while not sync_queue.empty():
        try:
            sync_queue.get_nowait()
        except queue.Empty:
            break

    return True, "Server stopped"


def new_session():
    """Changes the session id so Studio re-sends the full scene (e.g. after another .blend was opened)."""
    global _server_session
    if _is_running:
        _server_session = uuid.uuid4().hex


def is_running():
    return _is_running


def get_port():
    return _server_port
