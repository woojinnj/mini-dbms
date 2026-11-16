"""HTTP/1.1 API with bounded admission and a fixed worker pool."""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
from importlib.resources import files
import json
import logging
import socket
from socketserver import TCPServer
import threading
import time

from .errors import DBError, StorageError
from .sql import MAX_SQL_BYTES

MAX_BODY = 3 * 1024 * 1024
LOG = logging.getLogger("minidb.http")
UI_JSON = "application/vnd.minidb.ui+json"
UI_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/ui.css": ("ui.css", "text/css; charset=utf-8"),
    "/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
}


def browser_values(value):
    # Keep 64-bit INTEGER values exact when the browser decodes JSON.
    if type(value) is int and abs(value) >= 2**53:
        return str(value)
    if isinstance(value, dict):
        return {key: browser_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [browser_values(item) for item in value]
    return value


class PoolHTTPServer(HTTPServer):
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address, database, workers=8, queue_size=32, request_timeout=10, token=None):
        if workers < 1 or queue_size < 0 or request_timeout <= 0:
            raise ValueError("workers/timeout must be positive and queue_size nonnegative")
        self.database = database
        self.workers, self.queue_size = workers, queue_size
        self.request_timeout, self.token = request_timeout, token
        self.slots = threading.BoundedSemaphore(workers + queue_size)
        self.metrics_lock = threading.Lock()
        self.metrics = {"accepted": 0, "rejected": 0, "active": 0, "peak_active": 0,
                        "completed": 0, "queries": 0, "errors": 0}
        self.started = time.monotonic()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="minidb-worker")
        try:
            super().__init__(address, QueryHandler)
        except Exception:
            self.pool.shutdown(wait=True)
            raise

    def server_bind(self):
        # The bound address is sufficient for MiniDB. HTTPServer.server_bind
        # also does a synchronous reverse DNS lookup before it starts listening.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def increment(self, key):
        with self.metrics_lock:
            self.metrics[key] += 1

    def snapshot(self):
        with self.metrics_lock:
            return {**self.metrics, "workers": self.workers, "queue_capacity": self.queue_size,
                    "uptime_seconds": round(time.monotonic() - self.started, 3)}

    def process_request(self, request, client_address):
        request.settimeout(self.request_timeout)
        if not self.slots.acquire(blocking=False):
            self.increment("rejected")
            try:
                request.settimeout(0.1)
                body = b'{"error":{"code":"OVERLOADED","message":"Server is busy; retry later"}}'
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Connection: close\r\nRetry-After: 1\r\nContent-Length: "
                                + str(len(body)).encode() + b"\r\n\r\n" + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        self.increment("accepted")
        try:
            self.pool.submit(self._serve, request, client_address)
        except RuntimeError:
            self.slots.release()
            self.shutdown_request(request)

    def _serve(self, request, client_address):
        with self.metrics_lock:
            self.metrics["active"] += 1
            self.metrics["peak_active"] = max(self.metrics["peak_active"], self.metrics["active"])
        try:
            self.finish_request(request, client_address)
        except (OSError, TimeoutError):
            pass
        except Exception:
            LOG.exception("Unexpected request failure")
        finally:
            self.shutdown_request(request)
            with self.metrics_lock:
                self.metrics["active"] -= 1
                self.metrics["completed"] += 1
            self.slots.release()

    def server_close(self):
        super().server_close()
        self.pool.shutdown(wait=True)


class QueryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MiniDB/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        LOG.info("%s %s", self.client_address[0], format % args)

    def reply(self, status, payload, content_type="application/json"):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_body(status, body, f"{content_type}; charset=utf-8")

    def send_body(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True  # One request per job; idle keep-alives cannot hold workers.
        self.wfile.write(body)

    def error(self, status, code, message):
        self.server.increment("errors")
        self.reply(status, {"error": {"code": code, "message": message}})

    def send_error(self, code, message=None, explain=None):
        self.error(code, "HTTP_ERROR", message or self.responses.get(code, ("Error",))[0])

    def authorized(self):
        token = self.server.token
        provided = self.headers.get("Authorization", "")
        if token and not hmac.compare_digest(provided.encode("utf-8"), f"Bearer {token}".encode("utf-8")):
            self.error(401, "UNAUTHORIZED", "A valid bearer token is required")
            return False
        return True

    def do_GET(self):
        if self.path in UI_FILES:
            # The UI shell is public so it can ask for the API token when needed.
            name, content_type = UI_FILES[self.path]
            body = files(__package__).joinpath("web", name).read_bytes()
            self.send_body(200, body, content_type, {
                "Cache-Control": "no-cache",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                                           "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            })
            return
        if not self.authorized():
            return
        if self.path == "/health":
            self.reply(200, {"status": "ok", "tables": list(self.server.database.tables)})
        elif self.path == "/metrics":
            self.reply(200, self.server.snapshot())
        else:
            self.error(404, "NOT_FOUND", "Use POST /query, GET /health or GET /metrics")

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/query":
            self.error(404, "NOT_FOUND", "Use POST /query")
            return
        if self.headers.get("Transfer-Encoding"):
            self.error(400, "INVALID_REQUEST", "Transfer-Encoding is not supported")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self.error(411, "LENGTH_REQUIRED", "Exactly one valid Content-Length is required")
            return
        # Compare length before converting to avoid Python's huge-integer conversion limit.
        normalized_length = lengths[0].lstrip("0") or "0"
        if len(normalized_length) > 8 or int(normalized_length) > MAX_BODY:
            self.error(413, "BODY_TOO_LARGE", "Request body exceeds 3 MiB")
            return
        length = int(normalized_length)
        if self.headers.get_content_type() != "application/json":
            self.error(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json")
            return
        try:
            body = self.rfile.read(length)
        except (socket.timeout, TimeoutError):
            self.error(408, "REQUEST_TIMEOUT", "Timed out reading request body")
            return
        if len(body) != length:
            self.error(400, "INVALID_REQUEST", "Incomplete request body")
            return
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            self.error(400, "INVALID_JSON", "Body must be valid UTF-8 JSON")
            return
        if not isinstance(payload, dict) or set(payload) != {"sql"} or not isinstance(payload["sql"], str):
            self.error(400, "INVALID_REQUEST", 'Expected exactly {"sql": "..."}')
            return
        try:
            if len(payload["sql"].encode("utf-8")) > MAX_SQL_BYTES:
                self.error(413, "SQL_TOO_LARGE", "SQL exceeds 2 MiB")
                return
            self.server.increment("queries")
            result = self.server.database.execute(payload["sql"])
        except UnicodeError:
            self.error(400, "INVALID_REQUEST", "SQL contains invalid Unicode")
        except StorageError:
            LOG.exception("Storage failure")
            self.error(500, "STORAGE_ERROR", "Storage operation failed; inspect server logs")
        except DBError as error:
            self.error(400, type(error).__name__.upper(), str(error))
        except Exception:
            LOG.exception("Query failed")
            self.error(500, "INTERNAL_ERROR", "Unexpected server error")
        else:
            if self.headers.get("Accept") == UI_JSON:
                self.reply(200, {"result": browser_values(result)}, UI_JSON)
            else:
                self.reply(200, {"result": result})
