#!/usr/bin/env python3
"""Hermes Sidecar 附件上傳服務 / LAN 檔案分享（純標準庫零依賴）

流程：extension 截圖/檔案 → POST raw bytes → 存到伺服器目錄（uuid 檔名）
      → 回傳絕對路徑 + LAN URL → 前端把路徑注入 user 訊息 → agent 用 file 工具讀取

LAN 檔案分享：
  - 檔名自動換成 uuid（不會衝突、不需要管理，直接 cp 覆蓋或重複上傳都安全）
  - GET /files/<name> 開放整個 LAN，回傳正確 Content-Type（圖片可直接 markdown 顯示）
  - 背景執行緒定期清理超過 max-age-days（預設 7 天）的舊檔案，防止塞爆

端點：
  POST /upload?name=<filename>    body = raw bytes → {"path": "/abs/path", "url": "http://<host>/files/<uuid>.ext"}
  GET  /health                    → {"ok": true}
  GET  /files/<name>              讀回已上傳檔案（LAN 存取）
"""
import argparse
import json
import mimetypes
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_DIR = os.path.expanduser('~/agent-sidepanel/uploads')
MAX_SIZE = 50 * 1024 * 1024  # 50MB
DEFAULT_MAX_AGE_DAYS = 7
LAN_PREFIX = '192.168.0.'  # 家庭 LAN 子網


def lan_allowed(client_ip: str) -> bool:
    """允許整個 LAN + localhost（家用網路，不對外暴露）。"""
    return client_ip.startswith(LAN_PREFIX) or client_ip in ('127.0.0.1', '::1')


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype='application/json'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not lan_allowed(self.client_address[0]):
            self._send(403, '{"error": "forbidden: source IP not allowed"}')
            return
        path = urlparse(self.path).path
        if path == '/health':
            self._send(200, '{"ok": true}')
        elif path.startswith('/files/'):
            name = os.path.basename(path)  # 防路徑穿越
            fp = os.path.join(self.server.dir, name)
            if os.path.isfile(fp):
                ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                with open(fp, 'rb') as f:
                    self._send(200, f.read(), ctype)
            else:
                self._send(404, 'not found')
        else:
            self._send(404, 'not found')

    def do_POST(self):
        if not lan_allowed(self.client_address[0]):
            self._send(403, '{"error": "forbidden: source IP not allowed"}')
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
        _, ext = os.path.splitext(os.path.basename(name))
        ext = ext.lower() if 0 < len(ext) <= 10 else ''
        fname = f'{uuid.uuid4().hex}{ext}'
        fp = os.path.join(self.server.dir, fname)
        with open(fp, 'wb') as f:
            f.write(body)
        url = f'http://{self.server.public_host}/files/{fname}'
        self._send(200, json.dumps({"path": fp, "url": url}))

    def log_message(self, format, *args):  # 安靜模式
        pass


class UploadServer(HTTPServer):
    def __init__(self, addr, upload_dir, public_host, max_age_days):
        super().__init__(addr, Handler)
        self.dir = upload_dir          # Handler 用 self.server.dir 讀取
        self.public_host = public_host  # 回傳 URL 用的 LAN 位址，如 192.168.0.160:18778
        self.max_age_days = max_age_days


def cleanup_loop(server: UploadServer, interval: int = 3600):
    """背景執行緒：每小時刪除超過 max_age_days 的舊檔案。"""
    while True:
        time.sleep(interval)
        cutoff = time.time() - server.max_age_days * 86400
        try:
            for name in os.listdir(server.dir):
                fp = os.path.join(server.dir, name)
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    print(f'cleanup: removed {name} (older than {server.max_age_days} days)', flush=True)
        except OSError as e:
            print(f'cleanup error: {e}', flush=True)


def main():
    ap = argparse.ArgumentParser(description='Hermes Sidecar upload receiver / LAN file share')
    ap.add_argument('--port', type=int, default=18778)
    ap.add_argument('--dir', default=DEFAULT_DIR)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--public-host', default='192.168.0.160',
                    help='回傳 URL 用的 LAN 位址（不含 port）')
    ap.add_argument('--max-age-days', type=int, default=DEFAULT_MAX_AGE_DAYS,
                    help='自動刪除超過天數的檔案（0 = 不清理）')
    a = ap.parse_args()
    os.makedirs(a.dir, exist_ok=True)
    server = UploadServer((a.host, a.port), a.dir, f'{a.public_host}:{a.port}', a.max_age_days)
    if a.max_age_days > 0:
        threading.Thread(target=cleanup_loop, args=(server,), daemon=True).start()
    print(f'upload server listening on {a.host}:{a.port}  dir={a.dir}  '
          f'public={server.public_host}  max_age_days={a.max_age_days}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
