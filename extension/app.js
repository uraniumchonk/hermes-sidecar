
(() => {
  'use strict';

  /* ---------- i18n：依瀏覽器語言自動選繁中 / 英文 ---------- */
  const LOCALES = {
    zh: {
      appTitle: 'Hermes Sidecar',
      newTitle: '新對話（雙擊開啟設定）', newAria: '新對話',
      captureTitle: '截取目前網頁畫面', captureAria: '截取目前網頁畫面',
      readTitle: '讀取目前網頁文字', readAria: '讀取目前網頁文字',
      inputPh: '問小夜…',
      sendTitle: '送出', sendAria: '送出', stopTitle: '停止', stopAria: '停止',
      emptyMark: '喵', emptySub: '小夜在這裡，隨時可以開聊。', emptyHint: '輸入訊息開始對話', newChatDone: '新對話開始。',
      settingsTitle: '連線設定', fEndpoint: '端點（gateway API）', fKey: 'API Key', fModel: '模型', fUpload: '上傳端點（附件接收）',
      bTest: '測試連線', bCancel: '取消', bSave: '儲存',
      settingsHint: 'Key 存在本機瀏覽器 storage。截圖/檔案會先傳到上傳端點存檔，再把伺服器路徑注入訊息給 agent 讀取。測試連線使用 gateway 的 /health（免 key）。',
      testing: '測試中…', connOk: '連線正常', connBad: '回應異常', connFail: '無法連線', tTimeout: '逾時', tRefused: '拒絕連線',
      cors403: 'HTTP 403：gateway CORS 擋下 extension Origin，需 API_SERVER_CORS_ORIGINS=* 後重啟 gateway',
      corsHint: 'gateway CORS 擋下 chrome-extension Origin。請確認 API_SERVER_CORS_ORIGINS=* 後重啟 gateway',
      noTab: '找不到目前分頁',
      capturing: '正在截取畫面…', captureAdded: '截圖已加入，輸入訊息後送出（共 {n} 個附件）', captureFail: '截圖失敗',
      readingPage: '正在讀取頁面文字…', pageAdded: '頁面已加入，輸入訊息後送出（共 {n} 個附件）', pageFail: '讀取頁面失敗', pageSpecial: 'chrome:// 等特殊頁面無法讀取',
      uploadingClip: '正在上傳剪貼簿圖片…', clipAdded: '圖片已加入，輸入訊息後送出（共 {n} 個附件）', clipFail: '圖片上傳失敗',
      uploadFail: '上傳失敗', uploadNoPath: '上傳回應缺少 path',
      noStream: '瀏覽器不支援串流回應',
      fetchFail: '無法連線到 {ep}，請檢查 gateway 是否在跑、設定是否正確',
      errGeneric: '發生錯誤', stopped: '已停止',
      pendingImg: '截圖', pendingDoc: '頁面', rmAttach: '移除', rmAttachAria: '移除此附件',
      toolRunning: '執行中…', toolDone: '完成',
      attachPrompt: '請參考以下附件：',
      imgPart: '（附件圖片：{path}）',
      docPart: '（附件網頁：{title}）\n網址：{url}\n\n--- 頁面文字內容 ---\n{text}',
      docNoText: '（此頁面無文字內容）', unknown: '未知',
      imgAlt: '附件'
    },
    en: {
      appTitle: 'Hermes Sidecar',
      newTitle: 'New chat (double-click for settings)', newAria: 'New chat',
      captureTitle: 'Capture current page', captureAria: 'Capture current page',
      readTitle: 'Read current page text', readAria: 'Read current page text',
      inputPh: 'Ask…',
      sendTitle: 'Send', sendAria: 'Send', stopTitle: 'Stop', stopAria: 'Stop',
      emptyMark: 'Hi', emptySub: 'Your Hermes sidecar is ready.', emptyHint: 'Type a message to start', newChatDone: 'New chat started.',
      settingsTitle: 'Connection settings', fEndpoint: 'Endpoint (gateway API)', fKey: 'API Key', fModel: 'Model', fUpload: 'Upload endpoint (attachments)',
      bTest: 'Test connection', bCancel: 'Cancel', bSave: 'Save',
      settingsHint: 'Key is stored in browser storage. Screenshots and files go to the upload endpoint first; the server path is then injected into the message for the agent to read. Connection test uses gateway /health (no key).',
      testing: 'Testing…', connOk: 'Connected', connBad: 'Bad response', connFail: 'Connection failed', tTimeout: 'timeout', tRefused: 'connection refused',
      cors403: 'HTTP 403: gateway CORS blocked the extension Origin. Set API_SERVER_CORS_ORIGINS=* and restart the gateway',
      corsHint: 'Gateway CORS blocked the chrome-extension Origin. Set API_SERVER_CORS_ORIGINS=* and restart the gateway',
      noTab: 'No active tab found',
      capturing: 'Capturing page…', captureAdded: 'Screenshot buffered, type a message to send ({n} attachments)', captureFail: 'Capture failed',
      readingPage: 'Reading page text…', pageAdded: 'Page buffered, type a message to send ({n} attachments)', pageFail: 'Failed to read page', pageSpecial: 'Special pages like chrome:// cannot be read',
      uploadingClip: 'Uploading clipboard image…', clipAdded: 'Image buffered, type a message to send ({n} attachments)', clipFail: 'Image upload failed',
      uploadFail: 'Upload failed', uploadNoPath: 'Upload response missing path',
      noStream: 'Browser does not support streaming responses',
      fetchFail: 'Cannot reach {ep}. Check that the gateway is running and settings are correct',
      errGeneric: 'Something went wrong', stopped: 'Stopped',
      pendingImg: 'Shot', pendingDoc: 'Page', rmAttach: 'Remove', rmAttachAria: 'Remove this attachment',
      toolRunning: 'running…', toolDone: 'done',
      attachPrompt: 'Please refer to the attachments:',
      imgPart: '(Image: {path})',
      docPart: '(Page: {title})\nURL: {url}\n\n--- Page text ---\n{text}',
      docNoText: '(no text on this page)', unknown: 'unknown',
      imgAlt: 'Attachment'
    }
  };

  let T = LOCALES.zh;
  const t = (key, vars) => {
    let s = (T[key] !== undefined) ? T[key] : key;
    if (vars) for (const k in vars) s = s.replace('{' + k + '}', vars[k]);
    return s;
  };

  function applyLocale() {
    const zh = (navigator.language || '').toLowerCase().startsWith('zh');
    T = LOCALES[zh ? 'zh' : 'en'];
    document.title = T.appTitle;
    document.documentElement.lang = zh ? 'zh-Hant' : 'en';
    document.querySelectorAll('[data-i18n]').forEach((el) => { el.textContent = T[el.dataset.i18n] || ''; });
    document.querySelectorAll('[data-i18n-title]').forEach((el) => { el.title = T[el.dataset.i18nTitle] || ''; });
    document.querySelectorAll('[data-i18n-aria]').forEach((el) => { el.setAttribute('aria-label', T[el.dataset.i18nAria] || ''); });
    document.querySelectorAll('[data-i18n-ph]').forEach((el) => { el.placeholder = T[el.dataset.i18nPh] || ''; });
  }

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
        img.alt = t('imgAlt');
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
    card.innerHTML = '<span class="dot"></span><span class="tool-name"></span><span class="tool-status">' + t('toolRunning') + '</span>';
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
      els.btnSend.title = t('stopTitle');
      els.btnSend.setAttribute('aria-label', t('stopAria'));
      els.btnSend.innerHTML = '<svg viewBox="0 0 16 16" fill="currentColor"><rect x="4" y="4" width="8" height="8" rx="1.5"/></svg>';
    } else {
      els.btnSend.classList.remove('stop');
      els.btnSend.title = t('sendTitle');
      els.btnSend.setAttribute('aria-label', t('sendAria'));
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
          ctx.currentTool.querySelector('.tool-status').textContent = t('toolDone');
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
      if (!text) parts.push(t('attachPrompt'));
      else parts.push(text);
      for (const p of pending) {
        if (p.kind === 'image') {
          parts.push(t('imgPart', { path: p.path }));
          previews.push(p.preview);
        } else {
          parts.push(t('docPart', {
            title: p.title || '',
            url: p.url || t('unknown'),
            text: p.text || t('docNoText')
          }));
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
          detail = t('corsHint');
        }
        throw new Error('HTTP ' + resp.status + (detail ? '：' + detail : ''));
      }
      if (!resp.body) throw new Error(t('noStream'));

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
        toast(t('stopped'));
      } else {
        closeTextSeg(ctx);
        const emsg = (err.message || '').includes('Failed to fetch')
          ? t('fetchFail', { ep: cfg.endpoint })
          : err.message || t('errGeneric');
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
        n.textContent = t('pendingImg');
        chip.appendChild(n);
      } else {
        const n = document.createElement('span');
        n.className = 'pc-name';
        n.textContent = t('pendingDoc') + (p.title ? '：' + p.title : '');
        chip.appendChild(n);
      }
      const x = document.createElement('button');
      x.className = 'pc-x';
      x.type = 'button';
      x.title = t('rmAttach');
      x.setAttribute('aria-label', t('rmAttachAria'));
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
    if (!file) {
      toast(t('clipFail') + '：' + 'clipboard item has no file');
      return;
    }
    try {
      toast(t('uploadingClip'));
      const path = await uploadBlob(file, 'clipboard.png');
      const preview = await blobToDataUrl(file);
      pending.push({ kind: 'image', path, preview });
      renderPending();
      toast(t('clipAdded', { n: pending.length }));
    } catch (e) {
      toast(t('clipFail') + '：' + (e.message || e));
    }
  }

  // 全域 paste 監聽：無論焦點在哪，貼上圖片就走 buffer（文字貼上不受影響）
  function onPaste(e) {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (const item of items) {
      if (item.type && item.type.startsWith('image/')) {
        e.preventDefault();
        handlePastedImage(item.getAsFile && item.getAsFile());
        return;
      }
    }
    const files = (e.clipboardData && e.clipboardData.files) || [];
    for (const f of files) {
      if (f.type && f.type.startsWith('image/')) {
        e.preventDefault();
        handlePastedImage(f);
        return;
      }
    }
  }

  /* ---------- 附件上傳（檔案落地伺服器 → 回傳絕對路徑） ---------- */
  async function uploadBlob(blob, name) {
    const resp = await fetch(cfg.uploadUrl + '?name=' + encodeURIComponent(name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: blob
    });
    if (!resp.ok) throw new Error(t('uploadFail') + '（HTTP ' + resp.status + '）');
    const j = await resp.json().catch(() => ({}));
    if (!j.path) throw new Error(t('uploadNoPath'));
    return j.path;
  }

  /* ---------- 截取目前網頁畫面（buffer，不立即送出） ---------- */
  async function capturePage() {
    try {
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (!tab || tab.id == null) throw new Error(t('noTab'));
      toast(t('capturing'));
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
      const blob = await (await fetch(dataUrl)).blob();
      const path = await uploadBlob(blob, 'screenshot.png');
      pending.push({ kind: 'image', path, preview: dataUrl });
      renderPending();
      toast(t('captureAdded', { n: pending.length }));
    } catch (e) {
      toast(t('captureFail') + '：' + (e.message || e));
    }
  }

  /* ---------- 讀取目前網頁文字（buffer，不立即送出） ---------- */
  async function readPage() {
    try {
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (!tab || tab.id == null) throw new Error(t('noTab'));
      toast(t('readingPage'));
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
      toast(t('pageAdded', { n: pending.length }));
    } catch (e) {
      toast(t('pageFail') + '：' + (e.message || e) + '（' + t('pageSpecial') + '）');
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
    els.connStatus.textContent = t('testing');
    const ep = els.setEndpoint.value.trim() || DEFAULTS.endpoint;
    try {
      const base = ep.replace(/\/v1\/chat\/completions\/?$/, '');
      const resp = await fetch(base + '/health', { signal: AbortSignal.timeout(5000) });
      els.connStatus.style.color = resp.ok ? 'var(--success)' : 'var(--error)';
      els.connStatus.textContent = resp.ok
        ? t('connOk') + '（' + resp.status + '）'
        : (resp.status === 403
          ? t('cors403')
          : t('connBad') + '（HTTP ' + resp.status + '）');
    } catch (e) {
      els.connStatus.style.color = 'var(--error)';
      els.connStatus.textContent = t('connFail') + '：' + (e.name === 'TimeoutError' ? t('tTimeout') : t('tRefused'));
    }
  }

  /* ---------- 新對話 ---------- */
  function buildEmptyState() {
    const es = document.createElement('div');
    es.className = 'empty-state';
    const mark = document.createElement('div');
    mark.className = 'mark';
    mark.textContent = T.emptyMark;
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = T.emptySub;
    const hint = document.createElement('div');
    hint.className = 'sub';
    hint.id = 'empty-hint';
    hint.textContent = T.emptyHint;
    es.appendChild(mark);
    es.appendChild(sub);
    es.appendChild(hint);
    els.messages.appendChild(es);
  }

  function newChat() {
    if (streaming && controller) controller.abort();
    history = [];
    pending = [];
    renderPending();
    els.messages.innerHTML = '';
    buildEmptyState();
  }

  /* ---------- 初始化 ---------- */
  async function init() {
    applyLocale();
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

  // toast 點擊立即消失
  els.toast.addEventListener('click', () => {
    els.toast.classList.remove('show');
    clearTimeout(toastTimer);
  });

  els.input.addEventListener('input', autoResize);
  document.addEventListener('paste', onPaste);
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
