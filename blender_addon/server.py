"""
Roblox Studio <-> Blender Live-Link: HTTP Server

Single-threaded: the add-on's timer calls poll() on Blender's main thread, which handles the requests
that are already waiting (non-blocking) and appends incoming scene updates to `sync_queue`.
"""

import collections
import json
import select
import socket
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

# Incoming scene updates, consumed by sync_manager on the main thread
sync_queue = collections.deque()
# More pending updates than this means Blender can't keep up: Studio is told to retry later
MAX_PENDING = 500
# Requests handled per poll() call, so a burst of requests can't stall the UI
MAX_REQUESTS_PER_POLL = 16

_server_instance = None
_server_port = 28888
_is_running = False
# Unique per server start. Studio compares it and re-sends a full sync when Blender's server restarts.
_server_session = ""


class LiveLinkHTTPServer(HTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind the same port silently
    # (e.g. two Blender instances) and Studio requests then go to a random one.
    allow_reuse_address = False
    # handle_request() returns at once when no connection is waiting
    timeout = 0

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


class LiveLinkRequestHandler(BaseHTTPRequestHandler):
    # Requests are read on Blender's main thread: a client that stalls mid-request is dropped quickly
    timeout = 5

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
                    "queue_size": len(sync_queue),
                })
            elif self.path == "/poll_blender_changes":
                self._send_response_json(200, {
                    "status": "ok",
                    "session": _server_session,
                    "blender_changes": self._pop_outgoing(),
                })
            else:
                self._send_response_json(404, {"error": "Not found"})
        except Exception as e:
            print(f"[Roblox Live Link] Error handling GET {self.path}: {e}")
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
            except Exception as e:
                self._send_response_json(400, {"error": str(e)})
                return

            if data.get("event") in ("full_sync", "clear_all"):
                # Stale pending updates are superseded by a full sync / clear
                sync_queue.clear()
            elif len(sync_queue) >= MAX_PENDING:
                # Never drop updates silently: Studio keeps them and sends them again
                self._send_response_json(503, {"error": "Blender is busy, retry later"})
                return
            sync_queue.append(data)
            self._send_response_json(200, {
                "status": "queued",
                "session": _server_session,
                "blender_changes": self._pop_outgoing(),
            })

        elif self.path == "/clear":
            # Command to clear all live-linked objects in Blender
            sync_queue.clear()
            sync_queue.append({"event": "clear_all"})
            self._send_response_json(200, {"status": "clear_queued"})
        else:
            self._send_response_json(404, {"error": "Not found"})


def poll():
    """Handles waiting HTTP requests without blocking. Called from the add-on's timer on the main thread."""
    if not _is_running or _server_instance is None:
        return
    sock = _server_instance.socket
    for _ in range(MAX_REQUESTS_PER_POLL):
        try:
            ready, _, _ = select.select([sock], [], [], 0)
        except (OSError, ValueError):
            return
        if not ready:
            return
        _server_instance.handle_request()


def start_server(port=28888):
    global _server_instance, _server_port, _is_running, _server_session

    if _is_running:
        return True, f"Server already running on port {_server_port}"

    _server_port = port
    try:
        _server_instance = LiveLinkHTTPServer(("127.0.0.1", _server_port), LiveLinkRequestHandler)
    except Exception as e:
        return False, f"Failed to bind port {_server_port} (another Blender already running Live Link?): {e}"

    _is_running = True
    _server_session = uuid.uuid4().hex
    return True, f"Server started on http://127.0.0.1:{_server_port}"


def stop_server():
    global _server_instance, _is_running

    if not _is_running or _server_instance is None:
        _is_running = False
        return True, "Server not running"

    try:
        _server_instance.server_close()
    except Exception as e:
        print(f"[Roblox Live Link] Error closing server: {e}")
    finally:
        _is_running = False
        _server_instance = None

    sync_queue.clear()
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
