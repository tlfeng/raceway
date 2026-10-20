#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""es-proxy.py —— Kibana→ES 的透明转发代理，额外把请求体落盘。
用途: 定位 Kibana 面板报 "[query] query malformed" 时，ES 到底收到了什么查询。

  python3 es-proxy.py --listen 127.0.0.1:9201 --upstream 127.0.0.1:9200 --log /tmp/es-query.log

然后把 kibana.yml 的 elasticsearch.hosts 指向 http://127.0.0.1:9201，重启 Kibana。
排查完把 hosts 改回 9200 即可。
"""
import argparse
import http.server
import json
import os
import socketserver
import urllib.error
import urllib.request


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = "http://127.0.0.1:9200"
    logfile = "/tmp/es-query.log"

    def log_message(self, fmt, *a):                      # noqa: A003
        pass                                            # 静默默认 stderr 日志

    def _proxy(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = self.upstream + self.path
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "connection")}
        req = urllib.request.Request(url, data=body or None, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
                status = r.status
                ctype = r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            data = e.read()
            status = e.code
            ctype = e.headers.get("Content-Type", "application/json")
        except Exception as e:                           # noqa: BLE001
            data = json.dumps({"error": str(e)}).encode()
            status = 502
            ctype = "application/json"

        if body and ("_search" in self.path or "_async_search" in self.path):
            self._log(method, self.path, body, status, data)

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # ES 8 客户端会校验这个头，代理不透传的话 Kibana 直接判"不是 ES"并退出
        self.send_header("X-Elastic-Product", "Elasticsearch")
        self.end_headers()
        self.wfile.write(data)

    def _log(self, method, path, body, status, resp):
        with open(self.logfile, "a", encoding="utf-8") as f:
            f.write("\n===== %s %s  -> %d =====\n" % (method, path, status))
            try:
                f.write(json.dumps(json.loads(body), ensure_ascii=False, indent=1))
            except Exception:                             # noqa: BLE001
                f.write(body.decode("utf-8", "replace"))
            f.write("\n----- RESPONSE -----\n")
            try:
                f.write(json.dumps(json.loads(resp), ensure_ascii=False, indent=1)[:4000])
            except Exception:                             # noqa: BLE001
                f.write(resp.decode("utf-8", "replace")[:4000])
            f.write("\n")

    def do_GET(self):                                   # noqa: N802
        self._proxy("GET")

    def do_POST(self):                                  # noqa: N802
        self._proxy("POST")

    def do_PUT(self):                                   # noqa: N802
        self._proxy("PUT")

    def do_HEAD(self):                                  # noqa: N802
        self._proxy("HEAD")

    def do_DELETE(self):                                # noqa: N802
        self._proxy("DELETE")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:9201")
    ap.add_argument("--upstream", default="http://127.0.0.1:9200")
    ap.add_argument("--log", default="/tmp/es-query.log")
    args = ap.parse_args()
    host, port = args.listen.split(":")
    Handler.upstream = args.upstream.rstrip("/")
    Handler.logfile = args.log
    open(args.log, "w", encoding="utf-8").close()
    srv = Server((host, int(port)), Handler)
    print("proxy %s -> %s  (log: %s)" % (args.listen, Handler.upstream, args.log))
    srv.serve_forever()


if __name__ == "__main__":
    main()
