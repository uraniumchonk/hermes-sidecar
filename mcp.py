#!/usr/bin/env python3
"""
fileshare MCP Server - LAN 檔案分享 + meow-share 公網分享上傳工具（純標準庫零依賴）
透過 stdio 傳輸，可直接整合到 Hermes MCP 框架

只有一個工具：
  share_file(path, public=False)
    上傳本機檔案到 fileshare 服務（自動換成 uuid 檔名）
    public=False：回傳 LAN URL（僅家庭網路可存取）
    public=True ：發布到 meow-share（R2 public bucket），回傳短公網 URL
                  （share.thomas2018yulab.uk/<token>，7 天過期，任何網路可存取）

部署：
  meowhome（服務本機）：  python3 mcp.py
  meowplace（遠端上傳）： FILESHARE_URL=http://192.168.0.160:18778 python3 mcp.py
  （public=True 只在 meowhome 可用，需要本機 share_cli.py + r2.env 憑證）

回傳的 URL 可直接包進 markdown：
  圖片：![img](<url>)
  檔案：[檔名](<url>)
"""
import http.client
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get('FILESHARE_URL', 'http://127.0.0.1:18778')
MAX_SIZE = 1024 * 1024 * 1024  # 1GB（與伺服器端一致，2026-08-30 由 50MB 調高）
UPLOAD_TIMEOUT = 600  # 秒；1GB 走 LAN 也要留足餘裕
SHARE_CLI = os.path.expanduser('~/hermes-sidecar/share_cli.py')
R2_ENV = os.path.expanduser('~/hermes-sidecar/r2.env')  # R2 憑證（gitignore，只在 meowhome）


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def share_file(path: str, public: bool = False) -> dict:
    """上傳本機檔案到 fileshare 服務，回傳 URL。

    public=False：回傳 LAN URL（僅家庭網路可存取）
    public=True ：發布到 meow-share（R2 public bucket），回傳短公網 URL
                  （share.thomas2018yulab.uk/<token>，7 天過期，任何網路可存取）
    """
    if not os.path.isfile(path):
        return _err(f"檔案不存在: {path}")
    size = os.path.getsize(path)
    if size > MAX_SIZE:
        return _err(f"檔案過大（{size} bytes，上限 {MAX_SIZE}）")

    if public:
        # meow-share：R2 public bucket + custom domain（短 URL、定時過期）
        if not os.path.isfile(SHARE_CLI) or not os.path.isfile(R2_ENV):
            return _err(f"meow-share 不可用：share_cli.py 或 r2.env 不存在"
                        f"（public=True 只在 meowhome 可用，請改用 LAN 分享或找 meowhome 的 agent）")
        try:
            out = subprocess.run(
                [sys.executable, SHARE_CLI, 'publish', path, '--ttl', '7d'],
                capture_output=True, text=True, timeout=UPLOAD_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as e:
            return _err(f"meow-share publish 失敗: {e}")
        if out.returncode != 0:
            return _err(f"meow-share publish 失敗: {out.stderr.strip()[:300]}")
        url = ''
        for line in out.stdout.splitlines():
            if line.startswith('url:'):
                url = line.split('url:', 1)[1].strip()
        if not url:
            return _err(f"meow-share 回應缺少 url: {out.stdout[:300]}")
        return {"content": [{"type": "text", "text": url}]}

    name = os.path.basename(path)
    # 串流上傳（file-like body + 明確 Content-Length），不把整個檔案吃進記憶體
    parsed = urllib.parse.urlparse(BASE_URL)
    if not parsed.hostname:
        return _err(f"FILESHARE_URL 設定無效: {BASE_URL}")
    query = f'/upload?name={urllib.parse.quote(name)}'
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=UPLOAD_TIMEOUT)
    try:
        with open(path, 'rb') as f:
            conn.request(
                'POST',
                query,
                body=f,
                headers={'Content-Length': str(size)},
            )
            resp = conn.getresponse()
            result = json.loads(resp.read())
    except (OSError, ValueError) as e:
        return _err(f"上傳失敗: {e}（fileshare 服務是否運行？{BASE_URL}）")
    finally:
        conn.close()

    url = result.get("url", "")
    if not url:
        return _err(f"伺服器回應缺少 url: {result}")
    return {"content": [{"type": "text", "text": url}]}


# MCP 工具定義
TOOLS = [
    {
        "name": "share_file",
        "description": "上傳本機檔案到 fileshare 服務，回傳可分享的 URL。傳入檔案的絕對路徑；回傳的 URL 可直接包進 markdown（圖片用 ![img](url)、文字/程式碼用 [檔名](url)）。檔案自動換成 uuid 檔名，7 天後自動清理。public=False 回傳 LAN URL（僅家庭網路）；public=True 發布到 meow-share 回傳短公網 URL（share.thomas2018yulab.uk/<token>，7 天過期，任何網路可存取）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要上傳的本機檔案絕對路徑"},
                "public": {
                    "type": "boolean",
                    "description": "是否回傳公網 URL（meow-share 短連結，預設 false 回傳 LAN URL）",
                },
            },
            "required": ["path"],
        },
    },
]

# 工具函數映射
TOOL_FUNCTIONS = {
    "share_file": share_file,
}


def main():
    """MCP stdio 伺服器主迴圈。

    關鍵點（meowchat MCP 的教訓，2026-08-26）：
    - Hermes MCP client 每 180 秒發 keepalive ping（30 秒超時），不回應就判定
      server 死掉 → 重連 → 連續 5 次失敗後 park（工具全部消失）。ping 必須立即回應。
    - tool call 丟獨立 thread 處理，主迴圈不被長上傳（最大 50MB）阻塞，
      才能繼續讀 stdin 回應 ping。
    - stdout 寫入加鎖，防多 thread 輸出交錯。
    """

    send_lock = threading.Lock()

    def send(msg: dict):
        encoded = json.dumps(msg).encode() + b"\n"
        with send_lock:
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()

    def handle_tool_call(req_id, params):
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name in TOOL_FUNCTIONS:
            try:
                result = TOOL_FUNCTIONS[tool_name](**arguments)
                send({"jsonrpc": "2.0", "id": req_id, "result": result})
            except Exception as e:
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"錯誤: {str(e)}"}],
                        "isError": True,
                    },
                })
        else:
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"未知工具: {tool_name}"}],
                    "isError": True,
                },
            })

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fileshare", "version": "1.1.0"},
                },
            })

        elif method == "ping":
            # keepalive：必須立即回應，否則 client 判定 server 死掉
            send({"jsonrpc": "2.0", "id": req_id, "result": {}})

        elif method == "tools/list":
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
            })

        elif method == "tools/call":
            # 獨立 thread 執行，主迴圈保持讀 stdin（才能即時回應 ping）
            threading.Thread(
                target=handle_tool_call,
                args=(req_id, params),
                daemon=True,
            ).start()

        else:
            # 忽略未知方法（包括 notifications/initialized）
            pass


if __name__ == "__main__":
    main()
