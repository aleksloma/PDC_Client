// Dashboard JavaScript - Main application logic

// State
let currentChatId = null;
let currentConvId = null;
let selectedFiles = [];
let userMaxFiles = 1;
let currentSubscriptionSource = 'default';
let currentSubscriptionPlan = 'Basic';
let paddleScheduledChange = null;
let paddleScheduledPlan = null;
let isCurrentUserAdmin = !!(window.__IS_ADMIN__);
let isRequestInProgress = false;
let isFrictionlessFlowRunning = false;

const ALLOWED_UPLOAD_EXTENSIONS = ['xlsx', 'xls', 'csv', 'tsv'];

function _hasValidUploadExtension(filename) {
  if (!filename) return false;
  const idx = filename.lastIndexOf('.');
  if (idx < 0) return false;
  const ext = filename.slice(idx + 1).toLowerCase();
  return ALLOWED_UPLOAD_EXTENSIONS.includes(ext);
}

function _t(key, fallback) {
  if (window.i18n && typeof window.i18n.t === 'function') {
    const v = window.i18n.t(key);
    if (v && v !== key) return v;
  }
  return fallback;
}

// Files larger than this go through the signed-URL flow (browser → GCS direct),
// bypassing Cloud Run's 32 MiB request-body limit. Smaller files keep using the
// legacy POST /upload path unchanged.
const LARGE_UPLOAD_THRESHOLD_BYTES = 25 * 1024 * 1024;

// Stream one file through the three-step direct-to-GCS flow.
// Returns the parsed /upload/finalize JSON on success; throws on any failure.
async function _directUploadFile(file, description) {
  // 1. /upload/init — get a signed PUT URL
  const initRes = await fetch('/upload/init', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      filename: file.name,
      content_type: file.type || 'application/octet-stream',
      size_bytes: file.size,
    }),
  });
  let initData = null;
  try { initData = await initRes.json(); } catch (e) { initData = null; }
  if (!initRes.ok || !initData || !initData.signed_url || !initData.gcs_path) {
    const msg = (initData && (initData.error || initData.detail)) || `Upload init failed (${initRes.status})`;
    throw new Error(msg);
  }

  // 2. PUT the bytes straight to GCS via XHR (so upload.onprogress works)
  await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', initData.signed_url, true);
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    xhr.upload.onprogress = (evt) => {
      if (!evt.lengthComputable) return;
      const pct = Math.round((evt.loaded / evt.total) * 100);
      const statusEl = document.getElementById('frictionlessStatus');
      if (statusEl) statusEl.textContent = `Uploading ${file.name}: ${pct}%`;
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`Upload PUT failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error('Upload PUT network error'));
    xhr.onabort = () => reject(new Error('Upload PUT aborted'));
    xhr.send(file);
  });

  // 3. /upload/finalize — pull the GCS object into the user's store and run Steps 2-5
  const finalizeBody = { gcs_path: initData.gcs_path };
  if (description) finalizeBody.file_descriptions = { [file.name]: description };
  const finRes = await fetch('/upload/finalize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(finalizeBody),
  });
  let finData = null;
  try { finData = await finRes.json(); } catch (e) { finData = null; }
  if (!finRes.ok || !finData || finData.error) {
    const msg = (finData && (finData.error || finData.detail)) || `Upload finalize failed (${finRes.status})`;
    throw new Error(msg);
  }
  return finData;
}

// Frictionless flow progress overlay (separate from generic loadingOverlay so it can stay
// up across upload → autofill → generate steps without flicker)
function _showFrictionlessOverlay(statusKey) {
  const overlay = document.getElementById('frictionlessOverlay');
  const statusEl = document.getElementById('frictionlessStatus');
  if (statusEl) statusEl.textContent = _t(statusKey, statusKey);
  if (overlay) overlay.classList.remove('hidden');
}
function _updateFrictionlessStatus(statusKey) {
  const statusEl = document.getElementById('frictionlessStatus');
  if (statusEl) statusEl.textContent = _t(statusKey, statusKey);
}
function _hideFrictionlessOverlay() {
  const overlay = document.getElementById('frictionlessOverlay');
  if (overlay) overlay.classList.add('hidden');
}

// Simple cache for list data (stale-while-revalidate pattern)
const listCache = {
  conversations: { data: null, timestamp: 0 },
  activeChats: { data: null, timestamp: 0 },
  TTL: 60000, // 1 minute TTL

  get(key) {
    const entry = this[key];
    if (!entry || !entry.data) return null;
    return entry.data; // Return even if stale - we'll refresh in background
  },

  isStale(key) {
    const entry = this[key];
    if (!entry || !entry.timestamp) return true;
    return Date.now() - entry.timestamp > this.TTL;
  },

  set(key, data) {
    if (this[key]) {
      this[key].data = data;
      this[key].timestamp = Date.now();
    }
  }
};

// Track expanded chats for nested conversations
let expandedChats = new Set();

// DOM Elements
const welcomeState = document.getElementById('welcomeState');
const chatState = document.getElementById('chatState');
const chatMessages = document.getElementById('chatMessages');
const chatInput = document.getElementById('chatInput');

// Loading helpers
function showLoading(text = 'Processing...') {
  const overlay = document.getElementById('loadingOverlay');
  const loadingText = document.getElementById('loadingText');
  if (overlay) {
    if (loadingText) loadingText.textContent = text;
    overlay.classList.remove('hidden');
  }
}

function hideLoading() {
  const overlay = document.getElementById('loadingOverlay');
  if (overlay) overlay.classList.add('hidden');
}

// Toast notification (replaces browser alerts)
function showToast(text, isError = false) {
  const t = document.createElement('div');
  t.textContent = text;
  t.style.cssText = `position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;color:#fff;font-size:14px;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.15);transition:opacity 0.3s;background:${isError ? '#ef4444' : '#10b981'};`;
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 3000);
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', async () => {
  // Apply stored language to all static text before listeners wire up
  if (window.i18n) window.i18n.init();

  // Setup event listeners FIRST so buttons are immediately responsive
  setupEventListeners();
  
  // Check authentication
  const authRes = await fetch('/auth/me');
  const authData = await authRes.json();
  if (!authData.authenticated) {
    window.location.href = '/';
    return;
  }

  // Load lists and profile in parallel for faster loading
  // (profile doesn't block list display)
  await Promise.all([
    loadUserProfile(),
    loadUnifiedChatList()
  ]);

  // Auto-open conversation if arriving via /c/{conv_id} URL
  if (window.OPEN_CONV_ID && window.OPEN_CHAT_ID) {
    await openConversation(window.OPEN_CHAT_ID, window.OPEN_CONV_ID);
    window.OPEN_CONV_ID = '';
    window.OPEN_CHAT_ID = '';
  }
});

// Browser back/forward navigation
window.addEventListener('popstate', (e) => {
  if (e.state && e.state.convId) {
    openConversation(e.state.chatId, e.state.convId);
  } else {
    // Return to blank state without pushing history
    currentChatId = null;
    currentConvId = null;
    _highlightActiveConv(null);
    chatState.classList.add('hidden');
    welcomeState.classList.remove('hidden');
    showTopBarActions(false);
  }
});

// Load user profile
async function loadUserProfile() {
  try {
    const res = await fetch('/auth/profile');
    const profile = await res.json();
    userMaxFiles = getMaxFilesForPlan(profile.subscription_plan || 'Basic');
    currentSubscriptionSource = profile.subscription_source || 'default';
    currentSubscriptionPlan = profile.subscription_plan || 'Basic';
    paddleScheduledChange = profile.paddle_scheduled_change || null;
    paddleScheduledPlan = profile.paddle_scheduled_plan || null;
    isCurrentUserAdmin = !!profile.is_admin;

    // Update profile modal
    document.getElementById('profUsername').value = profile.username || '';
    document.getElementById('profFullName').value = profile.full_name || '';
    document.getElementById('profEmail').value = profile.email || '';
    document.getElementById('currentPlanName').textContent = profile.subscription_plan || 'Basic';

    // Highlight current plan and update button labels
    document.querySelectorAll('.plan-card').forEach(card => {
      card.classList.toggle('active', card.dataset.plan === profile.subscription_plan);
    });
    updatePlanButtons();

    // Show subscription status info
    var statusEl = document.getElementById('subscriptionStatus');
    if (statusEl) {
      if (paddleScheduledChange === 'cancel') {
        var expiresAt = profile.paddle_billing_period_end;
        var dateStr = expiresAt ? new Date(expiresAt).toLocaleDateString() : 'end of billing period';
        statusEl.innerHTML = '<span style="color:#f59e0b;">Cancellation scheduled — your ' + currentSubscriptionPlan +
          ' plan stays active until ' + dateStr + '.</span> ' +
          '<button class="ghost plan-select-sm" onclick="reactivateSubscription()">Undo Cancellation</button>';
        statusEl.style.display = 'block';
      } else if (paddleScheduledChange === 'downgrade' && paddleScheduledPlan) {
        var expiresAt = profile.paddle_billing_period_end;
        var dateStr = expiresAt ? new Date(expiresAt).toLocaleDateString() : 'next billing date';
        statusEl.innerHTML = '<span style="color:#f59e0b;">Switching to ' + paddleScheduledPlan +
          ' on ' + dateStr + '. You keep ' + currentSubscriptionPlan + ' features until then.</span> ' +
          '<button class="ghost plan-select-sm" onclick="reactivateSubscription()">Undo Downgrade</button>';
        statusEl.style.display = 'block';
      } else if (currentSubscriptionSource === 'paddle' && currentSubscriptionPlan !== 'Basic') {
        var billingEnd = profile.paddle_billing_period_end;
        if (billingEnd) {
          statusEl.innerHTML = '<span style="color:#64748b;">Renews on ' + new Date(billingEnd).toLocaleDateString() + '</span>';
          statusEl.style.display = 'block';
        } else {
          statusEl.style.display = 'none';
        }
      } else {
        statusEl.style.display = 'none';
      }
    }

    // Update profile button in topbar (initials + color set server-side, just update text)
    const fullName = profile.full_name || profile.username || '';
    const displayName = fullName || profile.username || 'User';
    const plan = profile.subscription_plan || 'Basic';
    document.getElementById('profileBtnName').textContent = displayName;
    document.getElementById('profileBtnPlan').textContent = plan;

    // Disable password section for Google OAuth users
    const isGoogleUser = profile.provider === 'google';
    const pwSection = document.getElementById('passwordSection');
    const pwFields = document.getElementById('passwordFields');
    const pwNotice = document.getElementById('passwordGoogleNotice');
    const btnChangePw = document.getElementById('btnChangePw');
    if (isGoogleUser) {
      pwSection.classList.add('disabled-section');
      pwFields.classList.add('hidden');
      pwNotice.classList.remove('hidden');
      btnChangePw.classList.add('hidden');
    } else {
      pwSection.classList.remove('disabled-section');
      pwFields.classList.remove('hidden');
      pwNotice.classList.add('hidden');
      btnChangePw.classList.remove('hidden');
    }
  } catch (e) {
    console.error('Failed to load profile:', e);
  }
}

// Update plan button labels based on current plan context
function updatePlanButtons() {
  document.querySelectorAll('.plan-select').forEach(function(btn) {
    var plan = btn.dataset.plan;
    if (plan === 'Enterprise') { btn.textContent = 'Contact Us'; return; }
    if (plan === currentSubscriptionPlan) {
      btn.textContent = 'Current Plan';
      btn.disabled = (paddleScheduledChange !== 'cancel' && paddleScheduledChange !== 'downgrade');
    } else if (paddleScheduledChange === 'downgrade' && plan === paddleScheduledPlan) {
      btn.textContent = 'Scheduled';
      btn.disabled = true;
    } else if (plan === 'Basic') {
      btn.textContent = 'Downgrade to Basic';
      btn.disabled = false;
    } else if (currentSubscriptionPlan === 'Basic') {
      btn.textContent = 'Get ' + plan;
      btn.disabled = false;
    } else {
      var rank = { Basic: 0, Standard: 1, Pro: 2, Enterprise: 3 };
      if (rank[plan] > rank[currentSubscriptionPlan]) {
        btn.textContent = 'Upgrade to ' + plan;
      } else {
        btn.textContent = 'Switch to ' + plan;
      }
      btn.disabled = false;
    }
  });
  // Highlight Enterprise card if active
  var entCard = document.getElementById('planEnterpriseCard');
  if (entCard) {
    if (currentSubscriptionPlan === 'Enterprise') entCard.classList.add('active');
    else entCard.classList.remove('active');
  }
}

// ---------------------------------------------------------------------------
// Subscription modal helpers
// ---------------------------------------------------------------------------
function _subModal() { return document.getElementById('subscriptionModal'); }
function _subTitle() { return document.getElementById('subModalTitle'); }
function _subBody()  { return document.getElementById('subModalBody'); }
function _subActions(){ return document.getElementById('subModalActions'); }

function _openSubModal(title, bodyHtml, actionsHtml) {
  _subTitle().textContent = title;
  _subBody().innerHTML = bodyHtml;
  _subActions().innerHTML = actionsHtml || '';
  _subModal().classList.remove('hidden');
}

function _closeSubModal() {
  _subModal().classList.add('hidden');
}

function _fmtCurrency(amount, currency) {
  var sym = currency === 'USD' ? '$' : currency + ' ';
  return sym + amount;
}

async function _updatePaymentMethod() {
  _subBody().innerHTML = '<div class="sub-modal-spinner"><p>Loading payment details...</p></div>';
  _subActions().innerHTML = '';
  try {
    var res = await fetch('/api/paddle/subscription/update-payment', { method: 'POST' });
    var data = await res.json();
    if (data.ok && data.transaction_id && window.Paddle) {
      _closeSubModal();
      Paddle.Checkout.open({ transactionId: data.transaction_id });
    } else {
      _openSubModal('Error', '<div class="sub-modal-highlight error">' + (data.error || 'Failed to load payment update') + '</div>',
        '<button class="ghost" onclick="_closeSubModal()">Close</button>');
    }
  } catch (e) {
    _openSubModal('Error', '<div class="sub-modal-highlight error">Failed to load payment details</div>',
      '<button class="ghost" onclick="_closeSubModal()">Close</button>');
  }
}

// Reactivate (undo scheduled cancellation) — now via modal
async function reactivateSubscription() {
  var title, desc;
  if (paddleScheduledChange === 'downgrade' && paddleScheduledPlan) {
    title = 'Keep ' + currentSubscriptionPlan + ' Plan';
    desc = '<p>Your plan is scheduled to switch to ' + paddleScheduledPlan + '.</p>' +
           '<p>Would you like to keep your ' + currentSubscriptionPlan + ' plan instead?</p>';
  } else {
    title = 'Keep ' + currentSubscriptionPlan + ' Plan';
    var statusSpan = document.querySelector('#subscriptionStatus span');
    var dateText = statusSpan ? statusSpan.textContent.replace(/.*until /, '').replace(/\..*/, '') : 'the end of your billing period';
    desc = '<p>Your ' + currentSubscriptionPlan + ' plan is scheduled to cancel on ' + dateText + '.</p>' +
           '<p>Would you like to continue your subscription?</p>';
  }
  _openSubModal(title, desc,
    '<button class="primary" onclick="_doReactivate()">Keep My Plan</button>' +
    '<button class="ghost" onclick="_closeSubModal()">Never Mind</button>'
  );
}

async function _doReactivate() {
  _subBody().innerHTML = '<div class="sub-modal-spinner"><p>Reactivating...</p></div>';
  _subActions().innerHTML = '';
  try {
    var res = await fetch('/api/paddle/subscription/reactivate', { method: 'POST' });
    var data = await res.json();
    if (data.ok) {
      _openSubModal('Plan Reactivated',
        '<div class="sub-modal-highlight success">Your ' + (data.plan || currentSubscriptionPlan) + ' plan will continue. No changes have been made.</div>',
        '<button class="primary" onclick="_closeSubModal(); loadUserProfile();">Done</button>');
    } else {
      _openSubModal('Error',
        '<div class="sub-modal-highlight error">' + (data.error || 'Failed to reactivate') + '</div>',
        '<button class="ghost" onclick="_closeSubModal()">Close</button>');
    }
  } catch (e) {
    _openSubModal('Error',
      '<div class="sub-modal-highlight error">Failed to reactivate subscription</div>',
      '<button class="ghost" onclick="_closeSubModal()">Close</button>');
  }
}

