#!/usr/bin/env python3
"""
fileshare MCP Server - LAN 檔案分享上傳工具（純標準庫零依賴）
透過 stdio 傳輸，可直接整合到 Hermes MCP 框架

只有一個工具：
  share_file(path)  上傳本機檔案到 fileshare 服務（自動換成 uuid 檔名），回傳 LAN URL

部署：
  meowhome（服務本機）：  python3 mcp.py
  meowplace（遠端上傳）： FILESHARE_URL=http://192.168.0.160:18778 python3 mcp.py

回傳的 URL 可直接包進 markdown：
  圖片：![img](http://192.168.0.160:18778/files/<uuid>.jpg)
  檔案：[檔名](http://192.168.0.160:18778/files/<uuid>.txt)
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get('FILESHARE_URL', 'http://127.0.0.1:18778')
MAX_SIZE = 50 * 1024 * 1024  # 與伺服器端一致


def share_file(path: str) -> dict:
    """上傳本機檔案到 LAN 檔案分享，回傳 LAN URL。"""
    if not os.path.isfile(path):
        return {"content": [{"type": "text", "text": f"檔案不存在: {path}"}], "isError": True}
    size = os.path.getsize(path)
    if size > MAX_SIZE:
        return {"content": [{"type": "text", "text": f"檔案過大（{size} bytes，上限 {MAX_SIZE}）"}], "isError": True}

    name = os.path.basename(path)
    with open(path, 'rb') as f:
        data = f.read()
    req = urllib.request.Request(
        f"{BASE_URL}/upload?name={urllib.parse.quote(name)}",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.URLError as e:
        return {"content": [{"type": "text", "text": f"上傳失敗: {e}（fileshare 服務是否運行？{BASE_URL}）"}], "isError": True}

    url = result.get("url", "")
    if not url:
        return {"content": [{"type": "text", "text": f"伺服器回應缺少 url: {result}"}], "isError": True}
    return {"content": [{"type": "text", "text": url}]}


# MCP 工具定義
TOOLS = [
    {
        "name": "share_file",
        "description": "上傳本機檔案到 LAN 檔案分享，回傳 LAN URL。傳入檔案的絕對路徑；回傳的 URL 可直接包進 markdown（圖片用 ![img](url)、文字/程式碼用 [檔名](url)）。檔案自動換成 uuid 檔名，7 天後自動清理。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要上傳的本機檔案絕對路徑"},
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
    """MCP stdio 伺服器主迴圈。"""

    def send(msg: dict):
        encoded = json.dumps(msg).encode() + b"\n"
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()

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
                    "serverInfo": {"name": "fileshare", "version": "1.0.0"},
                },
            })

        elif method == "tools/list":
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
            })

        elif method == "tools/call":
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

        else:
            # 忽略未知方法（包括 notifications/initialized）
            pass


if __name__ == "__main__":
    main()
