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

端點：
  POST /upload?name=<filename>[&public=1]   body = raw bytes
       → {"path": "/abs/path", "url": "http://<host>/files/<uuid>.ext"[, "public_url": "..."]}
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

    def do_GET(self):
        if not lan_allowed(self.client_address[0]):
            self._send(403, '{"error": "forbidden: source IP not allowed"}')
            return
        path = urlparse(self.path).path
        if path == '/health':
            self._send(200, json.dumps({'ok': True, 'r2': r2_enabled()}))
        elif path.startswith('/files/'):
            name = os.path.basename(path)  # 防路徑穿越
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
