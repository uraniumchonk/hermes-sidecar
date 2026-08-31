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

### upload_server (attachment receiver + LAN file share + R2 public share)

```bash
python3 upload_server.py --port 18778 --dir ~/agent-sidepanel/uploads
```

Options:

| Flag | Default | Notes |
|------|---------|-------|
| `--port` | `18778` | listen port |
| `--dir` | `~/agent-sidepanel/uploads` | storage directory |
| `--public-host` | `192.168.0.160` | host used in returned URLs |
| `--max-age-days` | `7` | auto-delete files older than N days (0 = never) |
| `--selftest` | — | run SigV4 test vectors and exit (no R2 creds needed) |

Files are stored with a **uuid filename** (collision-free, no management
needed — just `cp` over the directory or re-upload freely). A background
thread prunes files older than `--max-age-days` every hour (and the matching
R2 object, if public upload is enabled).

Endpoints:

```
POST /upload?name=<filename>[&public=1]   body = raw bytes
     → {"path": "/abs/path", "url": "http://192.168.0.160:18778/files/<uuid>.ext"
        [, "public_url": "https://<acct>.r2.cloudflarestorage.com/<bucket>/<uuid>.ext?X-Amz-..."]}
GET  /health                  → {"ok": true, "r2": <bool>}
GET  /files/<name>            → file bytes with correct Content-Type
```

`GET /files` is open to the whole LAN (192.168.0.0/24 + localhost) and sends
the proper `Content-Type`, so images render directly in markdown:

```markdown
![img](http://192.168.0.160:18778/files/<uuid>.jpg)
[notes](http://192.168.0.160:18778/files/<uuid>.txt)
```

#### R2 public share (optional)

Add `&public=1` to `POST /upload` to also stream the file to a Cloudflare R2
bucket and get back a `public_url` — a presigned GET link valid for up to 7
days (SigV4 max) that works from any network, not just the LAN. R2 egress is
free, so sharing large files is cheap.

Enable it by providing R2 credentials via environment variables (the systemd
unit loads `r2.env`):

```bash
# r2.env  (chmod 600)
R2_ACCOUNT_ID=<your account id>
R2_BUCKET=<bucket name>
R2_ACCESS_KEY=<R2 API token access key>
R2_SECRET_KEY=<R2 API token secret key>
R2_URL_EXPIRY=604800   # presigned URL lifetime in seconds (max 604800 = 7d)
```

If any of the four required vars is missing, `public` upload is disabled and
`GET /health` reports `"r2": false` — LAN sharing is unaffected. The SigV4
implementation is pure stdlib (region=`auto`, service=`s3`); verify it with
`python3 upload_server.py --selftest`.

### fileshare MCP (mcp.py)

Single tool `share_file(path, public=False)` — upload a local file, get back
a URL. `public=False` returns the LAN URL; `public=True` returns the R2
public URL (requires R2 to be configured on the server). Stdio transport,
pure stdlib, zero dependencies.

```yaml
# ~/.hermes/profiles/<p>/config.yaml
mcp_servers:
  fileshare:
    command: python3
    args:
      - /home/USER/hermes-sidecar/mcp.py
    env:
      FILESHARE_URL: http://127.0.0.1:18778   # or http://192.168.0.160:18778 for remote
    enabled: true
```

On remote machines (e.g. meowplace) point `FILESHARE_URL` at the meowhome
server so files land in the same shared directory.

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
