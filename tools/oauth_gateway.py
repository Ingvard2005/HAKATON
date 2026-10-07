"""Temporary tunnel gateway: exposes only the Bitrix OAuth callback, never Streamlit."""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # URLs may contain one-time codes.

    def do_GET(self):
        if urlsplit(self.path).path != "/oauth/bitrix24/callback" or len(self.path) > 8192:
            self.send_error(404)
            return
        connection = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=60)
        try:
            connection.request("GET", self.path)
            response = connection.getresponse()
            body = response.read(65536)
            self.send_response(response.status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
        except (OSError, http.client.HTTPException):
            self.send_error(502, "CallMind callback unavailable")
        finally:
            connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--upstream-port", type=int, default=8770)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Gateway)
    server.upstream_port = args.upstream_port
    server.serve_forever()
