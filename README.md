# Hermes Sidecar

Hermes Agent 的瀏覽器側掛面板：Chrome / Edge 原生 Side Panel extension，直連
Hermes gateway 的 API Server，把 agent 常駐在瀏覽器右側。

命名由來：重型機車旁那台側掛車（sidecar）——載著你的 agent 陪你上網。

## 功能

- 聊天 + markdown 渲染 + SSE 串流 + 工具呼叫卡片（依訊息順序排列）
- 截圖按鈕：抓目前網頁畫面 → 上傳伺服器 → 路徑注入訊息 → agent 自己讀
- 讀取頁面按鈕：抓目前網頁明文文字（帶網址）→ 送給 agent
- 剪貼簿圖片：Ctrl+V 貼圖進輸入框，走同一條上傳 buffer 流程
- 附件 buffer：截圖 / 頁面文字先暫存，輸入訊息後才一起送出，可累積多張
- 停止按鈕：串流中可中止並保留已產出內容
- 極窄模式：可塞進超窄側邊欄，間距最小化
- 拖曳調寬：瀏覽器原生 side panel 支援

## 架構

```
Chrome/Edge 側邊欄 (extension/)
  ├─ sidepanel.html / app.js   聊天 UI
  ├─ background.js             點圖示開側邊欄
  └─ vendor/                   marked + DOMPurify（本地化）
        │  HTTP (Bearer API_SERVER_KEY)
        ▼
Hermes gateway api_server :30001/v1/chat/completions
        │  POST raw bytes（截圖）
        ▼
upload_server.py :18778 → 存檔 uploads/ → 回傳絕對路徑 → 路徑注入訊息
```

## 前置：Hermes 端設定

在 gateway profile 的 `.env`：

```
API_SERVER_ENABLED=true
API_SERVER_PORT=30001
API_SERVER_HOST=0.0.0.0          # 讓其他機器可連
API_SERVER_KEY=<你的 key>
API_SERVER_CORS_ORIGINS=*        # 放行 chrome-extension:// Origin（必要）
```

改完重啟 gateway。防火牆（可選）：只放行信任來源的 30001 / 18778。

## 安裝

### extension（瀏覽器）

1. 開 `chrome://extensions`（Edge 用 `edge://extensions`）
2. 開發人員模式 → 載入未封裝項目 → 選 `extension/` 資料夾
3. 點工具列圖示開側邊欄，第一次自動彈設定：填端點、API Key、模型
4. 側邊欄右上角「+」= 新對話，雙擊 = 開啟設定

### upload_server（附件接收後端）

```bash
python3 upload_server.py --port 18778 --dir ~/hermes-sidecar/uploads
```

systemd 範例（bind 0.0.0.0 + 應用層 IP 白名單）：

```ini
[Unit]
Description=Hermes Sidecar upload receiver
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/USER/hermes-sidecar/upload_server.py --port 18778
Restart=always

[Install]
WantedBy=default.target
```

白名單在 `upload_server.py` 的 `ALLOWED_CLIENTS`（預設 192.168.0.10 / 127.0.0.1）。

## 設定

| 欄位 | 預設 | 說明 |
|------|------|------|
| 端點 | `http://192.168.0.160:30001/v1/chat/completions` | Hermes gateway |
| API Key | 空 | gateway 的 API_SERVER_KEY |
| 模型 | `qwen-27b-default` | gateway 接受的模型名 |
| 上傳端點 | `http://192.168.0.160:18778/upload` | 附件接收 |

## 授權

MIT。`extension/vendor/` 內含 [marked](https://github.com/markedjs/marked)
（MIT）與 [DOMPurify](https://github.com/cure53/DOMPurify)（Apache-2.0）的
本地化副本。
