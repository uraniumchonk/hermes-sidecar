#!/usr/bin/env python3
"""Hermes Sidecar 附件上傳服務 / LAN 檔案分享 / R2 公網分享（純標準庫零依賴）

流程：extension 截圖/檔案 → POST raw bytes → 存到伺服器目錄（uuid 檔名）
      → 回傳絕對路徑 + LAN URL → 前端把路徑注入 user 訊息 → agent 用 file 工具讀取

LAN 檔案分享：
  - 檔名自動換成 uuid（不會衝突、不需要管理，直接 cp 覆蓋或重複上傳都安全）
  - GET /files/<name> 開放整個 LAN，回傳正確 Content-Type（圖片可直接 markdown 顯示）
  - 背景執行緒定期清理超過 max-age-days（預設 7 天）的舊檔案，防止塞爆

R2 公網分享（選配，設定齊全才啟用）：
  - POST /upload?name=<file>&public=1 → 存本地後再串流 PUT 到 Cloudflare R2
    → 額外回傳 public_url（presigned GET，預設 7 天有效，與本地清理同步）
  - 本地清理舊檔時同步刪除 R2 物件（同一個 uuid key）
  - SigV4 簽名為純標準庫實作（region=auto, service=s3），可用 --selftest 對照
    AWS 官方測試向量驗證
  - 憑證從環境變數讀取（systemd EnvironmentFile）：
    R2_ACCOUNT_ID / R2_BUCKET / R2_ACCESS_KEY / R2_SECRET_KEY / R2_URL_EXPIRY

公網唯讀存取（Cloudflare tunnel，選配，設定齊全才啟用）：
  - 設定 FILESHARE_PUBLIC_HOST=<公網 hostname>（如 files.thomas2018yulab.uk）後，
    請求的 Host header 等於該值時進入「公網唯讀模式」：
    只放行 GET /files/<name>（讀檔），一律封鎖 /（上傳介面）、/files（清單）、
    /upload（上傳）——避免公開洩漏檔案清單或被人從外網塞檔。
  - 上傳回應額外回傳 tunnel_url（https://<PUBLIC_HOST>/files/<uuid>.ext），
    乾淨短連結，只要檔案在本地（7 天內）就有效，不需 public=1、不碰 R2。
  - 與 R2 的差異：tunnel 連結走自家 tunnel（需 meowhome 開著），R2 是離線備份
    （meowhome 關掉仍可存取）。兩者可並存，前端會同時顯示。

端點：
  GET  /                          內建上傳前端（拖放/進度/連結複製，純靜態 HTML 無外部依賴）
  POST /upload?name=<filename>[&public=1]   body = raw bytes
       → {"path": "/abs/path", "url": "http://<host>/files/<uuid>.ext"[, "public_url": "..."]}
  GET  /files                     已上傳檔案清單（JSON：name/orig/size/mtime，新到舊，上限 100 筆）
  GET  /health                    → {"ok": true, "r2": <bool>}
  GET  /files/<name>              讀回已上傳檔案（LAN 存取）
       圖片維持內嵌顯示（markdown 用途）；其餘類型一律強制下載並還原原始檔名
"""
import argparse
import hashlib
import hmac
import http.client
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

# ── R2 設定（任一缺漏 → public 功能停用，LAN 分享不受影響）─────────────
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID', '').strip()
R2_BUCKET = os.environ.get('R2_BUCKET', '').strip()
R2_ACCESS_KEY = os.environ.get('R2_ACCESS_KEY', '').strip()
R2_SECRET_KEY = os.environ.get('R2_SECRET_KEY', '').strip()
try:
    R2_URL_EXPIRY = int(os.environ.get('R2_URL_EXPIRY', str(7 * 86400)))
except ValueError:
    R2_URL_EXPIRY = 7 * 86400
R2_URL_EXPIRY = max(1, min(R2_URL_EXPIRY, 7 * 86400))  # SigV4 presign 上限 7 天
R2_REGION = 'auto'  # R2 固定
R2_SERVICE = 's3'
R2_PUT_TIMEOUT = 600  # 秒；1GB 走公網留足餘裕

