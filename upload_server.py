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
  GET  /                          內建前端（清單/搜尋/上傳/預覽/編輯，純靜態 HTML 零外部依賴）
  GET  /view/<name>               檔案檢視器（SPA 路由，回同一前端頁）
  GET  /edit/<name>               文字編輯器（SPA 路由，回同一前端頁）
  POST /upload?name=<filename>    body = raw bytes
       → {"path": "/abs/path", "url": "http://<host>/files/<uuid>.ext"}
  GET  /files?q=<搜尋>            已上傳檔案清單（JSON：name/orig/size/mtime，新到舊，上限 100 筆）
  GET  /health                    → {"ok": true}
  GET  /files/<name>              讀回已上傳檔案（LAN 存取）
       ?mode=inline   內嵌顯示（瀏覽器內建 PDF viewer、media 直接播放）
       ?mode=download 一律強制下載
       預設：圖片內嵌（markdown 用途）；其餘類型強制下載並還原原始檔名
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

FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meow File Share</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
<link rel="icon" type="image/png" sizes="48x48" href="/favicon-48.png">
<link rel="icon" type="image/png" sizes="64x64" href="/favicon-64.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<style>
:root{
  --bg-canvas:#09090b; --bg-surface:#131316; --bg-elevated:#1c1c21; --bg-raised:#26262c;
  --text-primary:#ececee; --text-secondary:#a1a1aa; --text-muted:#63636b;
  --border-subtle:rgba(255,255,255,.06); --border-default:rgba(255,255,255,.10);
  --accent:#34d399; --accent-dim:rgba(52,211,153,.14);
  --err:#f87171; --warn:#fbbf24;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --radius:8px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark}
body{
  background:var(--bg-canvas);color:var(--text-primary);min-height:100dvh;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC","Microsoft JhengHei",sans-serif;
  font-size:14px;line-height:1.6;
}
a{color:var(--accent);text-decoration:none}
button{
  background:var(--bg-elevated);color:var(--text-primary);border:1px solid var(--border-default);
  border-radius:6px;padding:5px 12px;font-size:12px;cursor:pointer;font-family:inherit;
  transition:background .15s ease-out,border-color .15s ease-out,transform .1s ease-out;
}
button:hover{background:var(--bg-raised)}
button:active{transform:scale(.97)}
button:disabled{opacity:.4;cursor:not-allowed}
button.primary{background:var(--accent-dim);border-color:rgba(52,211,153,.35);color:var(--accent)}
button.primary:hover{background:rgba(52,211,153,.22)}
input,textarea{font-family:inherit;color:var(--text-primary)}
.wrap{max-width:1000px;margin:0 auto;padding:24px 16px 60px}
header{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:18px;flex-wrap:wrap}
h1{font-size:18px;font-weight:600;letter-spacing:.3px}
.search{
  flex:1;min-width:200px;max-width:360px;background:var(--bg-surface);
  border:1px solid var(--border-default);border-radius:var(--radius);
  padding:7px 12px;font-size:13px;outline:none;
  transition:border-color .15s ease-out;
}
.search:focus{border-color:rgba(52,211,153,.4)}
.search::placeholder{color:var(--text-muted)}
.drop{
  border:1.5px dashed var(--border-default);border-radius:var(--radius);padding:18px 16px;
  text-align:center;cursor:pointer;background:var(--bg-surface);
  transition:border-color .15s ease-out,background .15s ease-out;
}
.drop:hover,.drop.over{border-color:var(--accent);background:var(--bg-elevated)}
.drop .big{font-size:13px;margin-bottom:4px}
.drop .small{font-size:11px;color:var(--text-muted)}
.queue{margin-top:12px;display:flex;flex-direction:column;gap:8px}
.item{background:var(--bg-surface);border:1px solid var(--border-subtle);border-radius:var(--radius);padding:10px 12px}
.row1{display:flex;justify-content:space-between;gap:10px;font-size:12px;margin-bottom:6px}
.name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta{color:var(--text-muted);font-size:11px;flex:none}
.bar{height:5px;background:var(--bg-elevated);border-radius:3px;overflow:hidden}
.bar>div{height:100%;width:0;background:var(--accent);transition:width .15s ease-out}
.item.err .bar>div{background:var(--err)}
.result{margin-top:8px;display:none}
.item.done .result{display:block}
.lbl{font-size:11px;color:var(--text-muted);margin:6px 0 4px}
.urlbox{display:flex;gap:8px}
.urlbox input{
  flex:1;min-width:0;background:var(--bg-canvas);border:1px solid var(--border-default);
  border-radius:6px;padding:5px 8px;font-size:11px;font-family:var(--mono);
}
.hint{font-size:11px;color:var(--text-muted);margin-top:8px;line-height:1.6}
h2{font-size:13px;color:var(--text-secondary);margin:20px 0 10px;font-weight:500;display:flex;justify-content:space-between;align-items:baseline}
h2 .count{font-size:11px;color:var(--text-muted);font-weight:400}
.card{background:var(--bg-surface);border:1px solid var(--border-subtle);border-radius:var(--radius);overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{text-align:left;padding:7px 12px;border-bottom:1px solid var(--border-subtle)}
tr:last-child td{border-bottom:none}
th{color:var(--text-muted);font-weight:400;font-size:11px}
tbody tr{cursor:pointer;transition:background .12s ease-out}
tbody tr:hover{background:var(--bg-elevated)}
.mono{font-family:var(--mono);max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ops{white-space:nowrap}
.ops button{padding:3px 9px;font-size:11px}
a.open{color:var(--accent);font-size:11px;margin-left:6px}
.empty{color:var(--text-muted);font-size:12px;padding:16px;text-align:center}
.badge{
  display:inline-block;font-family:var(--mono);font-size:10px;font-weight:600;
  padding:1px 6px;border-radius:4px;margin-right:8px;vertical-align:1px;
  background:var(--bg-raised);color:var(--text-secondary);letter-spacing:.5px;
}
.badge.b-img{color:#7dd3fc;background:rgba(125,211,252,.12)}
.badge.b-vid{color:#c4b5fd;background:rgba(196,181,253,.12)}
.badge.b-aud{color:#f9a8d4;background:rgba(249,168,212,.12)}
.badge.b-pdf{color:#fca5a5;background:rgba(252,165,165,.12)}
.badge.b-md{color:var(--accent);background:var(--accent-dim)}
.badge.b-zip{color:var(--warn);background:rgba(251,191,36,.12)}
/* ── viewer ── */
.vbar{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.vbar .back{padding:4px 10px}
.vbar .vname{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:46vw}
.vbar .vmeta{font-size:11px;color:var(--text-muted);flex:none}
.vbar .vops{margin-left:auto;display:flex;gap:8px;flex:none}
.stage{
  background:var(--bg-surface);border:1px solid var(--border-subtle);border-radius:var(--radius);
  min-height:320px;display:flex;align-items:center;justify-content:center;overflow:hidden;
  position:relative;
}
.stage img{max-width:100%;max-height:70vh;display:block;cursor:grab;user-select:none;-webkit-user-drag:none}
.stage img:active{cursor:grabbing}
.stage .media{width:100%;max-height:70vh;display:block;background:#000}
.stage .pdfbox{width:100%;height:75vh;border:none;display:block}
.stage .fallback{padding:40px;text-align:center;color:var(--text-muted);font-size:13px}
.stage .fallback .big{font-size:15px;color:var(--text-secondary);margin-bottom:8px}
.code-scroll{overflow:auto;max-height:75vh;background:var(--bg-surface);border:1px solid var(--border-subtle);border-radius:var(--radius)}
.code-inner{display:flex;min-width:max-content}
.gutter{
  flex:none;text-align:right;padding:14px 10px;user-select:none;
  color:var(--text-muted);background:rgba(255,255,255,.02);border-right:1px solid var(--border-subtle);
}
pre.code{margin:0;padding:14px 18px;font-family:var(--mono);font-size:12.5px;line-height:1.65;overflow-x:auto}
.gutter,pre.code{white-space:pre}
.tk-c{color:#6b7280;font-style:italic}
.tk-s{color:#86efac}
.tk-n{color:#fbbf24}
.tk-k{color:#7dd3fc}
.tk-t{color:#c4b5fd}
/* markdown */
.md{padding:24px 28px;max-width:860px;margin:0 auto;line-height:1.75;font-size:14px;overflow-x:auto}
.md h1{font-size:24px;font-weight:700;margin:20px 0 12px;line-height:1.3}
.md h2{font-size:19px;font-weight:650;margin:18px 0 10px}
.md h3{font-size:16px;font-weight:600;margin:16px 0 8px}
.md h4,.md h5,.md h6{font-size:14px;font-weight:600;margin:14px 0 6px;color:var(--text-secondary)}
.md p{margin:0 0 12px}
.md ul,.md ol{margin:0 0 12px;padding-left:24px}
.md li{margin:3px 0}
.md blockquote{border-left:3px solid var(--border-default);padding:4px 14px;margin:0 0 12px;color:var(--text-secondary);background:rgba(255,255,255,.02)}
.md code{font-family:var(--mono);font-size:12.5px;background:var(--bg-raised);padding:1px 5px;border-radius:4px}
.md pre{background:var(--bg-canvas);border:1px solid var(--border-subtle);border-radius:var(--radius);padding:14px 16px;overflow-x:auto;margin:0 0 14px}
.md pre code{background:none;padding:0}
.md table{border-collapse:collapse;margin:0 0 14px;font-size:13px}
.md th,.md td{border:1px solid var(--border-default);padding:5px 12px}
.md th{background:var(--bg-elevated);font-weight:600}
.md hr{border:none;border-top:1px solid var(--border-default);margin:20px 0}
.md img{max-width:100%;border-radius:var(--radius)}
.md a{border-bottom:1px solid rgba(52,211,153,.4)}
/* image filter toolbar */
.ftool{display:flex;flex-wrap:wrap;gap:14px;align-items:center;background:var(--bg-surface);border:1px solid var(--border-subtle);border-radius:var(--radius);padding:12px 16px;margin-top:12px}
.fgroup{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--text-secondary)}
.fgroup input[type=range]{width:110px;accent-color:var(--accent)}
.fgroup .fval{font-family:var(--mono);font-size:11px;color:var(--text-muted);width:34px;text-align:right}
.fbtns{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap}
/* zip list */
.ziplist{width:100%;padding:8px 0}
.ziprow{display:flex;align-items:center;gap:10px;padding:6px 16px;font-size:12px;cursor:pointer;transition:background .12s ease-out}
.ziprow:hover{background:var(--bg-elevated)}
.ziprow .zn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);font-size:11.5px}
.ziprow .zs{color:var(--text-muted);font-size:11px;flex:none}
/* ── editor ── */
.edbar{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.edbar .ename{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.edbar .edstate{font-size:11px;color:var(--text-muted)}
.edbar .edstate.dirty{color:var(--warn)}
.edbar .edops{margin-left:auto;display:flex;gap:8px}
.ed-split{display:flex;gap:12px;align-items:stretch}
.ed-pane{flex:1;min-width:0;display:flex;flex-direction:column}
.ed-pane .pane-h{font-size:11px;color:var(--text-muted);margin-bottom:6px}
.ed-box{
  display:flex;position:relative;background:var(--bg-surface);
  border:1px solid var(--border-subtle);border-radius:var(--radius);overflow:hidden;
  height:min(72vh,720px);
}
.ed-gutter{
  flex:none;overflow:hidden;text-align:right;padding:14px 10px;user-select:none;
  color:var(--text-muted);background:rgba(255,255,255,.02);border-right:1px solid var(--border-subtle);
  font-family:var(--mono);font-size:12.5px;line-height:1.65;white-space:pre;
}
.ed-gutter>div{will-change:transform}
textarea.ed{
  flex:1;resize:none;border:none;outline:none;background:transparent;
  font-family:var(--mono);font-size:12.5px;line-height:1.65;padding:14px 18px;
  white-space:pre;overflow:auto;tab-size:2;
}
.ed-preview{
  background:var(--bg-surface);border:1px solid var(--border-subtle);border-radius:var(--radius);
  overflow:auto;height:min(72vh,720px);
}
.toast{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(12px);
  background:var(--bg-raised);border:1px solid var(--border-default);color:var(--text-primary);
  padding:8px 18px;border-radius:var(--radius);font-size:12px;opacity:0;pointer-events:none;
  transition:opacity .2s ease-out,transform .2s ease-out;z-index:50;
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
/* ── list toolbar ── */
.lbar{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.lbar .lmeta{font-size:11px;color:var(--text-muted)}
.lbar select{
  background:var(--bg-surface);color:var(--text-primary);border:1px solid var(--border-default);
  border-radius:6px;padding:4px 8px;font-size:12px;font-family:inherit;cursor:pointer;
}
.chip{
  background:var(--bg-surface);border:1px solid var(--border-default);color:var(--text-secondary);
  border-radius:9999px;padding:3px 12px;font-size:11px;cursor:pointer;
  transition:background .15s ease-out,color .15s ease-out,border-color .15s ease-out;
}
.chip:hover{background:var(--bg-elevated)}
.chip.on{background:var(--accent-dim);border-color:rgba(52,211,153,.4);color:var(--accent)}
.vt{display:flex;border:1px solid var(--border-default);border-radius:6px;overflow:hidden}
.vt button{border:none;border-radius:none;padding:4px 10px;font-size:11px;background:var(--bg-surface)}
.vt button.on{background:var(--accent-dim);color:var(--accent)}
.expiry{font-size:11px;color:var(--text-muted);white-space:nowrap}
.expiry.soon{color:var(--warn)}
/* grid view */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.tile{
  background:var(--bg-surface);border:1px solid var(--border-subtle);border-radius:var(--radius);
  overflow:hidden;cursor:pointer;transition:border-color .15s ease-out,background .15s ease-out;
}
.tile:hover{border-color:var(--border-default);background:var(--bg-elevated)}
.tile .thumb{height:104px;background:var(--bg-canvas);display:flex;align-items:center;justify-content:center;overflow:hidden}
.tile .thumb img{width:100%;height:100%;object-fit:cover;display:block}
.tile .thumb .tbadge{font-family:var(--mono);font-size:15px;font-weight:700;color:var(--text-muted);letter-spacing:1px}
.tile .tinfo{padding:8px 10px}
.tile .tname{font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tile .tmeta{font-size:10.5px;color:var(--text-muted);margin-top:2px;display:flex;justify-content:space-between;gap:6px}
/* ── image annotation ── */
.stage canvas.annot{position:absolute;left:50%;top:50%;pointer-events:none}
.stage canvas.annot.on{pointer-events:auto;cursor:crosshair}
.atool{display:flex;align-items:center;gap:10px;flex-wrap:wrap;width:100%;margin-top:4px;padding-top:10px;border-top:1px solid var(--border-subtle)}
.swatch{
  width:20px;height:20px;border-radius:50%;border:2px solid transparent;cursor:pointer;
  transition:transform .1s ease-out,border-color .15s ease-out;flex:none;
}
.swatch:hover{transform:scale(1.15)}
.swatch.on{border-color:var(--text-primary)}
.wbtn{padding:3px 9px;font-size:11px}
.wbtn.on{background:var(--accent-dim);border-color:rgba(52,211,153,.4);color:var(--accent)}
@media (max-width:760px){
  .ed-split{flex-direction:column}
  .vbar .vname{max-width:60vw}
  .fbtns{margin-left:0}
}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important}
}
</style>
</head>
<body>
<div class="wrap" id="app"></div>
<div class="toast" id="toast"></div>

<script>
'use strict';
// ── utils ────────────────────────────────────────────────────────────
const $app = document.getElementById('app');
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmtSize = n => n < 1024 ? n + ' B'
  : n < 1048576 ? (n/1024).toFixed(1) + ' KB'
  : n < 1073741824 ? (n/1048576).toFixed(1) + ' MB'
  : (n/1073741824).toFixed(2) + ' GB';
const fmtTime = t => new Date(t*1000).toLocaleString('zh-TW',
  {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
const fileUrl = name => '/files/' + encodeURIComponent(name);
const viewUrl = name => '/view/' + encodeURIComponent(name);
const editUrl = name => '/edit/' + encodeURIComponent(name);
const absUrl = name => location.origin + fileUrl(name);

let toastTimer = null;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 1800);
}
function copyText(text, btn) {
  const done = () => { if (btn) { const o = btn.textContent; btn.textContent = '已複製';
    setTimeout(() => btn.textContent = o, 1200); } else toast('已複製連結'); };
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
async function downloadBlob(name) {
  const r = await fetch(fileUrl(name) + '?mode=download');
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const b = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(b);
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// ── file type detection ──────────────────────────────────────────────
const EXT = {
  img: ['jpg','jpeg','png','gif','webp','bmp','svg','avif','ico'],
  vid: ['mp4','webm','mkv','mov','avi','m4v'],
  aud: ['mp3','wav','ogg','m4a','flac','aac','opus'],
  pdf: ['pdf'],
  md:  ['md','markdown'],
  zip: ['zip'],
  code: ['txt','log','csv','py','js','ts','jsx','tsx','json','yaml','yml','toml',
         'ini','conf','cfg','env','gitignore','sh','bash','zsh','bat','ps1',
         'css','html','htm','xml','c','h','cpp','hpp','rs','go','java','kt',
         'swift','sql','lua','r','php','pl','vb','srt','vtt','properties','gradle'],
};
function extOf(name) {
  const i = name.lastIndexOf('.');
  return i > 0 ? name.slice(i+1).toLowerCase() : '';
}
function kindOf(name) {
  const e = extOf(name);
  for (const k of Object.keys(EXT)) if (EXT[k].includes(e)) return k;
  return 'bin';
}
function badgeOf(name) {
  const k = kindOf(name), e = extOf(name);
  const label = {img:'IMG',vid:'VID',aud:'AUD',pdf:'PDF',md:'MD',zip:'ZIP',code:e.toUpperCase().slice(0,4)}[k] || 'BIN';
  return {label, cls: k === 'code' ? '' : 'b-' + k};
}
const TEXT_KINDS = new Set(['md','code']);

// ── routing ──────────────────────────────────────────────────────────
function route() {
  const p = location.pathname;
  if (p.startsWith('/view/')) return showViewer(decodeURIComponent(p.slice(6)));
  if (p.startsWith('/edit/')) return showEditor(decodeURIComponent(p.slice(6)));
  return showList();
}
window.addEventListener('popstate', route);

// ── list view ────────────────────────────────────────────────────────
let lastFiles = [];
const LS_STATE = {q:'', type:'all', sort:'new', latest:false, view:'list'};
const TYPE_CHIPS = [
  ['all','全部'], ['md','MD'], ['img','圖片'], ['txt','文字'],
  ['media','影音'], ['pdf','PDF'], ['zip','ZIP'], ['other','其他'],
];
function typeMatch(kind) {
  switch (LS_STATE.type) {
    case 'all': return true;
    case 'md': return kind === 'md';
    case 'img': return kind === 'img';
    case 'txt': return kind === 'code';
    case 'media': return kind === 'vid' || kind === 'aud';
    case 'pdf': return kind === 'pdf';
    case 'zip': return kind === 'zip';
    case 'other': return kind === 'bin';
  }
  return true;
}
function expiryOf(mtime) {
  const left = Math.ceil((mtime + 7 * 86400 - Date.now() / 1000) / 86400);
  return left <= 1 ? {text:'即將刪除', soon:true} : {text:'剩 ' + left + ' 天', soon:false};
}
async function showList() {
  $app.innerHTML =
    '<header><h1>Meow File Share</h1>' +
    '<input class="search" id="q" type="search" placeholder="搜尋檔案名稱…（按 / 聚焦）" autocomplete="off" value="' + esc(LS_STATE.q) + '"></header>' +
    '<div class="drop" id="drop">' +
    '<div class="big">把檔案拖進來，或點這裡選擇</div>' +
    '<div class="small">單檔最大 1GB · 限家庭網路 · 7 天後自動刪除</div>' +
    '<input type="file" id="file" multiple hidden></div>' +
    '<div class="queue" id="queue"></div>' +
    '<div class="lbar">' +
    '<span class="lmeta" id="lmeta"></span>' +
    '<span style="flex:1"></span>' +
    '<button class="chip" id="latestChip" title="同一個原始檔名只顯示最新一版">只看最新</button>' +
    '<select id="sortSel" title="排序方式">' +
    '<option value="new">最新</option><option value="old">最舊</option>' +
    '<option value="name">名稱</option><option value="size">大小</option></select>' +
    '<span class="vt"><button id="viewList" title="清單檢視">清單</button><button id="viewGrid" title="格狀檢視">格狀</button></span>' +
    '</div>' +
    '<div class="lbar" id="chips">' + TYPE_CHIPS.map(c =>
      '<button class="chip" data-type="' + c[0] + '">' + c[1] + '</button>').join('') + '</div>' +
    '<h2>檔案 <span class="count" id="count"></span></h2>' +
    '<div id="listWrap"></div>';

  const q = document.getElementById('q');
  let deb = null;
  q.addEventListener('input', () => {
    clearTimeout(deb);
    deb = setTimeout(() => { LS_STATE.q = q.value.trim(); loadFiles(); }, 200);
  });
  document.getElementById('latestChip').addEventListener('click', function () {
    LS_STATE.latest = !LS_STATE.latest;
    this.classList.toggle('on', LS_STATE.latest);
    applyList();
  });
  document.getElementById('sortSel').addEventListener('change', function () {
    LS_STATE.sort = this.value; applyList();
  });
  document.getElementById('viewList').addEventListener('click', () => setView('list'));
  document.getElementById('viewGrid').addEventListener('click', () => setView('grid'));
  document.querySelectorAll('#chips .chip').forEach(ch =>
    ch.addEventListener('click', () => {
      LS_STATE.type = ch.dataset.type;
      document.querySelectorAll('#chips .chip').forEach(c => c.classList.toggle('on', c === ch));
      applyList();
    }));
  if (!window.__listKeysBound) {
    window.__listKeysBound = true;
    document.addEventListener('keydown', e => {
      const si = document.getElementById('q');
      if (!si) return;
      if (e.key === '/' && document.activeElement !== si &&
          !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
        e.preventDefault(); si.focus();
      }
    });
  }

  const drop = document.getElementById('drop'), fi = document.getElementById('file');
  drop.addEventListener('click', () => fi.click());
  fi.addEventListener('change', () => { for (const f of fi.files) upload(f); fi.value = ''; });
  ['dragenter','dragover'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave','drop'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => { for (const f of e.dataTransfer.files) upload(f); });

  // restore control state
  document.getElementById('latestChip').classList.toggle('on', LS_STATE.latest);
  document.getElementById('sortSel').value = LS_STATE.sort;
  document.querySelectorAll('#chips .chip').forEach(c =>
    c.classList.toggle('on', c.dataset.type === LS_STATE.type));
  setView(LS_STATE.view);
  await loadFiles();
}
function setView(v) {
  LS_STATE.view = v;
  document.getElementById('viewList').classList.toggle('on', v === 'list');
  document.getElementById('viewGrid').classList.toggle('on', v === 'grid');
  applyList();
}
async function loadFiles() {
  let list;
  try {
    list = await (await fetch('/files' + (LS_STATE.q ? '?q=' + encodeURIComponent(LS_STATE.q) : ''))).json();
  } catch (e) { return; }
  lastFiles = list;
  applyList();
}
function filteredFiles() {
  let list = lastFiles.filter(it => typeMatch(kindOf(it.orig)));
  if (LS_STATE.latest) {
    const seen = new Map();
    for (const it of list) if (!seen.has(it.orig)) seen.set(it.orig, it); // 清單已新到舊
    list = [...seen.values()];
  }
  const s = LS_STATE.sort;
  if (s === 'old') list = [...list].reverse();
  else if (s === 'name') list = [...list].sort((a, b) => a.orig.localeCompare(b.orig, 'zh-Hant'));
  else if (s === 'size') list = [...list].sort((a, b) => b.size - a.size);
  return list;
}
function applyList() {
  const wrap = document.getElementById('listWrap');
  if (!wrap) return;
  const list = filteredFiles();
  const total = lastFiles.reduce((s, it) => s + it.size, 0);
  const meta = document.getElementById('lmeta');
  if (meta) meta.textContent = lastFiles.length + ' 個檔案 · 共 ' + fmtSize(total);
  const c = document.getElementById('count');
  if (c) c.textContent = list.length + ' 個' + (LS_STATE.q ? '（搜尋）' : '');
  if (!list.length) {
    wrap.innerHTML = '<div class="card"><div class="empty">尚無檔案</div></div>';
    return;
  }
  if (LS_STATE.view === 'grid') {
    wrap.innerHTML = '<div class="grid">' + list.map(it => {
      const b = badgeOf(it.orig);
      const k = kindOf(it.orig);
      const ex = expiryOf(it.mtime);
      const thumb = k === 'img'
        ? '<img loading="lazy" src="' + esc(fileUrl(it.name)) + '" alt="">'
        : '<span class="tbadge">' + b.label + '</span>';
      return '<div class="tile" data-name="' + esc(it.name) + '" title="' + esc(it.orig) + '">' +
        '<div class="thumb">' + thumb + '</div>' +
        '<div class="tinfo"><div class="tname">' + esc(it.orig) + '</div>' +
        '<div class="tmeta"><span>' + fmtSize(it.size) + '</span>' +
        '<span class="expiry' + (ex.soon ? ' soon' : '') + '">' + ex.text + '</span></div>' +
        '</div></div>';
    }).join('') + '</div>';
    wrap.querySelectorAll('.tile').forEach(t =>
      t.addEventListener('click', () => location.href = viewUrl(t.dataset.name)));
    return;
  }
  wrap.innerHTML = '<div class="card"><table>' +
    '<thead><tr><th>檔案</th><th>大小</th><th>時間</th><th>到期</th><th></th></tr></thead>' +
    '<tbody id="rows"></tbody></table></div>';
  const tb = document.getElementById('rows');
  for (const it of list) {
    const b = badgeOf(it.orig);
    const ex = expiryOf(it.mtime);
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="mono" title="' + esc(it.orig) + '"><span class="badge ' + b.cls + '">' + b.label + '</span>' +
      esc(it.orig) + '</td>' +
      '<td style="white-space:nowrap">' + fmtSize(it.size) + '</td>' +
      '<td style="white-space:nowrap;color:var(--text-secondary)">' + fmtTime(it.mtime) + '</td>' +
      '<td><span class="expiry' + (ex.soon ? ' soon' : '') + '">' + ex.text + '</span></td>' +
      '<td class="ops"><button data-act="copy" data-name="' + esc(it.name) + '">複製</button>' +
      '<a class="open" href="' + esc(fileUrl(it.name)) + '">下載</a></td>';
    tr.addEventListener('click', e => {
      if (e.target.closest('button, a')) return;
      location.href = viewUrl(it.name);
    });
    tb.appendChild(tr);
  }
  tb.querySelectorAll('button[data-act=copy]').forEach(btn =>
    btn.addEventListener('click', () => copyText(absUrl(btn.dataset.name), btn)));
}

// ── upload ───────────────────────────────────────────────────────────
function upload(file) {
  const item = document.createElement('div');
  item.className = 'item';
  item.innerHTML =
    '<div class="row1"><span class="name" title="' + esc(file.name) + '">' + esc(file.name) +
    '</span><span class="meta">' + fmtSize(file.size) + ' · 0%</span></div>' +
    '<div class="bar"><div></div></div><div class="result"></div>';
  document.getElementById('queue').prepend(item);
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
      if (data.url) {
        result.innerHTML =
          '<div class="lbl">LAN 連結</div><div class="urlbox">' +
          '<input readonly value="' + esc(data.url) + '">' +
          '<button data-copy="' + esc(data.url) + '">複製</button></div>' +
          '<div class="hint">點上方清單可開啟預覽；把連結貼給小夜她就能直接讀到檔案</div>';
        result.querySelector('button').addEventListener('click', function () {
          copyText(data.url, this);
        });
        loadFiles();
      }
    } else {
      item.classList.add('err');
      meta.textContent = '失敗 (HTTP ' + xhr.status + ')';
      result.innerHTML = '<div class="hint">' + esc(xhr.responseText.slice(0, 300)) + '</div>';
    }
  };
  xhr.onerror = () => { item.classList.add('err'); meta.textContent = '網路錯誤'; };
  xhr.send(file);
}

// ── markdown renderer (built-in, ~compact) ───────────────────────────
function inlineMD(s) {
  s = esc(s);
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return '\x00' + (codes.length - 1) + '\x00'; });
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)/g, '<img alt="$1" src="$2">');
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+[^)]*)?\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/&lt;([^\s&]+)&gt;/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/(^|[^\w])__([^_\n]+)__/g, '$1<strong>$2</strong>');
  s = s.replace(/(^|[^\w])_([^_\n]+)_/g, '$1<em>$2</em>');
  s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  s = s.replace(/\x00(\d+)\x00/g, (m, n) => '<code>' + codes[+n] + '</code>');
  return s;
}
function splitRow(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
}
function renderMD(src) {
  const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
  let html = '', i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      i++;
      const buf = [];
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      html += '<pre><code>' + esc(buf.join('\n')) + '</code></pre>';
      continue;
    }
    if (/^\s*$/.test(line)) { i++; continue; }
    const h = line.match(/^(#{1,6})\s+(.*)/);
    if (h) {
      const l = h[1].length;
      html += '<h' + l + '>' + inlineMD(h[2]) + '</h' + l + '>';
      i++; continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { html += '<hr>'; i++; continue; }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ''));
      html += '<blockquote>' + renderMD(buf.join('\n')) + '</blockquote>';
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length &&
        /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i+1]) && lines[i+1].includes('-')) {
      const header = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && !/^\s*$/.test(lines[i]))
        rows.push(splitRow(lines[i++]));
      html += '<table><thead><tr>' +
        header.map(c => '<th>' + inlineMD(c) + '</th>').join('') +
        '</tr></thead><tbody>' +
        rows.map(r => '<tr>' + r.map(c => '<td>' + inlineMD(c) + '</td>').join('') + '</tr>').join('') +
        '</tbody></table>';
      continue;
    }
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i]))
        items.push(lines[i++].replace(/^\s*([-*+]|\d+\.)\s+/, ''));
      html += (ordered ? '<ol>' : '<ul>') +
        items.map(it => '<li>' + inlineMD(it) + '</li>').join('') +
        (ordered ? '</ol>' : '</ul>');
      continue;
    }
    const buf = [line];
    i++;
    while (i < lines.length && !/^\s*$/.test(lines[i]) &&
           !/^(#{1,6}\s|```|>\s?|\s*(-{3,})\s*$|\s*([-*+]|\d+\.)\s+)/.test(lines[i]))
      buf.push(lines[i++]);
    html += '<p>' + buf.map(inlineMD).join('<br>') + '</p>';
  }
  return html;
}

// ── syntax highlighter (minimal, per-language-family) ────────────────
const NEVER = '(?!)';
const KW = {
  js: 'const|let|var|function|return|if|else|for|while|do|class|new|import|export|from|async|await|try|catch|finally|throw|switch|case|break|continue|default|typeof|instanceof|in|of|this|super|extends|static|get|set|delete|void|yield|enum|interface|type|public|private|protected|readonly|as|is|keyof|never|unknown|any|boolean|number|string|object|symbol|bigint|true|false|null|undefined|console|document|window',
  py: 'def|return|if|elif|else|for|while|class|import|from|as|with|try|except|finally|raise|pass|break|continue|lambda|yield|global|nonlocal|assert|del|in|not|and|or|is|None|True|False|self|print|async|await|match|case',
  sh: 'if|then|else|elif|fi|for|while|do|done|case|esac|function|in|return|exit|local|export|echo|set|unset|source|shift|break|continue|read|cd|ls|grep|awk|sed|find|curl|ssh|sudo|apt|pip|python3|git|docker',
  sql: 'select|from|where|insert|into|values|update|set|delete|create|table|drop|alter|add|join|left|right|inner|outer|on|as|and|or|not|in|is|null|like|between|order|by|group|having|limit|offset|distinct|count|sum|avg|min|max|primary|key|foreign|references|index|view|if|exists|case|when|then|else|end|union|all',
  css: 'color|background|margin|padding|border|display|position|width|height|font|line-height|text-align|overflow|flex|grid|top|left|right|bottom|z-index|opacity|transform|transition|animation|content|box-sizing|cursor|gap|align-items|justify-content|max-width|min-width|border-radius|box-shadow|white-space|vertical-align|letter-spacing|font-weight|font-size|background-color|border-color|outline|visibility|pointer-events|user-select|will-change|accent-color|tab-size|resize|overflow-x|overflow-y',
  yaml: 'true|false|null|yes|no|on|off',
  ini: 'true|false|null|yes|no',
  json: 'true|false|null',
};
const HL_SPECS = {
  js:   { com: '//[^\n]*|/\\*[\\s\\S]*?\\*/', str: "'(?:\\\\.|[^'\\\\\\n])*'|\"(?:\\\\.|[^\"\\\\\\n])*\"|`(?:\\\\.|[^`\\\\])*`", num: '\\b0[xXbBoO][\\da-fA-F]+\\b|\\b\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b', kw: KW.js },
  py:   { com: '#[^\n]*', str: "'''[\\s\\S]*?'''|\"\"\"[\\s\\S]*?\"\"\"|'(?:\\\\.|[^'\\\\\\n])*'|\"(?:\\\\.|[^\"\\\\\\n])*\"", num: '\\b\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b', kw: KW.py },
  sh:   { com: '#[^\n]*', str: "'(?:\\\\.|[^'\\\\\\n])*'|\"(?:\\\\.|[^\"\\\\\\n])*\"", num: '\\b\\d+\\b', kw: KW.sh },
  sql:  { com: '--[^\n]*', str: "'(?:''|[^'])*'", num: '\\b\\d[\\d_]*(?:\\.\\d+)?\\b', kw: KW.sql },
  css:  { com: '/\\*[\\s\\S]*?\\*/', str: "'[^'\\n]*'|\"[^\"\\n]*\"", num: '#[\\da-fA-F]{3,8}\\b|\\b\\d+(?:\\.\\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg|fr|ch|ex)?\\b', kw: KW.css },
  html: { com: '<!--[\\s\\S]*?-->', str: "'[^'\\n]*'|\"[^\"\\n]*\"", num: NEVER, kw: NEVER, tag: '</?[a-zA-Z][\\w-]*' },
  yaml: { com: '#[^\n]*', str: "'(?:\\\\.|[^'\\\\\\n])*'|\"(?:\\\\.|[^\"\\\\\\n])*\"", num: '\\b\\d[\\d_]*(?:\\.\\d+)?\\b', kw: KW.yaml },
  ini:  { com: '#[^\\n]*|;[^\n]*', str: "'[^'\\n]*'|\"[^\"\\n]*\"", num: '\\b\\d[\\d_]*(?:\\.\\d+)?\\b', kw: KW.ini },
  json: { com: NEVER, str: "\"(?:\\\\.|[^\"\\\\\\n])*\"", num: '\\b\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b', kw: KW.json },
  plain:{ com: NEVER, str: NEVER, num: NEVER, kw: NEVER },
};
const HL_LANG = {
  js:'js', ts:'js', jsx:'js', tsx:'js', mjs:'js', cjs:'js',
  py:'py', sh:'sh', bash:'sh', zsh:'sh', bat:'sh', ps1:'sh',
  sql:'sql', css:'css', html:'html', htm:'html', xml:'html', svg:'html',
  yaml:'yaml', yml:'yaml', toml:'ini', ini:'ini', conf:'ini', cfg:'ini',
  env:'ini', gitignore:'ini', properties:'ini',
  json:'json',
};
function highlight(code, name) {
  const lang = HL_LANG[extOf(name)] || 'plain';
  const spec = HL_SPECS[lang];
  if (spec.com === NEVER && spec.str === NEVER && spec.num === NEVER &&
      spec.kw === NEVER && !spec.tag) return esc(code);
  const parts = [
    '(' + spec.com + ')',
    '(' + spec.str + ')',
    '(' + spec.num + ')',
    spec.kw !== NEVER ? '(\\b(?:' + spec.kw + ')\\b)' : '(' + NEVER + ')',
  ];
  if (spec.tag) parts.push('(' + spec.tag + ')');
  const re = new RegExp(parts.join('|'), 'g');
  let out = '', last = 0, m;
  while ((m = re.exec(code))) {
    if (m[0].length === 0) { re.lastIndex++; continue; }
    if (m.index > last) out += esc(code.slice(last, m.index));
    let cls = 'k';
    if (m[1] !== undefined) cls = 'c';
    else if (m[2] !== undefined) cls = 's';
    else if (m[3] !== undefined) cls = 'n';
    else if (m[5] !== undefined) cls = 't';
    out += '<span class="tk-' + cls + '">' + esc(m[0]) + '</span>';
    last = re.lastIndex;
  }
  out += esc(code.slice(last));
  return out;
}

// ── viewer ───────────────────────────────────────────────────────────
async function showViewer(name) {
  let meta = lastFiles.find(f => f.name === name);
  if (!meta) {
    try {
      const list = await (await fetch('/files')).json();
      lastFiles = list;
      meta = list.find(f => f.name === name);
    } catch (e) {}
  }
  const orig = (meta && meta.orig) || name;
  const size = meta ? fmtSize(meta.size) : '';
  const kind = kindOf(name);

  $app.innerHTML =
    '<div class="vbar">' +
    '<button class="back" id="back">← 檔案清單</button>' +
    '<span class="vname" title="' + esc(orig) + '">' + esc(orig) + '</span>' +
    (size ? '<span class="vmeta">' + size + '</span>' : '') +
    '<span class="vops">' +
    '<button id="opCopy">複製連結</button>' +
    '<button id="opDl">下載</button>' +
    (TEXT_KINDS.has(kind) ? '<button id="opEdit" class="primary">編輯</button>' : '') +
    '</span></div>' +
    '<div id="vbody"><div class="stage"><div class="fallback">載入中…</div></div></div>';

  document.getElementById('back').addEventListener('click', () => history.back());
  document.getElementById('opCopy').addEventListener('click', function () { copyText(absUrl(name), this); });
  document.getElementById('opDl').addEventListener('click', () => downloadBlob(orig).catch(() => toast('下載失敗')));
  const opEdit = document.getElementById('opEdit');
  if (opEdit) opEdit.addEventListener('click', () => location.href = editUrl(name));

  const body = document.getElementById('vbody');
  const fail = msg => {
    body.innerHTML = '<div class="stage"><div class="fallback"><div class="big">無法預覽</div>' +
      esc(msg) + '<div style="margin-top:14px"><button id="fbDl">下載檔案</button></div></div></div>';
    document.getElementById('fbDl').addEventListener('click', () => downloadBlob(orig).catch(() => {}));
  };

  try {
    if (kind === 'img') return viewImage(name, orig, body);
    if (kind === 'vid' || kind === 'aud') {
      const tag = kind === 'vid' ? 'video' : 'audio';
      body.innerHTML = '<div class="stage"><' + tag + ' class="media" controls src="' + esc(fileUrl(name)) + '"></' + tag + '></div>';
      return;
    }
    if (kind === 'pdf') {
      body.innerHTML = '<div class="stage"><iframe class="pdfbox" src="' + esc(fileUrl(name) + '?mode=inline') + '"></iframe></div>';
      return;
    }
    if (kind === 'zip') return viewZip(name, orig, body);
    if (TEXT_KINDS.has(kind)) return viewText(name, orig, body);
    // binary fallback
    body.innerHTML = '<div class="stage"><div class="fallback"><div class="big">此類型沒有內建預覽</div>' +
      '直接下載開啟<div style="margin-top:14px"><button id="fbDl">下載檔案</button></div></div></div>';
    document.getElementById('fbDl').addEventListener('click', () => downloadBlob(orig).catch(() => {}));
  } catch (e) { fail(e.message || String(e)); }
}

async function fetchText(name) {
  const r = await fetch(fileUrl(name));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.text();
}

function viewText(name, orig, body) {
  fetchText(name).then(text => {
    if (text.length > 2 * 1024 * 1024) {
      body.innerHTML = '<div class="stage"><div class="fallback"><div class="big">檔案超過 2MB</div>為保持流暢不內嵌顯示，請下載開啟</div></div>';
      return;
    }
    const isMd = kindOf(name) === 'md';
    const lines = text.split('\n').length;
    const nums = Array.from({length: lines}, (_, i) => i + 1).join('\n');
    const content = isMd
      ? '<div class="md">' + renderMD(text) + '</div>'
      : '<div class="code-scroll"><div class="code-inner"><div class="gutter">' + nums + '</div><pre class="code"><code>' + highlight(text, name) + '</code></pre></div></div>';
    body.innerHTML =
      (isMd ? '<div class="vbar" style="margin-top:14px"><button id="mdToggle">切換原始碼</button><span class="vmeta">' + lines + ' 行</span></div>' : '<div class="vbar" style="margin-top:14px"><span class="vmeta">' + lines + ' 行 · ' + fmtSize(text.length) + '</span></div>') +
      '<div id="mdWrap">' + content + '</div>';
    if (isMd) {
      let raw = false;
      document.getElementById('mdToggle').addEventListener('click', function () {
        raw = !raw;
        document.getElementById('mdWrap').innerHTML = raw
          ? '<div class="code-scroll"><div class="code-inner"><div class="gutter">' + nums + '</div><pre class="code"><code>' + esc(text) + '</code></pre></div></div>'
          : '<div class="md">' + renderMD(text) + '</div>';
        this.textContent = raw ? '切換渲染' : '切換原始碼';
      });
    }
  }).catch(e => {
    body.innerHTML = '<div class="stage"><div class="fallback">讀取失敗：' + esc(e.message) + '</div></div>';
  });
}

// ── image viewer: zoom / pan / filters / export ──────────────────────
function viewImage(name, orig, body) {
  const url = fileUrl(name);
  body.innerHTML =
    '<div class="stage" id="stage"><img id="vimg" src="' + esc(url) + '" alt="' + esc(orig) + '">' +
    '<canvas id="annot" class="annot"></canvas></div>' +
    '<div class="ftool" id="ftool">' +
    fSlider('亮度', 'bright', 100, 0, 200) +
    fSlider('對比', 'contrast', 100, 0, 200) +
    fSlider('飽和', 'saturate', 100, 0, 300) +
    fSlider('模糊', 'blur', 0, 0, 10) +
    fSlider('懷舊', 'sepia', 0, 0, 100) +
    fSlider('灰階', 'gray', 0, 0, 100) +
    '<span class="fbtns">' +
    '<button id="fRot">旋轉 90°</button>' +
    '<button id="fFlipH">左右翻轉</button>' +
    '<button id="fFlipV">上下翻轉</button>' +
    '<button id="fReset">重設</button>' +
    '<button id="fFit">適應視窗</button>' +
    '<button id="fSave" class="primary">另存新檔</button>' +
    '</span>' +
    '<div class="atool">' +
    '<button id="aToggle" class="wbtn">標註</button>' +
    '<span style="width:1px;height:18px;background:var(--border-default)"></span>' +
    '<button class="wbtn on" data-tool="pen" title="自由畫筆">畫筆</button>' +
    '<button class="wbtn" data-tool="rect" title="拖出方框">方框</button>' +
    '<button class="wbtn" data-tool="ellipse" title="拖出圓圈">圓圈</button>' +
    '<button class="wbtn" data-tool="arrow" title="拖出箭頭">箭頭</button>' +
    '<button class="wbtn" data-tool="select" title="選取：拖動移動、拉角點調大小">選取</button>' +
    '<span style="width:1px;height:18px;background:var(--border-default)"></span>' +
    '<span class="swatch on" data-color="#ff5252" style="background:#ff5252" title="紅"></span>' +
    '<span class="swatch" data-color="#ffd60a" style="background:#ffd60a" title="黃"></span>' +
    '<span class="swatch" data-color="#34d399" style="background:#34d399" title="綠"></span>' +
    '<span class="swatch" data-color="#60a5fa" style="background:#60a5fa" title="藍"></span>' +
    '<span class="swatch" data-color="#ffffff" style="background:#ffffff" title="白"></span>' +
    '<span style="width:1px;height:18px;background:var(--border-default)"></span>' +
    '<button class="wbtn on" data-w="1">細</button>' +
    '<button class="wbtn" data-w="2">中</button>' +
    '<button class="wbtn" data-w="3">粗</button>' +
    '<span style="width:1px;height:18px;background:var(--border-default)"></span>' +
    '<button class="wbtn" id="aDelete" title="刪除選取的形狀">刪除</button>' +
    '<button class="wbtn" id="aClear">清除</button>' +
    '</div></div>';

  const img = document.getElementById('vimg');
  const stage = document.getElementById('stage');
  const annot = document.getElementById('annot');
  const actx = annot.getContext('2d');
  const S = { scale: 1, tx: 0, ty: 0, rot: 0, fh: false, fv: false,
              bright: 100, contrast: 100, saturate: 100, blur: 0, sepia: 0, gray: 0 };
  const unit = v => (v === 0 || v === 100) ? '' : v / 100;
  function filterCSS() {
    return 'brightness(' + (S.bright/100) + ') contrast(' + (S.contrast/100) + ') saturate(' +
      (S.saturate/100) + ')' + (S.blur ? ' blur(' + S.blur + 'px)' : '') +
      (S.sepia ? ' sepia(' + (S.sepia/100) + ')' : '') + (S.gray ? ' grayscale(' + (S.gray/100) + ')' : '');
  }
  function apply() {
    const t = 'translate(' + S.tx + 'px,' + S.ty + 'px) scale(' + S.scale + ') rotate(' + S.rot + 'deg)' +
      (S.fh ? ' scaleX(-1)' : '') + (S.fv ? ' scaleY(-1)' : '');
    img.style.transform = t;
    img.style.filter = filterCSS();
    annot.style.transform = 'translate(-50%,-50%) ' + t;
  }
  apply();

  // zoom toward cursor
  stage.addEventListener('wheel', e => {
    e.preventDefault();
    const f = e.deltaY < 0 ? 1.12 : 1/1.12;
    const ns = Math.min(8, Math.max(0.1, S.scale * f));
    const rect = stage.getBoundingClientRect();
    const cx = e.clientX - rect.left - rect.width/2;
    const cy = e.clientY - rect.top - rect.height/2;
    const k = ns / S.scale;
    S.tx = cx - k * (cx - S.tx);
    S.ty = cy - k * (cy - S.ty);
    S.scale = ns;
    apply();
  }, {passive: false});
  // pan
  let drag = null;
  stage.addEventListener('pointerdown', e => {
    drag = {x: e.clientX, y: e.clientY, tx: S.tx, ty: S.ty};
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener('pointermove', e => {
    if (!drag) return;
    S.tx = drag.tx + (e.clientX - drag.x);
    S.ty = drag.ty + (e.clientY - drag.y);
    apply();
  });
  ['pointerup','pointercancel'].forEach(ev => stage.addEventListener(ev, () => drag = null));
  stage.addEventListener('dblclick', () => {
    if (S.scale === 1) { S.scale = 2; } else { S.scale = 1; S.tx = 0; S.ty = 0; }
    apply();
  });

  // filters
  const sliders = {};
  document.querySelectorAll('#ftool input[type=range]').forEach(r => {
    sliders[r.id] = r;
    r.addEventListener('input', () => {
      S[r.id] = +r.value;
      r.parentElement.querySelector('.fval').textContent = r.value;
      apply();
    });
  });
  document.getElementById('fRot').addEventListener('click', () => { S.rot = (S.rot + 90) % 360; apply(); });
  document.getElementById('fFlipH').addEventListener('click', () => { S.fh = !S.fh; apply(); });
  document.getElementById('fFlipV').addEventListener('click', () => { S.fv = !S.fv; apply(); });
  document.getElementById('fReset').addEventListener('click', () => {
    Object.assign(S, {scale:1,tx:0,ty:0,rot:0,fh:false,fv:false,bright:100,contrast:100,saturate:100,blur:0,sepia:0,gray:0});
    for (const id in sliders) {
      sliders[id].value = +sliders[id].dataset.def;
      sliders[id].parentElement.querySelector('.fval').textContent = sliders[id].dataset.def;
    }
    apply();
  });
  document.getElementById('fFit').addEventListener('click', () => { S.scale = 1; S.tx = 0; S.ty = 0; apply(); });

  // ── annotation (drawing + shapes on image) ──
  const A = {
    on: false, color: '#ff5252', w: 1,
    tool: 'pen',        // 'pen' | 'rect' | 'ellipse' | 'arrow' | 'select'
    shapes: [],         // committed shapes (image/canvas coordinates)
    cur: null,          // shape being drawn
    sel: null,          // selected shape
    action: null,       // null | 'draw' | 'move' | 'resize'
    start: null,        // draw start point
    grab: null,         // last pointer point (for move)
    handle: -1,         // handle index being resized
  };
  function sizeAnnot() {
    if (!img.naturalWidth) return;
    annot.width = img.naturalWidth;
    annot.height = img.naturalHeight;
    renderShapes();
  }
  if (img.complete) sizeAnnot();
  else img.addEventListener('load', sizeAnnot);
  function lineW() {
    const base = Math.max(2, img.naturalWidth * 0.0015);
    return [base, base * 2, base * 4][A.w - 1];
  }
  // screen point -> image (canvas) coordinates.
  // The canvas is statically positioned at the stage CENTER (left:50%, top:50%),
  // so the reference point is the stage center, not the stage top-left.
  function toAnnot(e) {
    const sr = stage.getBoundingClientRect();
    const m = new DOMMatrix(getComputedStyle(annot).transform);
    const S = { x: sr.left + sr.width / 2, y: sr.top + sr.height / 2 };
    return new DOMPoint(e.clientX - S.x, e.clientY - S.y).matrixTransform(m.inverse());
  }
  function drawShape(ctx, s) {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    if (s.type === 'pen') {
      if (s.points.length < 2) {
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.arc(s.points[0].x, s.points[0].y, s.width / 2, 0, Math.PI * 2);
        ctx.fill();
        return;
      }
      ctx.beginPath();
      s.points.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
      ctx.stroke();
    } else if (s.type === 'rect') {
      ctx.strokeRect(s.x, s.y, s.w, s.h);
    } else if (s.type === 'ellipse') {
      ctx.beginPath();
      ctx.ellipse(s.x + s.w / 2, s.y + s.h / 2, Math.abs(s.w / 2), Math.abs(s.h / 2), 0, 0, Math.PI * 2);
      ctx.stroke();
    } else if (s.type === 'arrow') {
      ctx.beginPath();
      ctx.moveTo(s.x1, s.y1);
      ctx.lineTo(s.x2, s.y2);
      ctx.stroke();
      const ang = Math.atan2(s.y2 - s.y1, s.x2 - s.x1);
      const hl = Math.max(10, s.width * 4);
      ctx.beginPath();
      ctx.moveTo(s.x2, s.y2);
      ctx.lineTo(s.x2 - hl * Math.cos(ang - Math.PI / 6), s.y2 - hl * Math.sin(ang - Math.PI / 6));
      ctx.moveTo(s.x2, s.y2);
      ctx.lineTo(s.x2 - hl * Math.cos(ang + Math.PI / 6), s.y2 - hl * Math.sin(ang + Math.PI / 6));
      ctx.stroke();
    }
  }
  function handlesOf(s) {
    if (s.type === 'rect' || s.type === 'ellipse')
      return [[s.x, s.y], [s.x + s.w, s.y], [s.x, s.y + s.h], [s.x + s.w, s.y + s.h]];
    if (s.type === 'arrow') return [[s.x1, s.y1], [s.x2, s.y2]];
    return [];
  }
  function drawHandles(ctx, s) {
    const r = Math.max(4, s.width);
    for (const [hx, hy] of handlesOf(s)) {
      ctx.fillStyle = '#fff';
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2;
      ctx.fillRect(hx - r, hy - r, r * 2, r * 2);
      ctx.strokeRect(hx - r, hy - r, r * 2, r * 2);
    }
  }
  function renderShapes() {
    actx.clearRect(0, 0, annot.width, annot.height);
    for (const s of A.shapes) drawShape(actx, s);
    if (A.cur) drawShape(actx, A.cur);
    if (A.sel) drawHandles(actx, A.sel);
  }
  function distToSeg(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1, dy = y2 - y1, l2 = dx * dx + dy * dy;
    if (!l2) return Math.hypot(px - x1, py - y1);
    const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / l2));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }
  function hitShape(s, p) {
    const tol = Math.max(6, s.width);
    if (s.type === 'pen') {
      for (let i = 1; i < s.points.length; i++)
        if (distToSeg(p.x, p.y, s.points[i-1].x, s.points[i-1].y, s.points[i].x, s.points[i].y) <= tol) return true;
      return false;
    }
    if (s.type === 'rect')
      return p.x >= s.x - tol && p.x <= s.x + s.w + tol && p.y >= s.y - tol && p.y <= s.y + s.h + tol;
    if (s.type === 'ellipse') {
      const rx = Math.abs(s.w / 2) + tol, ry = Math.abs(s.h / 2) + tol;
      return ((p.x - s.x - s.w/2) / rx) ** 2 + ((p.y - s.y - s.h/2) / ry) ** 2 <= 1;
    }
    if (s.type === 'arrow') return distToSeg(p.x, p.y, s.x1, s.y1, s.x2, s.y2) <= tol;
    return false;
  }
  function hitHandle(s, p) {
    const r = Math.max(8, s.width * 2);
    const hs = handlesOf(s);
    for (let i = 0; i < hs.length; i++)
      if (Math.hypot(p.x - hs[i][0], p.y - hs[i][1]) <= r) return i;
    return -1;
  }
  function moveShape(s, dx, dy) {
    if (s.type === 'pen') s.points.forEach(p => { p.x += dx; p.y += dy; });
    else if (s.type === 'rect' || s.type === 'ellipse') { s.x += dx; s.y += dy; }
    else if (s.type === 'arrow') { s.x1 += dx; s.y1 += dy; s.x2 += dx; s.y2 += dy; }
  }
  function resizeShape(s, hi, p) {
    if (s.type === 'rect' || s.type === 'ellipse') {
      const x0 = s.x, y0 = s.y, x1 = s.x + s.w, y1 = s.y + s.h;
      let nx = x0, ny = y0, nx1 = x1, ny1 = y1;
      if (hi === 0) { nx = p.x; ny = p.y; }
      else if (hi === 1) { nx1 = p.x; ny = p.y; }
      else if (hi === 2) { nx = p.x; ny1 = p.y; }
      else { nx1 = p.x; ny1 = p.y; }
      s.x = Math.min(nx, nx1); s.y = Math.min(ny, ny1);
      s.w = Math.abs(nx1 - nx); s.h = Math.abs(ny1 - ny);
    } else if (s.type === 'arrow') {
      if (hi === 0) { s.x1 = p.x; s.y1 = p.y; }
      else { s.x2 = p.x; s.y2 = p.y; }
    }
  }
  annot.addEventListener('pointerdown', e => {
    if (!A.on) return;
    e.preventDefault();
    e.stopPropagation();
    const p = toAnnot(e);
    try { annot.setPointerCapture(e.pointerId); } catch (_) {}
    if (A.tool === 'select') {
      if (A.sel && hitHandle(A.sel, p) >= 0) {
        A.action = 'resize';
        A.handle = hitHandle(A.sel, p);
        return;
      }
      for (let i = A.shapes.length - 1; i >= 0; i--) {
        if (hitShape(A.shapes[i], p)) {
          A.sel = A.shapes[i];
          A.action = 'move';
          A.grab = p;
          renderShapes();
          return;
        }
      }
      A.sel = null;
      renderShapes();
      return;
    }
    // drawing tools
    A.action = 'draw';
    A.start = p;
    const w = lineW();
    if (A.tool === 'pen') A.cur = { type: 'pen', points: [p], color: A.color, width: w };
    else if (A.tool === 'rect') A.cur = { type: 'rect', x: p.x, y: p.y, w: 0, h: 0, color: A.color, width: w };
    else if (A.tool === 'ellipse') A.cur = { type: 'ellipse', x: p.x, y: p.y, w: 0, h: 0, color: A.color, width: w };
    else if (A.tool === 'arrow') A.cur = { type: 'arrow', x1: p.x, y1: p.y, x2: p.x, y2: p.y, color: A.color, width: w };
    A.sel = null;
    renderShapes();
  });
  annot.addEventListener('pointermove', e => {
    if (!A.on || !A.action) return;
    const p = toAnnot(e);
    if (A.action === 'draw' && A.cur) {
      if (A.cur.type === 'pen') A.cur.points.push(p);
      else if (A.cur.type === 'rect' || A.cur.type === 'ellipse') {
        A.cur.x = Math.min(A.start.x, p.x);
        A.cur.y = Math.min(A.start.y, p.y);
        A.cur.w = Math.abs(p.x - A.start.x);
        A.cur.h = Math.abs(p.y - A.start.y);
      } else if (A.cur.type === 'arrow') { A.cur.x2 = p.x; A.cur.y2 = p.y; }
      renderShapes();
    } else if (A.action === 'move' && A.sel) {
      moveShape(A.sel, p.x - A.grab.x, p.y - A.grab.y);
      A.grab = p;
      renderShapes();
    } else if (A.action === 'resize' && A.sel) {
      resizeShape(A.sel, A.handle, p);
      renderShapes();
    }
  });
  ['pointerup','pointercancel'].forEach(ev => annot.addEventListener(ev, () => {
    if (A.action === 'draw' && A.cur) {
      const s = A.cur;
      let ok = false;
      if (s.type === 'pen') ok = s.points.length > 2;
      else if (s.type === 'rect' || s.type === 'ellipse') ok = s.w > 4 && s.h > 4;
      else if (s.type === 'arrow') ok = Math.hypot(s.x2 - s.x1, s.y2 - s.y1) > 6;
      if (ok) A.shapes.push(s);
      A.cur = null;
    }
    A.action = null;
    A.handle = -1;
    renderShapes();
  }));
  document.getElementById('aToggle').addEventListener('click', function () {
    A.on = !A.on;
    this.classList.toggle('on', A.on);
    annot.classList.toggle('on', A.on);
  });
  document.querySelectorAll('#ftool .swatch').forEach(s => s.addEventListener('click', () => {
    A.color = s.dataset.color;
    document.querySelectorAll('#ftool .swatch').forEach(x => x.classList.toggle('on', x === s));
  }));
  document.querySelectorAll('#ftool .wbtn[data-w]').forEach(b => b.addEventListener('click', () => {
    A.w = +b.dataset.w;
    document.querySelectorAll('#ftool .wbtn[data-w]').forEach(x => x.classList.toggle('on', x === b));
  }));
  document.querySelectorAll('#ftool .wbtn[data-tool]').forEach(b => b.addEventListener('click', () => {
    A.tool = b.dataset.tool;
    document.querySelectorAll('#ftool .wbtn[data-tool]').forEach(x => x.classList.toggle('on', x === b));
  }));
  document.getElementById('aDelete').addEventListener('click', () => {
    if (A.sel) {
      A.shapes.splice(A.shapes.indexOf(A.sel), 1);
      A.sel = null;
      renderShapes();
    }
  });
  document.getElementById('aClear').addEventListener('click', () => {
    A.shapes = [];
    A.sel = null;
    A.cur = null;
    renderShapes();
  });

  // export as new copy via canvas
  document.getElementById('fSave').addEventListener('click', async () => {
    try {
      const w = img.naturalWidth, h = img.naturalHeight;
      const swap = S.rot % 180 !== 0;
      const canvas = document.createElement('canvas');
      canvas.width = swap ? h : w;
      canvas.height = swap ? w : h;
      const ctx = canvas.getContext('2d');
      const filterSupported = typeof ctx.filter === 'string';
      if (filterSupported) ctx.filter = filterCSS();
      ctx.translate(canvas.width/2, canvas.height/2);
      ctx.rotate(S.rot * Math.PI/180);
      ctx.scale(S.fh ? -1 : 1, S.fv ? -1 : 1);
      ctx.drawImage(img, -w/2, -h/2);
      if (A.shapes.length) {
        ctx.save();
        ctx.translate(-w/2, -h/2);
        for (const s of A.shapes) drawShape(ctx, s);
        ctx.restore();
      }
      const isJpeg = /jpe?g/i.test(extOf(orig));
      const blob = await new Promise(res =>
        canvas.toBlob(res, isJpeg ? 'image/jpeg' : 'image/png', 0.92));
      if (!blob) throw new Error('canvas 匯出失敗');
      const base = orig.replace(/\.[^.]+$/, '');
      const newName = base + '_edited' + (isJpeg ? '.jpg' : '.png');
      const r = await fetch('/upload?name=' + encodeURIComponent(newName), {
        method: 'POST', body: blob,
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      toast(filterSupported ? '已另存新檔' : '已另存（此瀏覽器不支援濾鏡匯出）');
      showSaved(data.url, newName);
    } catch (e) { toast('匯出失敗：' + e.message); }
  });
}
function fSlider(label, id, def, min, max) {
  return '<span class="fgroup">' + label +
    '<input type="range" id="' + id + '" data-def="' + def + '" min="' + min + '" max="' + max + '" value="' + def + '">' +
    '<span class="fval">' + def + '</span></span>';
}
function showSaved(url, newName) {
  const w = document.createElement('div');
  w.style.cssText = 'margin-top:12px;background:var(--bg-surface);border:1px solid rgba(52,211,153,.3);border-radius:var(--radius);padding:12px 16px;font-size:12px';
  w.innerHTML = '<span style="color:var(--accent)">已另存：</span><span class="mono" style="max-width:none">' + esc(newName) + '</span>' +
    '<div class="urlbox" style="margin-top:8px"><input readonly value="' + esc(url) + '"><button id="svCopy">複製</button></div>';
  $app.appendChild(w);
  w.querySelector('#svCopy').addEventListener('click', function () { copyText(url, this); });
}

// ── zip viewer (native DecompressionStream) ──────────────────────────
async function viewZip(name, orig, body) {
  const r = await fetch(fileUrl(name));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const buf = new Uint8Array(await r.arrayBuffer());
  const entries = parseZip(buf);
  if (!entries.length) throw new Error('找不到 zip 目錄');
  const list = document.createElement('div');
  list.className = 'ziplist';
  list.innerHTML = '<div class="vbar" style="margin:0 0 8px"><span class="vmeta">' + entries.length + ' 個項目</span></div>';
  for (const en of entries) {
    if (en.dir) continue;
    const row = document.createElement('div');
    row.className = 'ziprow';
    row.innerHTML = '<span class="zn">' + esc(en.name) + '</span><span class="zs">' + fmtSize(en.size) + '</span>';
    row.addEventListener('click', () => previewZipEntry(name, orig, en, buf));
    list.appendChild(row);
  }
  body.innerHTML = '';
  body.appendChild(list);
}
function parseZip(buf) {
  // EOCD scan
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65558); i--) {
    if (buf[i] === 0x50 && buf[i+1] === 0x4b && buf[i+2] === 0x05 && buf[i+3] === 0x06) { eocd = i; break; }
  }
  if (eocd < 0) return [];
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  const count = dv.getUint16(eocd + 10, true);
  const off = dv.getUint32(eocd + 16, true);
  const out = [];
  let p = off;
  for (let n = 0; n < count; n++) {
    if (dv.getUint32(p, true) !== 0x02014b50) break;
    const method = dv.getUint16(p + 10, true);
    const csize = dv.getUint32(p + 20, true);
    const usize = dv.getUint32(p + 24, true);
    const nlen = dv.getUint16(p + 28, true);
    const elen = dv.getUint16(p + 30, true);
    const clen = dv.getUint16(p + 32, true);
    const lfh = dv.getUint32(p + 42, true);
    const ename = new TextDecoder().decode(buf.subarray(p + 46, p + 46 + nlen));
    out.push({name: ename, dir: ename.endsWith('/'), size: usize, method, csize, lfh});
    p += 46 + nlen + elen + clen;
  }
  return out;
}
async function inflateRaw(data) {
  const ds = new DecompressionStream('deflate-raw');
  const stream = new Blob([data]).stream().pipeThrough(ds);
  return new Uint8Array(await new Response(stream).arrayBuffer());
}
async function previewZipEntry(name, orig, en, buf) {
  const body = document.getElementById('vbody');
  const start = en.lfh + 30;
  const nlen = new DataView(buf.buffer, buf.byteOffset, buf.byteLength).getUint16(en.lfh + 26, true);
  const elen = new DataView(buf.buffer, buf.byteOffset, buf.byteLength).getUint16(en.lfh + 28, true);
  const dataStart = start + nlen + elen;
  const raw = buf.subarray(dataStart, dataStart + en.csize);
  const data = en.method === 0 ? new Uint8Array(raw) : await inflateRaw(raw);
  const k = kindOf(en.name);
  if (k === 'img') {
    const blob = new Blob([data], {type: 'image/' + extOf(en.name)});
    body.innerHTML = '<div class="stage"><img id="vimg" src="' + URL.createObjectURL(blob) + '" alt="' + esc(en.name) + '"></div>' +
      '<div class="hint" style="margin-top:10px">zip 內的圖片（' + esc(en.name) + '）</div>';
  } else if (TEXT_KINDS.has(k) || k === 'bin' && data.length < 1024 * 1024) {
    const text = new TextDecoder().decode(data);
    const isMd = k === 'md';
    const lines = text.split('\n').length;
    const nums = Array.from({length: lines}, (_, i) => i + 1).join('\n');
    body.innerHTML =
      '<div class="vbar" style="margin-top:0"><button id="zBack">← 回到 zip 清單</button><span class="vname">' + esc(en.name) + '</span></div>' +
      (isMd ? '<div class="md">' + renderMD(text) + '</div>'
            : '<div class="code-scroll"><div class="code-inner"><div class="gutter">' + nums + '</div><pre class="code"><code>' + highlight(text, en.name) + '</code></pre></div></div>');
    document.getElementById('zBack').addEventListener('click', () => viewZip(name, orig, document.getElementById('vbody')));
  } else {
    const blob = new Blob([data]);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = en.name.split('/').pop();
    document.body.appendChild(a); a.click(); a.remove();
    toast('zip 內此類型不支援預覽，已下載');
  }
}

// ── editor ───────────────────────────────────────────────────────────
async function showEditor(name) {
  let meta = lastFiles.find(f => f.name === name);
  if (!meta) {
    try {
      const list = await (await fetch('/files')).json();
      lastFiles = list;
      meta = list.find(f => f.name === name);
    } catch (e) {}
  }
  const orig = (meta && meta.orig) || name;
  const kind = kindOf(name);
  if (!TEXT_KINDS.has(kind)) {
    location.href = viewUrl(name);
    return;
  }
  let text;
  try {
    text = await fetchText(name);
  } catch (e) {
    $app.innerHTML = '<div class="vbar"><button id="back">← 檔案清單</button></div><div class="stage"><div class="fallback">讀取失敗：' + esc(e.message) + '</div></div>';
    document.getElementById('back').addEventListener('click', () => history.back());
    return;
  }
  if (text.length > 2 * 1024 * 1024) {
    $app.innerHTML = '<div class="vbar"><button id="back">← 檔案清單</button></div><div class="stage"><div class="fallback"><div class="big">檔案超過 2MB</div>為保持流暢不開放編輯，請下載開啟</div></div>';
    document.getElementById('back').addEventListener('click', () => history.back());
    return;
  }

  const isMd = kind === 'md';
  $app.innerHTML =
    '<div class="edbar">' +
    '<button id="back">← 檔案清單</button>' +
    '<span class="ename" title="' + esc(orig) + '">編輯：' + esc(orig) + '</span>' +
    '<span class="edstate" id="edState">未更動</span>' +
    '<span class="edops">' +
    '<button id="edCancel">取消</button>' +
    '<button id="edSave" class="primary">另存新檔</button>' +
    '</span></div>' +
    (isMd
      ? '<div class="ed-split">' +
        '<div class="ed-pane"><div class="pane-h">編輯</div>' + edBox() + '</div>' +
        '<div class="ed-pane"><div class="pane-h">預覽</div><div class="ed-preview md" id="edPrev"></div></div>' +
        '</div>'
      : '<div class="ed-pane">' + edBox() + '</div>');

  function edBox() {
    return '<div class="ed-box"><div class="ed-gutter"><div id="edNums"></div></div>' +
      '<textarea class="ed" id="edTa" spellcheck="false" wrap="off"></textarea></div>';
  }

  const ta = document.getElementById('edTa');
  const nums = document.getElementById('edNums');
  const state = document.getElementById('edState');
  ta.value = text;

  function refreshNums() {
    const n = ta.value.split('\n').length;
    nums.innerHTML = Array.from({length: n}, (_, i) => i + 1).join('\n');
    nums.style.transform = 'translateY(' + (-ta.scrollTop) + 'px)';
  }
  refreshNums();
  ta.addEventListener('scroll', () => { nums.style.transform = 'translateY(' + (-ta.scrollTop) + 'px)'; });
  ta.addEventListener('input', () => {
    refreshNums();
    state.textContent = '未儲存';
    state.classList.add('dirty');
    if (isMd) document.getElementById('edPrev').innerHTML = renderMD(ta.value) || '<p style="color:var(--text-muted)">（空）</p>';
  });
  ta.addEventListener('keydown', e => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const s = ta.selectionStart, epos = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + '  ' + ta.value.slice(epos);
      ta.selectionStart = ta.selectionEnd = s + 2;
      ta.dispatchEvent(new Event('input'));
    }
  });
  if (isMd) document.getElementById('edPrev').innerHTML = renderMD(text) || '<p style="color:var(--text-muted)">（空）</p>';

  document.getElementById('back').addEventListener('click', () => history.back());
  document.getElementById('edCancel').addEventListener('click', () => history.back());
  document.getElementById('edSave').addEventListener('click', async () => {
    const btn = document.getElementById('edSave');
    btn.disabled = true;
    btn.textContent = '儲存中…';
    try {
      const base = orig.replace(/\.[^.]+$/, '');
      const ext = extOf(orig) || 'txt';
      const newName = base + '_edited.' + ext;
      const blob = new Blob([ta.value], {type: 'text/plain;charset=utf-8'});
      const r = await fetch('/upload?name=' + encodeURIComponent(newName), {method: 'POST', body: blob});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      state.textContent = '已另存';
      state.classList.remove('dirty');
      showSaved(data.url, newName);
    } catch (e) {
      toast('儲存失敗：' + e.message);
      btn.disabled = false;
      btn.textContent = '另存新檔';
    }
  });
}