function getMaxFilesForPlan(plan) {
  const limits = { Basic: 1, Standard: 5, Pro: 10, Enterprise: 999 };
  return limits[plan] || 1;
}

// Enterprise "Contact Us" — open About modal scrolled to contact section
function openAboutContact() {
  var profile = document.getElementById('profileModal');
  if (profile) profile.classList.add('hidden');
  var modal = document.getElementById('aboutModal');
  if (modal) {
    modal.classList.remove('hidden');
    setTimeout(function() {
      var section = document.getElementById('contact-us');
      if (section) section.scrollIntoView({ behavior: 'smooth' });
    }, 100);
  }
}

// Load conversations (with cache)
async function loadConversations(forceRefresh = false) {
  const container = document.getElementById('conversationList');

  // Show cached data immediately if available
  const cached = listCache.get('conversations');
  if (cached && !forceRefresh) {
    renderConversations(cached);
    // If not stale, we're done
    if (!listCache.isStale('conversations')) return;
    // Otherwise, refresh in background (don't await)
    fetchConversations().then(data => {
      if (data) renderConversations(data);
    });
    return;
  }

  // No cache - fetch and render
  const data = await fetchConversations();
  if (data) renderConversations(data);
}

async function fetchConversations() {
  try {
    const res = await fetch('/auth/conversations');
    const data = await res.json();
    const conversations = data.conversations || [];
    listCache.set('conversations', conversations);
    return conversations;
  } catch (e) {
    console.error('Failed to fetch conversations:', e);
    return null;
  }
}

function renderConversations(conversations) {
  const container = document.getElementById('conversationList');
  if (conversations.length === 0) {
    container.innerHTML = '<div class="list-empty">No conversations yet</div>';
    return;
  }
  container.innerHTML = conversations.map(conv => `
    <div class="list-item-wrapper">
      <button class="list-item" data-type="conversation" data-chat-id="${conv.chat_id}" data-conv-id="${conv.conv_id}">
        <span class="list-item-title">${escapeHtml(conv.title || 'Untitled')}</span>
        <span class="list-item-subtitle">${conv.chat_name || ''}</span>
      </button>
      <button class="item-menu-btn" data-type="conversation" data-chat-id="${conv.chat_id}" data-conv-id="${conv.conv_id}" data-title="${escapeHtml(conv.title || 'Untitled')}" title="More options">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
          <circle cx="8" cy="3" r="1.5"/>
          <circle cx="8" cy="8" r="1.5"/>
          <circle cx="8" cy="13" r="1.5"/>
        </svg>
      </button>
    </div>
  `).join('');
  attachMenuListeners();
}

// Load all active chats (my chats + shared) - SINGLE fetch, with cache
async function loadActiveChats(forceRefresh = false) {
  // Show cached data immediately if available
  const cached = listCache.get('activeChats');
  if (cached && !forceRefresh) {
    renderMyChats(cached.filter(c => !c.is_shared));
    renderSharedChats(cached.filter(c => c.is_shared));
    // If not stale, we're done
    if (!listCache.isStale('activeChats')) return;
    // Refresh in background
    fetchActiveChats().then(data => {
      if (data) {
        renderMyChats(data.filter(c => !c.is_shared));
        renderSharedChats(data.filter(c => c.is_shared));
      }
    });
    return;
  }
  // No cache - fetch and render
  const data = await fetchActiveChats();
  if (data) {
    renderMyChats(data.filter(c => !c.is_shared));
    renderSharedChats(data.filter(c => c.is_shared));
  }
}

async function fetchActiveChats() {
  try {
    const res = await fetch('/auth/active_chats');
    const data = await res.json();
    const chats = data.active_chats || [];
    listCache.set('activeChats', chats);
    return chats;
  } catch (e) {
    console.error('Failed to fetch active chats:', e);
    return null;
  }
}

function renderMyChats(chats) {
  const container = document.getElementById('myChatsList');
  if (chats.length === 0) {
    container.innerHTML = '<div class="list-empty">No chats yet</div>';
    return;
  }
  container.innerHTML = chats.map(chat => `
    <div class="list-item-wrapper">
      <button class="list-item${chat.chat_id === currentChatId ? ' active' : ''}" data-type="chat" data-chat-id="${chat.chat_id}" data-slug="${chat.slug || ''}">
        <span class="list-item-title">${escapeHtml(chat.name || 'Untitled')}</span>
        <span class="list-item-subtitle">${chat.files ? chat.files.slice(0, 2).join(', ') : ''}</span>
      </button>
      <button class="item-menu-btn" data-type="chat" data-chat-id="${chat.chat_id}" data-name="${escapeHtml(chat.name || 'Untitled')}" title="More options">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
          <circle cx="8" cy="3" r="1.5"/>
          <circle cx="8" cy="8" r="1.5"/>
          <circle cx="8" cy="13" r="1.5"/>
        </svg>
      </button>
    </div>
  `).join('');
  attachMenuListeners();
}

function renderSharedChats(chats) {
  const container = document.getElementById('sharedList');
  if (chats.length === 0) {
    container.innerHTML = '<div class="list-empty">No shared chats</div>';
    return;
  }
  container.innerHTML = chats.map(chat => `
    <div class="list-item-wrapper">
      <button class="list-item${chat.chat_id === currentChatId ? ' active' : ''}" data-type="chat" data-chat-id="${chat.chat_id}" data-slug="${chat.slug || ''}">
        <span class="list-item-title">${escapeHtml(chat.name || 'Untitled')}</span>
        <span class="list-item-subtitle">Shared by ${chat.owner || 'unknown'}</span>
      </button>
      <button class="item-menu-btn" data-type="chat" data-chat-id="${chat.chat_id}" data-name="${escapeHtml(chat.name || 'Untitled')}" data-shared="true" title="More options">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
          <circle cx="8" cy="3" r="1.5"/>
          <circle cx="8" cy="8" r="1.5"/>
          <circle cx="8" cy="13" r="1.5"/>
        </svg>
      </button>
    </div>
  `).join('');
  attachMenuListeners();
}

// Backward compat wrappers (some places call these directly)
async function loadMyChats(forceRefresh = false) {
  return loadUnifiedChatList(forceRefresh);
}
async function loadSharedChats(forceRefresh = false) {
  return loadUnifiedChatList(forceRefresh);
}

// ===== NEW UNIFIED CHAT LIST =====

// Load unified chat list (combines chats and conversations)
async function loadUnifiedChatList(forceRefresh = false) {
  // Load chats and conversations in parallel
  const [chatsData, conversationsData] = await Promise.all([
    fetchActiveChats(forceRefresh),
    fetchConversations(forceRefresh)
  ]);

  const chats = chatsData || [];
  const conversations = conversationsData || [];

  renderUnifiedChatList(chats, conversations);
}

async function fetchActiveChats(forceRefresh = false) {
  const cached = listCache.get('activeChats');
  if (cached && !forceRefresh && !listCache.isStale('activeChats')) {
    return cached;
  }
  try {
    const res = await fetch('/auth/active_chats');
    // Handle authentication failure - redirect to login
    if (res.status === 401) {
      console.warn('Session expired, redirecting to login');
      window.location.href = '/';
      return [];
    }
    const data = await res.json();
    const chats = data.active_chats || [];
    listCache.set('activeChats', chats);
    return chats;
  } catch (e) {
    console.error('Failed to fetch active chats:', e);
    return cached || [];
  }
}

async function fetchConversations(forceRefresh = false) {
  const cached = listCache.get('conversations');
  if (cached && !forceRefresh && !listCache.isStale('conversations')) {
    return cached;
  }
  try {
    const res = await fetch('/auth/conversations');
    // Handle authentication failure - redirect to login
    if (res.status === 401) {
      console.warn('Session expired, redirecting to login');
      window.location.href = '/';
      return [];
    }
    const data = await res.json();
    const conversations = data.conversations || [];
    listCache.set('conversations', conversations);
    return conversations;
  } catch (e) {
    console.error('Failed to fetch conversations:', e);
    return cached || [];
  }
}

function renderUnifiedChatList(chats, conversations) {
  const container = document.getElementById('chatsList');
  if (!container) return;

  if (chats.length === 0) {
    container.innerHTML = '<div class="chats-list-empty">No chats yet. Click "+ Create New" to get started.</div>';
    return;
  }

  // Group conversations by chat_id
  const convByChat = {};
  for (const conv of conversations) {
    const chatId = conv.chat_id;
    if (!convByChat[chatId]) convByChat[chatId] = [];
    convByChat[chatId].push(conv);
  }

  // Sort conversations within each chat by date (newest first)
  for (const chatId in convByChat) {
    convByChat[chatId].sort((a, b) => {
      const dateA = a.updated_at || a.created_at || '';
      const dateB = b.updated_at || b.created_at || '';
      return dateB.localeCompare(dateA);
    });
  }

  // Render chats
  container.innerHTML = chats.map(chat => {
    const chatId = chat.chat_id;
    const isShared = chat.is_shared;
    const isPublished = chat.is_published;
    const isExpanded = expandedChats.has(chatId);
    const chatConversations = convByChat[chatId] || [];
    const hasConversations = chatConversations.length > 0;

    // Build subtitle
    let subtitle = '';
    if (isPublished && chat.published_by) {
      subtitle = `Published by ${chat.published_by}`;
    } else if (isShared && chat.owner) {
      subtitle = `Shared by ${chat.owner}`;
    } else if (chat.files && chat.files.length > 0) {
      subtitle = chat.files.slice(0, 2).join(', ');
      if (chat.files.length > 2) subtitle += '...';
    }

    // Determine wrapper class: published takes priority over shared for styling
    let wrapperClass = 'chat-item-wrapper';
    if (isPublished) wrapperClass += ' published';
    else if (isShared) wrapperClass += ' shared';
    if (isExpanded) wrapperClass += ' expanded';

    return `
      <div class="${wrapperClass}" data-chat-id="${chatId}">
        <div class="chat-item-row${chatId === currentChatId && !currentConvId ? ' active' : ''}" data-chat-id="${chatId}" data-slug="${chat.slug || ''}">
          <button class="chat-expand-btn${isExpanded ? ' expanded' : ''}" data-chat-id="${chatId}" title="Show conversations">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
              <path d="M4.5 2L8.5 6L4.5 10" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/>
            </svg>
          </button>
          <div class="chat-info">
            <span class="chat-name">${escapeHtml(chat.name || 'Untitled')}</span>
            ${subtitle ? `<span class="chat-subtitle">${escapeHtml(subtitle)}</span>` : ''}
          </div>
          <button class="chat-menu-btn" data-type="chat" data-chat-id="${chatId}" data-name="${escapeHtml(chat.name || 'Untitled')}" data-shared="${isShared ? 'true' : 'false'}" data-published="${isPublished ? 'true' : 'false'}" title="More options">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
              <circle cx="8" cy="3" r="1.5"/>
              <circle cx="8" cy="8" r="1.5"/>
              <circle cx="8" cy="13" r="1.5"/>
            </svg>
          </button>
        </div>
        <div class="chat-conversations${isExpanded ? ' visible' : ''}" data-chat-id="${chatId}">
          <button class="conversation-item start-new" data-chat-id="${chatId}">
            <span class="conversation-title">+ Start new conversation</span>
          </button>
          ${chatConversations.map(conv => `
            <div class="conversation-item${conv.conv_id === currentConvId ? ' active' : ''}" data-chat-id="${chatId}" data-conv-id="${conv.conv_id}">
              <span class="conversation-title">${escapeHtml(conv.title || 'Untitled')}</span>
              <button class="conversation-menu-btn" data-type="conversation" data-chat-id="${chatId}" data-conv-id="${conv.conv_id}" data-title="${escapeHtml(conv.title || 'Untitled')}" data-published="${conv.is_published ? 'true' : 'false'}" title="More options">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                  <circle cx="8" cy="3" r="1.5"/>
                  <circle cx="8" cy="8" r="1.5"/>
                  <circle cx="8" cy="13" r="1.5"/>
                </svg>
              </button>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  }).join('');

  // Attach event listeners
  attachUnifiedListeners();
}

function attachUnifiedListeners() {
  // Expand/collapse buttons
  document.querySelectorAll('.chat-expand-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const chatId = btn.dataset.chatId;
      toggleChatExpand(chatId);
    });
  });

  // Chat row click (toggle expand/collapse conversations list)
  document.querySelectorAll('.chat-item-row').forEach(row => {
    row.addEventListener('click', (e) => {
      // Don't trigger if clicking on buttons
      if (e.target.closest('.chat-expand-btn') || e.target.closest('.chat-menu-btn')) return;

      const chatId = row.dataset.chatId;
      toggleChatExpand(chatId);
    });
  });

  // Chat menu buttons
  document.querySelectorAll('.chat-menu-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      showItemMenu(btn);
    });
  });

  // Start new conversation buttons
  document.querySelectorAll('.conversation-item.start-new').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const chatId = btn.dataset.chatId;
      openChat(chatId);

      // Close sidebar on mobile
      if (window.innerWidth < 768) {
        closeSidebar();
      }
    });
  });

  // Conversation items
  document.querySelectorAll('.conversation-item:not(.start-new)').forEach(item => {
    item.addEventListener('click', (e) => {
      // Don't trigger if clicking on menu button
      if (e.target.closest('.conversation-menu-btn')) return;

      const chatId = item.dataset.chatId;
      const convId = item.dataset.convId;
      openConversation(chatId, convId);

      // Close sidebar on mobile
      if (window.innerWidth < 768) {
        closeSidebar();
      }
    });
  });

  // Conversation menu buttons
  document.querySelectorAll('.conversation-menu-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      showItemMenu(btn);
    });
  });
}

function toggleChatExpand(chatId) {
  const wrapper = document.querySelector(`.chat-item-wrapper[data-chat-id="${chatId}"]`);
  const expandBtn = wrapper?.querySelector('.chat-expand-btn');
  const conversations = wrapper?.querySelector('.chat-conversations');

  if (!wrapper || !conversations) return;

  const isExpanded = expandedChats.has(chatId);

  if (isExpanded) {
    expandedChats.delete(chatId);
    wrapper.classList.remove('expanded');
    expandBtn?.classList.remove('expanded');
    conversations.classList.remove('visible');
  } else {
    // Collapse all other chats first (single-open accordion)
    for (const otherId of [...expandedChats]) {
      if (otherId === chatId) continue;
      expandedChats.delete(otherId);
      const otherWrapper = document.querySelector(`.chat-item-wrapper[data-chat-id="${otherId}"]`);
      if (otherWrapper) {
        otherWrapper.classList.remove('expanded');
        otherWrapper.querySelector('.chat-expand-btn')?.classList.remove('expanded');
        otherWrapper.querySelector('.chat-conversations')?.classList.remove('visible');
      }
    }
    expandedChats.add(chatId);
    wrapper.classList.add('expanded');
    expandBtn?.classList.add('expanded');
    conversations.classList.add('visible');
  }
}

// Helper function to close sidebar (for mobile)
function closeSidebar() {
  const sidebar = document.getElementById('sidebar');
  const sidebarBackdrop = document.getElementById('sidebarBackdrop');
  sidebar?.classList.remove('open');
  sidebarBackdrop?.classList.remove('visible');
  document.body.style.overflow = '';
}

