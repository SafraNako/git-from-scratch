"""Real git servers for the tests: ``git daemon`` for git:// and ``git http-backend`` behind a tiny CGI gateway for smart HTTP."""
import http.server
import socket
import subprocess
import threading
import time

import gitoracle as go


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class GitDaemon:
    def __init__(self, root):
        self.port = free_port()
        self.proc = subprocess.Popen([go.GIT, "daemon", "--reuseaddr", "--base-path=" + root, "--export-all", "--enable=receive-pack", "--listen=127.0.0.1",
                                      "--port=%d" % self.port, root], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=go.env())
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("git daemon did not start")

    def url(self, name):
        return "git://127.0.0.1:%d/%s" % (self.port, name)

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


class HttpBackend:
    def __init__(self, root):
        backend_root = root

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _run(self):
                path, _, query = self.path.partition("?")
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                env = go.env()
                env.update({"GIT_PROJECT_ROOT": backend_root, "GIT_HTTP_EXPORT_ALL": "1", "REQUEST_METHOD": self.command, "PATH_INFO": path, "QUERY_STRING": query,
                            "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)), "REMOTE_USER": "tester", "REMOTE_ADDR": "127.0.0.1"})
                if self.headers.get("Git-Protocol"):
                    env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
                p = subprocess.run([go.GIT, "http-backend"], input=body, capture_output=True, env=env)
                head, _, payload = p.stdout.partition(b"\r\n\r\n")
                if not _:
                    head, _, payload = p.stdout.partition(b"\n\n")
                status, headers = 200, []
                for line in head.decode("latin-1").splitlines():
                    name, _, value = line.partition(":")
                    if name.lower() == "status":
                        status = int(value.split()[0])
                    elif name:
                        headers.append((name.strip(), value.strip()))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _run
            do_POST = _run

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, name):
        return "http://127.0.0.1:%d/%s" % (self.port, name)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
