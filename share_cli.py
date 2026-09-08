#!/usr/bin/env python3
"""
meow-share 管理 CLI（R2 public bucket + custom domain 檔案分享）

公網存取：https://share.thomas2018yulab.uk/<token>
  - 只有明確 publish 過的檔案才在 public bucket 裡（不是全通）
  - token = 16 hex（64 bit），不知道 token 就無法存取，bucket root 不允許 list
  - 定時過期：manifest 記 expires_at，cleanup 刪過期物件（cron 每日跑）

管理操作（主人 ask 小夜時執行）：
  publish <file> [--ttl 7d] [--name <name>]   發布檔案 → 回公網 URL
  list                                        列出已發布檔案（含到期日）
  info <token>                                單一檔案詳情
  delete <token>                              刪除（R2 物件 + manifest）
  renew <token> [--ttl 7d]                    延長到期日
  cleanup                                     刪除所有過期檔案（cron 用）
  status                                      bucket / domain 狀態

憑證：
  - R2: ~/hermes-sidecar/r2.env（與 fileshare 共用 access key，跨 bucket 有效）
  - Cloudflare API: ~/.config/cloudflare/credentials.env（status 用）

SigV4 實作複用 upload_server.py 的純標準庫版本。
"""
import argparse
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

# ── 常數 ─────────────────────────────────────────────────────────────
R2_ENV = os.path.expanduser('~/hermes-sidecar/r2.env')
CF_ENV = os.path.expanduser('~/.config/cloudflare/credentials.env')
MANIFEST = os.path.expanduser('~/.config/meow-share/manifest.json')
BUCKET = 'meow-share'
DOMAIN = 'share.thomas2018yulab.uk'
BASE_URL = f'https://{DOMAIN}'
DEFAULT_TTL = '7d'
CHUNK = 1024 * 1024


def load_env(path):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env


R2 = load_env(R2_ENV)
CF = load_env(CF_ENV)
R2_ACCOUNT_ID = R2.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY = R2.get('R2_ACCESS_KEY', '')
R2_SECRET_KEY = R2.get('R2_SECRET_KEY', '')
R2_REGION = 'auto'
R2_SERVICE = 's3'


