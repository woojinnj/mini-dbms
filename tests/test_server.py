from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from minidb import Database
from minidb.server import MAX_BODY, UI_JSON, PoolHTTPServer


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(self.temp.name, durable=False)
        self.server = PoolHTTPServer(("127.0.0.1", 0), self.db, workers=4, queue_size=32, request_timeout=1)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.db.close()
        self.temp.cleanup()

    def request(self, path="/query", payload=None, method="POST", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            body = json.dumps(payload).encode() if payload is not None else b""
            connection.request(method, path, body, headers or {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw(self, request):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5) as connection:
            connection.sendall(request)
            chunks = []
            while data := connection.recv(65536):
                chunks.append(data)
        return b"".join(chunks)

    def test_server_binds_without_reverse_dns(self):
        with patch("socket.gethostbyaddr", side_effect=AssertionError("unexpected reverse DNS lookup")):
            with PoolHTTPServer(("127.0.0.1", 0), self.db) as server:
                self.assertGreater(server.server_port, 0)

    def test_health_query_metrics_and_errors(self):
        self.assertEqual(self.request("/health", method="GET")[0], 200)
        status, body = self.request(payload={"sql": "INSERT INTO users VALUES ('한글', 25, NULL)"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["first_id"], 1)
        status, body = self.request(payload={"sql": "SELECT * FROM users WHERE id = 1"})
        self.assertEqual(body["result"]["rows"][0]["name"], "한글")
        self.assertEqual(body["result"]["plan"], "BPLUS_POINT_LOOKUP")
        status, metrics = self.request("/metrics", method="GET")
        self.assertEqual(metrics["queries"], 2)
        self.assertEqual(self.request("/missing", method="GET")[0], 404)
        for payload in ({"sql": "INVALID"}, {"sql": "SELECT * FROM missing"}, {"sql": 1}, [], {},
                        {"sql": "SELECT * FROM users; SELECT * FROM users"}, {"sql": "\ud800"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request(payload=payload)[0], 400)
        self.assertEqual(self.request(payload={"sql": "SELECT * FROM users"}, headers={"Content-Type": "text/plain"})[0], 415)

    def test_browser_query_can_create_and_use_table(self):
        status, body = self.request(
            payload={"sql": "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"},
            headers={"Content-Type": "application/json", "Accept": UI_JSON},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["command"], "CREATE TABLE")
        self.assertEqual(body["result"]["table"], "products")
        _, health = self.request("/health", method="GET")
        self.assertIn("products", health["tables"])

        status, _ = self.request(payload={"sql": "INSERT INTO products (name) VALUES ('keyboard');"})
        self.assertEqual(status, 200)
        status, body = self.request(payload={"sql": "SELECT * FROM products;"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["rows"], [{"id": 1, "name": "keyboard"}])

    def test_http_body_validation(self):
        cases = [
            (b"POST /query HTTP/1.1\r\nHost: localhost\r\n\r\n", 411),
            (b"POST /query HTTP/1.1\r\nHost: localhost\r\nContent-Length: -1\r\n\r\n", 411),
            (b"POST /query HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx", 411),
            (f"POST /query HTTP/1.1\r\nHost: localhost\r\nContent-Length: {MAX_BODY + 1}\r\n\r\n".encode(), 413),
            (b"POST /query HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n", 400),
            (b"POST /query HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 1\r\n\r\nx", 400),
            (b"POST /query HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\nx", 408),
        ]
        for request, status in cases:
            with self.subTest(status=status, request=request[:100]):
                self.assertIn(f" {status} ".encode(), self.raw(request).split(b"\r\n")[0])

    def test_optional_authentication(self):
        self.server.token = "test-secret"
        self.assertEqual(self.request("/health", method="GET")[0], 401)
        self.assertEqual(self.request("/health", method="GET", headers={"Authorization": "Bearer test-secret"})[0], 200)

    def test_browser_assets_load_before_api_authentication(self):
        self.server.token = "test-secret"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            for path, mime, marker in (
                ("/", "text/html", b'<textarea id="sql"'),
                ("/ui.css", "text/css", b".workspace"),
                ("/ui.js", "text/javascript", b'fetch(path'),
            ):
                with self.subTest(path=path):
                    connection.request("GET", path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get_content_type(), mime)
                    self.assertIn(marker, response.read())
        finally:
            connection.close()
        self.assertEqual(self.request(payload={"sql": "SELECT * FROM users"})[0], 401)

    def test_browser_response_preserves_large_integer_values(self):
        self.db.execute("INSERT INTO users VALUES ('large', 9223372036854775807, NULL)")
        payload = {"sql": "SELECT age FROM users"}
        status, body = self.request(payload=payload, headers={"Content-Type": "application/json", "Accept": UI_JSON})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["rows"], [{"age": "9223372036854775807"}])
        status, body = self.request(payload=payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["rows"], [{"age": 9223372036854775807}])

    def test_concurrent_http_inserts_and_reads(self):
        def work(i):
            status, body = self.request(payload={"sql": f"INSERT INTO users VALUES ('u{i}', {i}, NULL)"})
            self.assertEqual(status, 200, body)
            assigned = body["result"]["first_id"]
            status, body = self.request(payload={"sql": f"SELECT * FROM users WHERE id = {assigned}"})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["result"]["rows"][0]["age"], i)
            return assigned

        with ThreadPoolExecutor(max_workers=12) as pool:
            ids = list(pool.map(work, range(100)))
        self.assertEqual(sorted(ids), list(range(1, 101)))
        self.assertEqual(self.db.tables["users"].index.size, 100)
        metrics = self.server.snapshot()
        self.assertGreater(metrics["peak_active"], 1)
        self.assertLessEqual(metrics["peak_active"], 4)

    def test_overload_returns_503_without_unbounded_threads(self):
        # Occupy all four workers and all 32 queue slots using real sockets.
        self.server.request_timeout = 5
        connections = []
        baseline = self.server.snapshot()["accepted"]
        try:
            for _ in range(36):
                connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
                connection.sendall(b"GET /health HTTP/1.1\r\n")
                connections.append(connection)
            deadline = time.monotonic() + 3
            while self.server.snapshot()["accepted"] < baseline + 36 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(self.server.snapshot()["accepted"], baseline + 36)
            status, body = self.request("/health", method="GET")
            self.assertEqual(status, 503)
            self.assertEqual(body["error"]["code"], "OVERLOADED")
            self.assertLessEqual(self.server.snapshot()["peak_active"], 4)
        finally:
            for connection in connections:
                connection.close()
