
(() => {
  'use strict';

  const STORE_KEY = 'meowAgentConfig';
  const DEFAULTS = {
    endpoint: 'http://192.168.0.160:30001/v1/chat/completions',
    apiKey: '',
    model: 'qwen-27b-default',
    uploadUrl: 'http://192.168.0.160:18778/upload'
  };

  let cfg = { ...DEFAULTS };
  let history = [];
  let controller = null;
  let streaming = false;
  let toastTimer = null;
  // buffer 附件：{kind:'image', path, preview} 或 {kind:'doc', title, url, text}
  let pending = [];

  const $ = (id) => document.getElementById(id);
  const els = {
    messages: $('messages'),
    input: $('input'),
    pending: $('pending'),
    btnSend: $('btn-send'),
    btnNew: $('btn-new'),
    btnCapture: $('btn-capture'),
    btnReadPage: $('btn-readpage'),
    toast: $('toast'),
    overlay: $('settings-overlay'),
    setEndpoint: $('set-endpoint'),
    setKey: $('set-key'),
    setModel: $('set-model'),
    setUpload: $('set-upload'),
    btnTest: $('btn-test'),
    btnCancel: $('btn-cancel'),
    btnSave: $('btn-save'),
    connStatus: $('conn-status'),
    emptyHint: $('empty-hint')
  };

  /* ---------- 訊息渲染 ---------- */
  function renderMarkdown(text) {
    return DOMPurify.sanitize(marked.parse(text, { breaks: true, gfm: true }));
  }

  function scrollBottom() {
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function addUserBubble(text, previews) {
    hideEmptyState();
    const div = document.createElement('div');
    div.className = 'msg user';
    const txt = document.createElement('div');
    txt.textContent = text;
    div.appendChild(txt);
    if (Array.isArray(previews)) {
      for (const p of previews) {
        if (!p) continue;
        const img = document.createElement('img');
        img.src = p;
        img.className = 'attach-img';
        img.alt = '附件';
        div.appendChild(img);
      }
    }
    els.messages.appendChild(div);
    scrollBottom();
    return div;
  }

  function createAssistantBubble() {
    hideEmptyState();
    const div = document.createElement('div');
    div.className = 'msg assistant';
    els.messages.appendChild(div);
    scrollBottom();
    return div;
  }

  // 工具卡插入指定容器（依訊息順序放在該輪訊息內部）
  function addToolCard(parent, name) {
    const card = document.createElement('div');
    card.className = 'tool-card running';
    card.innerHTML = '<span class="dot"></span><span class="tool-name"></span><span class="tool-status">執行中…</span>';
    card.querySelector('.tool-name').textContent = name;
    parent.appendChild(card);
    scrollBottom();
    return card;
  }

  function hideEmptyState() {
    const es = els.messages.querySelector('.empty-state');
    if (es) es.remove();
  }

  /* ---------- 輸入 ---------- */
  function autoResize() {
    els.input.style.height = 'auto';
    els.input.style.height = Math.min(els.input.scrollHeight, 140) + 'px';
  }

  function setStreamingUI(on) {
    streaming = on;
    // 注意：不能設 disabled，否則串流中按不到「停止」鍵
    if (on) {
      els.btnSend.classList.add('stop');
      els.btnSend.title = '停止';
      els.btnSend.innerHTML = '<svg viewBox="0 0 16 16" fill="currentColor"><rect x="4" y="4" width="8" height="8" rx="1.5"/></svg>';
    } else {
      els.btnSend.classList.remove('stop');
      els.btnSend.title = '送出';
      els.btnSend.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 8L13.5 2.5 10 13.5 7.5 8.5 2.5 8z"/></svg>';
    }
  }

  /* ---------- SSE ---------- */
  // 從 buffer 中取出完整 SSE 事件；回傳 [{event, data}]，殘餘留在 buffer
  function splitSSE(buf) {
    const events = [];
    let idx;
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let eventName = '';
      const dataLines = [];
      for (const line of block.split('\n')) {
        if (line.startsWith(':')) continue;             // 註解 / keepalive
        if (line.startsWith('event:')) eventName = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) events.push({ event: eventName, data: dataLines.join('\n') });
    }
    return { events, rest: buf };
  }

  // 文字段落管理：工具卡插在「目前文字段落之後」，後續文字開新段落，
  // 讓工具呼叫塊依 LLM 訊息順序排列，而不是全部塞到訊息尾端
  function newTextSeg(ctx) {
    const md = document.createElement('div');
    md.className = 'md';
    const seg = { el: md, text: '' };
    ctx.textSegs.push(seg);
    ctx.div.appendChild(md);
    ctx.cur = seg;
    md.appendChild(ctx.cursor);
    return seg;
  }

  function closeTextSeg(ctx) {
    if (ctx.cur) {
      if (ctx.cursor.parentNode) ctx.cursor.remove();
      ctx.cur = null;
    }
  }

  function finalizeAssistant(ctx) {
    closeTextSeg(ctx);
    for (const seg of ctx.textSegs) {
      if (seg.text) seg.el.innerHTML = renderMarkdown(seg.text);
    }
    const full = ctx.textSegs.map((s) => s.text).join('\n');
    if (full) history.push({ role: 'assistant', content: full });
  }

  function handleSSE(ev, ctx) {
    if (ev.data === '[DONE]') return;

    let obj;
    try { obj = JSON.parse(ev.data); } catch { return; }

    // Hermes tool progress 事件（event: hermes.tool.progress）
    if (ev.event === 'hermes.tool.progress' || (obj.event === 'hermes.tool.progress')) {
      const p = obj.data || obj;
      const name = p.name || p.tool || 'tool';
      const status = p.status || '';
      if (status === 'completed' || status === 'done' || status === 'success') {
        if (ctx.currentTool) {
          ctx.currentTool.className = 'tool-card done';
          ctx.currentTool.querySelector('.tool-status').textContent = '完成';
        }
      } else if (status === 'running' || status === 'started') {
        closeTextSeg(ctx);
        ctx.currentTool = addToolCard(ctx.div, name);
      }
      return;
    }

    const choice = obj.choices && obj.choices[0];
    if (!choice) return;
    const delta = choice.delta || {};

    // 工具呼叫：收掉目前文字段落，工具卡插在它後面
    if (delta.tool_calls && delta.tool_calls.length) {
      const tc = delta.tool_calls[0];
      if (tc.function && tc.function.name && !ctx.toolNames.has(tc.function.name)) {
        ctx.toolNames.add(tc.function.name);
        closeTextSeg(ctx);
        ctx.currentTool = addToolCard(ctx.div, tc.function.name);
      }
      return;
    }

    // reasoning（若有）— 保持在訊息最上方
    if (delta.reasoning_content) {
      if (!ctx.thinkingEl) {
        ctx.thinkingEl = document.createElement('div');
        ctx.thinkingEl.className = 'thinking';
        ctx.div.insertBefore(ctx.thinkingEl, ctx.div.firstChild);
      }
      ctx.thinkingEl.textContent += delta.reasoning_content;
      scrollBottom();
      return;
    }

    // 一般內容：段落被工具卡收掉後，自動開新段落接續
    if (delta.content) {
      if (!ctx.cur) newTextSeg(ctx);
      ctx.cur.text += delta.content;
      ctx.cur.el.textContent = ctx.cur.text;
      scrollBottom();
    }
  }

  /* ---------- 送出 ---------- */
  function send() {
    const text = els.input.value.trim();
    if (streaming || (!text && !pending.length)) return;
    els.input.value = '';
    autoResize();

    let content = text;
    const previews = [];
    if (pending.length) {
      const parts = [];
      if (!text) parts.push('請參考以下附件：');
      else parts.push(text);
      for (const p of pending) {
        if (p.kind === 'image') {
          parts.push('（附件圖片：' + p.path + '）');
          previews.push(p.preview);
        } else {
          parts.push('（附件網頁：' + (p.title || '') + '）\n網址：' + (p.url || '未知') +
            '\n\n--- 頁面文字內容 ---\n' + (p.text || '（此頁面無文字內容）'));
        }
      }
      content = parts.join('\n\n');
    }
    pending = [];
    renderPending();
    runStream(content, previews);
  }

  // userContent: string 或 OpenAI content array；previews 僅用於前端縮圖顯示
  async function runStream(userContent, previews) {
    if (streaming) return;
    if (!cfg.apiKey) {
      openSettings();
      return;
    }

    const displayText = typeof userContent === 'string' ? userContent : '';
    addUserBubble(displayText, previews);
    history.push({ role: 'user', content: userContent });

    const ctx = {
      div: null,
      textSegs: [],
      cur: null,
      thinkingEl: null,
      toolNames: new Set(),
      currentTool: null,
      cursor: null
    };
    ctx.div = createAssistantBubble();
    ctx.cursor = document.createElement('span');
    ctx.cursor.className = 'cursor';
    ctx.cursor.innerHTML = '<span class="bar"></span>';

    controller = new AbortController();
    setStreamingUI(true);

    try {
      const resp = await fetch(cfg.endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(cfg.apiKey ? { 'Authorization': 'Bearer ' + cfg.apiKey } : {})
        },
        body: JSON.stringify({
          model: cfg.model,
          stream: true,
          messages: history
        }),
        signal: controller.signal
      });

      if (!resp.ok) {
        let detail = '';
        try { const e = await resp.json(); detail = e.error && (e.error.message || e.error) || ''; } catch {}
        if (resp.status === 403 && !detail) {
          detail = 'gateway CORS 擋下 chrome-extension Origin。請確認 API_SERVER_CORS_ORIGINS=* 後重啟 gateway';
        }
        throw new Error('HTTP ' + resp.status + (detail ? '：' + detail : ''));
      }
      if (!resp.body) throw new Error('瀏覽器不支援串流回應');

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const { events, rest } = splitSSE(buf);
        buf = rest;
        for (const ev of events) handleSSE(ev, ctx);
      }

      // flush 殘餘：stream 結束時最後一個事件可能沒有尾隨空行
      if (buf.trim()) {
        const { events } = splitSSE(buf + '\n\n');
        for (const ev of events) handleSSE(ev, ctx);
      }

      // 收尾：串流期間顯示純文字，結束後渲染 markdown
      finalizeAssistant(ctx);
      scrollBottom();

    } catch (err) {
      if (err.name === 'AbortError') {
        // 使用者停止：保留已產生的內容
        finalizeAssistant(ctx);
        toast('已停止');
      } else {
        closeTextSeg(ctx);
        const emsg = (err.message || '').includes('Failed to fetch')
          ? '無法連線到 ' + cfg.endpoint + '，請檢查 gateway 是否在跑、設定是否正確'
          : err.message || '發生錯誤';
        const e = document.createElement('div');
        e.className = 'md';
        e.innerHTML = '<span style="color:var(--error)">' + DOMPurify.sanitize(emsg) + '</span>';
        ctx.div.appendChild(e);
      }
      scrollBottom();
    } finally {
      controller = null;
      setStreamingUI(false);
    }
  }

  function stop() {
    if (controller) controller.abort();
  }

  /* ---------- Toast ---------- */
  function toast(msg) {
    els.toast.textContent = msg;
    els.toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.remove('show'), 3500);
  }

  /* ---------- 附件 buffer（截圖 / 網頁文字，輸入訊息後才一起送出） ---------- */
  function renderPending() {
    els.pending.innerHTML = '';
    if (!pending.length) { els.pending.hidden = true; return; }
    els.pending.hidden = false;
    pending.forEach((p, i) => {
      const chip = document.createElement('span');
      chip.className = 'pending-chip';
      if (p.kind === 'image') {
        const img = document.createElement('img');
        img.src = p.preview;
        img.alt = '';
        chip.appendChild(img);
        const n = document.createElement('span');
        n.className = 'pc-name';
        n.textContent = '截圖';
        chip.appendChild(n);
      } else {
        const n = document.createElement('span');
        n.className = 'pc-name';
        n.textContent = '頁面' + (p.title ? '：' + p.title : '');
        chip.appendChild(n);
      }
      const x = document.createElement('button');
      x.className = 'pc-x';
      x.type = 'button';
      x.title = '移除';
      x.setAttribute('aria-label', '移除此附件');
      x.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
      x.addEventListener('click', () => {
        pending.splice(i, 1);
        renderPending();
      });
      chip.appendChild(x);
      els.pending.appendChild(chip);
    });
  }

  /* ---------- 剪貼簿圖片（貼進輸入框 → 走 buffer，同截圖流程） ---------- */
  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = reject;
      r.readAsDataURL(blob);
    });
  }

  async function handlePastedImage(file) {
    if (!file) return;
    try {
      toast('正在上傳剪貼簿圖片…');
      const path = await uploadBlob(file, 'clipboard.png');
      const preview = await blobToDataUrl(file);
      pending.push({ kind: 'image', path, preview });
      renderPending();
      toast('圖片已加入，輸入訊息後送出（共 ' + pending.length + ' 個附件）');
    } catch (e) {
      toast('圖片上傳失敗：' + (e.message || e));
    }
  }

  /* ---------- 附件上傳（檔案落地伺服器 → 回傳絕對路徑） ---------- */
  async function uploadBlob(blob, name) {
    const resp = await fetch(cfg.uploadUrl + '?name=' + encodeURIComponent(name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: blob
    });
    if (!resp.ok) throw new Error('上傳失敗（HTTP ' + resp.status + '）');
    const j = await resp.json().catch(() => ({}));
    if (!j.path) throw new Error('上傳回應缺少 path');
    return j.path;
  }

  /* ---------- 截取目前網頁畫面（buffer，不立即送出） ---------- */
  async function capturePage() {
    try {
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (!tab || tab.id == null) throw new Error('找不到目前分頁');
      toast('正在截取畫面…');
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
      const blob = await (await fetch(dataUrl)).blob();
      const path = await uploadBlob(blob, 'screenshot.png');
      pending.push({ kind: 'image', path, preview: dataUrl });
      renderPending();
      toast('截圖已加入，輸入訊息後送出（共 ' + pending.length + ' 個附件）');
    } catch (e) {
      toast('截圖失敗：' + (e.message || e));
    }
  }

  /* ---------- 讀取目前網頁文字（buffer，不立即送出） ---------- */
  async function readPage() {
    try {
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (!tab || tab.id == null) throw new Error('找不到目前分頁');
      toast('正在讀取頁面文字…');
      const [res] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const text = (document.body && document.body.innerText) ? document.body.innerText : '';
          return {
            title: document.title || '',
            url: location.href,
            text: text.slice(0, 40000)
          };
        }
      });
      const d = (res && res.result) || {};
      pending.push({ kind: 'doc', title: d.title, url: d.url, text: d.text });
      renderPending();
      toast('頁面已加入，輸入訊息後送出（共 ' + pending.length + ' 個附件）');
    } catch (e) {
      toast('讀取頁面失敗：' + (e.message || e) + '（chrome:// 等特殊頁面無法讀取）');
    }
  }

  /* ---------- 設定 ---------- */
  function openSettings() {
    els.setEndpoint.value = cfg.endpoint;
    els.setKey.value = cfg.apiKey;
    els.setModel.value = cfg.model;
    els.setUpload.value = cfg.uploadUrl;
    els.connStatus.textContent = '';
    els.overlay.classList.add('open');
    els.setEndpoint.focus();
  }

  function closeSettings() {
    els.overlay.classList.remove('open');
  }

  async function saveSettings() {
    cfg.endpoint = els.setEndpoint.value.trim() || DEFAULTS.endpoint;
    cfg.apiKey = els.setKey.value.trim();
    cfg.model = els.setModel.value.trim() || DEFAULTS.model;
    cfg.uploadUrl = els.setUpload.value.trim() || DEFAULTS.uploadUrl;
    await chrome.storage.local.set({ [STORE_KEY]: cfg });
    closeSettings();
  }

  async function testConnection() {
    els.connStatus.textContent = '測試中…';
    const ep = els.setEndpoint.value.trim() || DEFAULTS.endpoint;
    try {
      const base = ep.replace(/\/v1\/chat\/completions\/?$/, '');
      const resp = await fetch(base + '/health', { signal: AbortSignal.timeout(5000) });
      els.connStatus.style.color = resp.ok ? 'var(--success)' : 'var(--error)';
      els.connStatus.textContent = resp.ok
        ? '連線正常（' + resp.status + '）'
        : (resp.status === 403
          ? 'HTTP 403：gateway CORS 擋下 extension Origin，需 API_SERVER_CORS_ORIGINS=* 後重啟 gateway'
          : '回應異常（HTTP ' + resp.status + '）');
    } catch (e) {
      els.connStatus.style.color = 'var(--error)';
      els.connStatus.textContent = '無法連線：' + (e.name === 'TimeoutError' ? '逾時' : '拒絕連線');
    }
  }

  /* ---------- 新對話 ---------- */
  function newChat() {
    if (streaming && controller) controller.abort();
    history = [];
    pending = [];
    renderPending();
    els.messages.innerHTML = '';
    const es = document.createElement('div');
    es.className = 'empty-state';
    es.innerHTML = '<div class="mark">喵</div><div class="sub">新對話開始。</div>';
    els.messages.appendChild(es);
  }

  /* ---------- 初始化 ---------- */
  async function init() {
    try {
      const stored = await chrome.storage.local.get(STORE_KEY);
      cfg = { ...DEFAULTS, ...(stored[STORE_KEY] || {}) };
    } catch { /* storage 不可用時用預設 */ }

    // 沒有 key 時自動開設定
    if (!cfg.apiKey) {
      setTimeout(openSettings, 300);
    }
  }

  els.btnSend.addEventListener('click', () => streaming ? stop() : send());
  els.btnNew.addEventListener('click', newChat);
  els.btnNew.addEventListener('dblclick', openSettings);
  els.btnCapture.addEventListener('click', capturePage);
  els.btnReadPage.addEventListener('click', readPage);
  els.btnSave.addEventListener('click', saveSettings);
  els.btnCancel.addEventListener('click', closeSettings);
  els.btnTest.addEventListener('click', testConnection);

  els.input.addEventListener('input', autoResize);
  els.input.addEventListener('paste', (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (const item of items) {
      if (item.type && item.type.startsWith('image/')) {
        e.preventDefault();
        handlePastedImage(item.getAsFile());
        break;
      }
    }
  });
  els.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  els.overlay.addEventListener('click', (e) => {
    if (e.target === els.overlay) closeSettings();
  });

  init();
})();
