#!/usr/bin/env python3
"""Hermes Sidecar 附件上傳服務 / LAN 檔案分享（純標準庫零依賴）

流程：extension 截圖/檔案 → POST raw bytes → 存到伺服器目錄（uuid 檔名）
      → 回傳絕對路徑 + LAN URL → 前端把路徑注入 user 訊息 → agent 用 file 工具讀取

LAN 檔案分享：
  - 檔名自動換成 uuid（不會衝突、不需要管理，直接 cp 覆蓋或重複上傳都安全）
  - GET /files/<name> 開放整個 LAN，回傳正確 Content-Type（圖片可直接 markdown 顯示）
  - 背景執行緒定期清理超過 max-age-days（預設 7 天）的舊檔案，防止塞爆
  - 純 LAN 服務：所有端點只接受 192.168.0.* / localhost（2026-09-08 起移除
    R2 presigned 公網分享與 Cloudflare tunnel 公網唯讀模式；公網檔案分享
    改走 meow-share：share_cli.py + R2 public bucket share.thomas2018yulab.uk）

端點：
  GET  /                          內建上傳前端（拖放/進度/連結複製，純靜態 HTML 無外部依賴）
  POST /upload?name=<filename>    body = raw bytes
       → {"path": "/abs/path", "url": "http://<host>/files/<uuid>.ext"}
  GET  /files                     已上傳檔案清單（JSON：name/orig/size/mtime，新到舊，上限 100 筆）
  GET  /health                    → {"ok": true}
  GET  /files/<name>              讀回已上傳檔案（LAN 存取）
       圖片維持內嵌顯示（markdown 用途）；其餘類型一律強制下載並還原原始檔名
"""
import argparse
import hashlib
import json
import mimetypes
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

DEFAULT_DIR = os.path.expanduser('~/agent-sidepanel/uploads')
MAX_SIZE = 1024 * 1024 * 1024  # 1GB（內網專用，2026-08-30 由 50MB 調高）
DEFAULT_MAX_AGE_DAYS = 7
LAN_PREFIX = '192.168.0.'  # 家庭 LAN 子網
CHUNK = 8 * 1024 * 1024


# ── 內建前端（純靜態 HTML，無外部依賴；GET / 直接回傳）────────────────
# 功能：拖放/點選上傳、即時進度與速度、LAN/公網連結複製、最近上傳清單。
# 上傳走 XHR（可取得 upload progress），檔案由瀏覽器串流送出，1GB 大檔不爆記憶體。

