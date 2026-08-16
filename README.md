# Hermes Sidecar

A browser side panel for [Hermes Agent](https://hermes-agent.nousresearch.com/):
a lightweight Chrome / Edge extension that keeps your Hermes agent docked to
the right side of the browser via the native Side Panel API.

The name comes from the motorcycle sidecar — it rides along next to you while
you browse.

## Features

- Chat with markdown rendering, SSE streaming, and in-order tool call cards
- Capture button: screenshot the current page, upload to your server, inject
  the server path into the message, and let the agent read it itself
- Read page button: grab the visible page text (prefixed with the URL) and
  send it to the agent
- Clipboard images: paste (Ctrl+V) an image anywhere in the panel — it goes
  through the same upload-and-buffer flow
- Attachment buffer: screenshots and page texts accumulate first and are only
  sent together with the message you type (multiple screenshots supported)
- Stop button: abort mid-stream while keeping the content produced so far
- Ultra-narrow mode: minimal spacing, wraps at any width
- Auto UI language: Traditional Chinese / English based on the browser locale
- Width is adjustable by dragging the panel edge (native side panel behavior)

## Architecture

```
Chrome/Edge side panel (extension/)
  ├─ sidepanel.html / app.js   chat UI (i18n, SSE, buffer)
  ├─ background.js             open panel on toolbar click
  └─ vendor/                   marked + DOMPurify (localized copies)
        │  HTTP (Bearer API_SERVER_KEY)
        ▼
Hermes gateway api_server  :30001/v1/chat/completions
        │  POST raw bytes (screenshots / clipboard images)
        ▼
upload_server.py :18778  → saves to uploads/ → returns absolute path
        → the path is injected into the message for the agent to read
```

## Hermes-side prerequisites

In the gateway profile's `.env`:

```
API_SERVER_ENABLED=true
API_SERVER_PORT=30001
API_SERVER_HOST=0.0.0.0          # so other machines can reach it
API_SERVER_KEY=<your key>
API_SERVER_CORS_ORIGINS=*        # allow the chrome-extension:// Origin (required)
```

Restart the gateway afterwards. Optionally restrict 30001 / 18778 with a
firewall to trusted sources only.

## Installation

### Extension

1. Open `chrome://extensions` (or `edge://extensions`)
2. Enable Developer mode → Load unpacked → select the `extension/` folder
3. Click the toolbar icon to open the side panel. On first launch the settings
   dialog opens automatically: fill in endpoint, API key, and model
4. The `+` button in the top-right corner starts a new chat; double-click it
   to reopen settings

### upload_server (attachment receiver)

```bash
python3 upload_server.py --port 18778 --dir ~/hermes-sidecar/uploads
```

Example systemd unit (binds 0.0.0.0, with an application-level IP allowlist):

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

The allowlist lives in `ALLOWED_CLIENTS` inside `upload_server.py`
(defaults to 192.168.0.10 / 127.0.0.1 — adjust for your LAN).

## Settings

| Field | Default | Notes |
|-------|---------|-------|
| Endpoint | `http://192.168.0.160:30001/v1/chat/completions` | Hermes gateway |
| API Key | empty | gateway `API_SERVER_KEY` |
| Model | `qwen-27b-default` | any model name the gateway accepts |
| Upload endpoint | `http://192.168.0.160:18778/upload` | attachment receiver |

## License

MIT. `extension/vendor/` bundles local copies of
[marked](https://github.com/markedjs/marked) (MIT) and
[DOMPurify](https://github.com/cure53/DOMPurify) (Apache-2.0).