// Setup event listeners
function setupEventListeners() {
  // Mobile sidebar toggle
  const sidebar = document.getElementById('sidebar');
  const sidebarBackdrop = document.getElementById('sidebarBackdrop');
  const btnHamburger = document.getElementById('btnHamburger');
  const btnCloseSidebar = document.getElementById('btnCloseSidebar');

  function openSidebar() {
    sidebar.classList.add('open');
    sidebarBackdrop.classList.add('visible');
    document.body.style.overflow = 'hidden';
  }

  function closeSidebar() {
    sidebar.classList.remove('open');
    sidebarBackdrop.classList.remove('visible');
    document.body.style.overflow = '';
  }

  btnHamburger.addEventListener('click', openSidebar);
  btnCloseSidebar.addEventListener('click', closeSidebar);
  sidebarBackdrop.addEventListener('click', closeSidebar);

  // Note: Old dropdown toggle and list item click handlers removed
  // Now handled by attachUnifiedListeners() which is called after rendering

  // Create New button
  document.getElementById('btnCreateNew').addEventListener('click', openCreateWizard);
  
  // Profile icon dropdown
  const profileIconBtn = document.getElementById('btnProfileIcon');
  const profileDropdown = document.getElementById('profileDropdown');
  
  profileIconBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    profileDropdown.classList.toggle('hidden');
  });
  
  // Close dropdown when clicking outside
  document.addEventListener('click', () => {
    profileDropdown.classList.add('hidden');
  });
  
  // Profile button — opens profile modal at top
  document.getElementById('btnProfile').addEventListener('click', () => {
    profileDropdown.classList.add('hidden');
    document.getElementById('profileModal').classList.remove('hidden');
  });

  // Subscriptions button — opens profile modal scrolled to subscription section
  document.getElementById('btnSubscriptions').addEventListener('click', () => {
    profileDropdown.classList.add('hidden');
    document.getElementById('profileModal').classList.remove('hidden');
    setTimeout(() => {
      document.querySelector('.subscription-section')?.scrollIntoView({ behavior: 'smooth' });
    }, 100);
  });
  
  // Logout button
  document.getElementById('btnLogout').addEventListener('click', async () => {
    profileDropdown.classList.add('hidden');
    await fetch('/auth/logout', { method: 'POST' });
    window.location.href = '/';
  });

  // Language selector moved to main-page navbar — /lab has no switcher.
  // i18n.init() at DOMContentLoaded still applies the stored language preference.

  // Close modals
  document.getElementById('closeWizard').addEventListener('click', closeCreateWizard);
  document.getElementById('closeProfile').addEventListener('click', () => {
    document.getElementById('profileModal').classList.add('hidden');
  });
  document.getElementById('closeSchemaViewer').addEventListener('click', () => {
    document.getElementById('schemaViewerModal').classList.add('hidden');
  });
  document.getElementById('closeShare')?.addEventListener('click', () => {
    document.getElementById('shareModal').classList.add('hidden');
  });

  // Single-step wizard: only "Generate Chat" button (Back was removed)
  document.getElementById('btnWizardNext').addEventListener('click', wizardNextStep);

  // Modal file input
  document.getElementById('fileInput').addEventListener('change', handleFileSelection);

  // ───── Welcome-state drop target ────────────────────────────────────────────
  // Drop target on /lab is the welcome area (#welcomeState), which is a sibling of .top-bar
  // inside .main-content. Listening here keeps the top bar and sidebar at full brightness
  // during drag — only the welcome area dims. Dropping a file anywhere in this region starts
  // a brand-new chat via the frictionless flow.
  const welcomeState = document.getElementById('welcomeState');
  let welcomeDragCounter = 0;

  function _hasFilesPayload(e) {
    if (!e || !e.dataTransfer) return false;
    const types = e.dataTransfer.types;
    if (!types) return false;
    for (let i = 0; i < types.length; i++) {
      if (types[i] === 'Files') return true;
    }
    return false;
  }

  function _isTextInputTarget(target) {
    if (!target || !(target instanceof Element)) return false;
    return !!target.closest('input, textarea, [contenteditable="true"], [contenteditable=""]');
  }

  if (welcomeState) {
    welcomeState.addEventListener('dragenter', (e) => {
      if (!_hasFilesPayload(e)) return;
      if (_isTextInputTarget(e.target)) return;
      e.preventDefault();
      welcomeDragCounter++;
      welcomeState.classList.add('drag-active');
    });
    welcomeState.addEventListener('dragover', (e) => {
      if (!_hasFilesPayload(e)) return;
      if (_isTextInputTarget(e.target)) return;
      e.preventDefault();
    });
    welcomeState.addEventListener('dragleave', (e) => {
      if (!_hasFilesPayload(e)) return;
      welcomeDragCounter = Math.max(0, welcomeDragCounter - 1);
      if (welcomeDragCounter === 0) welcomeState.classList.remove('drag-active');
    });
    welcomeState.addEventListener('drop', (e) => {
      if (!_hasFilesPayload(e)) return;
      if (_isTextInputTarget(e.target)) return;
      e.preventDefault();
      welcomeDragCounter = 0;
      welcomeState.classList.remove('drag-active');
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length === 0) return;
      runFrictionlessFlow(files, { resetSession: true });
    });
    // Defensive: if a drag is cancelled outside the element, clear the dim state.
    window.addEventListener('dragend', () => {
      welcomeDragCounter = 0;
      welcomeState.classList.remove('drag-active');
    });
  }

  // ───── Create New modal drop target ─────────────────────────────────────────
  // Drop anywhere inside the wizard modal triggers the same frictionless flow as a page drop.
  // Listeners are bound once (idempotent) — they fire only when the modal is visible because
  // #createNewModal has display:none in its `hidden` state. Drag-feedback dim is scoped to the
  // modal's .modal-content so only the visible card area shows the visual.
  const createNewModal = document.getElementById('createNewModal');
  const wizardModalContent = createNewModal ? createNewModal.querySelector('.modal-content') : null;
  let modalDragCounter = 0;

  if (createNewModal && wizardModalContent) {
    createNewModal.addEventListener('dragenter', (e) => {
      if (!_hasFilesPayload(e)) return;
      if (_isTextInputTarget(e.target)) return;
      e.preventDefault();
      modalDragCounter++;
      wizardModalContent.classList.add('drag-active');
    });
    createNewModal.addEventListener('dragover', (e) => {
      if (!_hasFilesPayload(e)) return;
      if (_isTextInputTarget(e.target)) return;
      e.preventDefault();
    });
    createNewModal.addEventListener('dragleave', (e) => {
      if (!_hasFilesPayload(e)) return;
      modalDragCounter = Math.max(0, modalDragCounter - 1);
      if (modalDragCounter === 0) wizardModalContent.classList.remove('drag-active');
    });
    createNewModal.addEventListener('drop', (e) => {
      if (!_hasFilesPayload(e)) return;
      if (_isTextInputTarget(e.target)) return;
      e.preventDefault();
      modalDragCounter = 0;
      wizardModalContent.classList.remove('drag-active');
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length === 0) return;
      // Modal drops ACCUMULATE — same path the file-picker uses. The user must click
      // "Generate Chat" to actually start the upload+autofill+generate flow. This lets
      // them drop multiple files and/or share a Google Sheet in the same session before
      // committing. Plan-limit and extension validation live in addFilesToSelection.
      addFilesToSelection(files);
    });
    // Defensive: if a drag is cancelled outside the modal, clear the dim state.
    window.addEventListener('dragend', () => {
      modalDragCounter = 0;
      wizardModalContent.classList.remove('drag-active');
    });
  }

  // Google Sheet import
  document.getElementById('shareGoogleSheetBtn').addEventListener('click', () => {
    document.getElementById('googleSheetInputBox').classList.remove('hidden');
  });
  document.getElementById('cancelGoogleSheetBtn').addEventListener('click', () => {
    document.getElementById('googleSheetInputBox').classList.add('hidden');
    document.getElementById('googleUrlInput').value = '';
  });
  document.getElementById('importFromUrlBtn').addEventListener('click', importFromGoogleUrl);

  // Chat input
  chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!isRequestInProgress) sendMessage();
    }
  });
  document.getElementById('btnSend').addEventListener('click', sendMessage);
  
  // Auto-resize textarea
  chatInput.addEventListener('input', () => {
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 150) + 'px';
  });

  // Chat actions
  document.getElementById('btnSchema')?.addEventListener('click', openSchemaViewer);

  // Auto Analytics button (idle / processing / done state machine)
  AutoAnalytics.init();

  // Download Analytics dropdown: main button toggles menu; menu items dispatch by format.
  const dlBtn = document.getElementById('btnDownloadReport');
  const dlMenu = document.getElementById('downloadReportMenu');
  if (dlBtn && dlMenu) {
    dlBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      dlMenu.classList.toggle('hidden');
    });
    dlMenu.querySelectorAll('.dropdown-item').forEach(item => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        const format = item.dataset.format || 'pdf';
        dlMenu.classList.add('hidden');
        downloadReport(format);
      });
    });
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#downloadReportDropdown')) {
        dlMenu.classList.add('hidden');
      }
    });
  }
  
  // Save profile
  document.getElementById('btnSaveProfile').addEventListener('click', saveProfile);
  document.getElementById('btnChangePw').addEventListener('click', changePassword);
  
  // Plan selection
  document.querySelectorAll('.plan-select').forEach(btn => {
    btn.addEventListener('click', async () => {
      const plan = btn.dataset.plan;
      if (plan === 'Enterprise') { openAboutContact(); return; }
      await selectPlan(plan);
    });
  });
  
  // Copy share link
  document.getElementById('copyShareLink')?.addEventListener('click', () => {
    const input = document.getElementById('shareLink');
    input.select();
    document.execCommand('copy');
  });
  
  // Save share settings
  document.getElementById('btnSaveShare')?.addEventListener('click', saveShareSettings);
}

// Store welcome message for new chats
let currentWelcomeMessage = '';

// ===================== Auto Analytics =====================
// Per-chat background auto-analysis -> branded PPTX. The #btnAutoAnalytics
// button has three states: idle ("Run Auto Analytics"), processing
// ("Processing…", disabled) and done ("Download Presentation", green).
const AutoAnalytics = (function () {
  let pollTimer = null;
  let pollChatId = null;

  function btn() { return document.getElementById('btnAutoAnalytics'); }

  function _t(key, fallback) {
    try { return (window.i18n && window.i18n.t) ? window.i18n.t(key) : fallback; }
    catch (e) { return fallback; }
  }

  // Reflect a state on the button: 'idle' | 'processing' | 'done'.
  function setState(state) {
    const b = btn();
    if (!b) return;
    const icon = b.querySelector('.aa-btn-icon');
    const label = b.querySelector('.aa-btn-label');
    b.dataset.aaState = state;
    if (state === 'processing') {
      if (icon) icon.textContent = '✨';
      if (label) label.textContent = _t('lab.processing', 'Processing…');
      b.disabled = true;
      b.classList.remove('auto-analytics-done');
    } else if (state === 'done') {
      if (icon) icon.textContent = '📥';
      if (label) label.textContent = _t('lab.download_presentation', 'Download Presentation');
      b.disabled = false;
      b.classList.add('auto-analytics-done');
    } else { // idle
      if (icon) icon.textContent = '✨';
      if (label) label.textContent = _t('lab.run_auto_analytics', 'Run Auto Analytics');
      b.disabled = false;
      b.classList.remove('auto-analytics-done');
    }
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    pollChatId = null;
  }

  // Poll status every 5s; scoped to chatId so a stale poll never updates the
  // button for a different chat.
  function startPolling(chatId) {
    stopPolling();
    pollChatId = chatId;
    pollTimer = setInterval(async () => {
      if (pollChatId !== currentChatId) { stopPolling(); return; }
      try {
        const res = await fetch(`/api/chat/${chatId}/auto_analysis/status`);
        if (!res.ok) return;
        const data = await res.json();
        if (pollChatId !== currentChatId) { stopPolling(); return; }
        if (data.status === 'done') {
          setState('done');
          stopPolling();
        } else if (data.status === 'idle') {
          // Run crashed / reset server-side — allow the user to retry.
          setState('idle');
          stopPolling();
        }
      } catch (e) { /* transient network error — keep polling */ }
    }, 5000);
  }

  // Temporary popup that auto-dismisses after 5 seconds (no native confirm()).
  function showPopup() {
    const popup = document.getElementById('autoAnalyticsPopup');
    if (!popup) return;
    popup.classList.remove('hidden');
    setTimeout(() => { popup.classList.add('hidden'); }, 5000);
  }

  async function start(chatId) {
    try {
      const res = await fetch(`/api/chat/${chatId}/auto_analysis/start`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (data.status === 'done') {
        setState('done');
        return;
      }
      setState('processing');
      showPopup();
      startPolling(chatId);
    } catch (e) {
      console.error('Auto analysis start failed:', e);
      showToast('Failed to start auto analysis', true);
      setState('idle');
    }
  }

  async function download(chatId) {
    try {
      const res = await fetch(`/api/chat/${chatId}/auto_analysis/download`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: 'Download failed' }));
        showToast(err.error || 'Download failed', true);
        return;
      }
      const disposition = res.headers.get('Content-Disposition') || '';
      const m = disposition.match(/filename="?([^"]+)"?/);
      const filename = m ? m[1] : 'auto_analysis.pptx';
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      console.error('Auto analysis download failed:', e);
      showToast('Failed to download presentation', true);
    }
  }

  function onClick() {
    const b = btn();
    if (!b || !currentChatId) return;
    const state = b.dataset.aaState || 'idle';
    if (state === 'processing') return;          // no-op (button is disabled)
    if (state === 'done') { download(currentChatId); return; }
    start(currentChatId);                         // idle -> start
  }

  // Fetch state for a chat and set the button accordingly; resumes polling
  // when a run is still in progress.
  async function refresh(chatId) {
    setState('idle');
    try {
      const res = await fetch(`/api/chat/${chatId}/auto_analysis/status`);
      if (!res.ok) return;
      const data = await res.json();
      if (chatId !== currentChatId) return;       // user already navigated away
      if (data.status === 'done') {
        setState('done');
        stopPolling();
      } else if (data.status === 'processing') {
        setState('processing');
        startPolling(chatId);
      } else {
        setState('idle');
        stopPolling();
      }
    } catch (e) { /* leave button idle on failure */ }
  }

  function init() {
    const b = btn();
    if (b) {
      setState('idle');
      b.addEventListener('click', onClick);
    }
  }

  return { init, refresh, setState, stopPolling };
})();

// Show/hide top bar action buttons
function showTopBarActions(show) {
  const btnSchema = document.getElementById('btnSchema');
  const btnAuto = document.getElementById('btnAutoAnalytics');
  if (show) {
    btnSchema?.classList.remove('hidden');
    btnAuto?.classList.remove('hidden');
  } else {
    btnSchema?.classList.add('hidden');
    btnAuto?.classList.add('hidden');
    AutoAnalytics.stopPolling();
    // Also hide report dropdown when top bar actions are hidden
    document.getElementById('downloadReportDropdown')?.classList.add('hidden');
    document.getElementById('downloadReportMenu')?.classList.add('hidden');
  }
}

// Check if conversation has enough reportable content (plots/tables) for analytics export
function updateReportButtonVisibility(history) {
  const dropdown = document.getElementById('downloadReportDropdown');
  if (!dropdown) return;
  let reportableCount = 0;
  (history || []).forEach(m => {
    if (m.role !== 'ai') return;
    // Count multi-image entries (each image is a separate chart)
    if (m.images && Array.isArray(m.images) && m.images.length > 0) {
      reportableCount += m.images.filter(img => img.image_base64).length;
    } else if (m.image_base64 || m.table) {
      reportableCount += 1;
    }
  });
  if (reportableCount >= 2) {
    dropdown.classList.remove('hidden');
  } else {
    dropdown.classList.add('hidden');
    document.getElementById('downloadReportMenu')?.classList.add('hidden');
  }
}