// ── boot ─────────────────────────────────────────────────────────────
route();
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

    def _serve_file(self, name: str, mode: str = 'default'):
        """伺服單一檔案。name 已 basename（防路徑穿越）。
        mode: default = 圖片內嵌、其餘強制下載；inline = 內嵌顯示（瀏覽器內建 PDF viewer 用）；
              download = 一律強制下載。"""
        if name.endswith('.orig'):
            self._send(404, 'not found')  # sidecar 不對外開放
            return
        fp = os.path.join(self.server.dir, name)
        if os.path.isfile(fp):
            ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            disposition = None
            if mode == 'inline':
                disposition = 'inline'
            elif mode == 'download' or not ctype.startswith('image/'):
                # 強制下載並還原原始檔名（非圖片預設如此；圖片預設內嵌供 markdown 使用）
                disp_name = _read_orig_name(fp) or name
                disposition = _content_disposition(disp_name)
            with open(fp, 'rb') as f:
                self._send(200, f.read(), ctype, disposition)
        else:
            self._send(404, 'not found')

    def _serve_favicon(self, path):
        """伺服 favicon（SVG + 各尺寸 PNG），檔放在腳本同目錄。"""
        name = os.path.basename(path)
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        if os.path.isfile(fp):
            ctype = 'image/svg+xml' if name.endswith('.svg') else 'image/png'
            with open(fp, 'rb') as f:
                self._send(200, f.read(), ctype)
        else:
            self._send(404, 'not found')

    def _list_files(self, q: str = ''):
        """已上傳檔案清單（排除 .orig sidecar），依時間新到舊，上限 100 筆。
        q = 名稱搜尋（uuid 檔名或原始檔名任一符合）。"""
        items = []
        ql = q.strip().lower()
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
            orig = _read_orig_name(fp) or name
            if ql and ql not in name.lower() and ql not in orig.lower():
                continue
            items.append({
                'name': name,
                'orig': orig,
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
        elif path == '/' or path.startswith('/view/') or path.startswith('/edit/'):
            # SPA：檢視器/編輯器是前端路由，伺服器回同一頁，由 JS 讀 pathname 分派
            self._send(200, FRONTEND_HTML, 'text/html; charset=utf-8')
        elif path == '/files':
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get('q') or [''])[0].strip()
            self._send(200, json.dumps(self._list_files(q), ensure_ascii=False))
        elif path.startswith('/files/'):
            qs = parse_qs(urlparse(self.path).query)
            mode = (qs.get('mode') or ['default'])[0]
            if mode not in ('default', 'inline', 'download'):
                mode = 'default'
            self._serve_file(os.path.basename(path), mode)
        elif path in ('/favicon.svg', '/favicon-16.png', '/favicon-32.png',
                      '/favicon-48.png', '/favicon-64.png', '/apple-touch-icon.png'):
            self._serve_favicon(path)
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