FRONTEND_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meow File Share</title>
<link rel="icon" href="data:,">
<style>
:root{
  --bg:#09090b; --panel:#18181b; --panel2:#27272a; --border:#3f3f46;
  --text:#e4e4e7; --dim:#a1a1aa; --accent:#34d399; --err:#f87171;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg);color:var(--text);min-height:100vh;padding:28px 16px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC","Microsoft JhengHei",sans-serif;
}
.wrap{max-width:640px;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
h1{font-size:20px;font-weight:600;letter-spacing:.3px}
.drop{
  border:2px dashed var(--border);border-radius:12px;padding:40px 16px;text-align:center;
  cursor:pointer;transition:border-color .15s,background .15s;background:var(--panel);
}
.drop:hover,.drop.over{border-color:var(--accent);background:#1c1c21}
.drop .big{font-size:15px;margin-bottom:8px}
.drop .small{font-size:12px;color:var(--dim);line-height:1.7}
.queue{margin-top:16px;display:flex;flex-direction:column;gap:10px}
.item{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.row1{display:flex;justify-content:space-between;gap:10px;font-size:13px;margin-bottom:8px}
.name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta{color:var(--dim);font-size:12px;flex:none}
.bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden}
.bar>div{height:100%;width:0;background:var(--accent);transition:width .15s}
.item.err .bar>div{background:var(--err)}
.result{margin-top:10px;display:none}
.item.done .result{display:block}
.lbl{font-size:11px;color:var(--dim);margin:8px 0 4px}
.urlbox{display:flex;gap:8px}
.urlbox input{
  flex:1;min-width:0;background:var(--bg);border:1px solid var(--border);color:var(--text);
  border-radius:6px;padding:6px 8px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
}
button{
  background:var(--panel2);color:var(--text);border:1px solid var(--border);border-radius:6px;
  padding:6px 12px;font-size:12px;cursor:pointer;white-space:nowrap;
}
button:hover{background:#34343a}
.hint{font-size:11px;color:var(--dim);margin-top:10px;line-height:1.6}
h2{font-size:14px;color:var(--dim);margin:22px 0 10px;font-weight:500}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{text-align:left;padding:7px 12px;border-bottom:1px solid var(--panel2)}
tr:last-child td{border-bottom:none}
th{color:var(--dim);font-weight:400;font-size:11px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ops{white-space:nowrap}
.ops button{padding:3px 10px;font-size:11px}
a.open{color:var(--accent);text-decoration:none;font-size:11px;margin-left:6px}
.empty{color:var(--dim);font-size:12px;padding:16px;text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Meow File Share</h1>
  </header>

  <div class="drop" id="drop">
    <div class="big">把檔案拖進來，或點這裡選擇</div>
    <div class="small">單檔最大 1GB · 限家庭網路 · 7 天後自動刪除</div>
    <input type="file" id="file" multiple hidden>
  </div>

  <div class="queue" id="queue"></div>

  <h2>最近上傳</h2>
  <div class="card">
    <table>
      <thead><tr><th>檔案</th><th>大小</th><th>時間</th><th></th></tr></thead>
      <tbody id="recent"></tbody>
    </table>
    <div class="empty" id="recentEmpty">尚無檔案</div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const drop = $('drop'), fileInput = $('file'), queueEl = $('queue');
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmtSize = n => n < 1024 ? n + ' B'
  : n < 1048576 ? (n/1024).toFixed(1) + ' KB'
  : n < 1073741824 ? (n/1048576).toFixed(1) + ' MB'
  : (n/1073741824).toFixed(2) + ' GB';
const fmtTime = t => new Date(t*1000).toLocaleString('zh-TW',
  {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});

fetch('/health').then(r => r.json()).then(() => {
  loadRecent();
}).catch(() => {});

function copyText(text, btn) {
  const done = () => { const o = btn.textContent; btn.textContent = '已複製!';
    setTimeout(() => btn.textContent = o, 1200); };
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); }
    catch (e) { prompt('手動複製:', text); }
    document.body.removeChild(ta);
  };
  if (navigator.clipboard && window.isSecureContext)
    navigator.clipboard.writeText(text).then(done).catch(fallback);
  else fallback();
}
document.addEventListener('click', e => {
  const b = e.target.closest('button[data-copy]');
  if (b) copyText(b.dataset.copy, b);
});

async function loadRecent() {
  try {
    const list = await (await fetch('/files')).json();
    const tb = $('recent'); tb.innerHTML = '';
    $('recentEmpty').style.display = list.length ? 'none' : 'block';
    for (const it of list.slice(0, 30)) {
      const url = '/files/' + encodeURIComponent(it.name);
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td class="mono" title="' + esc(it.name) + '">' + esc(it.orig) + '</td>' +
        '<td>' + fmtSize(it.size) + '</td><td>' + fmtTime(it.mtime) + '</td>' +
        '<td class="ops"><button data-copy="' + esc(url) + '">複製</button>' +
        '<a class="open" href="' + esc(url) + '">開啟</a></td>';
      tb.appendChild(tr);
    }
  } catch (e) { /* ignore */ }
}

drop.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });
['dragenter','dragover'].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave','drop'].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => addFiles(e.dataTransfer.files));

function addFiles(files) { for (const f of files) upload(f); }

