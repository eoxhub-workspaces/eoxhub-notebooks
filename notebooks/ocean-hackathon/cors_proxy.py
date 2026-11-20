#!/usr/bin/env python3
"""Local CORS & Zarr file proxy for Jupyter / EOXHub (standard library only).

Serves two purposes behind `http://localhost:<port>` locally or
`<HUB_ORIGIN><JUPYTERHUB_SERVICE_PREFIX>proxy/<port>` on EOXHub / JupyterHub:

1. **Upstream S3 CORS proxy** (default routes, e.g. `/EarthCODE/OSCAssets/...`):
   The CloudFerro bucket only sends CORS headers for GET, so browsers block the
   HEAD requests made by OpenLayers' GeoZarr source. This proxy forwards GET/HEAD
   (including `Range`) to the bucket and adds permissive CORS headers.

2. **Local cluster Zarr store server** (`/local/<path>`):
   Jupyter Server's `/files/` endpoint hides dot-files (`.zmetadata`, `.zgroup`,
   `.zarray`, `.zattrs`) by default (`ContentsManager.allow_hidden = False`),
   breaking Zarr v2 stores. Serving local stores under `/local/<path>` through
   this proxy exposes dot-files as well as HTTP `HEAD` and `Range` (`206 Partial
   Content`) requests needed by `zarrita` / `ol/source/GeoZarr`.

Usage:
    python cors_proxy.py                              # 127.0.0.1:8010, local root = .
    python cors_proxy.py --port 9000 --local-root .
"""

import argparse
import http.client
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = "s3.waw4-1.cloudferro.com"
LOCAL_PREFIX = "/local/"
CHUNK_COPY_SIZE = 64 * 1024

# Request headers passed on to the bucket / response headers passed back.
FORWARD_REQUEST = ("Range", "If-None-Match", "If-Modified-Since", "If-Range")
FORWARD_RESPONSE = (
    "Content-Type",
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Last-Modified",
    "Cache-Control",
)

JSON_FILENAMES = {".zmetadata", ".zgroup", ".zarray", ".zattrs", "zarr.json"}

_local = threading.local()  # one keep-alive upstream connection per thread


def _upstream(method, path, headers):
    """Send the request upstream, reconnecting once if the connection went stale."""
    for attempt in (1, 2):
        conn = getattr(_local, "conn", None)
        if conn is None:
            conn = _local.conn = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=60)
        try:
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            return response, response.read()
        except (http.client.HTTPException, OSError):
            conn.close()
            _local.conn = None
            if attempt == 2:
                raise


def _parse_byte_range(range_header, file_size):
    """Parse an HTTP Range header (`bytes=start-end`, `bytes=start-`, `bytes=-N`).

    Returns:
      - `None` if no valid Range syntax was supplied (caller should serve 200 OK),
      - `"unsatisfiable"` if the range lies outside `file_size` (caller sends 416),
      - `(start, end)` inclusive byte offsets for a 206 Partial Content response.
    """
    if not range_header or not range_header.startswith("bytes="):
        return None
    # Multi-range requests are not used by zarrita; fall back to full 200 if comma-separated.
    spec = range_header[len("bytes=") :].strip()
    if "," in spec or "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    start_s, end_s = start_s.strip(), end_s.strip()
    try:
        if start_s == "":
            suffix = int(end_s)
            if suffix <= 0 or file_size == 0:
                return "unsatisfiable"
            start = max(0, file_size - suffix)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
    except ValueError:
        return None

    if file_size == 0 or start < 0 or start >= file_size or start > end:
        return "unsatisfiable"
    return start, min(end, file_size - 1)


def _guess_content_type(file_path):
    name = os.path.basename(file_path)
    if name in JSON_FILENAMES or name.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


class CorsProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive towards the browser / jupyter-server-proxy

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            self.headers.get("Access-Control-Request-Headers", "*"),
        )
        self.send_header(
            "Access-Control-Expose-Headers",
            "Content-Length, Content-Range, Accept-Ranges, ETag, Last-Modified",
        )
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_bytes(self, status, body, content_type="text/plain; charset=utf-8", extra_headers=()):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        for key, value in extra_headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def do_OPTIONS(self):  # CORS preflight: answer locally
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_healthz(self):
        payload = json.dumps(
            {
                "status": "ok",
                "local_root": self.server.local_root,
                "upstream_host": UPSTREAM_HOST,
            }
        ).encode("utf-8") + b"\n"
        self._send_bytes(200, payload, content_type="application/json")

    def _handle_local(self, raw_path):
        rel_url_path = raw_path[len(LOCAL_PREFIX) :] if raw_path.startswith(LOCAL_PREFIX) else ""
        rel_path = urllib.parse.unquote(rel_url_path).lstrip("/")

        root_abs = self.server.local_root
        # Normalize lexically (`abspath`/`normpath`) rather than `realpath` so `..` traversal
        # outside `local_root` is blocked while symlinked mounts inside `local_root`
        # (such as `./bucket -> /mnt/s3/...` on EOXHub) remain accessible.
        candidate_abs = os.path.abspath(os.path.normpath(os.path.join(root_abs, rel_path)))
        if os.path.commonpath([root_abs, candidate_abs]) != root_abs:
            self._send_bytes(403, b"Forbidden: path escapes local root\n")
            return

        if not rel_path or not os.path.isfile(candidate_abs):
            self._send_bytes(404, b"Not Found\n")
            return

        try:
            file_size = os.path.getsize(candidate_abs)
        except OSError as error:
            self._send_bytes(500, f"Failed to stat local file: {error}\n".encode("utf-8"))
            return

        content_type = _guess_content_type(candidate_abs)
        range_header = self.headers.get("Range") if self.command == "GET" else None
        parsed_range = _parse_byte_range(range_header, file_size)

        if parsed_range == "unsatisfiable":
            self._send_bytes(
                416,
                b"Requested Range Not Satisfiable\n",
                extra_headers=(("Content-Range", f"bytes */{file_size}"),),
            )
            return

        if self.command == "HEAD":
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            return

        headers_sent = False
        try:
            with open(candidate_abs, "rb") as fh:
                if parsed_range is not None:
                    start, end = parsed_range
                    length = end - start + 1
                    fh.seek(start)
                    data = fh.read(length)
                    self.send_response(206, "Partial Content")
                    self._cors()
                    self.send_header("Content-Type", content_type)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    headers_sent = True
                    self.wfile.write(data)
                else:
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", content_type)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(file_size))
                    self.end_headers()
                    headers_sent = True
                    remaining = file_size
                    while remaining > 0:
                        chunk = fh.read(min(CHUNK_COPY_SIZE, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except OSError as error:
            if not headers_sent:
                self._send_bytes(500, f"Failed reading local file: {error}\n".encode("utf-8"))

    def _handle_upstream(self):
        headers = {h: self.headers[h] for h in FORWARD_REQUEST if h in self.headers}
        try:
            response, body = _upstream(self.command, self.path, headers)
        except (http.client.HTTPException, OSError) as error:
            self._send_bytes(502, f"Upstream request failed: {error}\n".encode("utf-8"))
            return

        self.send_response(response.status, response.reason)
        self._cors()
        for name in FORWARD_RESPONSE:
            value = response.getheader(name)
            if value is not None:
                self.send_header(name, value)
        # For HEAD the body is empty, so report the size the bucket announced.
        length = response.getheader("Content-Length") if self.command == "HEAD" else None
        self.send_header("Content-Length", length or str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self):
        parsed_path = urllib.parse.urlsplit(self.path).path
        try:
            if parsed_path == "/healthz":
                self._handle_healthz()
            elif parsed_path == "/local" or parsed_path.startswith(LOCAL_PREFIX):
                self._handle_local(parsed_path)
            else:
                self._handle_upstream()
        except (BrokenPipeError, ConnectionResetError):
            # Browser aborted an in-flight tile fetch (e.g. during map pan/zoom).
            return

    do_GET = _dispatch
    do_HEAD = _dispatch

    def log_message(self, format, *args):
        if self.server.verbose:
            super().log_message(format, *args)


def _default_local_root() -> str:
    if os.environ.get("JUPYTER_SERVER_ROOT"):
        return os.environ["JUPYTER_SERVER_ROOT"]
    home = os.path.abspath(os.path.expanduser("~"))
    cwd = os.path.abspath(os.getcwd())
    return home if os.path.commonpath([home, cwd]) == home else cwd


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8010, help="port to listen on (default: 8010)")
    parser.add_argument(
        "--local-root",
        default=_default_local_root(),
        help="directory exposed under /local/ for cluster-local Zarr stores (default: JUPYTER_SERVER_ROOT or $HOME)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every request")
    args = parser.parse_args()

    local_root = os.path.abspath(args.local_root)
    server = ThreadingHTTPServer((args.host, args.port), CorsProxy)
    server.daemon_threads = True
    server.verbose = args.verbose
    server.local_root = local_root
    print(
        f"CORS & Zarr proxy listening on http://{args.host}:{args.port}/\n"
        f"  upstream : /...        -> https://{UPSTREAM_HOST}/...\n"
        f"  local    : /local/...  -> {local_root}/..."
    )
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
