// static/chat.js — v1.9 (full-screen minimal chat, renamed title)
/*
  Purpose: Client logic for the chat page bound to a specific chat_id.
  Responsibilities:
  - Load existing chat history (text + image + table) and render it
  - Send user questions to backend and stream back a single answer payload
  - Minimal UI with sticky input and auto-scroll
*/
(function () {
  const chatId = window.CHAT_ID;
  const chatList = document.getElementById("chatList");
  const askBtn = document.getElementById("askBtn");
  const textarea = document.getElementById("question");
  const tokenBar = document.getElementById("tokenBar");
  const params = new URLSearchParams(location.search);
  let convId = params.get('conv_id') || null;

  /* ---------- STYLES ---------- */
  const style = document.createElement("style");
  style.textContent = `
    html, body {
      margin:0 !important; padding:0 !important; height:100dvh; width:100vw;
      font-family:'Segoe UI',sans-serif;
      background:#f5f6fa;
      overflow:hidden;
    }
    body {
      display:flex; flex-direction:column;
      min-height:100dvh;
    }
    .topbar { padding:6px 12px; margin-bottom:0; background:#fff; border-bottom:1px solid #ddd; flex-shrink:0; position:relative !important; z-index:1000; display:flex; justify-content:space-between; align-items:center; min-height:60px; }
    .topbar h1 { font-size:18px; margin:0; }
    .topbar p { font-size:13px; }
    .topbar .topbar-left { flex: 1; }
    .topbar .topbar-right { flex: 1; display:flex; justify-content:flex-end; align-items:center; gap:8px; }
    .topbar .logo-header { height:40px; }
    /* Ensure centered logo is above everything and clickable */
    .topbar .logo-header { position:absolute; left:50%; top:50%; transform: translate(-50%, -50%); z-index:1001; cursor:pointer; display:inline-block; line-height:0; }
    .topbar .logo-header img { display:block; pointer-events:none; }
    #btnShowSchema { padding:8px 14px; font-size:14px; background:#667eea; color:white; border:none; border-radius:6px; cursor:pointer; }
    section.chat, .card.chat {
      position: fixed;
      left: 0; right: 0;
      top: var(--topbar-h, 72px);
      bottom: 0;
      display: grid;
      grid-template-rows: auto 1fr auto;
      width: 100%;
      background: white;
      box-shadow: none;
      border-radius: 0;
      border: none;
      overflow: hidden;
      margin: 0 !important;
      padding: 0 !important;
      z-index: 1;
    }
    h3 {
      margin:0; padding:8px 12px;
      background:#ffffff;
      border-bottom:1px solid #ddd;
      font-size:18px;
      font-weight:600;
      color:#2d3436;
      display:flex;
      justify-content:space-between;
      align-items:center;
    }
    .chat-list { 
      overflow-y:auto; 
      padding:8px; 
      margin:0; 
      height:100%; 
      min-height:0; 
      max-height:100%;
      display:block;
      box-sizing:border-box;
    }
    #chatList { height:100%; }
    .msg { margin-bottom:12px; line-height:1.5; }
    .msg.ai { background:#f1f2f6; padding:8px 12px; border-radius:10px; max-width:95%; white-space:pre-wrap; }
    .msg.me { align-self:flex-end; background:#dff9fb; padding:8px 12px; border-radius:10px; max-width:80%; white-space:pre-wrap; }
    .msg.ai .image-container {
      position: relative;
      display: block;
      width: 100%;
      margin-top: 10px;
      max-width: min(900px, 100%);
    }
    .msg.ai img {
      max-width: 100%;
      max-height: 700px;
      object-fit: contain;
      border: 1px solid #e7e8ea;
      border-radius: 8px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.1);
      display: block;
    }
    @media (max-width: 768px) {
      .msg.ai .image-container { max-width: 100%; }
      .msg.ai img { max-height: 70vh; }
    }
    .fullscreen-btn {
      position: absolute;
      top: 8px;
      right: 8px;
      background: rgba(0,0,0,0.6);
      color: white;
      border: none;
      border-radius: 4px;
      padding: 6px 12px;
      cursor: pointer;
      font-size: 12px;
      font-weight: 500;
      transition: background 0.2s;
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .fullscreen-btn:hover {
      background: rgba(0,0,0,0.8);
    }
    .fullscreen-modal {
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(0,0,0,0.95);
      z-index: 9999;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .fullscreen-modal.active {
      display: flex;
    }
    .fullscreen-modal img {
      max-width: 95%;
      max-height: 95%;
      object-fit: contain;
      border-radius: 8px;
    }
    .fullscreen-close {
      position: absolute;
      top: 20px;
      right: 20px;
      background: rgba(255,255,255,0.9);
      color: #333;
      border: none;
      border-radius: 50%;
      width: 40px;
      height: 40px;
      cursor: pointer;
      font-size: 24px;
      font-weight: bold;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: background 0.2s;
    }
    .fullscreen-close:hover {
      background: white;
    }
    .chat-input { display:flex; padding:4px 8px; border-top:1px solid #ddd; flex-shrink:0; margin:0; }
    textarea { flex:1; resize:none; border:none; outline:none; font-size:16px; padding:8px; margin:0; }
    button { background:#0984e3; color:white; border:none; border-radius:8px; padding:10px 18px; cursor:pointer; margin:0 0 0 8px; font-size:16px; }
    button:hover { background:#74b9ff; }
    table.datachat { border-collapse:collapse; width:100%; font-size:14px; margin-top:8px; }
    table.datachat th, table.datachat td { border:1px solid #e3e6ea; padding:6px 8px; text-align:left; }
    table.datachat th { background:#eef3ff; position:sticky; top:0; }
    .meta { font-size:11px; color:#555; margin-bottom:4px; }
    .token-bar { padding:0; margin:0; font-size:10px; color:#666; background:transparent; border:none; white-space:nowrap; }
    .msg.ai p { margin: 4px 0; line-height: 1.5; }
    .msg.ai ul { margin: 8px 0; padding-left: 20px; }
    .msg.ai li { margin: 4px 0; line-height: 1.5; }
    .msg.ai strong { font-weight: 600; color: #1f6feb; }
  `;
  document.head.appendChild(style);

  // Dynamically set topbar height CSS variable so chat fills viewport exactly
  function updateTopbarHeight(){
    try {
      const tb = document.querySelector('.topbar');
      const h = tb ? Math.ceil(tb.getBoundingClientRect().height) : 72;
      document.documentElement.style.setProperty('--topbar-h', h + 'px');
    } catch {}
  }
  updateTopbarHeight();
  window.addEventListener('resize', updateTopbarHeight);
  try {
    if ('ResizeObserver' in window) {
      const tb = document.querySelector('.topbar');
      if (tb) {
        const ro = new ResizeObserver(()=> updateTopbarHeight());
        ro.observe(tb);
      }
    }
  } catch {}
  // Recompute after fonts/images settle
  setTimeout(updateTopbarHeight, 100);
  window.addEventListener('load', updateTopbarHeight);

  // Ensure chat-list uses CSS grid space (clear any inline heights)
  function fitChatHeights(){
    try {
      const list = document.getElementById('chatList');
      if (list) { list.style.height = ''; list.style.maxHeight = ''; }
    } catch {}
  }
  fitChatHeights();
  window.addEventListener('resize', fitChatHeights);

  // Replace title text and move token bar into header
  const titleEl = document.querySelector("h3");
  if (titleEl) {
    titleEl.innerHTML = '<span>Your personal data analyst</span>';
    // Move token bar into header
    if (tokenBar) {
      titleEl.appendChild(tokenBar);
    }
  }

  // Function to load and show schema
  async function loadAndShowSchema() {
    try {
      // Get modal elements when function is called (not at script load time)
      const schemaViewerModal = document.getElementById('schemaViewerModal');
      const schemaViewerContainer = document.getElementById('schemaViewerContainer');
      
      if (!schemaViewerContainer || !schemaViewerModal) {
        console.error('Schema viewer modal not found in DOM');
        alert('Schema viewer not available. Please refresh the page.');
        return;
      }
      
      schemaViewerContainer.innerHTML = '<p style="color:#666;">Loading...</p>';
      schemaViewerModal.classList.remove('hidden');
      schemaViewerModal.style.display = 'grid'; // Match CSS - use grid not flex
      schemaViewerModal.style.zIndex = '9999'; // Ensure it's on top
      
      const res = await fetch(`/api/chat/${chatId}/schema`);
      const data = await res.json();
      
      if (!res.ok || data.error) {
        schemaViewerContainer.innerHTML = `<p style="color:#ef4444;">${data.error || 'Failed to load schema'}</p>`;
        return;
      }
      
      const files = data.files || [];
      const commonFields = data.common_fields || [];
      
      if (files.length === 0) {
        schemaViewerContainer.innerHTML = '<p style="color:#666;">No field descriptions available.</p>';
        return;
      }
      
      let html = '';
      
      for (const file of files) {
        const fname = file.file_name || 'Unknown';
        const fileDesc = file.file_description || '';
        const schema = file.schema || {};
        const fields = schema.fields || {};
        
        html += `<div class="schema-card" style="margin-bottom: 16px; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px;">`;
        html += `<h4 style="margin: 0 0 8px 0; color: #374151; font-size: 16px;">📄 ${fname}</h4>`;
        
        if (fileDesc) {
          html += `<p style="margin: 0 0 12px 0; color: #6b7280; font-size: 14px; font-style: italic;">${fileDesc}</p>`;
        }
        
        const fieldEntries = Object.entries(fields);
        if (fieldEntries.length === 0) {
          html += `<p style="color: #9ca3af; font-size: 13px;">No field descriptions defined.</p>`;
        } else {
          html += `<table style="width: 100%; border-collapse: collapse; font-size: 14px;">`;
          html += `<thead><tr style="background: #f9fafb;"><th style="text-align: left; padding: 8px; border-bottom: 1px solid #e5e7eb;">Column</th><th style="text-align: left; padding: 8px; border-bottom: 1px solid #e5e7eb;">Description</th></tr></thead>`;
          html += `<tbody>`;
          
          for (const [col, fieldData] of fieldEntries) {
            const desc = fieldData?.description || '<span style="color:#9ca3af;">Not described</span>';
            const values = (typeof fieldData === 'object' && fieldData !== null) ? (fieldData.values || {}) : {};
            const valueEntries = Object.entries(values);
            
            html += `<tr>`;
            html += `<td style="padding: 8px; border-bottom: 1px solid #f3f4f6; font-weight: 500; color: #1f2937;">${col}</td>`;
            html += `<td style="padding: 8px; border-bottom: 1px solid #f3f4f6; color: #4b5563;">${desc}`;
            
            // Show ALL categorical values (with or without descriptions)
            if (valueEntries.length > 0) {
              html += `<div style="margin-top: 6px; font-size: 12px; color: #6b7280;"><strong>Categorical Values:</strong><ul style="margin: 4px 0 0 16px; padding: 0;">`;
              for (const [val, valDesc] of valueEntries) {
                const displayDesc = (valDesc && typeof valDesc === 'string' && valDesc.trim()) 
                  ? valDesc 
                  : '<span style="color:#9ca3af; font-style:italic;">(value itself is description)</span>';
                html += `<li style="margin-bottom:2px;"><code style="background:#f3f4f6; padding:2px 6px; border-radius:3px;">${val}</code> → ${displayDesc}</li>`;
              }
              html += `</ul></div>`;
            }
            
            html += `</td></tr>`;
          }
          
          html += `</tbody></table>`;
        }
        
        html += `</div>`;
      }
      
      // Show common fields if any
      if (commonFields.length > 0) {
        html += `<div class="schema-card" style="margin-top: 16px; border: 1px solid #a78bfa; border-radius: 8px; padding: 16px; background: #faf5ff;">`;
        html += `<h4 style="margin: 0 0 12px 0; color: #7c3aed;">🔗 Field Connections</h4>`;
        html += `<ul style="margin: 0; padding-left: 20px;">`;
        for (const rel of commonFields) {
          html += `<li style="margin-bottom: 6px;"><code>${rel.file1}[${rel.column1}]</code> ⟷ <code>${rel.file2}[${rel.column2}]</code></li>`;
        }
        html += `</ul></div>`;
      }
      
      schemaViewerContainer.innerHTML = html;
      
    } catch (e) {
      console.error('Error loading schema:', e);
      const container = document.getElementById('schemaViewerContainer');
      if (container) {
        container.innerHTML = `<p style="color:#ef4444;">Error loading schema: ${e.message}</p>`;
      }
      alert('Error loading schema: ' + e.message);
    }
  }

  // Add View Field Descriptions button to topbar with immediate event listener
  const topbar = document.querySelector('.topbar');
  if (topbar && !document.getElementById('btnShowSchema')) {
    const viewBtn = document.createElement('button');
    viewBtn.id = 'btnShowSchema';
    viewBtn.textContent = 'View Field Descriptions';
    viewBtn.style.cssText = 'position: absolute; right: 16px; top: 50%; transform: translateY(-50%); padding: 10px 16px; font-size: 14px; background: #667eea; color: white; border: none; border-radius: 6px; cursor: pointer; z-index: 1002; font-weight: 500;';
    
    // Attach click handler immediately
    viewBtn.addEventListener('click', (e) => {
      e.preventDefault();
      loadAndShowSchema();
    });
    
    topbar.appendChild(viewBtn);
  }

  /* ---------- FUNCTIONS ---------- */
  // Simple markdown to HTML converter
  function renderMarkdown(text) {
    if (!text) return '';
    
    // Escape HTML to prevent XSS
    const escapeHtml = (str) => {
      const div = document.createElement('div');
      div.textContent = str;
      return div.innerHTML;
    };
    
    let html = escapeHtml(text);
    
    // Bold: **text** -> <strong>text</strong>
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    
    // Bullet points: - text or • text -> <li>text</li>
    html = html.replace(/^[\-•]\s+(.+)$/gm, '<li>$1</li>');
    
    // Wrap consecutive <li> items in <ul>
    html = html.replace(/(<li>.*<\/li>\n?)+/g, (match) => '<ul>' + match + '</ul>');
    
    // Line breaks
    html = html.replace(/\n\n/g, '</p><p>');
    html = html.replace(/\n/g, '<br>');
    
    // Wrap in paragraph if not already wrapped
    if (!html.startsWith('<ul>') && !html.startsWith('<p>')) {
      html = '<p>' + html + '</p>';
    }
    
    return html;
  }
  
  // Create a new AI message container.
  function createAiMessage() {
    const div = document.createElement("div");
    div.className = "msg ai";
    return div;
  }

  // Create fullscreen modal for images
  const fullscreenModal = document.createElement('div');
  fullscreenModal.className = 'fullscreen-modal';
  fullscreenModal.innerHTML = `
    <button class="fullscreen-close" title="Close (Esc)">×</button>
    <img src="" alt="Full screen view">
  `;
  document.body.appendChild(fullscreenModal);

  const modalImg = fullscreenModal.querySelector('img');
  const modalClose = fullscreenModal.querySelector('.fullscreen-close');

  // Close modal handlers
  function closeFullscreen() {
    fullscreenModal.classList.remove('active');
  }
  modalClose.addEventListener('click', closeFullscreen);
  fullscreenModal.addEventListener('click', (e) => {
    if (e.target === fullscreenModal) closeFullscreen();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && fullscreenModal.classList.contains('active')) {
      closeFullscreen();
    }
  });

  // Wrap image with container and fullscreen button
  function createImageWithFullscreen(base64Data, messageContext = '') {
    const container = document.createElement('div');
    container.className = 'image-container';
    container.style.position = 'relative';
    
    const img = document.createElement('img');
    img.src = 'data:image/png;base64,' + base64Data;
    
    const btnContainer = document.createElement('div');
    btnContainer.style.cssText = 'display: flex; gap: 8px; margin-top: 8px;';
    
    const viewBtn = document.createElement('button');
    viewBtn.className = 'fullscreen-btn';
    viewBtn.innerHTML = '🔍 View Larger';
    viewBtn.title = 'View full screen';
    
    viewBtn.addEventListener('click', () => {
      modalImg.src = img.src;
      fullscreenModal.classList.add('active');
    });
    
    const downloadBtn = document.createElement('button');
    downloadBtn.className = 'ghost';
    downloadBtn.innerHTML = '📥 Download';
    downloadBtn.title = 'Download image';
    downloadBtn.style.cssText = 'padding: 6px 12px; font-size: 13px;';
    
    downloadBtn.addEventListener('click', async () => {
      try {
        downloadBtn.disabled = true;
        downloadBtn.textContent = 'Generating name...';
        
        // Get filename from LLM
        const response = await fetch(`/api/chat/${chatId}/generate_filename`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: messageContext, type: 'image' })
        });
        const data = await response.json();
        const filename = data.filename || 'chart';
        
        // Download image
        const link = document.createElement('a');
        link.href = img.src;
        link.download = `${filename}.png`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        
        downloadBtn.textContent = '📥 Download';
      } catch (e) {
        console.error('Download failed:', e);
        alert('Failed to download image');
        downloadBtn.textContent = '📥 Download';
      } finally {
        downloadBtn.disabled = false;
      }
    });
    
    btnContainer.appendChild(viewBtn);
    btnContainer.appendChild(downloadBtn);

    container.appendChild(img);
    container.appendChild(btnContainer);

    return container;
  }

  // Renders a Plotly interactive chart inside an iframe and adds
  // [View Larger] + [Download] buttons. The Download button posts the raw
  // Plotly HTML to /export_plotly_png and saves the returned high-res PNG.
  // NOTE: dashboard.js has a parallel copy of this function with the same
  // signature. Keep them in sync.
  function createPlotlyContainer(htmlString, chatIdRef, messageContext = '') {
    const plotlyContainer = document.createElement('div');
    plotlyContainer.className = 'plotly-container';
    plotlyContainer.style.cssText = 'margin-top: 10px; width: 100%; max-width: 900px;';

    const iframe = document.createElement('iframe');
    iframe.className = 'plotly-iframe';
    iframe.style.cssText = 'width: 100%; border: 1px solid #e7e8ea; border-radius: 8px; display: block;';
    // Sandbox: allow the chart's scripts to run but block top-level navigation /
    // new-tab popups from the blank-origin srcdoc document. (keep in sync: dashboard.js)
    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin');
    iframe.srcdoc = htmlString;
    plotlyContainer.appendChild(iframe);

    const btnContainer = document.createElement('div');
    btnContainer.style.cssText = 'display: flex; gap: 8px; margin-top: 8px;';

    const viewBtn = document.createElement('button');
    viewBtn.className = 'ghost';
    viewBtn.innerHTML = '🔍 View Larger';
    viewBtn.style.cssText = 'padding: 6px 12px; font-size: 13px;';
    viewBtn.addEventListener('click', () => {
      const modal = document.createElement('div');
      modal.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.95); z-index: 10000; display: flex; align-items: center; justify-content: center; padding: 0;';

      const modalIframe = document.createElement('iframe');
      modalIframe.setAttribute('sandbox', 'allow-scripts allow-same-origin');
      modalIframe.srcdoc = htmlString;
      modalIframe.style.cssText = 'width: 100%; height: 100%; border: none; background: white; position: relative; z-index: 1;';

      const closeBtn = document.createElement('button');
      closeBtn.textContent = '×';
      closeBtn.style.cssText = 'position: fixed; top: 20px; right: 30px; background: rgba(255,255,255,0.9); border: 2px solid white; color: #333; font-size: 40px; cursor: pointer; padding: 0; width: 50px; height: 50px; border-radius: 50%; z-index: 10001; font-weight: bold; transition: all 0.2s;';
      closeBtn.onmouseenter = () => closeBtn.style.background = 'white';
      closeBtn.onmouseleave = () => closeBtn.style.background = 'rgba(255,255,255,0.9)';

      modal.appendChild(modalIframe);
      modal.appendChild(closeBtn);
      document.body.appendChild(modal);

      const closeModal = () => modal.remove();
      closeBtn.addEventListener('click', closeModal);
      modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
      document.addEventListener('keydown', function escHandler(e) {
        if (e.key === 'Escape') {
          closeModal();
          document.removeEventListener('keydown', escHandler);
        }
      });
    });

    const downloadBtn = document.createElement('button');
    downloadBtn.className = 'ghost';
    downloadBtn.innerHTML = '📥 Download';
    downloadBtn.title = 'Download high-resolution PNG';
    downloadBtn.style.cssText = 'padding: 6px 12px; font-size: 13px;';
    downloadBtn.addEventListener('click', async () => {
      const originalLabel = downloadBtn.innerHTML;
      try {
        downloadBtn.disabled = true;
        downloadBtn.textContent = 'Generating...';

        let filename = 'chart';
        try {
          const nameRes = await fetch(`/api/chat/${chatIdRef}/generate_filename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: messageContext, type: 'image' })
          });
          const nameData = await nameRes.json();
          if (nameData && nameData.filename) filename = nameData.filename;
        } catch (_) {}

        const res = await fetch(`/api/chat/${chatIdRef}/export_plotly_png`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ html: htmlString, filename, scale: 3 })
        });
        if (!res.ok) {
          let serverMsg = `HTTP ${res.status}`;
          try {
            const errData = await res.clone().json();
            if (errData && errData.error) serverMsg = errData.error;
          } catch (_) {
            try { serverMsg = (await res.text()).slice(0, 300) || serverMsg; } catch (_) {}
          }
          console.error('Plotly download server error:', res.status, serverMsg);
          throw new Error(serverMsg);
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `${filename}.png`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
      } catch (e) {
        console.error('Plotly download failed:', e);
        alert('Download failed: ' + (e && e.message ? e.message : 'unknown error'));
      } finally {
        downloadBtn.disabled = false;
        downloadBtn.innerHTML = originalLabel;
      }
    });

    btnContainer.appendChild(viewBtn);
    btnContainer.appendChild(downloadBtn);
    plotlyContainer.appendChild(btnContainer);

    return plotlyContainer;
  }

  // Fetch and display welcome message (only once per chat)
  let welcomeShown = false;
  async function showWelcomeMessage() {
    if (welcomeShown) return; // Prevent duplicate welcome messages
    
    try {
      const res = await fetch(`/api/chat/${chatId}/welcome`);
      const data = await res.json();
      
      if (data && data.message) {
        welcomeShown = true;
        const aiDiv = createAiMessage();
        const contentDiv = document.createElement('div');
        contentDiv.innerHTML = renderMarkdown(data.message);
        aiDiv.appendChild(contentDiv);
        chatList.appendChild(aiDiv);
        
        // Auto-scroll to show welcome message
        setTimeout(() => {
          chatList.scrollTop = chatList.scrollHeight;
        }, 100);
      }
    } catch (err) {
      // Silently fail - welcome message is optional
      console.log('Could not load welcome message:', err);
    }
  }

  // Load and render chat history for the current chatId.
  async function loadHistory() {
    let js;
    try {
      const url = convId
        ? `/api/chat/${chatId}/conversation/${convId}/history`
        : `/api/chat/${chatId}/history`;
      const res = await fetch(url);
      js = await res.json();
    } catch (_) {
      js = null;
    }
    const hist = (js && Array.isArray(js.history)) ? js.history : [];
    
    // Always show welcome message first (before history or if no history)
    await showWelcomeMessage();
    
    // If no additional history, return
    if (!hist.length) {
      return;
    }

    hist.forEach((m) => {
      if (!m || !m.role) return;
      if (m.role === "human") {
        const meDiv = document.createElement("div");
        meDiv.className = "msg me";
        meDiv.textContent = typeof m.content === "string" ? m.content : "";
        chatList.appendChild(meDiv);
      } else {
        const aiDiv = createAiMessage();
        const txt = (typeof m.content === "string" ? m.content : "").trim();
        if (txt) {
          const p = document.createElement("div");
          p.innerHTML = renderMarkdown(txt);
          aiDiv.appendChild(p);
        }
        if (m.image_base64) {
          // Check if it's interactive HTML (Plotly) or base64 image
          if (m.image_base64.trim().startsWith('<') && m.image_base64.includes('plotly')) {
            aiDiv.appendChild(createPlotlyContainer(m.image_base64, chatId, txt));
          } else {
            // It's a base64 image (matplotlib/seaborn)
            const imgContainer = createImageWithFullscreen(m.image_base64, txt);
            imgContainer.querySelector('img').onload = () => {
              chatList.scrollTop = chatList.scrollHeight;
            };
            aiDiv.appendChild(imgContainer);
          }
        }
        if (m.table && typeof m.table === "object") {
          const rendered = appendTableTo(aiDiv, m.table, m.full_table_key, txt);
          if (!rendered) {
            console.error('Failed to render table in history:', m.table);
          }
        }
        chatList.appendChild(aiDiv);
      }
    });
    // Auto-scroll to bottom after history loads
    setTimeout(() => {
      chatList.scrollTop = chatList.scrollHeight;
    }, 100);
    try {
      const cum = sumTokensFromHistory(hist);
      const any = (cum.input_tokens||0) + (cum.output_tokens||0) + (cum.total_tokens||0) + (cum.reasoning_tokens||0);
      if (any > 0) renderTokens(cum, calcPriceCents(cum));
    } catch {}
  }

  function renderTokens(tok, priceCents){
    if (!tokenBar) return;
    const t = tok || {};
    const inTok = t.input_tokens != null ? t.input_tokens : (t.prompt_tokens || 0);
    const outTok = t.output_tokens != null ? t.output_tokens : (t.completion_tokens || 0);
    const total = t.total_tokens != null ? t.total_tokens : (inTok + outTok);
    const think = t.reasoning_tokens != null ? t.reasoning_tokens : 0;
    const price = (typeof priceCents === 'number' && !Number.isNaN(priceCents)) ? Number(priceCents) : null;
    const priceStr = (price !== null) ? price.toFixed(2) : null;
    tokenBar.textContent = `Tokens — input: ${inTok}  |  output: ${outTok}  |  total: ${total}`
      + (think ? `  |  reasoning: ${think}` : '')
      + (priceStr !== null ? `  |  price (cents): ${priceStr}` : '');
  }

  function calcPriceCents(t){
    try {
      const ti = typeof t.input_tokens === 'number' ? t.input_tokens : 0;
      const to = typeof t.output_tokens === 'number' ? t.output_tokens : 0;
      const tr = typeof t.reasoning_tokens === 'number' ? t.reasoning_tokens : 0;
      // Use 2.5 Flash pricing (default): $0.30/1M input, $2.50/1M output
      // Most queries use 2.5 Flash which handles reasoning well!
      const cents = (ti * 0.30 + (to + tr) * 2.50) / 10_000.0;
      return Math.round(cents * 100) / 100; // two decimals
    } catch { return 0; }
  }

  function sumTokensFromHistory(hist){
    const cum = { input_tokens: 0, output_tokens: 0, total_tokens: 0, reasoning_tokens: 0 };
    if (!Array.isArray(hist)) return cum;
    hist.forEach((m)=>{
      if (!m || m.role !== 'ai') return;
      const t = m.tokens || null;
      if (!t || typeof t !== 'object') return;
      if (typeof t.input_tokens === 'number') cum.input_tokens += t.input_tokens;
      if (typeof t.output_tokens === 'number') cum.output_tokens += t.output_tokens;
      if (typeof t.total_tokens === 'number') cum.total_tokens += t.total_tokens;
      if (typeof t.reasoning_tokens === 'number') cum.reasoning_tokens += t.reasoning_tokens;
    });
    return cum;
  }

  // Render a small table preview block (top 10) with columns and row data.
  // Returns true if table was successfully rendered, false otherwise
  function appendTableTo(div, table, fullKey, messageContext = '') {
    try {
      if (!table || typeof table !== 'object') {
        console.warn('appendTableTo: table is null or not an object', table);
        return false;
      }
      const total = typeof table.total_rows === 'number' ? table.total_rows : null;
      const rows = Array.isArray(table.rows) ? table.rows : [];
      const cols = Array.isArray(table.columns) ? table.columns : [];
      
      console.log('appendTableTo:', { total, rowCount: rows.length, colCount: cols.length });
      
      if (total !== null && total <= 0) {
        console.warn('appendTableTo: total_rows is 0, hiding table');
        return false; // hide empty
      }
      if (!rows.length) {
        console.warn('appendTableTo: no rows, hiding table');
        return false; // hide empty
      }
      if (!cols.length) {
        console.warn('appendTableTo: no columns, hiding table');
        return false; // hide empty
      }
    } catch (err) {
      console.error('appendTableTo: validation error', err);
      return false;
    }
    const meta = document.createElement("div");
    const tot = typeof table.total_rows === "number" ? table.total_rows : null;
    const what = table.dtype === "series" ? "series" : "table";
    meta.className = "meta";
    const rowWord = (n) => n === 1 ? 'row' : 'rows';
    if (tot !== null && tot <= 10) {
      meta.textContent = `Showing ${tot} ${rowWord(tot)} (${what}).`;
    } else {
      meta.textContent = tot !== null
        ? `Showing top 10 of ${tot} ${rowWord(tot)} (${what}).`
        : `Showing top 10 (${what}).`;
    }
    div.appendChild(meta);

    // Prepare preview data
    const previewRows = Array.isArray(table.rows) ? table.rows.slice() : [];
    const previewCols = Array.isArray(table.columns) ? table.columns.slice() : [];
    let tbody = null;
    let thead = null;

    // Check if styled HTML is available (from pandas Styler with conditional formatting)
    console.log('Table object received:', { hasStyledHtml: !!table.styled_html, styledHtmlLen: table.styled_html ? table.styled_html.length : 0, keys: Object.keys(table) });
    if (table.styled_html && typeof table.styled_html === 'string') {
      console.log('Rendering styled HTML table');
      // Render styled HTML directly to preserve colors and formatting
      const styledContainer = document.createElement('div');
      styledContainer.className = 'styled-table-container';
      styledContainer.innerHTML = table.styled_html;
      
      // Apply consistent styling to the styled table
      const styledTable = styledContainer.querySelector('table');
      if (styledTable) {
        styledTable.style.cssText = 'border-collapse: collapse; width: 100%; font-size: 14px; margin-top: 8px;';
        // Style cells
        styledTable.querySelectorAll('th, td').forEach(cell => {
          cell.style.border = '1px solid #e3e6ea';
          cell.style.padding = '6px 8px';
          cell.style.textAlign = 'left';
        });
        // Style header
        styledTable.querySelectorAll('th').forEach(th => {
          if (!th.style.backgroundColor) {
            th.style.backgroundColor = '#eef3ff';
          }
        });
      }
      
      div.appendChild(styledContainer);
    } else {
      // Fallback: render plain table from rows/columns
      const tbl = document.createElement("table");
      tbl.className = "datachat";
      thead = document.createElement("thead");
      const trh = document.createElement("tr");
      (table.columns || []).forEach((c) => {
        const th = document.createElement("th");
        th.textContent = c;
        trh.appendChild(th);
      });
      thead.appendChild(trh);
      tbl.appendChild(thead);

      tbody = document.createElement("tbody");
      (previewRows).forEach((row) => {
        const tr = document.createElement("tr");
        (previewCols).forEach((c) => {
          const td = document.createElement("td");
          let v = row && Object.prototype.hasOwnProperty.call(row, c) ? row[c] : "";
          if (v === null || v === undefined) v = "";
          td.textContent = String(v);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      tbl.appendChild(tbody);
      div.appendChild(tbl);
    }
    
    // Declare these variables in outer scope so download button can access them
    let showingFull = false;
    let fullData = null;
    
    if (fullKey && (tot === null || (Array.isArray(previewRows) && tot > previewRows.length))) {
      const btn = document.createElement('button');
      btn.className = 'ghost';
      btn.textContent = 'Show full table';
      btn.style.marginTop = '6px';
      const renderRows = (cols, rows) => {
        tbody.innerHTML = '';
        // update header if columns differ
        if (Array.isArray(cols) && cols.join('|') !== previewCols.join('|')) {
          thead.innerHTML = '';
          const trh2 = document.createElement('tr');
          cols.forEach((c)=>{ const th = document.createElement('th'); th.textContent = c; trh2.appendChild(th); });
          thead.appendChild(trh2);
        }
        rows.forEach((row)=>{
          const tr = document.createElement('tr');
          (cols || []).forEach((c)=>{
            const td = document.createElement('td');
            let v = row && Object.prototype.hasOwnProperty.call(row, c) ? row[c] : '';
            if (v === null || v === undefined) v = '';
            td.textContent = String(v);
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
      };
      btn.addEventListener('click', async ()=>{
        if (!showingFull) {
          try {
            if (!fullData) {
              const res = await fetch(`/api/chat/${chatId}/full_table/${fullKey}`);
              const js = await res.json();
              if (!res.ok || !js || !Array.isArray(js.rows)) return;
              fullData = js;
            }
            renderRows(fullData.columns || previewCols, fullData.rows || []);
            const fullLen = Array.isArray(fullData.rows) ? fullData.rows.length : 0;
            meta.textContent = `Showing full ${fullLen} ${fullLen === 1 ? 'row' : 'rows'} (${what}).`;
            btn.textContent = 'Show top 10 rows';
            showingFull = true;
          } catch {}
        } else {
          renderRows(previewCols, previewRows);
          if (tot !== null && tot <= 10) {
            meta.textContent = `Showing ${tot} ${tot === 1 ? 'row' : 'rows'} (${what}).`;
          } else {
            meta.textContent = tot !== null ? `Showing top 10 of ${tot} ${tot === 1 ? 'row' : 'rows'} (${what}).` : `Showing top 10 (${what}).`;
          }
          btn.textContent = 'Show full table';
          showingFull = false;
        }
      });
      div.appendChild(btn);
    }
    
    // Add Excel download button
    const downloadBtn = document.createElement('button');
    downloadBtn.className = 'ghost';
    downloadBtn.innerHTML = '📥 Download Excel';
    downloadBtn.style.cssText = 'margin-top: 6px; padding: 6px 12px; font-size: 13px;';
    
    downloadBtn.addEventListener('click', async () => {
      try {
        downloadBtn.disabled = true;
        downloadBtn.textContent = 'Downloading...';

        const timestamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '_');
        const filename = `table_${timestamp}`;
        let excelResponse;

        if (fullKey) {
          // Use new endpoint: re-executes code server-side for full data
          excelResponse = await fetch(`/api/chat/${chatId}/download_excel/${fullKey}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: filename })
          });
        } else {
          // No full key — export the preview table directly
          excelResponse = await fetch(`/api/chat/${chatId}/export_excel`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              columns: table.columns || [],
              rows: table.rows || [],
              filename: filename
            })
          });
        }

        if (!excelResponse.ok) throw new Error('Export failed');

        const blob = await excelResponse.blob();
        const url = window.URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `${filename}.xlsx`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        window.URL.revokeObjectURL(url);

        downloadBtn.textContent = '📥 Download Excel';
      } catch (e) {
        console.error('Download failed:', e);
        alert('Failed to download Excel file');
        downloadBtn.textContent = '📥 Download Excel';
      } finally {
        downloadBtn.disabled = false;
      }
    });
    
    div.appendChild(downloadBtn);
    
    return true; // Successfully rendered table
  }

  // Send the user's question to /api/chat/{chatId}/chat and render the response.
  async function send() {
    const q = textarea.value.trim();
    if (!q) return;
    const meDiv = document.createElement("div");
    meDiv.className = "msg me";
    meDiv.textContent = q;
    chatList.appendChild(meDiv);
    textarea.value = "";
    // Auto-scroll to bottom after user message
    chatList.scrollTop = chatList.scrollHeight;

    // Show thinking indicator
    const thinkingDiv = document.createElement("div");
    thinkingDiv.className = "msg ai thinking-msg";
    thinkingDiv.innerHTML = '<div class="thinking-indicator"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span style="margin-left: 8px;">Thinking...</span></div>';
    chatList.appendChild(thinkingDiv);
    chatList.scrollTop = chatList.scrollHeight;

    let js;
    try {
      const res = await fetch(`/api/chat/${chatId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, conv_id: convId || null }),
      });
      try { js = await res.json(); } catch { js = { error: "Bad response from server" }; }
    } catch (err) {
      js = { error: "Network error: " + (err.message || "Could not reach server") };
    }

    // Remove thinking indicator
    thinkingDiv.remove();

    const aiDiv = createAiMessage();
    let hasContent = false;

    if (js.error) {
      aiDiv.textContent = "❌ " + js.error;
      hasContent = true;
    } else {
      if (Object.prototype.hasOwnProperty.call(js, 'tokens_cumulative')) {
        const pc = Object.prototype.hasOwnProperty.call(js, 'price_cents_cumulative') ? js.price_cents_cumulative : calcPriceCents(js.tokens_cumulative || {});
        renderTokens(js.tokens_cumulative || {}, pc);
      } else if (Object.prototype.hasOwnProperty.call(js, 'tokens')) {
        renderTokens(js.tokens || {}, null);
      }
      if (!convId && js.conv_id) {
        convId = js.conv_id;
        try {
          const sp = new URLSearchParams(location.search);
          sp.set('conv_id', convId);
          history.replaceState(null, '', location.pathname + '?' + sp.toString());
        } catch {}
      }
      if (js.answer && js.answer.trim()) {
        const p = document.createElement("div");
        p.innerHTML = renderMarkdown(js.answer.trim());
        aiDiv.appendChild(p);
        hasContent = true;
      }
      // Only show result_preview if there's no table (table is the formatted version)
      if (js.result_preview !== null && js.result_preview !== undefined && !js.table) {
        const previewDiv = document.createElement("div");
        previewDiv.style.marginTop = "8px";
        previewDiv.style.padding = "10px";
        previewDiv.style.backgroundColor = "#f9fafb";
        previewDiv.style.border = "1px solid #e5e7eb";
        previewDiv.style.borderRadius = "6px";
        previewDiv.style.fontFamily = "ui-monospace, SFMono-Regular, monospace";
        previewDiv.style.fontSize = "13px";
        previewDiv.style.whiteSpace = "pre-wrap";
        previewDiv.style.wordBreak = "break-word";
        try {
          previewDiv.textContent = typeof js.result_preview === "string" ? js.result_preview : JSON.stringify(js.result_preview, null, 2);
        } catch {
          previewDiv.textContent = String(js.result_preview);
        }
        aiDiv.appendChild(previewDiv);
        hasContent = true;
      }
      if (js.image_base64) {
        // Check if it's interactive HTML (Plotly) or base64 image
        if (js.image_base64.trim().startsWith('<') && js.image_base64.includes('plotly')) {
          aiDiv.appendChild(createPlotlyContainer(js.image_base64, chatId, js.answer || q));
          hasContent = true;
        } else {
          // It's a base64 image (matplotlib/seaborn)
          const imgContainer = createImageWithFullscreen(js.image_base64, js.answer || q);
          imgContainer.querySelector('img').onload = () => {
            chatList.scrollTop = chatList.scrollHeight;
          };
          aiDiv.appendChild(imgContainer);
          hasContent = true;
        }
      }
      if (js.table) {
        const tableRendered = appendTableTo(aiDiv, js.table, js.full_table_key, js.answer || q);
        if (tableRendered) {
          hasContent = true;
        } else {
          console.error('Failed to render table:', js.table);
        }
      }
      // Fallback: if no content was added, show a message so user knows something happened
      if (!hasContent) {
        const fallbackP = document.createElement("div");
        fallbackP.textContent = "⚠️ I processed your request but couldn't generate a visible response. Please try rephrasing your question.";
        fallbackP.style.color = "#92400e";
        aiDiv.appendChild(fallbackP);
      }
    }

    chatList.appendChild(aiDiv);
    // Auto-scroll to bottom after content renders
    setTimeout(() => {
      chatList.scrollTop = chatList.scrollHeight;
    }, 50);
  }

  askBtn.addEventListener("click", send);
  textarea.addEventListener("keypress", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  // Helper to show a system message in chat (not from AI, just info)
  function showSystemMessage(text, type = 'info') {
    const div = document.createElement('div');
    div.className = 'msg system';
    div.style.cssText = `
      background: ${type === 'warning' ? '#fef3c7' : '#f0f9ff'};
      color: ${type === 'warning' ? '#92400e' : '#0369a1'};
      border-left: 3px solid ${type === 'warning' ? '#f59e0b' : '#0ea5e9'};
      padding: 8px 12px;
      margin: 8px 12px;
      border-radius: 6px;
      font-size: 13px;
    `;
    div.textContent = text;
    chatList.appendChild(div);
    chatList.scrollTop = chatList.scrollHeight;
    return div;
  }

  // Store pending change data to show after welcome message
  let pendingChangeData = null;

  // Refresh linked Google Drive/Sheets sources (if any) before loading history
  async function refreshLinkedSources() {
    let checkingDiv = null;
    
    try {
      // Show checking message immediately (will be visible during download)
      checkingDiv = showSystemMessage('🔄 Checking linked Google Sheets...', 'info');
      
      const res = await fetch(`/api/chat/${chatId}/refresh_sources`, { method: 'POST' });
      const data = await res.json();
      
      // Debug log to see what backend returns
      console.log('Refresh sources response:', JSON.stringify(data, null, 2));
      
      // Remove checking message
      if (checkingDiv && checkingDiv.parentNode) {
        checkingDiv.remove();
      }
      
      // If no linked sources, nothing more to show
      if (!data || !data.has_linked_sources) {
        console.log('No linked sources found');
        return;
      }
      
      // If there were changes, store them to show after welcome message
      if (data.has_changes) {
        console.log('Changes detected, will show after welcome');
        pendingChangeData = data;
      } else {
        console.log('No changes detected');
      }
      
    } catch (e) {
      // Remove checking message if shown
      if (checkingDiv && checkingDiv.parentNode) {
        checkingDiv.remove();
      }
      console.log('Source refresh skipped:', e.message);
    }
  }

  // Show pending file changes (called after welcome/history is shown)
  function showPendingFileChanges() {
    if (!pendingChangeData) return;
    
    const added = pendingChangeData.added_columns || [];
    const removed = pendingChangeData.removed_columns || [];
    
    // Build summary message
    let summary = '📊 **Linked file updated:**\n';
    if (added.length > 0) {
      const addedCols = added.map(a => `• \`${a.column}\``).join('\n');
      summary += `\n**New columns added:**\n${addedCols}\n`;
    }
    if (removed.length > 0) {
      const removedCols = removed.map(r => `• \`${r.column}\``).join('\n');
      summary += `\n**Columns removed:**\n${removedCols}\n`;
    }
    
    // Show as AI-style message with the changes
    const aiDiv = createAiMessage();
    const p = document.createElement('div');
    p.innerHTML = renderMarkdown(summary);
    aiDiv.appendChild(p);
    chatList.appendChild(aiDiv);
    chatList.scrollTop = chatList.scrollHeight;
    
    // Clear pending data
    pendingChangeData = null;
  }

  // Initialize: refresh sources, then load history, then show changes
  refreshLinkedSources().finally(async () => {
    await loadHistory();
    // Show file change message after welcome/history
    showPendingFileChanges();
  });


  // Fallback: ensure clicking the centered logo always navigates to home (even if anchor click is intercepted)
  try {
    const logo = document.querySelector('.topbar .logo-header');
    if (logo) {
      // Ensure the anchor receives the click, not the img
      const img = logo.querySelector('img');
      if (img) img.style.pointerEvents = 'none';
      logo.style.pointerEvents = 'auto';
      // If it's not an anchor, or navigation is blocked, force navigate to home
      logo.addEventListener('click', (e)=>{
        // Let native anchor work if present
        if (logo.tagName && logo.tagName.toLowerCase() === 'a') return;
        e.preventDefault();
        location.href = '/';
      }, { passive: true });
    }
  } catch {}

  // Setup close handler for schema viewer modal
  const closeSchemaViewer = document.getElementById('closeSchemaViewer');
  const schemaViewerModal = document.getElementById('schemaViewerModal');
  
  if (closeSchemaViewer && schemaViewerModal) {
    closeSchemaViewer.addEventListener('click', () => {
      schemaViewerModal.classList.add('hidden');
      schemaViewerModal.style.display = 'none'; // Override inline style
    });
    schemaViewerModal.addEventListener('click', (e) => {
      if (e.target === schemaViewerModal) {
        schemaViewerModal.classList.add('hidden');
        schemaViewerModal.style.display = 'none'; // Override inline style
      }
    });
  }
})();
