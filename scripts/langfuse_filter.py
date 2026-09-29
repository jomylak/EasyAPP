"""Reverse proxy between goose and Langfuse that drops the re-sent chat history.

Goose attaches the whole conversation so far (`gen_ai.input.messages`) to every
LLM call, so an N-turn run stores it N times (quadratic; ~98% of Langfuse's
disk). Every assistant/tool message in it is already on its own span, so we
keep only the first call's copy (user prompt only) and drop the rest.
Everything else, other paths included, is forwarded untouched.
Point LANGFUSE_HOST at http://127.0.0.1:3100 (langfuse-filter.service).
"""
import gzip
import json
import re
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "http://127.0.0.1:3000"
KEY = "gen_ai.input.messages"
# any non-user message means this isn't the first call of the conversation
HISTORY = re.compile(r'"role"\s*:\s*"(assistant|tool)"')


def strip_history(node):
    """Delete KEY from every dict under node whose value carries history."""
    if isinstance(node, dict):
        v = node.get(KEY)
        if v is not None and HISTORY.search(v if isinstance(v, str) else json.dumps(v)):
            del node[KEY]
        for child in node.values():
            strip_history(child)
    elif isinstance(node, list):
        for child in node:
            strip_history(child)


def filter_body(body, encoding):
    """Return (body, encoding) with history stripped; unparseable bodies pass through."""
    try:
        raw = gzip.decompress(body) if encoding == "gzip" else body
        obj = json.loads(raw)
    except (OSError, ValueError):
        return body, encoding
    strip_history(obj)
    return json.dumps(obj, separators=(",", ":")).encode(), None


class Proxy(BaseHTTPRequestHandler):
    def _forward(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
        if body and self.path.startswith("/api/public/ingestion"):
            body, enc = filter_body(body, self.headers.get("Content-Encoding"))
            headers.pop("Content-Encoding", None)
            if enc:
                headers["Content-Encoding"] = enc
        req = urllib.request.Request(UPSTREAM + self.path, body or None, headers, method=self.command)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            resp = e
        except OSError:
            self.send_error(502)
            return
        data = resp.read()
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _forward

    def log_message(self, *a):  # journald already timestamps; skip per-request noise
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 3100), Proxy).serve_forever()