// Download analytics report (format: 'pdf' or 'pptx').
async function downloadReport(format) {
  const btn = document.getElementById('btnDownloadReport');
  if (!btn || !currentChatId || !currentConvId) return;
  const fmt = format === 'pptx' ? 'pptx' : 'pdf';
  const endpoint = fmt === 'pptx' ? 'download_pptx' : 'download_report';
  const defaultFilename = fmt === 'pptx' ? 'analytics_report.pptx' : 'analytics_report.pdf';

  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Generating Report...';

  try {
    const res = await fetch(`/api/chat/${currentChatId}/conversation/${currentConvId}/${endpoint}`, {
      method: 'POST'
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: 'Failed to generate report' }));
      showToast(err.error || 'Failed to generate report', true);
      return;
    }

    // Extract filename from Content-Disposition header
    const disposition = res.headers.get('Content-Disposition') || '';
    const filenameMatch = disposition.match(/filename="?([^"]+)"?/);
    const filename = filenameMatch ? filenameMatch[1] : defaultFilename;

    // Download the blob
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);

  } catch (e) {
    console.error('Report download failed:', e);
    showToast('Failed to download report', true);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

// Open chat
async function openChat(chatId) {
  currentChatId = chatId;
  currentConvId = null;
  _highlightActiveConv(null);
  _highlightActiveChat(chatId);

  showLoading('Loading chat...');
  
  try {
    // Get welcome message with pre-generated questions (stored in metadata - fast)
    const welcomeRes = await fetch(`/api/chat/${chatId}/welcome`);
    const welcomeData = await welcomeRes.json();
    let welcomeMessage = welcomeData.message || 'Hello! I\'m ready to help you analyze your data. Ask me anything!';
    const suggestedQuestions = welcomeData.suggested_questions || [];
    
    // Store welcome message for reference
    currentWelcomeMessage = welcomeMessage;
    
    // Show chat state
    welcomeState.classList.add('hidden');
    chatState.classList.remove('hidden');
    showTopBarActions(true);
    // Hide report dropdown for new chats (no conversation history yet)
    document.getElementById('downloadReportDropdown')?.classList.add('hidden');
    document.getElementById('downloadReportMenu')?.classList.add('hidden');

    // Show welcome message with questions (questions are pre-generated during chat creation)
    chatMessages.innerHTML = '';
    chatMessages.classList.remove('empty');
    appendWelcomeMessageWithQuestions(welcomeMessage, suggestedQuestions);

    // Restore the Auto Analytics button state for this chat (idle / processing / done).
    AutoAnalytics.refresh(chatId);

  } catch (e) {
    console.error('Failed to open chat:', e);
    showToast('Failed to open chat', true);
  }
  
  hideLoading();
}

// Append welcome message with clickable suggested questions embedded inside the message box
function appendWelcomeMessageWithQuestions(message, questions) {
  const wrapper = document.createElement('div');
  wrapper.className = 'message assistant';
  
  // Avatar
  const avatar = document.createElement('div');
  avatar.className = 'message-avatar';
  avatar.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg>';
  wrapper.appendChild(avatar);
  
  // Content container
  const content = document.createElement('div');
  content.className = 'message-content suggested-questions-container';
  
  // Message text (parse markdown)
  const textDiv = document.createElement('div');
  textDiv.className = 'message-text';
  textDiv.innerHTML = renderMarkdown(message);
  content.appendChild(textDiv);
  
  // Show questions if available (pre-generated during chat creation)
  if (questions && questions.length > 0) {
    appendSuggestedQuestionsToContainer(content, questions);
  }
  
  wrapper.appendChild(content);
  chatMessages.appendChild(wrapper);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

// Append suggested questions to a container element.
// Renders 1-3 plain-text bold questions inheriting the surrounding text color.
// The localized "For example you can ask:" intro line is the LAST line of the
// welcome message itself (produced by the LLM at chat creation), so the
// frontend does NOT render an intro line here. Clicking a question copies it
// into the chat input (existing behavior preserved).
function appendSuggestedQuestionsToContainer(container, questions) {
  if (!questions || !questions.length) return;

  const wrap = document.createElement('div');
  wrap.className = 'suggested-questions-embedded';
  wrap.style.cssText = 'display: flex; flex-direction: column; gap: 8px; margin-top: 14px;';

  questions.slice(0, 3).forEach((question) => {
    const btn = document.createElement('button');
    btn.className = 'suggested-question-btn';
    btn.type = 'button';
    // Inherit color/font from surrounding chat text — no hardcoded accent
    // color. Only overrides: bold, transparent background, no border, left
    // alignment, pointer cursor.
    btn.style.cssText = `
      display: block;
      width: 100%;
      padding: 4px 0;
      border: none;
      background: transparent;
      cursor: pointer;
      font: inherit;
      color: inherit;
      font-weight: 600;
      text-align: left;
      line-height: 1.5;
    `;
    btn.textContent = question;

    // Subtle, neutral hover: underline only — no color change.
    btn.onmouseenter = () => { btn.style.textDecoration = 'underline'; };
    btn.onmouseleave = () => { btn.style.textDecoration = 'none'; };

    btn.onclick = () => {
      const chatInput = document.getElementById('chatInput');
      if (chatInput) {
        chatInput.value = question;
        chatInput.focus();
        chatInput.dispatchEvent(new Event('input'));
      }
    };

    wrap.appendChild(btn);
  });

  container.appendChild(wrap);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

// Highlight the active conversation in the sidebar
function _highlightActiveConv(convId) {
  document.querySelectorAll('.conversation-item.active').forEach(el => el.classList.remove('active'));
  if (convId) {
    const target = document.querySelector(`.conversation-item[data-conv-id="${convId}"]`);
    if (target) target.classList.add('active');
  }
}

// Highlight the currently-open chat in the sidebar (works for both the unified
// list `.chat-item-row` and the legacy `.list-item` renderers). Toggles the
// `active` class in place — no full reload — so switching chats moves the
// highlight immediately.
function _highlightActiveChat(chatId) {
  document.querySelectorAll('.chat-item-row.active, .list-item.active')
    .forEach(el => el.classList.remove('active'));
  if (chatId) {
    document.querySelectorAll(
      `.chat-item-row[data-chat-id="${chatId}"], .list-item[data-chat-id="${chatId}"]`
    ).forEach(el => el.classList.add('active'));
  }
}

// Open existing conversation
async function openConversation(chatId, convId) {
  currentChatId = chatId;
  currentConvId = convId;
  _highlightActiveConv(convId);
  _highlightActiveChat(chatId);
  
  showLoading('Loading conversation...');
  
  try {
    const res = await fetch(`/api/chat/${chatId}/conversation/${convId}/history`);
    const data = await res.json();
    const history = data.history || [];
    
    // Show chat state
    welcomeState.classList.add('hidden');
    chatState.classList.remove('hidden');
    showTopBarActions(true);

    // Restore the Auto Analytics button state for this chat (idle / processing / done).
    AutoAnalytics.refresh(chatId);

    // Render messages
    chatMessages.classList.remove('empty');
    chatMessages.innerHTML = '';
    
    history.forEach(msg => {
      if (msg.images && Array.isArray(msg.images) && msg.images.length > 0) {
        // Multi-chart history entry: render each chart as a separate assistant message
        msg.images.forEach(img => {
          appendMessage('assistant', img.answer || '', img.image_base64, null, null);
        });
      } else {
        appendMessage(msg.role === 'human' ? 'user' : 'assistant', msg.content, msg.image_base64, msg.table, msg.full_table_key);
      }
    });

    chatMessages.scrollTop = chatMessages.scrollHeight;

    // Check if report button should be shown (need 2+ AI messages with plots/tables)
    updateReportButtonVisibility(history.map(m => ({
      role: m.role === 'human' ? 'human' : 'ai',
      image_base64: m.image_base64,
      images: m.images,
      table: m.table
    })));

  } catch (e) {
    console.error('Failed to open conversation:', e);
    showToast('Failed to open conversation', true);
  }

  hideLoading();

  // Update URL to permanent conversation link (outside try block to avoid
  // shadowing by local `const history = data.history` variable)
  if (currentConvId === convId) {
    window.history.pushState({ convId: convId, chatId: chatId }, '', `/c/${convId}`);
  }
}

// Append message to chat
function appendMessage(role, content, imageBase64, table, fullTableKey) {
  const messageDiv = document.createElement('div');
  messageDiv.className = `message ${role}`;

  const contentDiv = document.createElement('div');
  contentDiv.className = 'message-content';

  if (content) {
    const p = document.createElement('div');
    p.innerHTML = renderMarkdown(content);
    contentDiv.appendChild(p);
  }

  // Add edit button only for the last user message
  if (role === 'user' && content) {
    messageDiv.dataset.originalContent = content;
    // Remove edit buttons from all previous user messages (only last is editable)
    document.querySelectorAll('.message-edit-btn').forEach(btn => btn.remove());
    // Add edit button to this (latest) user message
    const editBtn = document.createElement('button');
    editBtn.className = 'message-edit-btn';
    editBtn.innerHTML = '✎ Edit';
    editBtn.title = 'Edit and regenerate';
    editBtn.addEventListener('click', () => startEditMessage(messageDiv));
    messageDiv.appendChild(editBtn);
  }
  
  if (imageBase64) {
    // Check if it's interactive HTML (Plotly) or base64 image
    if (imageBase64.trim().startsWith('<') && imageBase64.includes('plotly')) {
      contentDiv.appendChild(createPlotlyContainer(imageBase64, currentChatId, content || ''));
    } else {
      // It's a base64 image (matplotlib/seaborn)
      const imgContainer = createImageWithFullscreen(imageBase64, content || '');
      contentDiv.appendChild(imgContainer);
    }
  }
  
  if (table && table.columns && table.rows) {
    appendTableTo(contentDiv, table, fullTableKey, content || '');
  }
  
  messageDiv.appendChild(contentDiv);
  chatMessages.appendChild(messageDiv);
}

// Markdown renderer
function renderMarkdown(text) {
  if (!text) return '';
  
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

// Create fullscreen image viewer
function createImageWithFullscreen(base64Data, messageContext = '') {
  const container = document.createElement('div');
  container.className = 'image-container';
  container.style.cssText = 'position: relative; display: block; width: 100%; margin-top: 10px; max-width: min(900px, 100%);';

  const img = document.createElement('img');
  img.src = 'data:image/png;base64,' + base64Data;
  img.style.cssText = 'max-width: 100%; max-height: 700px; object-fit: contain; border: 1px solid #e7e8ea; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); display: block;';
  
  const btnContainer = document.createElement('div');
  btnContainer.style.cssText = 'display: flex; gap: 8px; margin-top: 8px;';
  
  const viewBtn = document.createElement('button');
  viewBtn.className = 'ghost';
  viewBtn.innerHTML = '🔍 View Larger';
  viewBtn.style.cssText = 'padding: 6px 12px; font-size: 13px;';

  viewBtn.addEventListener('click', () => {
    const modal = document.createElement('div');
    modal.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.9); z-index: 10000; display: flex; align-items: center; justify-content: center; cursor: zoom-out;';

    const modalImg = document.createElement('img');
    modalImg.src = img.src;
    modalImg.style.cssText = 'max-width: 95%; max-height: 95%; object-fit: contain;';

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '×';
    closeBtn.style.cssText = 'position: absolute; top: 20px; right: 30px; background: rgba(255,255,255,0.2); border: none; color: white; font-size: 40px; cursor: pointer; padding: 0; width: 50px; height: 50px; border-radius: 50%;';

    modal.appendChild(modalImg);
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
  downloadBtn.title = 'Download image';
  downloadBtn.style.cssText = 'padding: 6px 12px; font-size: 13px;';
  downloadBtn.addEventListener('click', async () => {
    const originalLabel = downloadBtn.innerHTML;
    try {
      downloadBtn.disabled = true;
      downloadBtn.textContent = 'Generating name...';
      let filename = 'chart';
      try {
        const response = await fetch(`/api/chat/${currentChatId}/generate_filename`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: messageContext, type: 'image' })
        });
        const data = await response.json();
        if (data && data.filename) filename = data.filename;
      } catch (_) {}
      const link = document.createElement('a');
      link.href = img.src;
      link.download = `${filename}.png`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    } catch (e) {
      console.error('Download failed:', e);
      alert('Failed to download image');
    } finally {
      downloadBtn.disabled = false;
      downloadBtn.innerHTML = originalLabel;
    }
  });

  container.appendChild(img);
  btnContainer.appendChild(viewBtn);
  btnContainer.appendChild(downloadBtn);
  container.appendChild(btnContainer);

  return container;
}

// Renders a Plotly interactive chart inside an iframe and adds
// [View Larger] + [Download] buttons. The Download button posts the raw
// Plotly HTML to /export_plotly_png and saves the returned high-res PNG.
// NOTE: chat.js has a parallel copy of this function with the same signature.
// Keep them in sync.
function createPlotlyContainer(htmlString, chatIdRef, messageContext = '') {
  const plotlyContainer = document.createElement('div');
  plotlyContainer.className = 'plotly-container';
  plotlyContainer.style.cssText = 'margin-top: 10px; width: 100%; max-width: 900px;';

  const iframe = document.createElement('iframe');
  iframe.className = 'plotly-iframe';
  iframe.style.cssText = 'width: 100%; border: 1px solid #e7e8ea; border-radius: 8px; display: block;';
  // Sandbox: allow the chart's scripts to run but block top-level navigation /
  // new-tab popups from the blank-origin srcdoc document. (keep in sync: chat.js)
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

// Full-featured table renderer with Excel export and full table view
function appendTableTo(div, table, fullKey, messageContext = '') {
  try {
    if (!table || typeof table !== 'object') {
      console.warn('appendTableTo: table is null or not an object', table);
      return false;
    }
    const total = typeof table.total_rows === 'number' ? table.total_rows : null;
    const rows = Array.isArray(table.rows) ? table.rows : [];
    const cols = Array.isArray(table.columns) ? table.columns : [];
    
    if (total !== null && total <= 0) return false;
    if (!rows.length || !cols.length) return false;
  } catch (err) {
    console.error('appendTableTo: validation error', err);
    return false;
  }
  
  const meta = document.createElement('div');
  const tot = typeof table.total_rows === 'number' ? table.total_rows : null;
  const what = table.dtype === 'series' ? 'series' : 'table';
  meta.style.cssText = 'font-size: 12px; color: #6b7280; margin: 8px 0 4px 0;';
  const rowWord = (n) => n === 1 ? 'row' : 'rows';
  if (tot !== null && tot <= 10) {
    meta.textContent = `Showing ${tot} ${rowWord(tot)} (${what}).`;
  } else {
    meta.textContent = tot !== null ? `Showing top 10 of ${tot} ${rowWord(tot)} (${what}).` : `Showing top 10 (${what}).`;
  }
  div.appendChild(meta);

  const previewCols = table.columns || [];
  const previewRows = Array.isArray(table.rows) ? table.rows.slice() : [];
  let tbody = null;
  let thead = null;
  let tableContainer = null;
  const hasStyledHtml = !!(table.styled_html && typeof table.styled_html === 'string');

  // Check if styled HTML is available (from pandas Styler with conditional formatting)
  if (hasStyledHtml) {
    // Render styled HTML directly to preserve colors and formatting
    const styledContainer = document.createElement('div');
    styledContainer.className = 'styled-table-container';
    styledContainer.innerHTML = table.styled_html;
    
    // Apply consistent styling to the styled table
    const styledTable = styledContainer.querySelector('table');
    if (styledTable) {
      styledTable.style.cssText = 'width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 14px;';
      styledTable.querySelectorAll('th, td').forEach(cell => {
        cell.style.border = '1px solid #e5e7eb';
        cell.style.padding = '8px';
        cell.style.textAlign = 'left';
      });
      styledTable.querySelectorAll('th').forEach(th => {
        if (!th.style.backgroundColor) {
          th.style.backgroundColor = '#f9fafb';
        }
        th.style.fontWeight = '600';
      });
      
      // Initially hide rows beyond top 10 to show preview
      const tableRows = styledTable.querySelectorAll('tbody tr');
      tableRows.forEach((tr, idx) => {
        if (idx >= 10) {
          tr.style.display = 'none';
        }
      });
    }
    tableContainer = styledContainer;
    div.appendChild(styledContainer);
  } else {
    // Fallback: render plain table
    const tbl = document.createElement('table');
    tbl.className = 'datachat';
    tbl.style.cssText = 'width: 100%; border-collapse: collapse; margin: 8px 0;';
    
    thead = document.createElement('thead');
    const trh = document.createElement('tr');
    
    previewCols.forEach((c) => {
      const th = document.createElement('th');
      th.textContent = c;
      th.style.cssText = 'border: 1px solid #e5e7eb; padding: 8px; background: #f9fafb; text-align: left; font-weight: 600;';
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    tbl.appendChild(thead);

    tbody = document.createElement('tbody');
    previewRows.slice(0, 10).forEach((row) => {
      const tr = document.createElement('tr');
      previewCols.forEach((c) => {
        const td = document.createElement('td');
        let v = row && Object.prototype.hasOwnProperty.call(row, c) ? row[c] : '';
        if (v === null || v === undefined) v = '';
        td.textContent = String(v);
        td.style.cssText = 'border: 1px solid #e5e7eb; padding: 8px;';
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    tableContainer = tbl;
    div.appendChild(tbl);
  }
  
  // Initially showing top 10 rows (even for styled HTML, we hide rows beyond 10)
  let showingFull = false;
  let fullData = null;
  
  // Show full table button
  if (fullKey && (tot === null || (Array.isArray(previewRows) && tot > previewRows.length))) {
    const btn = document.createElement('button');
    btn.className = 'ghost';
    btn.textContent = showingFull ? 'Show top 10 rows' : 'Show full table';
    btn.style.cssText = 'margin-top: 6px; padding: 6px 12px; font-size: 13px;';
    
    const renderRows = (cols, rows) => {
      // For styled HTML tables, just show/hide rows instead of rebuilding
      if (hasStyledHtml && tableContainer.className === 'styled-table-container') {
        const styledTable = tableContainer.querySelector('table');
        if (styledTable) {
          const tableRows = styledTable.querySelectorAll('tbody tr');
          const numRowsToShow = rows.length;
          
          tableRows.forEach((tr, idx) => {
            if (idx < numRowsToShow) {
              tr.style.display = '';
            } else {
              tr.style.display = 'none';
            }
          });
        }
        return;
      }
      
      // For plain tables, rebuild rows
      if (!tbody) return;
      
      tbody.innerHTML = '';
      if (Array.isArray(cols) && cols.join('|') !== previewCols.join('|')) {
        thead.innerHTML = '';
        const trh2 = document.createElement('tr');
        cols.forEach((c) => {
          const th = document.createElement('th');
          th.textContent = c;
          th.style.cssText = 'border: 1px solid #e5e7eb; padding: 8px; background: #f9fafb; text-align: left; font-weight: 600;';
          trh2.appendChild(th);
        });
        thead.appendChild(trh2);
      }
      rows.forEach((row) => {
        const tr = document.createElement('tr');
        (cols || []).forEach((c) => {
          const td = document.createElement('td');
          let v = row && Object.prototype.hasOwnProperty.call(row, c) ? row[c] : '';
          if (v === null || v === undefined) v = '';
          td.textContent = String(v);
          td.style.cssText = 'border: 1px solid #e5e7eb; padding: 8px;';
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
    };
    
    btn.addEventListener('click', async () => {
      if (!showingFull) {
        try {
          // For styled HTML, just show all rows; for plain tables, fetch and render full data
          if (hasStyledHtml) {
            const styledTable = tableContainer.querySelector('table');
            if (styledTable) {
              const tableRows = styledTable.querySelectorAll('tbody tr');
              const totalRows = tableRows.length;
              tableRows.forEach((tr) => {
                tr.style.display = '';
              });
              meta.textContent = `Showing full ${totalRows} ${totalRows === 1 ? 'row' : 'rows'} (${what}).`;
            }
          } else {
            if (!fullData) {
              const res = await fetch(`/api/chat/${currentChatId}/full_table/${fullKey}`);
              const js = await res.json();
              if (!res.ok || !js || !Array.isArray(js.rows)) return;
              fullData = js;
            }
            renderRows(fullData.columns || previewCols, fullData.rows || []);
            const fullLen = Array.isArray(fullData.rows) ? fullData.rows.length : 0;
            meta.textContent = `Showing full ${fullLen} ${fullLen === 1 ? 'row' : 'rows'} (${what}).`;
          }
          btn.textContent = 'Show top 10 rows';
          showingFull = true;
        } catch (e) {
          console.error('Failed to load full table:', e);
        }
      } else {
        // Hide rows beyond 10 for styled HTML, or render preview for plain tables
        if (hasStyledHtml) {
          const styledTable = tableContainer.querySelector('table');
          if (styledTable) {
            const tableRows = styledTable.querySelectorAll('tbody tr');
            tableRows.forEach((tr, idx) => {
              if (idx >= 10) {
                tr.style.display = 'none';
              }
            });
          }
        } else {
          renderRows(previewCols, previewRows.slice(0, 10));
        }
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
  
  // Excel download button
  const downloadBtn = document.createElement('button');
  downloadBtn.className = 'ghost';
  downloadBtn.innerHTML = '📥 Download Excel';
  downloadBtn.style.cssText = 'margin-top: 6px; padding: 6px 12px; font-size: 13px; margin-left: 8px;';
  
  downloadBtn.addEventListener('click', async () => {
    try {
      downloadBtn.disabled = true;
      downloadBtn.textContent = 'Downloading...';

      const timestamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '_');
      const filename = `table_${timestamp}`;
      let excelResponse;

      if (fullKey) {
        // Use new endpoint: re-executes code server-side for full data
        excelResponse = await fetch(`/api/chat/${currentChatId}/download_excel/${fullKey}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: filename })
        });
      } else {
        // No full key — export the preview table directly
        excelResponse = await fetch(`/api/chat/${currentChatId}/export_excel`, {
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
  return true;
}

// Lock/unlock send button while a response is being generated.
// Textarea remains enabled so the user can type their next question.
function setInputLocked(locked) {
  isRequestInProgress = locked;
  const btn = document.getElementById('btnSend');
  if (btn) btn.disabled = locked;
}

// Try to recover a response from saved conversation history after a failed send.
// Retries multiple times with increasing delays to handle in-flight saves.
// Returns true if recovery succeeded and the response was displayed.
async function _tryRecoverResponse(chatId, convId, loadingDiv) {
  if (!chatId || !convId) return false;
  const delays = [3000, 5000, 8000]; // Retry with increasing delays
  for (let attempt = 0; attempt < delays.length; attempt++) {
    try {
      await new Promise(r => setTimeout(r, delays[attempt]));
      const res = await fetch(`/api/chat/${chatId}/conversation/${convId}/history`);
      if (!res.ok) continue;
      const data = await res.json();
      const history = data.history || [];
      if (history.length < 2) continue;
      // Check if the last message is an AI response (server saved it)
      const last = history[history.length - 1];
      if (last.role !== 'ai') continue;
      const hasContent = last.content || last.image_base64 || (last.table && last.table.columns) || (last.images && last.images.length > 0);
      if (!hasContent) continue;
      // Recovery successful - display the saved response
      loadingDiv.remove();
      if (last.images && Array.isArray(last.images) && last.images.length > 0) {
        last.images.forEach(img => {
          appendMessage('assistant', img.answer || '', img.image_base64, null, null);
        });
      } else {
        appendMessage('assistant', last.content || '', last.image_base64, last.table, last.full_table_key);
      }
      chatMessages.scrollTop = chatMessages.scrollHeight;
      console.log(`Response recovered from saved history (attempt ${attempt + 1})`);
      return true;
    } catch (err) {
      console.warn(`Recovery attempt ${attempt + 1} failed:`, err);
    }
  }
  return false;
}

// Send message
async function sendMessage() {
  if (isRequestInProgress) return;
  const question = chatInput.value.trim();
  if (!question || !currentChatId) return;

  chatInput.value = '';
  chatInput.style.height = 'auto';

  // Lock input immediately
  setInputLocked(true);

  // Add user message
  appendMessage('user', question);
  chatMessages.scrollTop = chatMessages.scrollHeight;

  // Show animated loading bubble (progress text updated by server SSE events)
  const loadingDiv = document.createElement('div');
  loadingDiv.className = 'message assistant thinking-msg';
  loadingDiv.id = 'loadingBubble';
  loadingDiv.innerHTML = '<div class="message-content"><div class="thinking-indicator"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span class="loading-status-text">Thinking...</span></div></div>';
  chatMessages.appendChild(loadingDiv);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  const statusTextEl = loadingDiv.querySelector('.loading-status-text');

  // Abort controller with 240s timeout (Cloud Run has 300s, leave margin)
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 240000);

  try {
    // Use SSE streaming endpoint
    const res = await fetch(`/api/chat/${currentChatId}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, conv_id: currentConvId }),
      signal: controller.signal
    });

    // NOTE: Do NOT clearTimeout(timeoutId) here.
    // The SSE body hasn't been read yet — loading bubble must stay visible.
    // Cleanup happens when real content arrives or in the finally block.

    // Handle authentication failure - redirect to login
    if (res.status === 401) {
      console.warn('Session expired, redirecting to login');
      window.location.href = '/';
      return;
    }

    // Check if response is SSE stream
    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('text/event-stream')) {
      // SSE streaming path — loadingDiv stays visible until first real content
      let loadingBubbleVisible = true;
      let convIdUpdated = false;
      let chartCount = 0;
      let streamLoadingDiv = null;

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop(); // Keep incomplete line in buffer

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          let data;
          try {
            data = JSON.parse(line.slice(6));
          } catch (parseErr) {
            console.warn('SSE parse error:', parseErr);
            continue;
          }

          if (data.error) {
            if (loadingBubbleVisible) {
              loadingDiv.remove();
              loadingBubbleVisible = false;
              clearTimeout(timeoutId);
            }
            appendMessage('assistant', data.error);
            chatMessages.scrollTop = chatMessages.scrollHeight;
            continue;
          }

          // Handle real-time progress events from the server
          if (data.progress) {
            if (statusTextEl && loadingBubbleVisible) {
              statusTextEl.textContent = data.message || 'Processing...';
            }
            continue;
          }

          // Update conv_id on first event
          if (data.conv_id && !convIdUpdated) {
            if (!currentConvId) {
              currentConvId = data.conv_id;
              console.log('New conversation created:', data.conv_id);
              window.history.pushState({ convId: data.conv_id, chatId: currentChatId }, '', `/c/${data.conv_id}`);
              const convDropdown = document.getElementById('conversationList');
              const convToggle = document.querySelector('[data-target="conversationList"]');
              if (convDropdown && convToggle) {
                convDropdown.classList.remove('hidden');
                convToggle.classList.add('open');
              }
            }
            convIdUpdated = true;
          }

          if (data.partial) {
            // Remove main loading bubble on first real content
            if (loadingBubbleVisible) {
              loadingDiv.remove();
              loadingBubbleVisible = false;
              clearTimeout(timeoutId);
            }
            // Remove "loading next chart" indicator if present
            if (streamLoadingDiv) {
              streamLoadingDiv.remove();
              streamLoadingDiv = null;
            }

            // Append chart as a new assistant message
            chartCount++;
            appendMessage('assistant', data.answer || '', data.image_base64);
            chatMessages.scrollTop = chatMessages.scrollHeight;

            // Show "Rendering chart N of M..." if more charts expected
            if (data.chart_n < data.chart_total) {
              streamLoadingDiv = document.createElement('div');
              streamLoadingDiv.className = 'message assistant thinking-msg';
              streamLoadingDiv.innerHTML = `<div class="message-content"><div class="thinking-indicator"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span class="loading-status-text">Rendering chart ${data.chart_n + 1} of ${data.chart_total}...</span></div></div>`;
              chatMessages.appendChild(streamLoadingDiv);
              chatMessages.scrollTop = chatMessages.scrollHeight;
            }
          } else if (data.done) {
            // Remove main loading bubble if still visible (single-response path)
            if (loadingBubbleVisible) {
              loadingDiv.remove();
              loadingBubbleVisible = false;
              clearTimeout(timeoutId);
            }
            // Final event — remove any remaining inter-chart loading indicator
            if (streamLoadingDiv) {
              streamLoadingDiv.remove();
              streamLoadingDiv = null;
            }

            // For single-response (non-multi-plot) that came via SSE
            if (chartCount === 0) {
              const answer = data.answer || '';
              const hasContent = answer || data.image_base64 || (data.table && data.table.columns);
              if (!hasContent) {
                appendMessage('assistant', 'I processed your request but could not generate a response. Please try rephrasing your question.');
              } else {
                appendMessage('assistant', answer, data.image_base64, data.table, data.full_table_key);
              }
              chatMessages.scrollTop = chatMessages.scrollHeight;
            }

            // Update report dropdown visibility
            const allMsgs = chatMessages.querySelectorAll('.message.assistant');
            let reportable = 0;
            allMsgs.forEach(el => {
              if (el.querySelector('img, iframe, table')) reportable++;
            });
            if (reportable >= 2) {
              document.getElementById('downloadReportDropdown')?.classList.remove('hidden');
            }
          }
        }
      }

      // Refresh lists after stream completes (non-blocking)
      loadUnifiedChatList(true).catch(e => console.warn('List refresh failed:', e));

    } else {
      // Non-SSE fallback (JSON response, e.g. error responses)
      let data;
      try {
        data = await res.json();
      } catch (jsonError) {
        console.error('Server returned non-JSON response:', jsonError);
        const recovered = await _tryRecoverResponse(currentChatId, currentConvId, loadingDiv);
        if (!recovered) {
          loadingDiv.innerHTML = '<div class="message-content"><p style="color:#ef4444;">Server error. Please try again or refresh the page.</p></div>';
        }
        return;
      }

      if (data.error) {
        loadingDiv.innerHTML = `<div class="message-content"><p style="color:#ef4444;">${escapeHtml(data.error)}</p></div>`;
        return;
      }

      // Update conv_id if new
      if (data.conv_id && !currentConvId) {
        currentConvId = data.conv_id;
        console.log('New conversation created:', data.conv_id);
        window.history.pushState({ convId: data.conv_id, chatId: currentChatId }, '', `/c/${data.conv_id}`);
        const convDropdown = document.getElementById('conversationList');
        const convToggle = document.querySelector('[data-target="conversationList"]');
        if (convDropdown && convToggle) {
          convDropdown.classList.remove('hidden');
          convToggle.classList.add('open');
        }
      }

      loadUnifiedChatList(true).catch(e => console.warn('List refresh failed:', e));
      clearTimeout(timeoutId);
      loadingDiv.remove();

      const answer = data.answer || '';
      const hasContent = answer || data.image_base64 || (data.table && data.table.columns);
      if (!hasContent) {
        appendMessage('assistant', 'I processed your request but could not generate a response. Please try rephrasing your question.');
      } else {
        appendMessage('assistant', answer, data.image_base64, data.table, data.full_table_key);
      }
      chatMessages.scrollTop = chatMessages.scrollHeight;

      if (data.image_base64 || (data.table && data.table.columns)) {
        const allMsgs = chatMessages.querySelectorAll('.message.assistant');
        let reportable = 0;
        allMsgs.forEach(el => {
          if (el.querySelector('img, iframe, table')) reportable++;
        });
        if (reportable >= 2) {
          document.getElementById('downloadReportDropdown')?.classList.remove('hidden');
        }
      }
    }

  } catch (e) {
    clearTimeout(timeoutId);
    console.error('Failed to send message:', e);

    // Try to recover: the server may have saved the response before the connection dropped
    const recovered = await _tryRecoverResponse(currentChatId, currentConvId, loadingDiv);
    if (!recovered) {
      let errorMsg = 'Failed to send message. Please try again.';
      if (e.name === 'AbortError') {
        errorMsg = 'Request timed out. Your response may still be processing — try refreshing the page in a moment.';
      } else if (e.message) {
        if (e.message.includes('NetworkError') || e.message.includes('Failed to fetch')) {
          errorMsg = 'Network error. Please check your connection and try again.';
        } else if (e.message.includes('JSON')) {
          errorMsg = 'Server returned an invalid response. Please try again.';
        } else {
          errorMsg = `Error: ${e.message}`;
        }
      }
      loadingDiv.innerHTML = `<div class="message-content"><p style="color:#ef4444;">${escapeHtml(errorMsg)}</p></div>`;
    }
  } finally {
    setInputLocked(false);
    clearTimeout(timeoutId);
  }
}

// Edit message functions
function startEditMessage(messageDiv) {
  // Only allow editing if this is the last user message
  const allMessages = chatMessages.querySelectorAll('.message.user');
  const lastUserMessage = allMessages[allMessages.length - 1];

  if (messageDiv !== lastUserMessage) {
    showToast('You can only edit the last message', true);
    return;
  }

  // Check if already in edit mode
  if (messageDiv.classList.contains('editing')) return;

  // Capture original width BEFORE adding editing class
  const originalWidth = messageDiv.offsetWidth;

  const originalContent = messageDiv.dataset.originalContent;
  messageDiv.classList.add('editing');

  // Hide the content and edit button
  const contentDiv = messageDiv.querySelector('.message-content');
  const editBtn = messageDiv.querySelector('.message-edit-btn');
  if (contentDiv) contentDiv.style.display = 'none';
  if (editBtn) editBtn.style.display = 'none';

  // Create edit container with original width
  const editContainer = document.createElement('div');
  editContainer.className = 'message-edit-container';
  editContainer.style.width = originalWidth + 'px';

  const textarea = document.createElement('textarea');
  textarea.className = 'message-edit-textarea';
  textarea.value = originalContent;

  // Auto-resize textarea height to fit content
  const adjustHeight = () => {
    textarea.style.height = 'auto';
    textarea.style.height = textarea.scrollHeight + 'px';
  };
  textarea.addEventListener('input', adjustHeight);

  const buttonContainer = document.createElement('div');
  buttonContainer.className = 'message-edit-buttons';

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'edit-cancel-btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', () => cancelEditMessage(messageDiv));

  const saveBtn = document.createElement('button');
  saveBtn.className = 'edit-send-btn';
  saveBtn.textContent = 'Send';
  saveBtn.addEventListener('click', () => editAndRegenerate(messageDiv, textarea.value.trim()));

  buttonContainer.appendChild(cancelBtn);
  buttonContainer.appendChild(saveBtn);

  editContainer.appendChild(textarea);
  editContainer.appendChild(buttonContainer);

  messageDiv.appendChild(editContainer);

  // Set initial height and focus
  setTimeout(() => {
    adjustHeight();
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  }, 0);
}

function cancelEditMessage(messageDiv) {
  messageDiv.classList.remove('editing');

  // Show the content and edit button again
  const contentDiv = messageDiv.querySelector('.message-content');
  const editBtn = messageDiv.querySelector('.message-edit-btn');
  if (contentDiv) contentDiv.style.display = '';
  if (editBtn) editBtn.style.display = '';

  // Remove edit container
  const editContainer = messageDiv.querySelector('.message-edit-container');
  if (editContainer) editContainer.remove();
}

async function editAndRegenerate(messageDiv, editedQuestion) {
  if (!editedQuestion) {
    showToast('Question cannot be empty', true);
    return;
  }

  if (!currentChatId || !currentConvId) {
    showToast('No active conversation', true);
    return;
  }

  // Lock input
  setInputLocked(true);

  // Remove the edit UI
  cancelEditMessage(messageDiv);

  // Find and remove this message and the AI response after it
  const allMessages = Array.from(chatMessages.querySelectorAll('.message'));
  const msgIndex = allMessages.indexOf(messageDiv);

  // Remove this message and all messages after it (which should be the AI response)
  for (let i = allMessages.length - 1; i >= msgIndex; i--) {
    allMessages[i].remove();
  }

  // Add the edited user message
  appendMessage('user', editedQuestion);
  chatMessages.scrollTop = chatMessages.scrollHeight;

  // Show animated loading indicator
  const loadingDiv = document.createElement('div');
  loadingDiv.className = 'message assistant thinking-msg';
  loadingDiv.innerHTML = '<div class="message-content"><div class="thinking-indicator"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span class="loading-status-text">Regenerating...</span></div></div>';
  chatMessages.appendChild(loadingDiv);
  chatMessages.scrollTop = chatMessages.scrollHeight;

  const editController = new AbortController();
  const editTimeoutId = setTimeout(() => editController.abort(), 240000);

  try {
    const res = await fetch(`/api/chat/${currentChatId}/edit-regenerate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        edited_question: editedQuestion,
        conv_id: currentConvId
      }),
      signal: editController.signal
    });

    clearTimeout(editTimeoutId);

    let data;
    try {
      data = await res.json();
    } catch (jsonError) {
      console.error('Server returned non-JSON response:', jsonError);
      const recovered = await _tryRecoverResponse(currentChatId, currentConvId, loadingDiv);
      if (!recovered) {
        loadingDiv.innerHTML = '<div class="message-content"><p style="color:#ef4444;">Server error. Please try again or refresh the page.</p></div>';
      }
      return;
    }

    if (data.error) {
      loadingDiv.innerHTML = `<div class="message-content"><p style="color:#ef4444;">${escapeHtml(data.error)}</p></div>`;
      return;
    }

    // Remove loading and add response
    loadingDiv.remove();

    const answer = data.answer || '';
    const hasContent = answer || data.image_base64 || (data.table && data.table.columns);

    if (!hasContent) {
      console.warn('Empty response from server:', data);
      appendMessage('assistant', 'I processed your request but could not generate a response. Please try rephrasing your question.');
    } else {
      appendMessage('assistant', answer, data.image_base64, data.table, data.full_table_key);
    }
    chatMessages.scrollTop = chatMessages.scrollHeight;

  } catch (e) {
    clearTimeout(editTimeoutId);
    console.error('Failed to regenerate message:', e);
    const recovered = await _tryRecoverResponse(currentChatId, currentConvId, loadingDiv);
    if (!recovered) {
      let errorMsg = 'Failed to regenerate. Please try again.';
      if (e.name === 'AbortError') {
        errorMsg = 'Request timed out. Your response may still be processing — try refreshing the page in a moment.';
      }
      loadingDiv.innerHTML = `<div class="message-content"><p style="color:#ef4444;">${escapeHtml(errorMsg)}</p></div>`;
    }
  } finally {
    setInputLocked(false);
    clearTimeout(editTimeoutId);
  }
}

// Create wizard functions (single-step modal — Generate Chat button is the only flow trigger)
function openCreateWizard() {
  selectedFiles = [];
  renderSelectedFilesList();
  _updateWizardGenerateBtn();
  // Reset session so the wizard always starts fresh
  fetch('/new_session', { method: 'POST' });
  document.getElementById('createNewModal').classList.remove('hidden');
}

// Enable Generate Chat only when there's at least one selected file (or shared Google Sheet).
// Uses the standard `disabled` attribute on the existing .primary button — no new visual.
function _updateWizardGenerateBtn() {
  const btn = document.getElementById('btnWizardNext');
  if (!btn) return;
  btn.disabled = selectedFiles.length === 0;
}

function closeCreateWizard() {
  document.getElementById('createNewModal').classList.add('hidden');
}

// Single-step wizard "Generate Chat" handler — uses the same frictionless flow as page drop
async function wizardNextStep() {
  if (selectedFiles.length === 0) {
    showToast('Please select at least one file', true);
    return;
  }
  // Close the modal first so the progress overlay is the only thing visible
  closeCreateWizard();

  const regularFiles = selectedFiles.filter(f => !f._isGoogleSheet);
  // Google Sheets are already uploaded by /upload_from_url; if any are selected,
  // we MUST NOT call /new_session again (that would wipe them). The wizard already
  // reset the session in openCreateWizard().
  await runFrictionlessFlow(regularFiles, { skipUploadIfEmpty: true, resetSession: false });
}

// File handling — used by wizard <input>, page drop, welcome pick-button drop
function handleFileSelection(e) {
  if (e.target.files.length) {
    addFilesToSelection(e.target.files);
    e.target.value = '';
  }
}

function addFilesToSelection(newFiles) {
  const unique = Array.from(newFiles).filter(f =>
    !selectedFiles.some(existing => existing.name === f.name)
  );

  if (selectedFiles.length + unique.length > userMaxFiles) {
    showToast(`Your plan allows only ${userMaxFiles} file(s). Remove some or upgrade.`, true);
    return;
  }

  // Reject unsupported types up-front
  for (const f of unique) {
    if (!f._isGoogleSheet && !_hasValidUploadExtension(f.name)) {
      showToast(_t('lab.invalid_file_type', 'Unsupported file type. Use .xlsx, .xls, .csv, or .tsv.'), true);
      return;
    }
  }

  selectedFiles.push(...unique);
  renderSelectedFilesList();
  _updateWizardGenerateBtn();
}

function removeFile(index) {
  selectedFiles.splice(index, 1);
  renderSelectedFilesList();
  _updateWizardGenerateBtn();
}

// Single-step wizard now shows just the filename + remove button (no description input)
function renderSelectedFilesList() {
  const container = document.getElementById('selectedFilesList');
  if (!container) return;
  container.innerHTML = '';
  selectedFiles.forEach((file, index) => {
    const isGoogle = !!file._isGoogleSheet;
    const row = document.createElement('div');
    row.className = 'selected-file-row' + (isGoogle ? ' google-sheet' : '');
    row.innerHTML = `
      <span class="selected-file-name">
        ${index + 1}. ${escapeHtml(file.name)}
        ${isGoogle ? '<span class="selected-file-badge">📎 Google Sheet</span>' : ''}
      </span>
      <button type="button" class="file-remove-btn" onclick="removeFile(${index})">×</button>
    `;
    container.appendChild(row);
  });
}

/**
 * Frictionless drop flow.
 * Steps: Upload → /schema_autofill_full → /generate_chatdata → openChat
 * Used by both page-level drop and the single-step Create New modal.
 *
 * @param {File[]} files local files to upload (may be empty when only Google Sheets are present;
 *   in that case `opts.skipUploadIfEmpty` should be true so we still continue to autofill+generate).
 * @param {{skipUploadIfEmpty?: boolean, resetSession?: boolean}} opts
 */
async function runFrictionlessFlow(files, opts) {
  if (isFrictionlessFlowRunning) return;
  opts = opts || {};
  const filesArr = Array.from(files || []);

  // Validate file extensions
  for (const f of filesArr) {
    if (!_hasValidUploadExtension(f.name)) {
      showToast(_t('lab.invalid_file_type', 'Unsupported file type. Use .xlsx, .xls, .csv, or .tsv.'), true);
      return;
    }
  }

  // Plan limit check (covers both page drop and modal flows)
  if (filesArr.length > userMaxFiles) {
    showToast(`Your plan allows only ${userMaxFiles} file(s) per upload. Upgrade to upload more.`, true);
    return;
  }

  if (filesArr.length === 0 && !opts.skipUploadIfEmpty) {
    showToast('Please select at least one file', true);
    return;
  }

  isFrictionlessFlowRunning = true;
  _showFrictionlessOverlay('lab.flow_uploading');

  try {
    // Step 1: Upload (only if we have local files; Google Sheets are already on the server)
    if (filesArr.length > 0) {
      // Reset session unless caller already did so (the wizard resets on open)
      if (opts.resetSession !== false) {
        try { await fetch('/new_session', { method: 'POST' }); } catch (e) { /* non-fatal */ }
      }

      const hasLargeFile = filesArr.some(f => f.size > LARGE_UPLOAD_THRESHOLD_BYTES);
      if (hasLargeFile) {
        // Direct-to-GCS path: /new_session above already reset the session, so
        // we just init→PUT→finalize sequentially for each file.
        try {
          for (const f of filesArr) {
            await _directUploadFile(f, null);
          }
        } catch (err) {
          _hideFrictionlessOverlay();
          showToast((err && err.message) || 'Upload failed', true);
          return;
        }
      } else {
        const formData = new FormData();
        filesArr.forEach(f => formData.append('files', f));
        // No `file_descriptions` field — backend now accepts upload without descriptions and the
        // /schema_autofill_full step generates them.

        const upRes = await fetch('/upload', { method: 'POST', body: formData });
        let upData = {};
        try { upData = await upRes.json(); } catch (e) { upData = {}; }
        if (!upRes.ok || upData.error) {
          const msg = upData.error || `Upload failed (${upRes.status})`;
          _hideFrictionlessOverlay();
          showToast(msg, true);
          return;
        }
      }
    }

    // Step 2: AI auto-fill of file + column descriptions in one call per file
    _updateFrictionlessStatus('lab.flow_analyzing');
    try {
      const afRes = await fetch('/schema_autofill_full', { method: 'POST' });
      if (!afRes.ok) {
        // Continue — chat can still be created with empty descriptions
        console.warn('[FRICTIONLESS] schema_autofill_full HTTP', afRes.status);
        showToast(_t('lab.ai_desc_unavailable', 'AI descriptions unavailable, you can edit them later'), true);
      }
    } catch (e) {
      console.warn('[FRICTIONLESS] schema_autofill_full failed:', e);
      showToast(_t('lab.ai_desc_unavailable', 'AI descriptions unavailable, you can edit them later'), true);
    }

    // Step 3: Create the chat
    _updateFrictionlessStatus('lab.flow_creating');
    let genData = null;
    try {
      const genRes = await fetch('/generate_chatdata', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      try { genData = await genRes.json(); } catch (e) { genData = null; }
      if (!genRes.ok || !genData || genData.error) {
        const msg = (genData && genData.error) || `Chat creation failed (${genRes.status})`;
        _hideFrictionlessOverlay();
        showToast(msg, true);
        return;
      }
    } catch (e) {
      console.error('[FRICTIONLESS] generate_chatdata failed:', e);
      _hideFrictionlessOverlay();
      showToast('Failed to create chat — please try again', true);
      return;
    }

    // Refresh sidebar list (force) and open the new chat
    try { await loadUnifiedChatList(true); } catch (e) { /* non-fatal */ }
    try {
      await openChat(genData.chat_id);
    } catch (e) {
      console.error('[FRICTIONLESS] openChat failed:', e);
      showToast('Chat created but failed to open — refresh and pick it from the sidebar', true);
    }
  } finally {
    isFrictionlessFlowRunning = false;
    _hideFrictionlessOverlay();
  }
}

// Import from Google URL
async function importFromGoogleUrl() {
  const url = document.getElementById('googleUrlInput').value.trim();
  const status = document.getElementById('urlImportStatus');
  
  if (!url) {
    status.textContent = 'Please enter a URL';
    status.style.color = '#ef4444';
    return;
  }
  
  showLoading('Importing from Google...');
  
  try {
    const res = await fetch('/upload_from_url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, description: 'Shared Google Sheet' })
    });
    
    const data = await res.json();
    
    if (data.error) {
      status.textContent = data.error;
      status.style.color = '#ef4444';
    } else {
      // Add as a file
      const fakeFile = { name: data.filename, _isGoogleSheet: true };
      selectedFiles.push(fakeFile);
      renderSelectedFilesList();
      _updateWizardGenerateBtn();

      document.getElementById('googleSheetInputBox').classList.add('hidden');
      document.getElementById('googleUrlInput').value = '';
      status.textContent = '';
    }
  } catch (e) {
    status.textContent = 'Failed to import';
    status.style.color = '#ef4444';
  }
  
  hideLoading();
}

// (Schema editing in the new flow lives in the post-creation modal — see openSchemaViewer.)
// The previous in-wizard step-2 schema editor and per-file description inputs were removed
// when the frictionless drop flow shipped. AI now generates BOTH file and column descriptions
// in a single /schema_autofill_full call right after upload.

// Profile functions
async function saveProfile() {
  showLoading('Saving...');

  // If password fields are filled, change password first
  const newPw = (document.getElementById('profNewPw').value || '').trim();
  if (newPw) {
    const confirmPw = (document.getElementById('profConfirmPw').value || '').trim();
    if (newPw !== confirmPw) {
      document.getElementById('pwMatchError').classList.remove('hidden');
      showToast('Passwords do not match', true);
      hideLoading();
      return;
    }
    document.getElementById('pwMatchError').classList.add('hidden');
    try {
      const current = document.getElementById('profCurrentPw').value;
      const res = await fetch('/auth/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: current, new_password: newPw })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(data.error || 'Failed to change password', true);
        hideLoading();
        return;
      }
      document.getElementById('profCurrentPw').value = '';
      document.getElementById('profNewPw').value = '';
      document.getElementById('profConfirmPw').value = '';
    } catch (e) {
      showToast('Failed to change password', true);
      hideLoading();
      return;
    }
  }

  try {
    await fetch('/auth/profile/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email: document.getElementById('profEmail').value,
        full_name: document.getElementById('profFullName').value,
        new_username: document.getElementById('profUsername').value
      })
    });
    showToast(newPw ? 'Profile and password saved' : 'Profile saved');
    document.getElementById('profileModal').classList.add('hidden');
  } catch (e) {
    showToast('Failed to save profile', true);
  }

  hideLoading();
}

async function changePassword() {
  const current = document.getElementById('profCurrentPw').value;
  const newPw = (document.getElementById('profNewPw').value || '').trim();
  const confirmPw = (document.getElementById('profConfirmPw').value || '').trim();

  if (!newPw) {
    showToast('Please enter a new password', true);
    return;
  }
  if (newPw !== confirmPw) {
    document.getElementById('pwMatchError').classList.remove('hidden');
    showToast('Passwords do not match', true);
    return;
  }
  document.getElementById('pwMatchError').classList.add('hidden');

  showLoading('Changing password...');

  try {
    const res = await fetch('/auth/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: current, new_password: newPw })
    });
    const data = await res.json();

    if (!res.ok || data.error) {
      showToast(data.error || 'Failed to change password', true);
    } else {
      showToast('Password changed');
      document.getElementById('profCurrentPw').value = '';
      document.getElementById('profNewPw').value = '';
      document.getElementById('profConfirmPw').value = '';
    }
  } catch (e) {
    showToast('Failed to change password', true);
  }

  hideLoading();
}

async function selectPlan(plan) {
  // Enterprise — open About modal to contact section
  if (plan === 'Enterprise') { openAboutContact(); return; }
  // Undo scheduled cancellation or downgrade
  if (plan === currentSubscriptionPlan) {
    if (paddleScheduledChange === 'cancel' || paddleScheduledChange === 'downgrade') {
      reactivateSubscription();
    }
    return;
  }

  // Show loading modal
  var rankMap = { Basic: 0, Standard: 1, Pro: 2, Enterprise: 3 };
  var actionLabel = (rankMap[plan] > rankMap[currentSubscriptionPlan]) ? 'Upgrading' : 'Switching';
  _openSubModal(actionLabel + ' to ' + plan + '...',
    '<div class="sub-modal-spinner"><p>Calculating your price...</p></div>', '');

  try {
    var res = await fetch('/api/paddle/subscription/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan: plan })
    });
    var data = await res.json();

    if (!data.ok) {
      _openSubModal('Error',
        '<div class="sub-modal-highlight error">' + (data.error || 'Failed to load pricing') + '</div>',
        '<button class="ghost" onclick="_closeSubModal()">Close</button>');
      return;
    }

    _showPreviewModal(data);

  } catch (e) {
    _openSubModal('Error',
      '<div class="sub-modal-highlight error">Failed to load pricing details. Please try again.</div>',
      '<button class="primary" onclick="_closeSubModal(); selectPlan(\'' + plan + '\')">Try Again</button>' +
      '<button class="ghost" onclick="_closeSubModal()">Close</button>');
  }
}

function _showPreviewModal(data) {
  var ct = data.change_type;
  var body = '';
  var actions = '';

  // --- New subscription: open Paddle checkout ---
  if (ct === 'new_subscription') {
    _closeSubModal();
    _openPaddleCheckout(data.new_plan);
    return;
  }

  // --- Admin direct change ---
  if (ct === 'admin_direct') {
    var title = (data.new_plan === 'Basic') ? 'Downgrade to Basic' : 'Change to ' + data.new_plan;
    body = '<p>' + data.message + '</p>';
    actions = '<button class="primary" onclick="_confirmAdminChange(\'' + data.new_plan + '\')">Confirm</button>' +
              '<button class="ghost" onclick="_closeSubModal()">Cancel</button>';
    _openSubModal(title, body, actions);
    return;
  }

  // --- Upgrade (e.g. Standard → Pro) ---
  if (ct === 'upgrade') {
    body = '<div class="sub-modal-row"><span class="label">Current plan</span><span class="value">' + data.current_plan + ' (' + data.current_price + ')</span></div>' +
           '<div class="sub-modal-row"><span class="label">New plan</span><span class="value">' + data.new_plan + ' (' + data.new_price + ')</span></div>' +
           '<hr class="sub-modal-divider">';
    if (data.immediate_charge) {
      body += '<div class="sub-modal-row"><span class="label">Prorated charge today</span><span class="value">' + _fmtCurrency(data.immediate_charge.amount, data.immediate_charge.currency) + '</span></div>';
    }
    if (data.next_billing) {
      body += '<div class="sub-modal-row"><span class="label">Next billing' + (data.next_billing.date_formatted ? ' (' + data.next_billing.date_formatted + ')' : '') + '</span><span class="value">' + _fmtCurrency(data.next_billing.amount, data.next_billing.currency) + '/mo</span></div>';
    }
    if (data.immediate_charge) {
      body += '<div class="sub-modal-highlight">Your card on file will be charged ' + _fmtCurrency(data.immediate_charge.amount, data.immediate_charge.currency) + ' immediately for the upgrade.</div>';
      body += '<div class="sub-modal-link">Need to change payment method? <a onclick="_updatePaymentMethod()">Update payment details</a></div>';
    }
    actions = '<button class="primary" onclick="_confirmPlanChange(\'upgrade\', \'' + data.new_plan + '\')">Confirm Upgrade</button>' +
              '<button class="ghost" onclick="_closeSubModal()">Cancel</button>';
    _openSubModal('Upgrade to ' + data.new_plan, body, actions);
    return;
  }

  // --- Downgrade between paid (e.g. Pro → Standard) ---
  if (ct === 'downgrade') {
    body = '<div class="sub-modal-row"><span class="label">Current plan</span><span class="value">' + data.current_plan + ' (' + data.current_price + ')</span></div>' +
           '<div class="sub-modal-row"><span class="label">New plan</span><span class="value">' + data.new_plan + ' (' + data.new_price + ')</span></div>' +
           '<hr class="sub-modal-divider">' +
           '<div class="sub-modal-row"><span class="label">No charge today</span><span class="value"></span></div>';
    if (data.current_period_ends_formatted) {
      body += '<div class="sub-modal-row"><span class="label">Change takes effect</span><span class="value">' + data.current_period_ends_formatted + '</span></div>';
    }
    if (data.next_billing) {
      body += '<div class="sub-modal-row"><span class="label">Next billing</span><span class="value">' + _fmtCurrency(data.next_billing.amount, data.next_billing.currency) + '/mo</span></div>';
    }
    body += '<div class="sub-modal-highlight warning">You\'ll keep ' + data.current_plan + ' features until ' + (data.current_period_ends_formatted || 'the end of your billing period') + '.</div>';
    actions = '<button class="primary" onclick="_confirmPlanChange(\'downgrade\', \'' + data.new_plan + '\')">Confirm Downgrade</button>' +
              '<button class="ghost" onclick="_closeSubModal()">Keep ' + data.current_plan + '</button>';
    _openSubModal('Downgrade to ' + data.new_plan, body, actions);
    return;
  }

  // --- Cancel (Paid → Basic) ---
  if (ct === 'cancel') {
    body = '<div class="sub-modal-row"><span class="label">Current plan</span><span class="value">' + data.current_plan + ' (' + data.current_price + ')</span></div>' +
           '<hr class="sub-modal-divider">' +
           '<div class="sub-modal-highlight warning">' + data.message + '</div>' +
           '<p style="font-size:13px;color:#6b7280;margin-top:8px;">After that, you\'ll switch to the free Basic plan with:</p>' +
           '<ul class="sub-modal-features"><li>1 file per upload</li><li>10 messages per day</li><li>1 PDF report/month</li></ul>';
    actions = '<button class="primary" onclick="_confirmPlanChange(\'cancel\', \'Basic\')">Confirm Downgrade</button>' +
              '<button class="ghost" onclick="_closeSubModal()">Keep Plan</button>';
    _openSubModal('Downgrade to Basic', body, actions);
    return;
  }
}

async function _confirmPlanChange(changeType, plan) {
  _subBody().innerHTML = '<div class="sub-modal-spinner"><p>Processing...</p></div>';
  _subActions().innerHTML = '';

  try {
    var endpoint, body = {};
    if (changeType === 'cancel') {
      endpoint = '/api/paddle/subscription/cancel';
    } else if (changeType === 'undo_cancel') {
      endpoint = '/api/paddle/subscription/reactivate';
    } else {
      endpoint = '/api/paddle/subscription/update';
      body = { plan: plan };
    }

    var res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    var data = await res.json();

    if (data.ok) {
      var msg = '';
      var title = 'Plan Updated';
      if (changeType === 'cancel') {
        if (data.immediate) {
          msg = 'You\'re now on the Basic plan.';
        } else {
          var dateStr = data.effective_at ? new Date(data.effective_at).toLocaleDateString() : 'end of billing period';
          msg = 'Your plan will switch to Basic on ' + dateStr + '. You\'ll keep your current features until then.';
          title = 'Cancellation Scheduled';
        }
      } else if (changeType === 'undo_cancel') {
        msg = 'Your ' + (data.plan || plan) + ' plan will continue.';
      } else if (data.immediate === false) {
        var dateStr = data.effective_at ? new Date(data.effective_at).toLocaleDateString() : 'next billing date';
        msg = 'You\'ll switch to ' + plan + ' on ' + dateStr + '. You keep all ' + currentSubscriptionPlan + ' features until then.';
        title = 'Downgrade Scheduled';
      } else {
        msg = 'You\'re now on the ' + plan + ' plan.';
      }
      _openSubModal(title,
        '<div class="sub-modal-highlight success">' + msg + '</div>',
        '<button class="primary" onclick="_closeSubModal(); loadUserProfile();">Done</button>');
    } else {
      _openSubModal('Error',
        '<div class="sub-modal-highlight error">' + (data.error || 'Failed to process plan change') + '</div>',
        '<button class="primary" onclick="_closeSubModal(); selectPlan(\'' + plan + '\')">Try Again</button>' +
        '<button class="ghost" onclick="_closeSubModal()">Close</button>');
    }
  } catch (e) {
    _openSubModal('Error',
      '<div class="sub-modal-highlight error">Failed to process plan change. Please try again.</div>',
      '<button class="primary" onclick="_closeSubModal(); selectPlan(\'' + plan + '\')">Try Again</button>' +
      '<button class="ghost" onclick="_closeSubModal()">Close</button>');
  }
}

async function _confirmAdminChange(plan) {
  _subBody().innerHTML = '<div class="sub-modal-spinner"><p>Processing...</p></div>';
  _subActions().innerHTML = '';
  try {
    var res = await fetch('/auth/subscription', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan: plan })
    });
    var data = await res.json();
    if (data.error) {
      _openSubModal('Error',
        '<div class="sub-modal-highlight error">' + data.error + '</div>',
        '<button class="ghost" onclick="_closeSubModal()">Close</button>');
    } else {
      _openSubModal('Plan Updated',
        '<div class="sub-modal-highlight success">Switched to ' + plan + ' plan.</div>',
        '<button class="primary" onclick="_closeSubModal(); loadUserProfile();">Done</button>');
    }
  } catch (e) {
    _openSubModal('Error',
      '<div class="sub-modal-highlight error">Failed to change plan</div>',
      '<button class="ghost" onclick="_closeSubModal()">Close</button>');
  }
}

function _openPaddleCheckout(plan) {
  var cfg = window._paddleConfig;
  if (!cfg || !cfg.prices || !cfg.prices[plan]) {
    showToast('Payment system loading, please try again', true);
    return;
  }
  var emailEl = document.getElementById('profEmail');
  var usernameEl = document.getElementById('profUsername');
  var email = (emailEl && emailEl.value) || '';
  var username = (usernameEl && usernameEl.value) || '';
  try {
    var checkoutOpts = {
      items: [{ priceId: cfg.prices[plan], quantity: 1 }],
      customData: { username: username, email: email },
      settings: {
        successUrl: window.location.origin + '/lab?upgraded=true',
        displayMode: 'overlay',
        theme: 'light'
      }
    };
    if (email) checkoutOpts.customer = { email: email };
    Paddle.Checkout.open(checkoutOpts);
  } catch (e) {
    console.error('Paddle.Checkout.open error:', e);
    showToast('Failed to open checkout', true);
  }
}

// Share functions
async function openShareModal() {
  if (!currentChatId) return;
  
  showLoading('Loading share info...');
  
  try {
    const res = await fetch(`/api/chat/${currentChatId}/share`);
    const data = await res.json();
    
    document.getElementById('shareLink').value = data.absolute_url || '';
    document.getElementById('shareEmails').value = (data.allowed_emails || []).join(', ');
    
    document.getElementById('shareModal').classList.remove('hidden');
  } catch (e) {
    console.error('Failed to load share info:', e);
  }
  
  hideLoading();
}

async function saveShareSettings() {
  if (!currentChatId) return;
  
  const emailsStr = document.getElementById('shareEmails').value;
  const emails = emailsStr.split(',').map(e => e.trim()).filter(e => e);

  showLoading('Saving...');

  try {
    // Share with emails if any
    if (emails.length > 0) {
      await fetch(`/api/chat/${currentChatId}/share`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ emails })
      });
    }
    
    showToast('Sharing settings saved');
    document.getElementById('shareModal').classList.add('hidden');
  } catch (e) {
    showToast('Failed to save sharing settings', true);
  }
  
  hideLoading();
}

// View / Edit Descriptions modal — chat-scoped, editable
// Renders each file as a section with:
//   - editable file_description (pencil → inline input → Save/Cancel)
//   - editable column descriptions (one row per column, same pencil pattern)
// Saves via POST /api/chat/{chat_id}/schema (chat-level meta, persists for the whole chat).
// In-memory cache of the current chat's schema, refreshed each time the modal opens.
let _editorChatSchema = { files: [] };

async function openSchemaViewer() {
  if (!currentChatId) return;
  showLoading('Loading schema...');
  try {
    const res = await fetch(`/api/chat/${currentChatId}/schema`);
    if (!res.ok) {
      showToast('Failed to load schema', true);
      hideLoading();
      return;
    }
    const data = await res.json();
    _editorChatSchema = { files: Array.isArray(data.files) ? data.files : [] };
    _renderEditableSchema();
    document.getElementById('schemaViewerModal').classList.remove('hidden');
  } catch (e) {
    console.error('Failed to load schema:', e);
    showToast('Failed to load schema', true);
  }
  hideLoading();
}

function _renderEditableSchema() {
  const container = document.getElementById('schemaViewerContainer');
  if (!container) return;
  container.innerHTML = '';

  const files = _editorChatSchema.files || [];
  if (files.length === 0) {
    container.innerHTML = '<p style="color:#6b7280;">No files in this chat.</p>';
    return;
  }

  files.forEach((file, fileIdx) => {
    const section = document.createElement('div');
    section.className = 'editor-section';
    section.dataset.fileIdx = String(fileIdx);

    const heading = document.createElement('h4');
    heading.textContent = file.file_name || '(unnamed file)';
    section.appendChild(heading);

    // File description row
    section.appendChild(_buildEditorRow({
      labelText: _t('schema.file_description_label', 'File description'),
      dtype: '',
      value: file.file_description || '',
      multiline: true,
      onSave: (newVal, done) => _saveFileDescription(fileIdx, newVal, done),
    }));

    // Per-column rows
    const fields = (file.schema && file.schema.fields) ? file.schema.fields : {};
    const colNames = Object.keys(fields);
    if (colNames.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'editor-section-sub';
      empty.textContent = 'No columns indexed for this file.';
      section.appendChild(empty);
    } else {
      colNames.forEach(col => {
        const info = fields[col] || {};
        const dtype = info.technical_description ? String(info.technical_description).split(',')[0] : '';
        section.appendChild(_buildEditorRow({
          labelText: col,
          dtype: dtype,
          value: (typeof info.description === 'string') ? info.description : '',
          multiline: false,
          onSave: (newVal, done) => _saveColumnDescription(fileIdx, col, newVal, done),
        }));
      });
    }

    container.appendChild(section);
  });
}

function _buildEditorRow(opts) {
  // opts: { labelText, dtype, value, multiline, onSave(newVal, done) }
  const row = document.createElement('div');
  row.className = 'editor-row';

  const labelEl = document.createElement('div');
  labelEl.className = 'editor-label';
  labelEl.textContent = opts.labelText;
  if (opts.dtype) {
    const dtypeEl = document.createElement('span');
    dtypeEl.className = 'editor-dtype';
    dtypeEl.textContent = opts.dtype;
    labelEl.appendChild(dtypeEl);
  }
  row.appendChild(labelEl);

  const valueWrap = document.createElement('div');
  valueWrap.className = 'editor-value';
  row.appendChild(valueWrap);

  const renderRead = (currentValue) => {
    valueWrap.innerHTML = '';
    const text = document.createElement('div');
    text.className = 'editor-value-text';
    if (!currentValue) {
      text.classList.add('empty');
      text.textContent = _t('schema.no_description', 'No description yet — click the pencil to add one.');
    } else {
      text.textContent = currentValue;
    }
    valueWrap.appendChild(text);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'editor-edit-btn';
    btn.title = 'Edit';
    btn.innerHTML = '✏️';
    btn.addEventListener('click', () => renderEdit(currentValue));
    valueWrap.appendChild(btn);
  };

  const renderEdit = (currentValue) => {
    valueWrap.innerHTML = '';
    const form = document.createElement('div');
    form.className = 'editor-edit-form';

    const input = opts.multiline ? document.createElement('textarea') : document.createElement('input');
    input.className = 'editor-edit-input';
    if (!opts.multiline) input.type = 'text';
    input.value = currentValue || '';
    if (opts.multiline) input.rows = 2;
    form.appendChild(input);

    const errEl = document.createElement('div');
    errEl.className = 'editor-edit-error';
    errEl.style.display = 'none';
    form.appendChild(errEl);

    const actions = document.createElement('div');
    actions.className = 'editor-edit-actions';

    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'editor-save-btn';
    saveBtn.textContent = _t('common.save', 'Save');

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'editor-cancel-btn';
    cancelBtn.textContent = _t('common.cancel', 'Cancel');

    cancelBtn.addEventListener('click', () => renderRead(currentValue));

    saveBtn.addEventListener('click', () => {
      const newVal = (input.value || '').trim();
      saveBtn.disabled = true;
      cancelBtn.disabled = true;
      input.disabled = true;
      errEl.style.display = 'none';
      opts.onSave(newVal, (ok) => {
        if (ok) {
          showToast(_t('schema.saved', 'Saved'));
          renderRead(newVal);
        } else {
          saveBtn.disabled = false;
          cancelBtn.disabled = false;
          input.disabled = false;
          errEl.textContent = _t('schema.save_failed', 'Save failed — try again');
          errEl.style.display = 'block';
          showToast(_t('schema.save_failed', 'Save failed — try again'), true);
        }
      });
    });

    actions.appendChild(saveBtn);
    actions.appendChild(cancelBtn);
    form.appendChild(actions);
    valueWrap.appendChild(form);

    setTimeout(() => input.focus(), 30);
  };

  renderRead(opts.value || '');
  return row;
}

// Build the chat-level schema POST payload from the in-memory editor state and persist it.
// IMPORTANT: writes to chat-level meta (POST /api/chat/{chat_id}/schema), not per-conversation.
async function _persistChatSchema() {
  if (!currentChatId) return false;
  const filesPayload = (_editorChatSchema.files || []).map(file => {
    const fields = (file.schema && file.schema.fields) ? file.schema.fields : {};
    const fieldsOut = {};
    for (const [col, info] of Object.entries(fields)) {
      fieldsOut[col] = {
        description: (info && typeof info.description === 'string') ? info.description : '',
        technical_description: (info && info.technical_description) || null,
        values: (info && info.values && typeof info.values === 'object') ? info.values : null,
      };
    }
    return {
      file_name: file.file_name,
      file_description: file.file_description || '',
      fields: fieldsOut,
    };
  });

  try {
    const res = await fetch(`/api/chat/${currentChatId}/schema`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ files: filesPayload }),
    });
    return res.ok;
  } catch (e) {
    console.error('Failed to save chat schema:', e);
    return false;
  }
}

async function _saveFileDescription(fileIdx, newVal, done) {
  const file = (_editorChatSchema.files || [])[fileIdx];
  if (!file) { done(false); return; }
  const prev = file.file_description;
  file.file_description = newVal;
  const ok = await _persistChatSchema();
  if (!ok) file.file_description = prev;
  done(ok);
}

async function _saveColumnDescription(fileIdx, colName, newVal, done) {
  const file = (_editorChatSchema.files || [])[fileIdx];
  if (!file) { done(false); return; }
  if (!file.schema) file.schema = { file_name: file.file_name, fields: {} };
  if (!file.schema.fields) file.schema.fields = {};
  if (!file.schema.fields[colName]) file.schema.fields[colName] = {};
  const prev = file.schema.fields[colName].description;
  file.schema.fields[colName].description = newVal;
  const ok = await _persistChatSchema();
  if (!ok) file.schema.fields[colName].description = prev;
  done(ok);
}

// TODO(categorical): per-value descriptions are still saved through the legacy
// /api/chat/{chat_id}/schema payload (the `values` object on each field) but the
// View / Edit Descriptions modal does not yet expose them inline. They will be
// added in a follow-up. Edit categorical-value descriptions through the legacy
// /config page until then.

// Utility
function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Context menu for list items
function attachMenuListeners() {
  document.querySelectorAll('.item-menu-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      showItemMenu(btn);
    });
  });
}

function showItemMenu(btn) {
  // Close any existing menu
  const existingMenu = document.querySelector('.item-context-menu');
  if (existingMenu) existingMenu.remove();

  const type = btn.dataset.type;
  const chatId = btn.dataset.chatId;
  const convId = btn.dataset.convId;
  const title = btn.dataset.title || btn.dataset.name || 'Untitled';
  const isShared = btn.dataset.shared === 'true';
  const isPublished = btn.dataset.published === 'true';

  const menu = document.createElement('div');
  menu.className = 'item-context-menu';

  let menuItems = '';

  // Rename option (not for shared or published-only items)
  if (!isShared && !isPublished) {
    menuItems += `<button class="context-menu-item" data-action="rename">
      <span>✏️</span> Rename
    </button>`;
  }

  // Share option (for both chats and conversations, not already shared)
  if (!isShared && !isPublished) {
    menuItems += `<button class="context-menu-item" data-action="share">
      <span>🔗</span> Share
    </button>`;
  }

  // Publish / Cancel Publishing (admin-only)
  if (isCurrentUserAdmin) {
    if (isPublished) {
      menuItems += `<button class="context-menu-item" data-action="unpublish">
        <span>📤</span> Cancel Publishing
      </button>`;
    } else {
      menuItems += `<button class="context-menu-item" data-action="publish">
        <span>📢</span> Publish
      </button>`;
    }
  }

  // Delete option
  menuItems += `<button class="context-menu-item delete" data-action="delete">
    <span>🗑️</span> Delete
  </button>`;

  menu.innerHTML = menuItems;

  // Position menu
  const rect = btn.getBoundingClientRect();
  menu.style.position = 'fixed';
  menu.style.top = rect.bottom + 'px';
  menu.style.left = (rect.left - 100) + 'px';

  // Ensure menu stays within viewport
  if (rect.left - 100 < 10) {
    menu.style.left = '10px';
  }

  document.body.appendChild(menu);

  // Handle menu actions
  menu.querySelectorAll('.context-menu-item').forEach(item => {
    item.addEventListener('click', async (e) => {
      e.stopPropagation();
      const action = item.dataset.action;
      menu.remove();

      if (action === 'rename') {
        await handleRename(type, chatId, convId, title);
      } else if (action === 'share') {
        await handleShare(type, chatId, convId, title);
      } else if (action === 'publish') {
        await handlePublish(type, chatId, convId, title);
      } else if (action === 'unpublish') {
        await handleUnpublish(type, chatId, convId, title);
      } else if (action === 'delete') {
        await handleDelete(type, chatId, convId, title);
      }
    });
  });

  // Close menu on outside click
  setTimeout(() => {
    document.addEventListener('click', function closeMenu() {
      menu.remove();
      document.removeEventListener('click', closeMenu);
    });
  }, 10);
}

async function handleRename(type, chatId, convId, currentName) {
  return new Promise((resolve) => {
    const modal = document.getElementById('renameModal');
    const titleEl = document.getElementById('renameModalTitle');
    const labelEl = document.getElementById('renameLabel');
    const input = document.getElementById('renameInput');
    const btnConfirm = document.getElementById('btnConfirmRename');
    const btnCancel = document.getElementById('btnCancelRename');
    const btnClose = document.getElementById('closeRename');
    
    // Set up modal
    titleEl.textContent = `Rename ${type === 'conversation' ? 'Conversation' : 'Chat'}`;
    labelEl.textContent = `Enter new name for "${currentName}"`;
    input.value = currentName;
    
    modal.classList.remove('hidden');
    input.focus();
    input.select();
    
    const closeModal = () => {
      modal.classList.add('hidden');
      resolve();
    };
    
    const confirmRename = async () => {
      const newName = input.value.trim();
      if (!newName || newName === currentName) {
        closeModal();
        return;
      }
      
      try {
        modal.classList.add('hidden');
        showLoading('Renaming...');
        
        const endpoint = type === 'conversation' 
          ? '/auth/conversations/rename'
          : '/auth/active_chats/rename';
        
        const body = type === 'conversation'
          ? { conv_id: convId, title: newName }
          : { chat_id: chatId, name: newName };
        
        const res = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        
        const data = await res.json();
        
        if (res.ok && data.ok) {
          showToast(`${type === 'conversation' ? 'Conversation' : 'Chat'} renamed successfully`);
          // Reload the unified list
          await loadUnifiedChatList(true);
        } else {
          showToast(data.error || 'Failed to rename', true);
        }
      } catch (e) {
        console.error('Rename failed:', e);
        showToast('Failed to rename', true);
      } finally {
        hideLoading();
        resolve();
      }
    };
    
    btnConfirm.onclick = confirmRename;
    btnCancel.onclick = closeModal;
    btnClose.onclick = closeModal;
    
    input.onkeydown = (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        confirmRename();
      } else if (e.key === 'Escape') {
        closeModal();
      }
    };
  });
}

async function handleShare(type, chatId, convId, title) {
  // Use the existing share modal
  const modal = document.getElementById('shareModal');
  const modalTitle = document.getElementById('shareModalTitle');
  const shareEmails = document.getElementById('shareEmails');
  const shareComment = document.getElementById('shareComment');
  const btnSave = document.getElementById('btnSaveShare');
  const btnClose = document.getElementById('closeShare');
  const btnCancelShare = document.getElementById('btnCancelShare');

  // Update modal title based on type
  if (type === 'conversation') {
    modalTitle.textContent = 'Share Conversation';
  } else {
    modalTitle.textContent = 'Share Chat';
  }

  shareEmails.value = '';
  if (shareComment) shareComment.value = '';

  modal.classList.remove('hidden');
  shareEmails.focus();

  const closeModal = () => {
    modal.classList.add('hidden');
    // Clean up event handlers
    btnSave.onclick = null;
    btnCancelShare.onclick = null;
    btnClose.onclick = null;
  };

  const saveShare = async () => {
    const emails = shareEmails.value.trim();
    if (!emails) {
      showToast('Please enter at least one email address', true);
      return;
    }

    try {
      modal.classList.add('hidden');
      showLoading('Sharing...');

      const emailList = emails.split(',').map(e => e.trim()).filter(e => e);

      if (emailList.length === 0) {
        showToast('No valid emails provided', true);
        hideLoading();
        return;
      }

      // Get optional comment
      const comment = shareComment?.value?.trim() || '';

      let endpoint, body;
      if (type === 'conversation' && convId) {
        // Conversation share: clones this conversation + shares parent chat
        endpoint = `/auth/conversations/${convId}/share`;
        body = { allowed_emails: emailList };
        if (comment) body.comment = comment;
      } else {
        // Chat share: shares the chat only (no conversations cloned)
        endpoint = `/api/chat/${chatId}/share`;
        body = { emails: emailList };
        if (comment) body.comment = comment;
      }

      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });

      const data = await res.json();

      if (res.ok) {
        if (type === 'conversation') {
          showToast('Conversation shared successfully');
        } else {
          showToast('Chat shared successfully');
        }
        // Refresh the list to show updated sharing status
        await loadUnifiedChatList(true);
      } else {
        showToast(data.error || 'Failed to share', true);
      }
    } catch (e) {
      console.error('Share failed:', e);
      showToast('Failed to share', true);
    } finally {
      hideLoading();
    }
  };

  btnSave.onclick = saveShare;
  btnCancelShare.onclick = closeModal;
  btnClose.onclick = closeModal;
}

async function handlePublish(type, chatId, convId, title) {
  const itemType = type === 'conversation' ? 'conversation' : 'chat';
  try {
    showLoading('Publishing...');
    let endpoint;
    if (type === 'conversation' && convId) {
      endpoint = `/auth/conversations/${convId}/publish`;
    } else {
      endpoint = `/api/chat/${chatId}/publish`;
    }
    const res = await fetch(endpoint, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      showToast(`${itemType.charAt(0).toUpperCase() + itemType.slice(1)} published successfully`);
      await loadUnifiedChatList(true);
    } else {
      showToast(data.error || 'Failed to publish', true);
    }
  } catch (e) {
    console.error('Publish failed:', e);
    showToast('Failed to publish', true);
  } finally {
    hideLoading();
  }
}

async function handleUnpublish(type, chatId, convId, title) {
  const itemType = type === 'conversation' ? 'conversation' : 'chat';
  try {
    showLoading('Unpublishing...');
    let endpoint;
    if (type === 'conversation' && convId) {
      endpoint = `/auth/conversations/${convId}/unpublish`;
    } else {
      endpoint = `/api/chat/${chatId}/unpublish`;
    }
    const res = await fetch(endpoint, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      showToast(`${itemType.charAt(0).toUpperCase() + itemType.slice(1)} unpublished`);
      await loadUnifiedChatList(true);
    } else {
      showToast(data.error || 'Failed to unpublish', true);
    }
  } catch (e) {
    console.error('Unpublish failed:', e);
    showToast('Failed to unpublish', true);
  } finally {
    hideLoading();
  }
}

async function handleDelete(type, chatId, convId, title) {
  return new Promise((resolve) => {
    const modal = document.getElementById('deleteModal');
    const message = document.getElementById('deleteMessage');
    const btnConfirm = document.getElementById('btnConfirmDelete');
    const btnCancel = document.getElementById('btnCancelDelete');
    const btnClose = document.getElementById('closeDelete');

    // Build delete message based on type
    if (type === 'chat') {
      // Count conversations under this chat
      const conversations = listCache.get('conversations') || [];
      const chatConversations = conversations.filter(c => c.chat_id === chatId);
      const convCount = chatConversations.length;

      if (convCount > 0) {
        message.innerHTML = `Delete chat "<strong>${escapeHtml(title)}</strong>" and all its conversations?<br><br>
          <span style="color: #dc2626;">This will permanently delete ${convCount} conversation${convCount > 1 ? 's' : ''} under this chat.</span>`;
      } else {
        message.innerHTML = `Delete chat "<strong>${escapeHtml(title)}</strong>"?`;
      }
    } else {
      message.innerHTML = `Delete conversation "<strong>${escapeHtml(title)}</strong>"?`;
    }

    modal.classList.remove('hidden');

    const closeModal = () => {
      modal.classList.add('hidden');
      // Clean up event handlers
      btnConfirm.onclick = null;
      btnCancel.onclick = null;
      btnClose.onclick = null;
      resolve();
    };

    const confirmDelete = async () => {
      modal.classList.add('hidden');

      // INSTANT UI UPDATE - remove from UI immediately, don't wait for backend
      showToast(`${type === 'conversation' ? 'Conversation' : 'Chat'} deleted`);

      if (type === 'conversation') {
        // Remove conversation from UI instantly
        const convItems = document.querySelectorAll(`.conversation-item[data-conv-id="${convId}"]`);
        convItems.forEach(item => item.remove());
        if (currentConvId === convId) {
          currentConvId = null;
          chatState.classList.add('hidden');
          welcomeState.classList.remove('hidden');
          showTopBarActions(false);
          window.history.pushState({}, '', '/lab');
        }
      } else {
        // Remove chat from UI instantly
        const chatWrapper = document.querySelector(`.chat-item-wrapper[data-chat-id="${chatId}"]`);
        if (chatWrapper) chatWrapper.remove();
        if (currentChatId === chatId) {
          currentChatId = null;
          currentConvId = null;
          chatState.classList.add('hidden');
          welcomeState.classList.remove('hidden');
          showTopBarActions(false);
          window.history.pushState({}, '', '/lab');
        }
      }

      // Invalidate cache so next load fetches fresh data
      listCache.conversations.timestamp = 0;
      listCache.activeChats.timestamp = 0;

      // Send delete to backend and verify it succeeds
      const endpoint = type === 'conversation'
        ? '/auth/conversations/delete'
        : `/api/chat/${chatId}/deactivate`;

      const body = type === 'conversation'
        ? { conv_id: convId }
        : {};

      try {
        const resp = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        if (!resp.ok) {
          console.error('Delete request failed:', resp.status);
          showToast('Delete may not have saved — please refresh', 'error');
        }
      } catch (e) {
        console.error('Delete request failed:', e);
        showToast('Delete may not have saved — please refresh', 'error');
      }

      resolve();
    };

    btnConfirm.onclick = confirmDelete;
    btnCancel.onclick = closeModal;
    btnClose.onclick = closeModal;
  });
}

// Make removeFile global
window.removeFile = removeFile;