# ── SigV4（複用 upload_server.py 純標準庫實作）──────────────────────
def _signing_key(secret_key, date_stamp, region, service):
    k_date = hmac.new(f'AWS4{secret_key}'.encode(), date_stamp.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(k_service, b'aws4_request', hashlib.sha256).digest()


def _canonical_request(method, uri, query, headers, payload_hash):
    signed_names = sorted(headers)
    canonical_headers = ''.join(f'{n}:{headers[n]}\n' for n in signed_names)
    signed_headers = ';'.join(signed_names)
    return '\n'.join([method, uri, query, canonical_headers, signed_headers, payload_hash])


def _signature(secret_key, amz_date, region, service, canonical_request):
    date_stamp = amz_date[:8]
    scope = f'{date_stamp}/{region}/{service}/aws4_request'
    string_to_sign = '\n'.join([
        'AWS4-HMAC-SHA256', amz_date, scope,
        hashlib.sha256(canonical_request.encode('utf-8')).hexdigest(),
    ])
    return hmac.new(_signing_key(secret_key, date_stamp, region, service),
                    string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()


def _now_amz_date():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _r2_host():
    return f'{R2_ACCOUNT_ID}.r2.cloudflarestorage.com'


def _r2_request(method, key, extra_headers=None, query='', body_file=None,
                content_length=0, timeout=600):
    """對 R2 發 SigV4 簽名請求。extra_headers 會一併簽名（metadata 用）。"""
    host, uri = _r2_host(), f'/{BUCKET}/{key}'
    payload_hash = hashlib.sha256(b'').hexdigest() if body_file is None else None
    if body_file is not None:
        # 上傳時先算整檔 sha256（串流）
        h = hashlib.sha256()
        with open(body_file, 'rb') as f:
            while True:
                c = f.read(CHUNK)
                if not c:
                    break
                h.update(c)
        payload_hash = h.hexdigest()
    else:
        payload_hash = hashlib.sha256(b'').hexdigest()
    amz_date = _now_amz_date()
    headers = {
        'host': host,
        'x-amz-date': amz_date,
        'x-amz-content-sha256': payload_hash,
    }
    if extra_headers:
        for k, v in extra_headers.items():
            headers[k.lower()] = v
    canonical = _canonical_request(method, uri, query, headers, payload_hash)
    sig = _signature(R2_SECRET_KEY, amz_date, R2_REGION, R2_SERVICE, canonical)
    scope = f'{amz_date[:8]}/{R2_REGION}/{R2_SERVICE}/aws4_request'
    headers['Authorization'] = (
        f'AWS4-HMAC-SHA256 Credential={R2_ACCESS_KEY}/{scope}, '
        f'SignedHeaders={";".join(sorted(headers))}, Signature={sig}')
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.putrequest(method, uri + (f'?{query}' if query else ''),
                        skip_host=True, skip_accept_encoding=True)
        for name, value in headers.items():
            conn.putheader(name, value)
        if body_file is not None:
            conn.putheader('Content-Length', str(content_length))
            with open(body_file, 'rb') as f:
                conn.endheaders()
                while True:
                    c = f.read(CHUNK)
                    if not c:
                        break
                    conn.send(c)
        else:
            conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read().decode('utf-8', 'replace')
    finally:
        conn.close()


def r2_put_object(key, filepath, extra_headers):
    return _r2_request('PUT', key, extra_headers=extra_headers,
                       body_file=filepath, content_length=os.path.getsize(filepath))


def r2_delete_object(key):
    return _r2_request('DELETE', key)


def r2_copy_replace_metadata(key, extra_headers):
    """CopyObject 到自身 + metadata REPLACE（renew 用）。"""
    headers = {
        'x-amz-copy-source': f'/{BUCKET}/{key}',
        'x-amz-metadata-directive': 'REPLACE',
    }
    for k, v in extra_headers.items():
        headers[k.lower()] = v
    return _r2_request('PUT', key, extra_headers=headers, query='x-id=CopyObject')


# ── manifest ─────────────────────────────────────────────────────────
def load_manifest():
    if not os.path.exists(MANIFEST):
        return {'version': 1, 'domain': DOMAIN, 'bucket': BUCKET, 'files': {}}
    with open(MANIFEST) as f:
        return json.load(f)


def save_manifest(m):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    tmp = MANIFEST + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
    os.replace(tmp, MANIFEST)


def parse_ttl(s):
    """'7d' / '24h' / '30m' / '1w' / 'never' / 純數字（秒）→ 秒數；never → None。"""
    s = s.strip().lower()
    if s == 'never':
        return None
    if s.isdigit():
        return int(s)
    m = re.fullmatch(r'(\d+)\s*([smhdw])', s)
    if not m:
        raise SystemExit(f'無法解析 TTL: {s}（可用 30m/24h/7d/1w/never）')
    n, unit = int(m.group(1)), m.group(2)
    return n * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[unit]


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def content_disposition(name):
    # HTML 用 inline（瀏覽器直接顯示，landing page 用）
    if name.lower().endswith(('.html', '.htm')):
        return 'inline'
    try:
        name.encode('ascii')
        return f'attachment; filename="{name}"'
    except UnicodeEncodeError:
        return f"attachment; filename*=UTF-8''{quote(name)}"


def guess_ct(name):
    ct, _ = mimetypes.guess_type(name)
    return ct or 'application/octet-stream'


# ── 命令 ─────────────────────────────────────────────────────────────
def cmd_publish(args):
    path = os.path.expanduser(args.file)
    if not os.path.isfile(path):
        raise SystemExit(f'檔案不存在: {path}')
    name = args.name or os.path.basename(path)
    ttl = parse_ttl(args.ttl)
    if args.key:
        key = args.key.strip('/')
        if not re.fullmatch(r'[A-Za-z0-9._/-]+', key):
            raise SystemExit(f'key 只能含字母數字 . _ / - : {key}')
    else:
        key = secrets.token_hex(8)
    published_at = now_iso()
    if ttl is None:
        expires_at = None
    else:
        expires_at = datetime.fromtimestamp(
            time.time() + ttl, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    extra = {
        'Content-Type': guess_ct(name),
        'Content-Disposition': content_disposition(name),
        'x-amz-meta-original-name': name,
        'x-amz-meta-published-at': published_at,
        'x-amz-meta-expires': expires_at or 'never',
    }
    status, body = r2_put_object(key, path, extra)
    if status != 200:
        raise SystemExit(f'R2 PUT 失敗: {status} {body[:300]}')
    m = load_manifest()
    m['files'][key] = {
        'name': name,
        'size': os.path.getsize(path),
        'published_at': published_at,
        'expires_at': expires_at,
        'url': f'{BASE_URL}/{key}',
    }
    save_manifest(m)
    print(f'url:        {BASE_URL}/{key}')
    print(f'name:       {name}')
    print(f'size:       {os.path.getsize(path):,} bytes')
    if expires_at:
        print(f'expires_at: {expires_at} (TTL {args.ttl})')
    else:
        print(f'expires_at: 永不過期 (TTL {args.ttl})')


def _fmt_file(token, f):
    if f['expires_at'] is None:
        mark = ' [永不過期]'
    elif f['expires_at'] < now_iso():
        mark = ' [已過期]'
    else:
        mark = ''
    exp = f['expires_at'] or 'never'
    return (f"{token}  {f['name'][:40]:40} {f['size']:>12,} B  "
            f"到期 {exp}{mark}")


def cmd_list(args):
    m = load_manifest()
    files = m['files']
    if not files:
        print('（沒有已發布的檔案）')
        return
    for token, f in sorted(files.items(), key=lambda x: x[1]['published_at'], reverse=True):
        print(_fmt_file(token, f))
    print(f'\n共 {len(files)} 個檔案')


def cmd_info(args):
    m = load_manifest()
    f = m['files'].get(args.token)
    if not f:
        raise SystemExit(f'找不到 token: {args.token}')
    print(json.dumps({args.token: f}, indent=2, ensure_ascii=False))


def cmd_delete(args):
    m = load_manifest()
    if args.token not in m['files']:
        raise SystemExit(f'找不到 token: {args.token}（可能已過期被 cleanup）')
    name = m['files'][args.token]['name']
    status, body = r2_delete_object(args.token)
    if status not in (200, 204):
        raise SystemExit(f'R2 DELETE 失敗: {status} {body[:300]}')
    del m['files'][args.token]
    save_manifest(m)
    print(f'已刪除: {args.token} ({name})')
    print(f'剩餘 {len(m["files"])} 個檔案')


def cmd_renew(args):
    m = load_manifest()
    f = m['files'].get(args.token)
    if not f:
        raise SystemExit(f'找不到 token: {args.token}')
    ttl = parse_ttl(args.ttl)
    if ttl is None:
        expires_at = None
    else:
        expires_at = datetime.fromtimestamp(
            time.time() + ttl, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    extra = {
        'Content-Type': guess_ct(f['name']),
        'Content-Disposition': content_disposition(f['name']),
        'x-amz-meta-original-name': f['name'],
        'x-amz-meta-published-at': f['published_at'],
        'x-amz-meta-expires': expires_at or 'never',
    }
    status, body = r2_copy_replace_metadata(args.token, extra)
    if status not in (200, 201):
        print(f'警告: R2 metadata 更新失敗 ({status})，只更新 manifest')
    f['expires_at'] = expires_at
    save_manifest(m)
    if expires_at:
        print(f'已延長: {args.token} → 到期 {expires_at} (TTL {args.ttl})')
    else:
        print(f'已改為永不過期: {args.token} (TTL {args.ttl})')


def cmd_cleanup(args):
    m = load_manifest()
    now = now_iso()
    expired = [t for t, f in m['files'].items()
               if f['expires_at'] is not None and f['expires_at'] < now]
    for t in expired:
        status, body = r2_delete_object(t)
        name = m['files'][t]['name']
        if status in (200, 204):
            print(f'刪除過期: {t} ({name})')
            del m['files'][t]
        else:
            print(f'刪除失敗: {t} ({name}) -> {status} {body[:200]}')
    if expired:
        save_manifest(m)
    print(f'cleanup 完成：刪 {len(expired)} 個，剩 {len(m["files"])} 個')


def cmd_status(args):
    if not CF.get('CLOUDFLARE_API_TOKEN'):
        print('（無 Cloudflare API token，略過 domain 狀態）')
        return
    import urllib.request
    acc = CF['CLOUDFLARE_ACCOUNT_ID']
    url = (f'https://api.cloudflare.com/client/v4/accounts/{acc}'
           f'/r2/buckets/{BUCKET}/domains/custom/{DOMAIN}')
    req = urllib.request.Request(
        url, headers={'Authorization': f'Bearer {CF["CLOUDFLARE_API_TOKEN"]}'})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    if not d.get('success'):
        print('domain 狀態查詢失敗:', d.get('errors'))
        return
    res = d['result']
    print(f'bucket: {BUCKET}')
    print(f'domain: {res["domain"]}')
    print(f'enabled: {res["enabled"]}')
    print(f'status:  ownership={res["status"]["ownership"]} ssl={res["status"]["ssl"]}')
    m = load_manifest()
    print(f'已發布檔案: {len(m["files"])} 個')


def main():
    p = argparse.ArgumentParser(description='meow-share 管理 CLI')
    sub = p.add_subparsers(dest='cmd', required=True)

    sp = sub.add_parser('publish', help='發布檔案到公網')
    sp.add_argument('file')
    sp.add_argument('--ttl', default=DEFAULT_TTL, help=f'有效期（預設 {DEFAULT_TTL}，never=永不過期）')
    sp.add_argument('--name', help='下載時顯示的檔名（預設用原檔名）')
    sp.add_argument('--key', help='自訂 object key（如 index.html；預設隨機 token）')
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser('list', help='列出已發布檔案')
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser('info', help='單一檔案詳情')
    sp.add_argument('token')
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser('delete', help='刪除檔案')
    sp.add_argument('token')
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser('renew', help='延長到期日')
    sp.add_argument('token')
    sp.add_argument('--ttl', default=DEFAULT_TTL)
    sp.set_defaults(func=cmd_renew)

    sp = sub.add_parser('cleanup', help='刪除所有過期檔案')
    sp.set_defaults(func=cmd_cleanup)

    sp = sub.add_parser('status', help='bucket/domain 狀態')
    sp.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