function upload(file) {
  const item = document.createElement('div');
  item.className = 'item';
  item.innerHTML =
    '<div class="row1"><span class="name" title="' + esc(file.name) + '">' + esc(file.name) +
    '</span><span class="meta">' + fmtSize(file.size) + ' · 0%</span></div>' +
    '<div class="bar"><div></div></div><div class="result"></div>';
  queueEl.prepend(item);
  const bar = item.querySelector('.bar > div'),
        meta = item.querySelector('.meta'),
        result = item.querySelector('.result');

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload?name=' + encodeURIComponent(file.name));
  let lastT = performance.now(), lastB = 0;
  xhr.upload.onprogress = e => {
    if (!e.total) return;
    const pct = Math.round(e.loaded / e.total * 100);
    bar.style.width = pct + '%';
    const now = performance.now(), dt = (now - lastT) / 1000;
    if (dt > 0.25) {
      meta.textContent = fmtSize(file.size) + ' · ' + pct + '% · ' +
        fmtSize((e.loaded - lastB) / dt) + '/s';
      lastT = now; lastB = e.loaded;
    }
  };
  xhr.onload = () => {
    if (xhr.status === 200) {
      item.classList.add('done');
      bar.style.width = '100%';
      meta.textContent = fmtSize(file.size) + ' · 完成';
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) {}
      const rows = [];
      if (data.url) rows.push(['LAN 連結', data.url]);
      result.innerHTML = rows.map(r =>
        '<div class="lbl">' + r[0] + '</div><div class="urlbox">' +
        '<input readonly value="' + esc(r[1]) + '"><button data-copy="' + esc(r[1]) + '">複製</button>' +
        '</div>').join('') +
        '<div class="hint">把連結貼到跟小夜的對話，她就能直接讀到檔案</div>';
      loadRecent();
    } else {
      item.classList.add('err');
      meta.textContent = '失敗 (HTTP ' + xhr.status + ')';
      result.innerHTML = '<div class="hint">' + esc(xhr.responseText.slice(0, 300)) + '</div>';
    }
  };
  xhr.onerror = () => {
    item.classList.add('err');
    meta.textContent = '網路錯誤';
  };
  xhr.send(file);
}