# ── 公網（Cloudflare tunnel）唯讀存取 ─────────────────────────────────
# 透過 tunnel 暴露的公網 hostname（如 files.thomas2018yulab.uk）。當請求的
# Host header 等於此值時進入「公網唯讀模式」：只放行 GET /files/<name>（讀檔），
# 一律封鎖 /（上傳介面）、/files（檔案清單）、/upload（上傳）——避免公開洩漏
# 檔案清單或被人從外網塞檔。留空 = 不啟用（純 LAN 行為，與舊版完全一致）。
# 設定在 r2.env：FILESHARE_PUBLIC_HOST=files.thomas2018yulab.uk
PUBLIC_HOST = os.environ.get('FILESHARE_PUBLIC_HOST', '').strip().lower()


def r2_enabled() -> bool:
    return bool(R2_ACCOUNT_ID and R2_BUCKET and R2_ACCESS_KEY and R2_SECRET_KEY)


# ── AWS Signature Version 4（純標準庫，參數化 region/service 以便測試）──

def _signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = hmac.new(f'AWS4{secret_key}'.encode(), date_stamp.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(k_service, b'aws4_request', hashlib.sha256).digest()


def _canonical_request(method: str, uri: str, query: str,
                       headers: dict, payload_hash: str) -> str:
    """headers: 小寫 header name → 已 trim 的 value。"""
    signed_names = sorted(headers)
    canonical_headers = ''.join(f'{n}:{headers[n]}\n' for n in signed_names)
    signed_headers = ';'.join(signed_names)
    return '\n'.join([method, uri, query, canonical_headers, signed_headers, payload_hash])


def _signature(secret_key: str, amz_date: str, region: str, service: str,
               canonical_request: str) -> str:
    date_stamp = amz_date[:8]
    scope = f'{date_stamp}/{region}/{service}/aws4_request'
    string_to_sign = '\n'.join([
        'AWS4-HMAC-SHA256',
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode('utf-8')).hexdigest(),
    ])
    return hmac.new(_signing_key(secret_key, date_stamp, region, service),
                    string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()


def _now_amz_date() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _r2_host() -> str:
    return f'{R2_ACCOUNT_ID}.r2.cloudflarestorage.com'


def _r2_uri(key: str) -> str:
    # key 是 uuid4().hex + 純字母數字副檔名，無需 percent-encode
    return f'/{R2_BUCKET}/{key}'


def _signed_headers_for(method: str, uri: str, host: str,
                        payload_hash: str, amz_date: str) -> dict:
    """產生 SigV4 header 認證所需的全部 headers（含 Authorization）。"""
    headers = {
        'host': host,
        'x-amz-date': amz_date,
        'x-amz-content-sha256': payload_hash,
    }
    canonical = _canonical_request(method, uri, '', headers, payload_hash)
    sig = _signature(R2_SECRET_KEY, amz_date, R2_REGION, R2_SERVICE, canonical)
    scope = f'{amz_date[:8]}/{R2_REGION}/{R2_SERVICE}/aws4_request'
    signed_names = ';'.join(sorted(headers))
    headers['Authorization'] = (
        f'AWS4-HMAC-SHA256 Credential={R2_ACCESS_KEY}/{scope}, '
        f'SignedHeaders={signed_names}, Signature={sig}')
    return headers


def _r2_request(method: str, key: str, payload_hash: str,
                body_file=None, content_length=0, timeout=60):
    """對 R2 發一個 SigV4 簽名的請求，回傳 (status, body_text)。"""
    host, uri = _r2_host(), _r2_uri(key)
    headers = _signed_headers_for(method, uri, host, payload_hash, _now_amz_date())
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        # skip_host=True：不自動加 Host，改由下方手動送（必須與簽名值一致，否則 400）
        conn.putrequest(method, uri, skip_host=True, skip_accept_encoding=True)
        for name, value in headers.items():
            conn.putheader(name, value)
        if body_file is not None:
            conn.putheader('Content-Length', str(content_length))
            with open(body_file, 'rb') as f:
                conn.endheaders()
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    conn.send(chunk)
        else:
            conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read().decode('utf-8', 'replace')
    finally:
        conn.close()


def r2_put_object(key: str, filepath: str, payload_sha256: str, content_length: int):
    """串流上傳本地檔案到 R2（不把整檔吃進記憶體）。"""
    return _r2_request('PUT', key, payload_sha256,
                       body_file=filepath, content_length=content_length,
                       timeout=R2_PUT_TIMEOUT)


def r2_delete_object(key: str):
    return _r2_request('DELETE', key, hashlib.sha256(b'').hexdigest())


def r2_presign_get(key: str, expires: int | None = None) -> str:
    """產生 presigned GET URL（query 參數認證，不需公開 bucket）。"""
    expires = expires or R2_URL_EXPIRY
    host, uri = _r2_host(), _r2_uri(key)
    amz_date = _now_amz_date()
    scope = f'{amz_date[:8]}/{R2_REGION}/{R2_SERVICE}/aws4_request'
    params = {
        'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
        'X-Amz-Credential': f'{R2_ACCESS_KEY}/{scope}',
        'X-Amz-Date': amz_date,
        'X-Amz-Expires': str(expires),
        'X-Amz-SignedHeaders': 'host',
    }
    canonical_query = '&'.join(
        f'{quote(k, safe="")}={quote(v, safe="")}' for k, v in sorted(params.items()))
    canonical = _canonical_request('GET', uri, canonical_query,
                                   {'host': host}, 'UNSIGNED-PAYLOAD')
    sig = _signature(R2_SECRET_KEY, amz_date, R2_REGION, R2_SERVICE, canonical)
    return f'https://{host}{uri}?{canonical_query}&X-Amz-Signature={sig}'


# ── SigV4 selftest：對照 AWS 官方測試向量 ──────────────────────────────

def selftest() -> int:
    """用官方 SigV4 測試向量驗證簽名實作（不需要 R2 憑證）。

    向量來源與交叉驗證（2026-08-31）：
      1. get-vanilla（header 認證）— AWS SigV4 test suite 請求格式
         （GET /, host example.amazonaws.com, x-amz-date 20150830T123600Z,
         secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY）。
         期望值已用 AWS C Runtime（awscrt，官方 gold standard）交叉驗證一致。
         注意：mhart/aws4 鏡像 repo 的 .authz 期望值與其 .req 不符（過期向量），勿用。
      2. presigned GET（query 認證）— AWS S3 文件 sigv4-query-string-auth 官方範例
         （examplebucket/test.txt, 2013-05-24, expires 86400, us-east-1/s3,
         AKIAIOSFODNN7EXAMPLE）。與 botocore SigV4QueryAuth 同格式（UNSIGNED-PAYLOAD）。
    """
    failures = 0

    # 向量 1：get-vanilla（header 認證）
    creq = _canonical_request(
        'GET', '/', '',
        {'host': 'example.amazonaws.com', 'x-amz-date': '20150830T123600Z'},
        'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
    expected_creq = (
        'GET\n/\n\n'
        'host:example.amazonaws.com\n'
        'x-amz-date:20150830T123600Z\n'
        '\n'
        'host;x-amz-date\n'
        'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
    if creq != expected_creq:
        print('FAIL get-vanilla canonical request:\n' + repr(creq))
        failures += 1
    else:
        print('PASS get-vanilla canonical request')
    sts_hash = hashlib.sha256(creq.encode()).hexdigest()
    if sts_hash != 'bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63':
        print(f'FAIL get-vanilla string-to-sign hash: {sts_hash}')
        failures += 1
    else:
        print('PASS get-vanilla string-to-sign hash')
    sig = _signature('wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                     '20150830T123600Z', 'us-east-1', 'service', creq)
    # 期望值經 AWS C Runtime（awscrt）交叉驗證
    if sig != 'ea21d6f05e96a897f6000a1a293f0a5bf0f92a00343409e820dce329ca6365ea':
        print(f'FAIL get-vanilla signature: {sig}')
        failures += 1
    else:
        print('PASS get-vanilla signature')

    # 向量 2：presigned GET（AWS S3 文件官方範例）
    #   GET https://examplebucket.s3.amazonaws.com/test.txt
    #   2013-05-24T00:00:00Z, expires 86400, region us-east-1, service s3
    #   access: AKIAIOSFODNN7EXAMPLE
    amz_date = '20130524T000000Z'
    scope = f'{amz_date[:8]}/us-east-1/s3/aws4_request'
    params = {
        'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
        'X-Amz-Credential': f'AKIAIOSFODNN7EXAMPLE/{scope}',
        'X-Amz-Date': amz_date,
        'X-Amz-Expires': '86400',
        'X-Amz-SignedHeaders': 'host',
    }
    canonical_query = '&'.join(
        f'{quote(k, safe="")}={quote(v, safe="")}' for k, v in sorted(params.items()))
    creq2 = _canonical_request('GET', '/test.txt', canonical_query,
                               {'host': 'examplebucket.s3.amazonaws.com'},
                               'UNSIGNED-PAYLOAD')
    sig2 = _signature('wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                      amz_date, 'us-east-1', 's3', creq2)
    if sig2 != 'aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404':
        print(f'FAIL presigned-get signature: {sig2}')
        failures += 1
    else:
        print('PASS presigned-get signature')

    if failures:
        print(f'selftest: {failures} 個向量失敗')
        return 1
    print('selftest: 全部通過')
    return 0


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
.status{font-size:12px;color:var(--dim);display:flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--err);flex:none}
.dot.on{background:var(--accent)}
.drop{
  border:2px dashed var(--border);border-radius:12px;padding:40px 16px;text-align:center;
  cursor:pointer;transition:border-color .15s,background .15s;background:var(--panel);
}
.drop:hover,.drop.over{border-color:var(--accent);background:#1c1c21}
.drop .big{font-size:15px;margin-bottom:8px}
.drop .small{font-size:12px;color:var(--dim);line-height:1.7}
.opts{display:flex;align-items:center;gap:8px;margin-top:12px;font-size:13px;color:var(--dim)}
.opts input{accent-color:var(--accent);width:15px;height:15px}
.opts.off{opacity:.45}
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
    <div class="status"><span class="dot" id="r2dot"></span><span id="r2txt">checking…</span></div>
  </header>

  <div class="drop" id="drop">
    <div class="big">把檔案拖進來，或點這裡選擇</div>
    <div class="small">單檔最大 1GB · 限家庭網路 · 7 天後自動刪除</div>
    <input type="file" id="file" multiple hidden>
  </div>
  <div class="opts" id="opts">
    <input type="checkbox" id="pub">
    <label for="pub">同時上傳到公網（R2，7 天有效）</label>
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
const drop = $('drop'), fileInput = $('file'), queueEl = $('queue'),
      pubBox = $('pub'), optsEl = $('opts');
let PUBLIC_HOST = '';  // 由 /health 填入（公網 tunnel hostname，空 = 未啟用）
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmtSize = n => n < 1024 ? n + ' B'
  : n < 1048576 ? (n/1024).toFixed(1) + ' KB'
  : n < 1073741824 ? (n/1048576).toFixed(1) + ' MB'
  : (n/1073741824).toFixed(2) + ' GB';
const fmtTime = t => new Date(t*1000).toLocaleString('zh-TW',
  {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});

fetch('/health').then(r => r.json()).then(h => {
  const on = !!h.r2;
  PUBLIC_HOST = h.public_host || '';
  $('r2dot').classList.toggle('on', on);
  $('r2txt').textContent = on ? 'R2 已就緒' : 'R2 未啟用（僅 LAN）';
  if (!on) { pubBox.disabled = true; optsEl.classList.add('off'); }
  loadRecent();  // 拿到 public_host 後重繪清單（補上「外網」按鈕）
}).catch(() => { $('r2txt').textContent = '服務連線失敗'; });

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
      const pubUrl = PUBLIC_HOST ? 'https://' + PUBLIC_HOST + '/files/' + encodeURIComponent(it.name) : '';
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td class="mono" title="' + esc(it.name) + '">' + esc(it.orig) + '</td>' +
        '<td>' + fmtSize(it.size) + '</td><td>' + fmtTime(it.mtime) + '</td>' +
        '<td class="ops"><button data-copy="' + esc(url) + '">複製</button>' +
        (pubUrl ? '<button data-copy="' + esc(pubUrl) + '">外網</button>' : '') +
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

  const pub = pubBox.checked && !pubBox.disabled;
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload?name=' + encodeURIComponent(file.name) + (pub ? '&public=1' : ''));
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
      if (data.tunnel_url) rows.push(['公網連結', data.tunnel_url]);
      if (data.public_url) rows.push(['R2 備份連結', data.public_url]);
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

    def _host_of(self) -> str:
        """請求的 Host header（去掉 port、轉小寫）。"""
        return (self.headers.get('Host') or '').split(':')[0].strip().lower()

    def _is_public(self) -> bool:
        """是否來自公網 tunnel（Host header 等於 FILESHARE_PUBLIC_HOST）。

        cloudflared 是同機轉發，來源 IP 是 127.0.0.1（會通過 lan_allowed），
        所以必須靠 Host header 區分公網請求，才能對公網套用唯讀限制。
        """
        return bool(PUBLIC_HOST) and self._host_of() == PUBLIC_HOST

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
        if self._is_public():
            # 公網唯讀：只放行 GET /files/<name>；/（上傳介面）、/files（清單）、
            # /health 一律 404，避免公開洩漏檔案清單或介面。
            if path.startswith('/files/'):
                self._serve_file(os.path.basename(path))
            else:
                self._send(404, 'not found')
            return
        if not lan_allowed(self.client_address[0]):
            self._send(403, '{"error": "forbidden: source IP not allowed"}')
            return
        if path == '/health':
            self._send(200, json.dumps({'ok': True, 'r2': r2_enabled(),
                                        'public_host': PUBLIC_HOST}))
        elif path == '/':
            self._send(200, FRONTEND_HTML, 'text/html; charset=utf-8')
        elif path == '/files':
            self._send(200, json.dumps(self._list_files(), ensure_ascii=False))
        elif path.startswith('/files/'):
            self._serve_file(os.path.basename(path))
        else:
            self._send(404, 'not found')

    def do_POST(self):
        if self._is_public():
            self._send(404, 'not found')  # 公網不開放上傳（防外網塞檔/塞爆磁碟）
            return
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
        want_public = (qs.get('public') or [''])[0].strip().lower() in ('1', 'true', 'yes')
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            self._send(400, '{"error": "bad Content-Length"}')
            return
        if length <= 0 or length > MAX_SIZE:
            self._send(413, '{"error": "size out of range"}')
            return
        if want_public and not r2_enabled():
            self._send(503, json.dumps({
                'error': 'public 上傳未啟用：伺服器缺少 R2 設定'
                         '（R2_ACCOUNT_ID / R2_BUCKET / R2_ACCESS_KEY / R2_SECRET_KEY）'}))
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
        result = {'path': fp, 'url': url}
        if PUBLIC_HOST:
            # 公網唯讀連結（走 Cloudflare tunnel，乾淨短連結）。
            # 只要檔案還在本地（7 天內）就有效，不需要 public=1、不碰 R2。
            result['tunnel_url'] = f'https://{PUBLIC_HOST}/files/{fname}'
        if want_public:
            status, err = r2_put_object(fname, fp, sha.hexdigest(), length)
            if status != 200:
                self._send(502, json.dumps({
                    'error': f'R2 上傳失敗 (HTTP {status}): {err[:300]}',
                    'local_url': url}))
                return
            result['public_url'] = r2_presign_get(fname)
        self._send(200, json.dumps(result))

    def log_message(self, format, *args):  # 安靜模式
        pass


class UploadServer(ThreadingHTTPServer):
    def __init__(self, addr, upload_dir, public_host, max_age_days):
        super().__init__(addr, Handler)
        self.dir = upload_dir          # Handler 用 self.server.dir 讀取
        self.public_host = public_host  # 回傳 URL 用的 LAN 位址，如 192.168.0.160:18778
        self.max_age_days = max_age_days


def cleanup_loop(server: UploadServer, interval: int = 3600):
    """背景執行緒：每小時刪除超過 max_age_days 的舊檔案（R2 物件同步刪除）。"""
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
                    continue  # sidecar 本身，無對應 R2 物件
                if r2_enabled():
                    try:
                        status, err = r2_delete_object(name)
                        print(f'cleanup: r2 delete {name} -> HTTP {status}', flush=True)
                    except Exception as e:
                        print(f'cleanup: r2 delete {name} failed: {e}', flush=True)
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
    ap.add_argument('--selftest', action='store_true',
                    help='跑 SigV4 測試向量後離開（不需要 R2 憑證）')
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    os.makedirs(a.dir, exist_ok=True)
    server = UploadServer((a.host, a.port), a.dir, f'{a.public_host}:{a.port}', a.max_age_days)
    if a.max_age_days > 0:
        threading.Thread(target=cleanup_loop, args=(server,), daemon=True).start()
    r2_state = 'on' if r2_enabled() else 'off（未設定 R2 憑證，public 上傳停用）'
    print(f'upload server listening on {a.host}:{a.port}  dir={a.dir}  '
          f'public={server.public_host}  max_age_days={a.max_age_days}  r2={r2_state}',
          flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
