#!/usr/bin/env python3
"""Hermes Sidecar 附件上傳服務（純標準庫零依賴）

流程：extension 截圖/檔案 → POST raw bytes → 存到伺服器目錄
      → 回傳絕對路徑 → 前端把路徑注入 user 訊息 → agent 用 file 工具讀取

端點：
  POST /upload?name=<filename>    body = raw bytes → {"path": "/abs/path/xxx.png"}
  GET  /health                    → {"ok": true}
  GET  /files/<name>              讀回已上傳檔案（除錯用）
"""
import argparse
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_DIR = os.path.expanduser('~/agent-sidepanel/uploads')
MAX_SIZE = 50 * 1024 * 1024  # 50MB

# 允許的來源 IP（應用層白名單，不依賴防火牆）
# 192.168.0.10 = 主人的 Windows 瀏覽器；127.0.0.1 = 本機管理/除錯
ALLOWED_CLIENTS = {'192.168.0.10', '127.0.0.1', '::1'}


class Handler(BaseHTTPRequestHandler):
    def _client_allowed(self):
        client = self.client_address[0]
        return client in ALLOWED_CLIENTS

    def _reject(self):
        self._send(403, '{"error": "forbidden: source IP not allowed"}')

    def _send(self, code, body, ctype='application/json'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._client_allowed():
            self._reject()
            return
        path = urlparse(self.path).path
        if path == '/health':
            self._send(200, '{"ok": true}')
        elif path.startswith('/files/'):
            name = os.path.basename(path)
            fp = os.path.join(self.server.dir, name)
            if os.path.isfile(fp):
                with open(fp, 'rb') as f:
                    self._send(200, f.read(), 'application/octet-stream')
            else:
                self._send(404, 'not found')
        else:
            self._send(404, 'not found')

    def do_POST(self):
        if not self._client_allowed():
            self._reject()
            return
        parsed = urlparse(self.path)
        if parsed.path != '/upload':
            self._send(404, 'not found')
            return
        name = (parse_qs(parsed.query).get('name') or [''])[0].strip()
        if not name:
            self._send(400, '{"error": "missing ?name="}')
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            self._send(400, '{"error": "bad Content-Length"}')
            return
        if length <= 0 or length > MAX_SIZE:
            self._send(413, '{"error": "size out of range"}')
            return
        body = self.rfile.read(length)
        base, ext = os.path.splitext(os.path.basename(name))
        fname = f'{int(time.time())}_{uuid.uuid4().hex[:8]}{ext.lower()}'
        fp = os.path.join(self.server.dir, fname)
        with open(fp, 'wb') as f:
            f.write(body)
        self._send(200, '{"path": "%s"}' % fp)

    def log_message(self, format, *args):  # 安靜模式
        pass


class UploadServer(HTTPServer):
    def __init__(self, addr, upload_dir):
        super().__init__(addr, Handler)
        self.dir = upload_dir  # Handler 用 self.server.dir 讀取


def main():
    ap = argparse.ArgumentParser(description='Hermes Sidecar upload receiver')
    ap.add_argument('--port', type=int, default=18778)
    ap.add_argument('--dir', default=DEFAULT_DIR)
    a = ap.parse_args()
    os.makedirs(a.dir, exist_ok=True)
    print(f'upload server listening on 0.0.0.0:{a.port}  dir={a.dir}  allow={sorted(ALLOWED_CLIENTS)}', flush=True)
    UploadServer(('0.0.0.0', a.port), a.dir).serve_forever()


if __name__ == '__main__':
    main()