loadRecent();
</script>
</body>
</html>
"""


# ── HTTP server ───────────────────────────────────────────────────────

def lan_allowed(client_ip: str) -> bool:
    """允許整個 LAN + localhost（家用網路，不對外暴露）。"""
    return client_ip.startswith(LAN_PREFIX) or client_ip in ('127.0.0.1', '::1')


def _safe_filename(name: str):
    """把原始檔名整理成可安全放入 Content-Disposition 的值（防 header 注入/路徑穿越）。"""
    name = os.path.basename(name)
    name = name.replace('"', '').replace('\r', '').replace('\n', '').strip()
    return name or None


def _read_orig_name(fp: str):
    """讀取 sidecar（<fp>.orig）記錄的原始檔名；不存在或讀失敗回傳 None。"""
    try:
        with open(fp + '.orig', 'r', encoding='utf-8') as mf:
            return _safe_filename(mf.read())
    except (OSError, UnicodeDecodeError):
        return None


def _content_disposition(name: str):
    """產生對非 ASCII 檔名安全的 Content-Disposition 值（RFC 5987）。

    send_header 以 latin-1 編碼 header 值，中文檔名會直接 UnicodeEncodeError 崩潰。
    解法：純 ASCII 檔名用 filename=；非 ASCII 檔名用 filename*=UTF-8''<percent-encoded>
    （現代瀏覽器讀 filename* 還原正確檔名），並附純 ASCII 的 filename= 給舊瀏覽器。
    """
    if not name:
        return None
    if name.isascii():
        return f'attachment; filename="{name}"'
    base, ext = os.path.splitext(name)
    fallback = ''.join(c if c.isascii() else '_' for c in base).strip('_') or 'file'
    fallback += ''.join(c if c.isascii() else '_' for c in ext)
    encoded = quote(name, safe='')
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _fix_maybe_mojibake(s: str) -> str:
    """客戶端若把 raw UTF-8 字節直接塞進 URL（未 percent-encode），
    http.server 會以 latin-1 解碼 request line，產生亂碼（如 筆記→ç­è¨）。
    此處嘗試還原：若 s 是 latin-1 解碼 UTF-8 的結果，重新 encode→decode 取回原文；
    否則（正常 percent-encode 解出的字串、或純 ASCII）原樣回傳。"""
    try:
        return s.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype='application/json', disposition=None):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        if disposition:
            self.send_header('Content-Disposition', disposition)
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, name: str):
        """伺服單一檔案（LAN 與公網共用）。name 已 basename（防路徑穿越）。"""
        if name.endswith('.orig'):
            self._send(404, 'not found')  # sidecar 不對外開放
            return
        fp = os.path.join(self.server.dir, name)
        if os.path.isfile(fp):
            ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            disposition = None
            if not ctype.startswith('image/'):
                # 圖片維持內嵌顯示（markdown 用途）；其餘類型一律強制下載並還原原始檔名
                # （文字類瀏覽器會內嵌顯示，octet-stream/pdf 等也一併下載並帶正確檔名）
                disp_name = _read_orig_name(fp) or name
                disposition = _content_disposition(disp_name)
            with open(fp, 'rb') as f:
                self._send(200, f.read(), ctype, disposition)
        else:
            self._send(404, 'not found')

    def _list_files(self):
        """已上傳檔案清單（排除 .orig sidecar），依時間新到舊，上限 100 筆。"""
        items = []
        try:
            names = os.listdir(self.server.dir)
        except OSError:
            return items
        for name in names:
            if name.endswith('.orig'):
                continue
            fp = os.path.join(self.server.dir, name)
            if not os.path.isfile(fp):
                continue
            try:
                st = os.stat(fp)
            except OSError:
                continue
            items.append({
                'name': name,
                'orig': _read_orig_name(fp) or name,
                'size': st.st_size,
                'mtime': int(st.st_mtime),
            })
        items.sort(key=lambda x: x['mtime'], reverse=True)
        return items[:100]

    def do_GET(self):
        path = urlparse(self.path).path
        if not lan_allowed(self.client_address[0]):
            self._send(403, '{"error": "forbidden: source IP not allowed"}')
            return
        if path == '/health':
            self._send(200, json.dumps({'ok': True}))
        elif path == '/':
            self._send(200, FRONTEND_HTML, 'text/html; charset=utf-8')
        elif path == '/files':
            self._send(200, json.dumps(self._list_files(), ensure_ascii=False))
        elif path.startswith('/files/'):
            self._serve_file(os.path.basename(path))
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
        qs = parse_qs(parsed.query)
        name = (qs.get('name') or [''])[0].strip()
        if not name:
            self._send(400, '{"error": "missing ?name="}')
            return
        name = _fix_maybe_mojibake(name)
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            self._send(400, '{"error": "bad Content-Length"}')
            return
        if length <= 0 or length > MAX_SIZE:
            self._send(413, '{"error": "size out of range"}')
            return
        _, ext = os.path.splitext(os.path.basename(name))
        ext = ext.lower() if 0 < len(ext) <= 10 else ''
        fname = f'{uuid.uuid4().hex}{ext}'
        fp = os.path.join(self.server.dir, fname)
        sha = hashlib.sha256()
        remaining = length
        with open(fp, 'wb') as f:
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                remaining -= len(chunk)
        # 記錄原始檔名到 sidecar，供下載時還原（避免 uuid 檔名）
        orig = _safe_filename(name)
        if orig and orig != fname:
            try:
                with open(fp + '.orig', 'w', encoding='utf-8') as mf:
                    mf.write(orig)
            except OSError:
                pass  # sidecar 寫失敗不影響上傳
        url = f'http://{self.server.public_host}/files/{fname}'
        self._send(200, json.dumps({'path': fp, 'url': url}))

    def log_message(self, format, *args):  # 安靜模式
        pass


class UploadServer(ThreadingHTTPServer):
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
                if not os.path.isfile(fp) or os.path.getmtime(fp) >= cutoff:
                    continue
                os.remove(fp)
                print(f'cleanup: removed {name} (older than {server.max_age_days} days)',
                      flush=True)
                if name.endswith('.orig'):
                    continue  # sidecar 本身
                sidecar = fp + '.orig'
                if os.path.isfile(sidecar):
                    os.remove(sidecar)
        except OSError as e:
            print(f'cleanup error: {e}', flush=True)


def main():
    ap = argparse.ArgumentParser(description='Hermes Sidecar upload receiver / LAN+R2 file share')
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
          f'public={server.public_host}  max_age_days={a.max_age_days}',
          flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
