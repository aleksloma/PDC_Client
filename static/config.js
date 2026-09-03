// static/config.js — Form & JSON field descriptions + robust "Generate ChatData"
/*
  Purpose: Client logic for the protected Config page.
  Responsibilities:
  - Check auth state and redirect to '/' if not logged in
  - Upload files and show server response
  - Render chat history for the logged-in user
  - Field descriptions editor (form and raw JSON) with save
  - Generate ChatData and link to chat page
*/

// Loading overlay helpers
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
  if (overlay) {
    overlay.classList.add('hidden');
  }
}

// Store user's max file limit (updated by displayUploadLimitWarning)
let userMaxFiles = 1;

// Store selected files (allows incremental adding)
let selectedFiles = [];

// Add files to the selection (incremental)
function addFilesToSelection(newFiles) {
  const out = document.getElementById('uploadOut');
  
  // Filter out duplicates
  const uniqueNewFiles = Array.from(newFiles).filter(newFile => 
    !selectedFiles.some(existing => existing.name === newFile.name && existing.size === newFile.size)
  );
  
  if (uniqueNewFiles.length === 0) {
    out.style.color = '#f59e0b';
    out.textContent = 'File(s) already added.';
    return;
  }
  
  // Check total count doesn't exceed limit
  const totalCount = selectedFiles.length + uniqueNewFiles.length;
  if (totalCount > userMaxFiles) {
    out.style.color = '#ef4444';
    out.textContent = `Cannot add ${uniqueNewFiles.length} file(s). Total would be ${totalCount}, but your plan allows only ${userMaxFiles} file${userMaxFiles > 1 ? 's' : ''}. Remove some files or upgrade your subscription.`;
    return;
  }
  
  // Add new files
  selectedFiles.push(...uniqueNewFiles);
  
  // Clear error messages
  out.textContent = '';
  out.style.color = '';
  
  // Refresh UI
  renderFileDescriptions();
}

// Remove a file from selection
function removeFile(index) {
  selectedFiles.splice(index, 1);
  renderFileDescriptions();
}

// Render file description inputs
function renderFileDescriptions() {
  const container = document.getElementById('fileDescriptionsContainer');
  container.innerHTML = '';
  
  if (selectedFiles.length === 0) return;
  
  selectedFiles.forEach((file, index) => {
    const isGoogleSheet = file._isGoogleSheet;
    
    const fileCard = document.createElement('div');
    fileCard.style.marginBottom = '12px';
    fileCard.style.padding = '12px';
    fileCard.style.border = isGoogleSheet ? '1px solid #86efac' : '1px solid #e5e7eb';
    fileCard.style.borderRadius = '6px';
    fileCard.style.backgroundColor = isGoogleSheet ? '#f0fdf4' : '#f9fafb';
    fileCard.style.position = 'relative';
    
    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.textContent = '×';
    removeBtn.style.position = 'absolute';
    removeBtn.style.right = '8px';
    removeBtn.style.top = '8px';
    removeBtn.style.background = '#ef4444';
    removeBtn.style.color = 'white';
    removeBtn.style.border = 'none';
    removeBtn.style.borderRadius = '4px';
    removeBtn.style.width = '24px';
    removeBtn.style.height = '24px';
    removeBtn.style.cursor = 'pointer';
    removeBtn.style.fontSize = '18px';
    removeBtn.style.lineHeight = '1';
    removeBtn.title = 'Remove this file';
    removeBtn.onclick = () => removeFile(index);
    
    const label = document.createElement('label');
    label.style.display = 'block';
    label.style.fontWeight = '600';
    label.style.marginBottom = '6px';
    label.style.color = '#374151';
    label.style.paddingRight = '30px';
    
    // Add Google Sheets badge if applicable
    if (isGoogleSheet) {
      label.innerHTML = `${index + 1}. ${file.name} <span style="background:#10b981;color:white;padding:2px 6px;border-radius:4px;font-size:11px;margin-left:6px;">📎 Google Sheet</span>`;
    } else {
      label.textContent = `${index + 1}. ${file.name}`;
    }
    
    const textarea = document.createElement('textarea');
    textarea.name = `file_description_${index}`;
    textarea.dataset.fileName = file.name;
    textarea.rows = 3;
    textarea.placeholder = `Describe what ${file.name} contains (units, meanings, context, etc.) - REQUIRED (min. 10 characters)`;
    textarea.style.width = '100%';
    textarea.required = true;
    textarea.minLength = 10;
    textarea.className = 'file-description-input';
    
    // Preserve existing description if re-rendering
    const existingInput = document.querySelector(`textarea[data-file-name="${file.name}"]`);
    if (existingInput) {
      textarea.value = existingInput.value;
    }
    
    fileCard.appendChild(removeBtn);
    fileCard.appendChild(label);
    fileCard.appendChild(textarea);
    container.appendChild(fileCard);
  });
}

// Handle file input change
async function handleFileSelection(e) {
  const files = e.target.files;
  if (files.length === 0) return;
  
  addFilesToSelection(files);
  
  // Reset input to allow re-selecting same file
  e.target.value = '';
}

// Submit the upload form: POST /upload with selected files and descriptions
async function upload(e){
  e.preventDefault();
  
  if (selectedFiles.length === 0) {
    const out = document.getElementById('uploadOut');
    out.textContent = 'Please select at least one file.';
    return;
  }
  
  // Validate that all descriptions are filled
  const descriptionInputs = document.querySelectorAll('.file-description-input');
  const descriptions = {};
  let hasEmptyDescription = false;
  
  descriptionInputs.forEach(input => {
    const fileName = input.dataset.fileName;
    const description = input.value.trim();
    if (!description || description.length < 10) {
      hasEmptyDescription = true;
      input.style.borderColor = '#ef4444';
      input.style.borderWidth = '2px';
    } else {
      input.style.borderColor = '';
      input.style.borderWidth = '';
      descriptions[fileName] = description;
    }
  });
  
  if (hasEmptyDescription) {
    const out = document.getElementById('uploadOut');
    out.textContent = 'Please provide descriptions for all files (minimum 10 characters each).';
    out.style.color = '#ef4444';
    return;
  }
  
  // Separate Google Sheet files (already uploaded) from regular files
  const regularFiles = selectedFiles.filter(f => !f._isGoogleSheet);
  const googleSheetFiles = selectedFiles.filter(f => f._isGoogleSheet);
  
  showLoading('Uploading files...');
  const out = document.getElementById('uploadOut');
  out.style.color = '';
  
  try {
    let allSuccess = true;
    const uploadedNames = [];
    
    // Upload regular files if any
    if (regularFiles.length > 0) {
      // Every file, regardless of size, goes through the multipart /upload path —
      // the on-prem build has no signed-URL (direct-to-GCS) branch.
      const formData = new FormData();
      regularFiles.forEach(file => {
        formData.append('files', file);
      });
      // Only include descriptions for regular files
      const regularDescriptions = {};
      regularFiles.forEach(f => {
        if (descriptions[f.name]) regularDescriptions[f.name] = descriptions[f.name];
      });
      formData.append('file_descriptions', JSON.stringify(regularDescriptions));

      const res = await fetch('/upload', { method: 'POST', body: formData });
      const js = await safeJson(res);
      // js.ok must be strictly true: a partial parse failure now answers
      // 200 {ok:false, error, saved:[...]} — `saved` alone is not success
      // and would list the FAILED files as uploaded (QA 2.1).
      if (res.ok && js && js.ok === true) {
        uploadedNames.push(...(js.saved || []));
      } else {
        allSuccess = false;
        out.textContent = (js && (js.error || js.message || js.detail)) || 'Upload failed.';
      }
    }
    
    // Update descriptions for Google Sheet files (already uploaded)
    for (const gsFile of googleSheetFiles) {
      const desc = descriptions[gsFile.name];
      if (desc) {
        // Update the file description via API
        const res = await fetch('/update_file_description', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: gsFile.name, description: desc })
        });
        const js = await safeJson(res);
        if (res.ok && js && js.ok) {
          uploadedNames.push(gsFile.name);
        } else {
          allSuccess = false;
        }
      }
    }
    
    if (allSuccess && uploadedNames.length > 0) {
      if (uploadedNames.length === 1) out.textContent = `${uploadedNames[0]} was uploaded successfully.`;
      else out.textContent = `${uploadedNames.join(', ')} were uploaded successfully.`;
      // Clear selected files and descriptions
      selectedFiles = [];
      pendingGoogleSheetUrl = null;
      pendingGoogleSheetFilename = null;
      document.getElementById('fileDescriptionsContainer').innerHTML = '';
      hideLoading();
      // Automatically trigger AI autofill and open schema editor
      triggerAutoFillAndOpenSchema();
      return; // skip finally hideLoading — already called
    }
  } finally {
    hideLoading();
  }
}

// Auto-fill field descriptions with AI after upload, then open schema editor
async function triggerAutoFillAndOpenSchema() {
  const statusEl = document.getElementById('autofillStatus');
  if (statusEl) {
    statusEl.style.display = 'block';
    statusEl.style.color = '#3b82f6';
    statusEl.textContent = 'AI is analyzing your columns...';
  }
  showLoading('AI is analyzing your columns...');
  try {
    const res = await fetch('/schema_autofill', { method: 'POST' });
    const js = await safeJson(res);
    if (res.ok && js && js.ok) {
      const filled = js.filled || 0;
      if (statusEl) {
        statusEl.style.color = '#16a34a';
        statusEl.textContent = `AI filled ${filled} field description${filled !== 1 ? 's' : ''}. Click "Edit field descriptions" to review.`;
      }
      showToast(`AI filled ${filled} field description${filled !== 1 ? 's' : ''}`);
    } else {
      if (statusEl) {
        statusEl.style.color = '#f59e0b';
        statusEl.textContent = 'AI descriptions unavailable — please fill manually.';
      }
      showToast('AI descriptions unavailable, please fill manually', true);
    }
  } catch (e) {
    if (statusEl) {
      statusEl.style.color = '#f59e0b';
      statusEl.textContent = 'AI descriptions unavailable — please fill manually.';
    }
    showToast('AI descriptions unavailable, please fill manually', true);
  } finally {
    hideLoading();
  }
  // Open the schema editor with pre-filled descriptions
  try {
    const [meta, details, commonFields] = await Promise.all([fetchSchema(), fetchSchemaDetails(), fetchCommonFields()]);
    currentMeta = meta || { files: [] };
    currentDetails = details || { files: [] };
    currentCommonFields = commonFields || { relationships: [], user_defined: [] };
    schemaEditorMode = 'form';
    onlyNeededFilter = false;
    renderFormSchemaEditor(currentMeta, currentDetails, { onlyNeeded: onlyNeededFilter });
    document.getElementById('schemaModal').classList.remove('hidden');
  } catch (e) {
    console.error('Failed to open schema editor:', e);
  }
}
// ----- Share/Public controls helpers (used after generating link and in Active modal) -----
async function fetchShareInfo(chatId){
  try {
    const res = await fetch(`/api/chat/${chatId}/share`);
    return await safeJson(res);
  } catch { return null; }
}

function renderShareControlsInline(container, chatId){
  container.innerHTML = '';
  const shareBtn = document.createElement('button');
  shareBtn.className = 'ghost small';
  shareBtn.textContent = 'Share';
  shareBtn.style.padding = '8px 14px';
  shareBtn.style.fontSize = '14px';
  const publicBtn = document.createElement('button');
  publicBtn.className = 'ghost small';
  publicBtn.textContent = 'Make public';
  publicBtn.style.padding = '8px 14px';
  publicBtn.style.fontSize = '14px';

  const panel = document.createElement('span');
  panel.style.display = 'none';
  panel.style.marginLeft = '6px';
  panel.style.gap = '6px';
  panel.style.alignItems = 'center';
  panel.style.whiteSpace = 'nowrap';
  const input = document.createElement('input');
  input.type = 'text'; input.placeholder = 'Enter emails (comma or space separated)';
  input.style.minWidth = '280px';
  const sendBtn = document.createElement('button'); sendBtn.className='ghost small'; sendBtn.textContent='Share';
  const cancelBtn = document.createElement('button'); cancelBtn.className='ghost small'; cancelBtn.textContent='Cancel';
  panel.appendChild(input); panel.appendChild(sendBtn); panel.appendChild(cancelBtn);

  container.appendChild(shareBtn);
  container.appendChild(publicBtn);
  container.appendChild(panel);

  shareBtn.addEventListener('click', ()=>{
    panel.style.display = (panel.style.display === 'none') ? 'inline-flex' : 'none';
    if (panel.style.display !== 'none') try{ input.focus(); }catch{}
  });
  cancelBtn.addEventListener('click', ()=>{ panel.style.display='none'; input.value=''; });

  sendBtn.addEventListener('click', async ()=>{
    const raw = (input.value || '').trim();
    if (!raw){ showToast('Enter at least one email', true); return; }
    const emails = raw.split(/[\s,;]+/).map(s=>s.trim()).filter(Boolean);
    const res = await fetch(`/api/chat/${chatId}/share`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ emails }) });
    const js = await safeJson(res);
    if (res.ok && js && js.ok){
      showToast(`Shared with ${emails.length} email(s)`);
      panel.style.display='none'; input.value='';
    } else {
      showToast((js && js.error) || 'Failed to share', true);
    }
  });

  (async ()=>{
    const info = await fetchShareInfo(chatId);
    if (!info || info.error) return;
    publicBtn.textContent = info.public ? 'Make private' : 'Make public';
    if (!info.is_owner) {
      publicBtn.style.display = 'none';
      shareBtn.style.display = 'none';
    }
  })();

  publicBtn.addEventListener('click', async ()=>{
    const cur = await fetchShareInfo(chatId);
    if (!cur || cur.error){ showToast('Cannot fetch sharing state', true); return; }
    const makePublic = !cur.public;
    const res = await fetch(`/api/chat/${chatId}/public`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ public: makePublic }) });
    const js = await safeJson(res);
    if (res.ok && js && js.ok){
      publicBtn.textContent = makePublic ? 'Make private' : 'Make public';
      showToast(makePublic ? 'Chat is now public' : 'Chat is now private');
    } else {
      showToast((js && js.error) || 'Failed to change public status', true);
    }
  });
}
document.getElementById('uploadForm').addEventListener('submit', upload);
document.getElementById('fileInput').addEventListener('change', handleFileSelection);

// Drag and drop functionality - works on entire upload section
const dropZoneSmall = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const uploadSection = document.getElementById('uploadForm').closest('.card');

// Click small drop zone to open file picker
dropZoneSmall.addEventListener('click', () => {
  fileInput.click();
});

// Prevent default drag behaviors on entire upload section
['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
  uploadSection.addEventListener(eventName, (e) => {
    e.preventDefault();
    e.stopPropagation();
  });
});

// Highlight when dragging over the upload section
let dragCounter = 0;
uploadSection.addEventListener('dragenter', () => {
  dragCounter++;
  uploadSection.style.outline = '3px dashed #3b82f6';
  uploadSection.style.outlineOffset = '-3px';
  uploadSection.style.backgroundColor = '#f0f7ff';
  dropZoneSmall.style.borderColor = '#3b82f6';
  dropZoneSmall.style.backgroundColor = '#eff6ff';
});

uploadSection.addEventListener('dragleave', () => {
  dragCounter--;
  if (dragCounter === 0) {
    uploadSection.style.outline = '';
    uploadSection.style.outlineOffset = '';
    uploadSection.style.backgroundColor = '';
    dropZoneSmall.style.borderColor = '#d1d5db';
    dropZoneSmall.style.backgroundColor = '#f9fafb';
  }
});

// Handle dropped files anywhere in the upload section
uploadSection.addEventListener('drop', (e) => {
  dragCounter = 0;
  uploadSection.style.outline = '';
  uploadSection.style.outlineOffset = '';
  uploadSection.style.backgroundColor = '';
  dropZoneSmall.style.borderColor = '#d1d5db';
  dropZoneSmall.style.backgroundColor = '#f9fafb';
  
  const files = e.dataTransfer.files;
  if (files.length > 0) {
    addFilesToSelection(files);
  }
});

// Display file upload limit warning based on user's subscription plan
async function displayUploadLimitWarning() {
  try {
    const response = await fetch('/auth/profile');
    const profile = await safeJson(response);
    
    if (!profile || profile.error) {
      return; // Don't show warning if we can't get profile
    }
    
    const plan = profile.subscription_plan || 'Basic';
    const planLimits = {
      'Basic': 1,
      'Standard': 5,
      'Pro': 10,
      'Enterprise': 999
    };
    
    const maxFiles = planLimits[plan] || 1;
    
    // Update the global variable so handleFileSelection can validate
    userMaxFiles = maxFiles;
    
    // Update file input to disable multiple selection if only 1 file allowed
    const fileInput = document.getElementById('fileInput');
    if (fileInput) {
      if (maxFiles === 1) {
        fileInput.removeAttribute('multiple');
      } else {
        fileInput.setAttribute('multiple', 'multiple');
      }
    }
    
    const warningDiv = document.getElementById('uploadLimitWarning');
    
    if (warningDiv) {
      warningDiv.innerHTML = `* Your file upload limit as ${plan} subscription user is <strong>${maxFiles}</strong> file${maxFiles > 1 ? 's' : ''}. You can change your subscription plan from your profile.`;
      warningDiv.style.display = 'block';
    }
  } catch (error) {
    console.error('Failed to fetch upload limit:', error);
  }
}

// Call on page load
displayUploadLimitWarning();

// Google Sheet sharing - show/hide URL input box
document.getElementById('shareGoogleSheetBtn')?.addEventListener('click', () => {
  const inputBox = document.getElementById('googleSheetInputBox');
  if (inputBox) {
    inputBox.style.display = 'block';
    document.getElementById('googleUrlInput')?.focus();
  }
});

document.getElementById('cancelGoogleSheetBtn')?.addEventListener('click', () => {
  const inputBox = document.getElementById('googleSheetInputBox');
  if (inputBox) {
    inputBox.style.display = 'none';
    document.getElementById('googleUrlInput').value = '';
    document.getElementById('urlImportStatus').textContent = '';
  }
});

// Store pending Google Sheet URL for the file description flow
let pendingGoogleSheetUrl = null;
let pendingGoogleSheetFilename = null;

// Google Drive/Sheets URL import handler - Step 1: Validate URL and show description input
document.getElementById('importFromUrlBtn')?.addEventListener('click', async () => {
  const urlInput = document.getElementById('googleUrlInput');
  const statusEl = document.getElementById('urlImportStatus');
  const btn = document.getElementById('importFromUrlBtn');
  const inputBox = document.getElementById('googleSheetInputBox');
  
  const url = (urlInput?.value || '').trim();
  
  if (!url) {
    showToast('Please paste a Google Drive or Sheets URL', true);
    return;
  }
  
  // Validate URL format
  const isGoogleUrl = url.includes('docs.google.com/spreadsheets') || url.includes('drive.google.com');
  if (!isGoogleUrl) {
    showToast('Please use a Google Sheets or Google Drive link', true);
    return;
  }
  
  // Disable button and show loading
  btn.disabled = true;
  btn.textContent = 'Validating...';
  statusEl.textContent = 'Checking URL...';
  statusEl.style.color = '#6b7280';
  
  try {
    // Validate URL by fetching - use a placeholder description, we'll update it later
    const res = await fetch('/upload_from_url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, description: '__PENDING__' })
    });
    
    const data = await safeJson(res);
    
    if (res.ok && data && data.ok) {
      const filename = data.filename || 'google_sheet.xlsx';
      
      // Hide the URL input box
      if (inputBox) inputBox.style.display = 'none';
      urlInput.value = '';
      statusEl.textContent = '';
      
      // Store pending info
      pendingGoogleSheetUrl = url;
      pendingGoogleSheetFilename = filename;
      
      // Create a fake file object to add to selectedFiles for description input
      const fakeFile = new File([''], filename, { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
      fakeFile._isGoogleSheet = true;
      fakeFile._googleUrl = url;
      
      // Add to selection to show description input
      selectedFiles.push(fakeFile);
      renderFileDescriptions();
      
      showToast(`Google Sheet loaded: ${filename}. Please add a description.`);
    } else {
      const errMsg = (data && data.error) || 'Import failed';
      statusEl.textContent = `✗ ${errMsg}`;
      statusEl.style.color = '#dc2626';
      showToast(errMsg, true);
    }
  } catch (err) {
    statusEl.textContent = '✗ Network error';
    statusEl.style.color = '#dc2626';
    showToast('Network error during import', true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Share';
  }
});

// Verify authentication and load history; otherwise redirect to landing.
async function refreshAuth(){
  const st = await fetch('/auth/me').then(safeJson).catch(()=>null);
  const logged = !!(st && st.authenticated);
  if (!logged) { location.href = '/'; return; }
  await loadHistory();
}

// Fetch and render the current user's chat history list with titles.
async function loadHistory(){
  const listEl = document.getElementById('historyList');
  if (!listEl) return; // history card removed from page
  listEl.textContent = 'Loading...';
  const res = await fetch('/auth/chats');
  const js = await safeJson(res);
  if (!res.ok || !js || !Array.isArray(js.chats)){
    listEl.textContent = 'No history.';
    return;
  }
  const chats = js.chats.slice().reverse();
  listEl.innerHTML = '';
  if (!chats.length){ listEl.textContent = 'No chats yet.'; return; }
  chats.forEach(item => {
    const a = document.createElement('a');
    a.href = '/chat/' + item.chat_id;
    a.target = '_blank'; a.rel = 'noopener noreferrer';
    const title = (item && typeof item.title === 'string' && item.title.trim()) ? item.title.trim() : item.chat_id;
    a.textContent = title + (item.ts ? (' — ' + item.ts) : '');
    const div = document.createElement('div');
    div.appendChild(a);
    listEl.appendChild(div);
  });
}

document.getElementById('btnLogoutTop').addEventListener('click', async ()=>{
  await fetch('/auth/logout', { method:'POST' });
  location.href = '/';
});

// ----- Profile modal -----
const profileModal = document.getElementById('profileModal');
let currentSubscriptionPlan = 'Basic';

var currentSubscriptionSource = 'default';
var paddleScheduledChange = null;
var paddleScheduledPlan = null;

function _refreshPlanUI() {
  ['Basic', 'Standard', 'Pro'].forEach(function(plan) {
    var btn = document.getElementById('plan' + plan);
    var card = document.getElementById('plan' + plan + 'Card');
    if (plan === currentSubscriptionPlan) {
      if (btn) {
        btn.classList.remove('ghost'); btn.textContent = 'Current Plan';
        btn.disabled = (paddleScheduledChange !== 'cancel' && paddleScheduledChange !== 'downgrade');
      }
      if (card) card.classList.add('active');
    } else if (paddleScheduledChange === 'downgrade' && plan === paddleScheduledPlan) {
      if (btn) { btn.classList.add('ghost'); btn.textContent = 'Scheduled'; btn.disabled = true; }
      if (card) card.classList.remove('active');
    } else {
      if (btn) {
        btn.classList.add('ghost');
        btn.disabled = false;
        if (plan === 'Basic') btn.textContent = 'Downgrade to Basic';
        else if (currentSubscriptionPlan === 'Basic') btn.textContent = 'Get ' + plan;
        else {
          var rank = { Basic: 0, Standard: 1, Pro: 2, Enterprise: 3 };
          btn.textContent = (rank[plan] > rank[currentSubscriptionPlan]) ? 'Upgrade to ' + plan : 'Switch to ' + plan;
        }
      }
      if (card) card.classList.remove('active');
    }
  });
  // Enterprise card — highlight if active
  var entCard = document.getElementById('planEnterpriseCard');
  if (entCard) {
    if (currentSubscriptionPlan === 'Enterprise') {
      entCard.classList.add('active');
    } else {
      entCard.classList.remove('active');
    }
  }
}

document.getElementById('btnProfile').addEventListener('click', async ()=>{
  const [p, plansData] = await Promise.all([
    fetch('/auth/profile').then(safeJson).catch(()=>null),
    fetch('/auth/subscription/plans').then(safeJson).catch(()=>null)
  ]);

  if (!p || p.error){ showToast((p && p.error) || 'Failed to load profile', true); return; }
  document.getElementById('profUsername').value = p.username || '';
  document.getElementById('profFullName').value = p.full_name || '';
  document.getElementById('profEmail').value = p.email || '';

  currentSubscriptionPlan = p.subscription_plan || 'Basic';
  currentSubscriptionSource = p.subscription_source || 'default';
  paddleScheduledChange = p.paddle_scheduled_change || null;
  paddleScheduledPlan = p.paddle_scheduled_plan || null;
  document.getElementById('currentPlanName').textContent = currentSubscriptionPlan;

  // Enterprise: messages are unlimited. Never display a numeric cap.
  const mt = document.getElementById('messagesToday');
  if (mt) mt.textContent = 'unlimited';

  _refreshPlanUI();
  profileModal.classList.remove('hidden');
});
document.getElementById('closeProfile').addEventListener('click', ()=> profileModal.classList.add('hidden'));

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

// ----- Subscription modal helpers (config page) -----
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
function _closeSubModal() { _subModal().classList.add('hidden'); }
function _fmtCurrency(amount, currency) {
  return (currency === 'USD' ? '$' : currency + ' ') + amount;
}

async function _updatePaymentMethod() {
  _subBody().innerHTML = '<div class="sub-modal-spinner"><p>Loading payment details...</p></div>';
  _subActions().innerHTML = '';
  try {
    var res = await fetch('/api/paddle/subscription/update-payment', { method: 'POST' });
    var data = await safeJson(res);
    if (data && data.ok && data.transaction_id && window.Paddle) {
      _closeSubModal();
      Paddle.Checkout.open({ transactionId: data.transaction_id });
    } else {
      _openSubModal('Error', '<div class="sub-modal-highlight error">' + ((data && data.error) || 'Failed to load payment update') + '</div>',
        '<button class="ghost" onclick="_closeSubModal()">Close</button>');
    }
  } catch (e) {
    _openSubModal('Error', '<div class="sub-modal-highlight error">Failed to load payment details</div>',
      '<button class="ghost" onclick="_closeSubModal()">Close</button>');
  }
}

function _showPreviewModal(data) {
  var ct = data.change_type;
  if (ct === 'new_subscription') { _closeSubModal(); _openPaddleCheckout(data.new_plan); return; }
  if (ct === 'admin_direct') {
    _openSubModal((data.new_plan === 'Basic' ? 'Downgrade to Basic' : 'Change to ' + data.new_plan),
      '<p>' + data.message + '</p>',
      '<button class="primary" onclick="_confirmAdminChange(\'' + data.new_plan + '\')">Confirm</button>' +
      '<button class="ghost" onclick="_closeSubModal()">Cancel</button>');
    return;
  }
  if (ct === 'upgrade') {
    var body = '<div class="sub-modal-row"><span class="label">Current plan</span><span class="value">' + data.current_plan + ' (' + data.current_price + ')</span></div>' +
      '<div class="sub-modal-row"><span class="label">New plan</span><span class="value">' + data.new_plan + ' (' + data.new_price + ')</span></div><hr class="sub-modal-divider">';
    if (data.immediate_charge) {
      body += '<div class="sub-modal-row"><span class="label">Prorated charge today</span><span class="value">' + _fmtCurrency(data.immediate_charge.amount, data.immediate_charge.currency) + '</span></div>';
    }
    if (data.next_billing) {
      body += '<div class="sub-modal-row"><span class="label">Next billing' + (data.next_billing.date_formatted ? ' (' + data.next_billing.date_formatted + ')' : '') + '</span><span class="value">' + _fmtCurrency(data.next_billing.amount, data.next_billing.currency) + '/mo</span></div>';
    }
    if (data.immediate_charge) {
      body += '<div class="sub-modal-highlight">Your card on file will be charged ' + _fmtCurrency(data.immediate_charge.amount, data.immediate_charge.currency) + ' immediately.</div>';
      body += '<div class="sub-modal-link">Need to change payment method? <a onclick="_updatePaymentMethod()">Update payment details</a></div>';
    }
    _openSubModal('Upgrade to ' + data.new_plan, body,
      '<button class="primary" onclick="_confirmPlanChange(\'upgrade\', \'' + data.new_plan + '\')">Confirm Upgrade</button>' +
      '<button class="ghost" onclick="_closeSubModal()">Cancel</button>');
    return;
  }
  if (ct === 'downgrade') {
    var body = '<div class="sub-modal-row"><span class="label">Current plan</span><span class="value">' + data.current_plan + ' (' + data.current_price + ')</span></div>' +
      '<div class="sub-modal-row"><span class="label">New plan</span><span class="value">' + data.new_plan + ' (' + data.new_price + ')</span></div><hr class="sub-modal-divider">' +
      '<div class="sub-modal-row"><span class="label">No charge today</span><span class="value"></span></div>';
    if (data.current_period_ends_formatted) body += '<div class="sub-modal-row"><span class="label">Change takes effect</span><span class="value">' + data.current_period_ends_formatted + '</span></div>';
    if (data.next_billing) body += '<div class="sub-modal-row"><span class="label">Next billing</span><span class="value">' + _fmtCurrency(data.next_billing.amount, data.next_billing.currency) + '/mo</span></div>';
    body += '<div class="sub-modal-highlight warning">You\'ll keep ' + data.current_plan + ' features until ' + (data.current_period_ends_formatted || 'the end of your billing period') + '.</div>';
    _openSubModal('Downgrade to ' + data.new_plan, body,
      '<button class="primary" onclick="_confirmPlanChange(\'downgrade\', \'' + data.new_plan + '\')">Confirm Downgrade</button>' +
      '<button class="ghost" onclick="_closeSubModal()">Keep ' + data.current_plan + '</button>');
    return;
  }
  if (ct === 'cancel') {
    var body = '<div class="sub-modal-row"><span class="label">Current plan</span><span class="value">' + data.current_plan + ' (' + data.current_price + ')</span></div><hr class="sub-modal-divider">' +
      '<div class="sub-modal-highlight warning">' + data.message + '</div>' +
      '<p style="font-size:13px;color:#6b7280;margin-top:8px;">After that, you\'ll switch to the free Basic plan with:</p>' +
      '<ul class="sub-modal-features"><li>1 file per upload</li><li>10 messages/day</li><li>1 PDF report/month</li></ul>';
    _openSubModal('Downgrade to Basic', body,
      '<button class="primary" onclick="_confirmPlanChange(\'cancel\', \'Basic\')">Confirm Downgrade</button>' +
      '<button class="ghost" onclick="_closeSubModal()">Keep Plan</button>');
    return;
  }
}

async function _confirmPlanChange(changeType, plan) {
  _subBody().innerHTML = '<div class="sub-modal-spinner"><p>Processing...</p></div>';
  _subActions().innerHTML = '';
  try {
    var endpoint, body = {};
    if (changeType === 'cancel') endpoint = '/api/paddle/subscription/cancel';
    else if (changeType === 'undo_cancel') endpoint = '/api/paddle/subscription/reactivate';
    else { endpoint = '/api/paddle/subscription/update'; body = { plan: plan }; }
    var res = await fetch(endpoint, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    var data = await safeJson(res);
    if (data && data.ok) {
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
      if (data.immediate) { currentSubscriptionPlan = plan; _refreshPlanUI(); document.getElementById('currentPlanName').textContent = plan; }
      _openSubModal(title, '<div class="sub-modal-highlight success">' + msg + '</div>',
        '<button class="primary" onclick="_closeSubModal(); location.reload();">Done</button>');
    } else {
      _openSubModal('Error', '<div class="sub-modal-highlight error">' + ((data && data.error) || 'Failed to process plan change') + '</div>',
        '<button class="ghost" onclick="_closeSubModal()">Close</button>');
    }
  } catch (e) {
    _openSubModal('Error', '<div class="sub-modal-highlight error">Failed to process plan change.</div>',
      '<button class="ghost" onclick="_closeSubModal()">Close</button>');
  }
}

async function _confirmAdminChange(plan) {
  _subBody().innerHTML = '<div class="sub-modal-spinner"><p>Processing...</p></div>';
  _subActions().innerHTML = '';
  try {
    var res = await fetch('/auth/subscription', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ plan: plan }) });
    var data = await safeJson(res);
    if (data && data.error) {
      _openSubModal('Error', '<div class="sub-modal-highlight error">' + data.error + '</div>',
        '<button class="ghost" onclick="_closeSubModal()">Close</button>');
    } else {
      currentSubscriptionPlan = plan; currentSubscriptionSource = 'default';
      _refreshPlanUI(); document.getElementById('currentPlanName').textContent = plan;
      _openSubModal('Plan Updated', '<div class="sub-modal-highlight success">Switched to ' + plan + ' plan.</div>',
        '<button class="primary" onclick="_closeSubModal()">Done</button>');
    }
  } catch (e) {
    _openSubModal('Error', '<div class="sub-modal-highlight error">Failed to change plan</div>',
      '<button class="ghost" onclick="_closeSubModal()">Close</button>');
  }
}

function _openPaddleCheckout(plan) {
  var cfg = window._paddleConfig;
  if (!cfg || !cfg.prices || !cfg.prices[plan]) { showToast('Payment system loading, please try again', true); return; }
  var email = (document.getElementById('profEmail') || {}).value || '';
  var username = (document.getElementById('profUsername') || {}).value || '';
  try {
    Paddle.Checkout.open({
      items: [{ priceId: cfg.prices[plan], quantity: 1 }],
      customData: { username: username, email: email },
      settings: { successUrl: window.location.origin + '/lab?upgraded=true', displayMode: 'overlay', theme: 'light' },
      customer: email ? { email: email } : undefined
    });
  } catch (e) { console.error('Paddle.Checkout.open error:', e); showToast('Failed to open checkout', true); }
}

// ----- Subscription plan buttons -----
['Basic', 'Standard', 'Pro'].forEach(plan => {
  const btn = document.getElementById('plan' + plan);
  if (btn) {
    btn.addEventListener('click', async ()=>{
      if (plan === currentSubscriptionPlan) {
        if (paddleScheduledChange === 'cancel') {
          _openSubModal('Keep ' + currentSubscriptionPlan + ' Plan',
            '<p>Your ' + currentSubscriptionPlan + ' plan is scheduled to cancel.</p><p>Would you like to continue your subscription?</p>',
            '<button class="primary" onclick="_confirmPlanChange(\'undo_cancel\', \'' + currentSubscriptionPlan + '\')">Keep My Plan</button>' +
            '<button class="ghost" onclick="_closeSubModal()">Never Mind</button>');
        } else if (paddleScheduledChange === 'downgrade') {
          _openSubModal('Keep ' + currentSubscriptionPlan + ' Plan',
            '<p>Your plan is scheduled to switch to ' + (paddleScheduledPlan || 'a lower plan') + '.</p><p>Would you like to keep your ' + currentSubscriptionPlan + ' plan instead?</p>',
            '<button class="primary" onclick="_confirmPlanChange(\'undo_cancel\', \'' + currentSubscriptionPlan + '\')">Keep My Plan</button>' +
            '<button class="ghost" onclick="_closeSubModal()">Never Mind</button>');
        }
        return;
      }

      var rankMap = { Basic: 0, Standard: 1, Pro: 2, Enterprise: 3 };
      var actionLabel = (rankMap[plan] > rankMap[currentSubscriptionPlan]) ? 'Upgrading' : 'Switching';
      _openSubModal(actionLabel + ' to ' + plan + '...',
        '<div class="sub-modal-spinner"><p>Calculating your price...</p></div>', '');

      try {
        var res = await fetch('/api/paddle/subscription/preview', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ plan: plan })
        });
        var data = await safeJson(res);
        if (!data || !data.ok) {
          _openSubModal('Error',
            '<div class="sub-modal-highlight error">' + ((data && data.error) || 'Failed to load pricing') + '</div>',
            '<button class="ghost" onclick="_closeSubModal()">Close</button>');
          return;
        }
        _showPreviewModal(data);
      } catch (e) {
        _openSubModal('Error',
          '<div class="sub-modal-highlight error">Failed to load pricing details.</div>',
          '<button class="ghost" onclick="_closeSubModal()">Close</button>');
      }
    });
  }
});

// Clamp days input in real time
(function(){
  const daysInput = document.getElementById('chatDays');
  if (daysInput){
    const maxDays = parseInt(daysInput.getAttribute('max') || '10', 10);
    const minDays = parseInt(daysInput.getAttribute('min') || '1', 10);
    daysInput.addEventListener('input', ()=>{
      let v = parseInt(daysInput.value || '', 10);
      if (isNaN(v)) return;
      if (v > maxDays) daysInput.value = String(maxDays);
      else if (v < minDays) daysInput.value = String(minDays);
    });
  }
})();

document.getElementById('btnSaveProfile').addEventListener('click', async ()=>{
  // If password fields are filled, change password first
  const newPw = (document.getElementById('profNewPw').value || '').trim();
  if (newPw) {
    const pwPayload = {
      current_password: document.getElementById('profCurrentPw').value || null,
      new_password: newPw,
    };
    const pwRes = await fetch('/auth/password', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(pwPayload) });
    const pwJs = await safeJson(pwRes);
    if (!pwRes.ok || !pwJs || !pwJs.ok) {
      showToast((pwJs && pwJs.error) || 'Failed to change password', true);
      return;
    }
    document.getElementById('profCurrentPw').value = '';
    document.getElementById('profNewPw').value = '';
  }

  const payload = {
    new_username: document.getElementById('profUsername').value || null,
    full_name: document.getElementById('profFullName').value || null,
    email: document.getElementById('profEmail').value || null,
  };
  const res = await fetch('/auth/profile/update', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  const js = await safeJson(res);
  if (res.ok && js && js.ok){
    showToast(newPw ? 'Profile and password saved' : 'Profile saved');
    profileModal.classList.add('hidden');
    await loadHistory();
  } else {
    showToast((js && js.error) || 'Failed to save', true);
  }
});

// ----- History modal management -----
const historyModal = document.getElementById('historyModal');
const btnHistoryTop = document.getElementById('btnHistoryTop');
const btnCloseHistory = document.getElementById('closeHistory');
if (btnHistoryTop && historyModal) {
  btnHistoryTop.addEventListener('click', async ()=>{
    await renderHistoryManage();
    historyModal.classList.remove('hidden');
  });
}
if (btnCloseHistory && historyModal) {
  btnCloseHistory.addEventListener('click', ()=> historyModal.classList.add('hidden'));
}

async function renderHistoryManage(){
  const box = document.getElementById('historyManageList');
  box.innerHTML = 'Loading…';
  const res = await fetch('/auth/conversations');
  const js = await safeJson(res);
  if (!res.ok || !js || !Array.isArray(js.conversations)) { box.textContent = 'No conversations yet.'; return; }
  box.innerHTML = '';
  js.conversations.slice().reverse().forEach(item => {
    const row = document.createElement('div'); row.className = 'row';
    const title = (item && typeof item.title === 'string' && item.title.trim()) ? item.title.trim() : (item.conv_id || 'conversation');
    const src = (item && typeof item.chat_name === 'string' && item.chat_name.trim()) ? (' — from ' + item.chat_name.trim()) : '';
    const a = document.createElement('a'); a.href = '/chat/' + item.chat_id + '?conv_id=' + encodeURIComponent(item.conv_id); a.target = '_blank'; a.rel='noopener noreferrer'; a.textContent = title + src + (item.started_at ? (' — ' + item.started_at) : '');
    const del = document.createElement('button'); del.className='ghost small'; del.textContent='Delete';
    del.addEventListener('click', async ()=>{
      const r = await fetch('/auth/conversations/delete', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ conv_id: item.conv_id }) });
      const j = await safeJson(r);
      if (r.ok && j && j.ok){
        await renderHistoryManage();
      } else {
        showToast((j && j.error) || 'Failed to delete', true);
      }
    });
    row.appendChild(a); row.appendChild(del);
    box.appendChild(row);
  });
}

// ----- Active chats modal -----
const activeModal = document.getElementById('activeModal');
const btnActiveTop = document.getElementById('btnActiveTop');
const btnCloseActive = document.getElementById('closeActive');
if (btnActiveTop && activeModal) {
  btnActiveTop.addEventListener('click', async ()=>{
    await renderActiveChats();
    activeModal.classList.remove('hidden');
  });
}
if (btnCloseActive && activeModal) {
  btnCloseActive.addEventListener('click', ()=> activeModal.classList.add('hidden'));
}

async function renderActiveChats(){
  const box = document.getElementById('activeList');
  box.innerHTML = 'Loading…';
  const res = await fetch('/auth/active_chats');
  const js = await safeJson(res);
  if (!res.ok || !js || !Array.isArray(js.active_chats)){ box.textContent = 'No active chats.'; return; }
  box.innerHTML = '';
  js.active_chats.slice().reverse().forEach(item => {
    const row = document.createElement('div'); row.className = 'row'; row.style.alignItems='center'; row.style.gap='6px'; row.style.flexWrap='wrap';
    const name = document.createElement('strong'); name.textContent = item.name || item.slug || item.chat_id;
    
    // Show if this is a shared chat (not owned by current user)
    if (item.is_shared) {
      const sharedBadge = document.createElement('span');
      sharedBadge.textContent = '(Shared)';
      sharedBadge.style.fontSize = '11px';
      sharedBadge.style.opacity = '0.7';
      sharedBadge.style.marginLeft = '6px';
      sharedBadge.style.fontStyle = 'italic';
      name.appendChild(sharedBadge);
    }
    
    const when = document.createElement('span'); when.textContent = item.expires_at ? ('Expires: ' + item.expires_at) : '';
    const link = document.createElement('a'); link.href = '/chat/' + item.chat_id + '/' + (item.slug || 'chat'); link.target='_blank'; link.rel='noopener noreferrer'; link.textContent = 'Open';
    const status = document.createElement('span'); status.textContent = item.active ? 'Active' : (item.deactivated ? 'Deactivated' : 'Expired'); status.style.opacity = '0.7';
    row.appendChild(name); row.appendChild(when); row.appendChild(status); row.appendChild(link);
    // Share/Public controls per chat
    const shareWrap = document.createElement('span');
    shareWrap.style.marginLeft = '8px';
    row.appendChild(shareWrap);
    try { renderShareControlsInline(shareWrap, item.chat_id); } catch {}
    const desc = document.createElement('div'); desc.style.display='flex'; desc.style.flexDirection='column'; desc.style.gap='4px'; desc.style.marginLeft='8px';
    const files = Array.isArray(item.files) ? item.files : [];
    if (files.length){
      const f = document.createElement('div'); f.style.fontSize='12px'; f.style.opacity='0.8'; f.textContent = 'Files: ' + files.join(', ');
      desc.appendChild(f);
    }
    const notes = (typeof item.notes_snippet === 'string' ? item.notes_snippet.trim() : '');
    if (notes){
      const n = document.createElement('div'); n.style.fontSize='12px'; n.style.opacity='0.8'; n.textContent = 'Notes: ' + notes;
      desc.appendChild(n);
    }
    if (desc.childNodes.length){
      const wrap = document.createElement('div'); wrap.style.display='flex'; wrap.style.flexDirection='column';
      wrap.appendChild(row);
      wrap.appendChild(desc);
      box.appendChild(wrap);
    } else {
      box.appendChild(row);
    }
    // Only show deactivate button if active AND not a shared chat (user must be owner)
    if (item.active && !item.is_shared){
      const stop = document.createElement('button'); stop.className='ghost small'; stop.textContent='Deactivate';
      stop.style.padding = '8px 14px';
      stop.style.fontSize = '14px';
      stop.addEventListener('click', async ()=>{
        const r = await fetch('/auth/active_chats/deactivate', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ chat_id: item.chat_id }) });
        const j = await safeJson(r);
        if (r.ok && j && j.ok){ await renderActiveChats(); } else { showToast((j && j.error) || 'Failed to deactivate', true); }
      });
      row.appendChild(stop);
      
      // Edit Descriptions button (owner only)
      const editDesc = document.createElement('button'); 
      editDesc.className='ghost small'; 
      editDesc.textContent='Edit Descriptions';
      editDesc.style.background = '#667eea';
      editDesc.style.color = 'white';
      editDesc.style.padding = '8px 14px';
      editDesc.style.fontSize = '14px';
      editDesc.style.whiteSpace = 'nowrap';
      editDesc.addEventListener('click', async ()=>{
        // Close the active chats modal first
        document.getElementById('activeModal').classList.add('hidden');
        await openChatSchemaEditor(item.chat_id, item.name || item.slug || item.chat_id);
      });
      row.appendChild(editDesc);
    }
    // row already appended above in wrap
  });
}

document.getElementById('btnChangePw').addEventListener('click', async ()=>{
  const payload = {
    current_password: document.getElementById('profCurrentPw').value || null,
    new_password: (document.getElementById('profNewPw').value || '').trim(),
  };
  if (!payload.new_password){ showToast('Enter new password', true); return; }
  const res = await fetch('/auth/password', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  const js = await safeJson(res);
  if (res.ok && js && js.ok){
    showToast('Password changed');
    document.getElementById('profCurrentPw').value = '';
    document.getElementById('profNewPw').value = '';
  } else {
    showToast((js && js.error) || 'Failed to change password', true);
  }
});

refreshAuth();

// Field descriptions editor state
let schemaEditorMode = 'form'; // 'form' | 'json' | 'common'
let currentMeta = null;
let currentDetails = null;
let currentCommonFields = null;
let onlyNeededFilter = false;

document.getElementById('btnNewSession').addEventListener('click', async ()=>{
  if(!confirm('This will clear your config (files + field descriptions). Continue?')) return;
  const res = await fetch('/new_session', { method: 'POST' });
  const js = await safeJson(res);
  // Reload without alert
  location.reload();
});

// Retrieve current session schema JSON.
async function fetchSchema(){
  const res = await fetch('/schema');
  return await safeJson(res);
}
// Compute per-column details for the field descriptions editor (types, uniques, needs input).
async function fetchSchemaDetails(){
  const res = await fetch('/schema_details');
  return await safeJson(res);
}
// Fetch common fields relationships
async function fetchCommonFields(){
  const res = await fetch('/schema_common_fields');
  const result = await safeJson(res);
  console.log('Fetched common fields:', result);
  return result;
}
// Save common fields relationships
async function saveCommonFields(relationships){
  console.log('Saving common fields:', relationships);
  const res = await fetch('/schema_common_fields', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ relationships })
  });
  const result = await safeJson(res);
  console.log('Save result:', result);
  return result;
}
document.getElementById('refreshSchema').addEventListener('click', async ()=>{
  await fetchSchema();
  // Silent refresh - no alert needed
});

// ----- JSON Schema Modal UI -----
const modal = document.getElementById('schemaModal');
const schemaContainer = document.getElementById('schemaContainer');
document.getElementById('openSchema').addEventListener('click', async ()=>{
  const [meta, details, commonFields] = await Promise.all([fetchSchema(), fetchSchemaDetails(), fetchCommonFields()]);
  currentMeta = meta || { files: [] };
  currentDetails = details || { files: [] };
  currentCommonFields = commonFields || { relationships: [], user_defined: [] };
  schemaEditorMode = 'form';
  onlyNeededFilter = false;
  renderFormSchemaEditor(currentMeta, currentDetails, { onlyNeeded: onlyNeededFilter });
  modal.classList.remove('hidden');
});
document.getElementById('closeSchema').addEventListener('click', ()=> modal.classList.add('hidden'));

/** Toolbar shared by editors (switch Form/Common Fields/JSON, toggle "Only needed"). */
function renderToolbar(root){
  const controls = document.createElement('div');
  controls.className = 'row';

  const btnForm = document.createElement('button');
  btnForm.type = 'button';
  btnForm.textContent = 'Form view';
  btnForm.className = schemaEditorMode === 'form' ? '' : 'ghost';
  btnForm.addEventListener('click', ()=>{
    schemaEditorMode = 'form';
    renderFormSchemaEditor(currentMeta, currentDetails, { onlyNeeded: onlyNeededFilter });
  });

  const btnCommon = document.createElement('button');
  btnCommon.type = 'button';
  btnCommon.textContent = 'Common Fields';
  btnCommon.className = schemaEditorMode === 'common' ? '' : 'ghost';
  // Disable if only one file
  const fileCount = (currentMeta && currentMeta.files) ? currentMeta.files.length : 0;
  if (fileCount < 2) {
    btnCommon.disabled = true;
    btnCommon.title = 'Upload at least 2 files to detect common fields';
  } else {
    btnCommon.addEventListener('click', ()=>{
      schemaEditorMode = 'common';
      renderCommonFieldsEditor(currentCommonFields);
    });
  }

  const btnJson = document.createElement('button');
  btnJson.type = 'button';
  btnJson.textContent = 'Raw JSON';
  btnJson.className = schemaEditorMode === 'json' ? '' : 'ghost';
  btnJson.addEventListener('click', ()=>{
    schemaEditorMode = 'json';
    renderJsonSchemaEditor(currentMeta);
  });

  const lbl = document.createElement('label');
  lbl.style.marginLeft = 'auto';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = !!onlyNeededFilter;
  cb.style.marginRight = '6px';
  lbl.appendChild(cb);
  lbl.appendChild(document.createTextNode('Only needed'));
  cb.addEventListener('change', (e)=>{
    onlyNeededFilter = !!e.target.checked;
    if (schemaEditorMode === 'form') {
      renderFormSchemaEditor(currentMeta, currentDetails, { onlyNeeded: onlyNeededFilter });
    }
  });

  controls.appendChild(btnForm);
  controls.appendChild(btnCommon);
  controls.appendChild(btnJson);
  controls.appendChild(lbl);
  root.appendChild(controls);
}

/** Build per-file JSON textareas (advanced editing mode). */
function renderJsonSchemaEditor(meta){
  schemaContainer.innerHTML = '';
  renderToolbar(schemaContainer);
  const files = (meta && meta.files) ? meta.files : [];
  if (!files.length){
    const p = document.createElement('p');
    p.textContent = 'No files uploaded yet.';
    schemaContainer.appendChild(p);
    return;
  }

  files.forEach(file => {
    const card = document.createElement('div');
    card.className = 'schema-card';

    const title = document.createElement('h4');
    title.textContent = file.file_name;
    card.appendChild(title);

    let fieldsObj = {};
    if (file.schema && typeof file.schema === 'object' && file.schema.fields){
      fieldsObj = file.schema.fields || {};
    }

    const ta = document.createElement('textarea');
    ta.rows = 16;
    ta.style.width = '100%';
    ta.style.fontFamily = 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace';
    ta.dataset.file = file.file_name;
    try { ta.value = JSON.stringify(fieldsObj, null, 2); } catch(_) { ta.value = '{}'; }

    const controls = document.createElement('div');
    controls.className = 'row';

    const btnBeautify = document.createElement('button');
    btnBeautify.type = 'button'; btnBeautify.className = 'ghost'; btnBeautify.textContent = 'Beautify';
    btnBeautify.addEventListener('click', ()=>{
      try { ta.value = JSON.stringify(JSON.parse(ta.value), null, 2); }
      catch (e) { alert('Invalid JSON: ' + e.message); }
    });

    const btnMinify = document.createElement('button');
    btnMinify.type = 'button'; btnMinify.className = 'ghost'; btnMinify.textContent = 'Minify';
    btnMinify.addEventListener('click', ()=>{
      try { ta.value = JSON.stringify(JSON.parse(ta.value)); }
      catch (e) { alert('Invalid JSON: ' + e.message); }
    });

    controls.appendChild(btnBeautify);
    controls.appendChild(btnMinify);
    card.appendChild(controls);
    card.appendChild(ta);
    schemaContainer.appendChild(card);
  });
}

/** Build per-file form editor (default, focused on fields that need input). */
function renderFormSchemaEditor(meta, details, opts){
  const onlyNeeded = !!(opts && opts.onlyNeeded);
  schemaContainer.innerHTML = '';
  renderToolbar(schemaContainer);
  const files = (meta && Array.isArray(meta.files)) ? meta.files : [];
  const detList = (details && Array.isArray(details.files)) ? details.files : [];
  const detByFile = {};
  for (const f of detList) detByFile[f.file_name] = f;

  if (!files.length){
    const p = document.createElement('p');
    p.textContent = 'No files uploaded yet.';
    schemaContainer.appendChild(p);
    return;
  }

  files.forEach(file => {
    const fn = file.file_name;
    const existing = (file.schema && file.schema.fields) ? (file.schema.fields || {}) : {};
    const colsInfo = (detByFile[fn] && detByFile[fn].columns) ? detByFile[fn].columns : {};
    const colNames = Object.keys(colsInfo);
    const filteredCols = onlyNeeded
      ? colNames.filter(c => colsInfo[c].needs_description || (Array.isArray(colsInfo[c].missing_value_descriptions) && colsInfo[c].missing_value_descriptions.length))
      : colNames;

    const card = document.createElement('div');
    card.className = 'schema-card';
    card.dataset.file = fn;

    const title = document.createElement('h4');
    title.textContent = fn + (onlyNeeded ? ` — ${filteredCols.length} need input` : '');
    card.appendChild(title);

    if (!filteredCols.length){
      const p = document.createElement('p');
      p.textContent = onlyNeeded ? 'Nothing needs input for this file.' : 'No columns found.';
      card.appendChild(p);
      schemaContainer.appendChild(card);
      return;
    }

    const table = document.createElement('table');
    table.className = 'schema-table';
    const thead = document.createElement('thead');
    const thr = document.createElement('tr');
    ['Column', 'Type', 'Description', 'Value meanings'].forEach(h => { const th = document.createElement('th'); th.textContent = h; thr.appendChild(th); });
    thead.appendChild(thr);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    filteredCols.forEach(col => {
      const info = colsInfo[col] || {};
      const tr = document.createElement('tr');
      tr.dataset.col = col;

      const tdCol = document.createElement('td');
      tdCol.textContent = col;
      tr.appendChild(tdCol);

      const tdType = document.createElement('td');
      const nun = (info.nunique != null) ? `, nunique=${info.nunique}` : '';
      tdType.textContent = (info.dtype || '-') + nun + (info.categorical ? ' (categorical)' : '');
      tr.appendChild(tdType);

      const tdDesc = document.createElement('td');
      const descTa = document.createElement('textarea');
      descTa.rows = 2; descTa.className = 'desc-input';
      descTa.dataset.file = fn; descTa.dataset.col = col;
      const existingDesc = (existing[col] && typeof existing[col].description === 'string') ? existing[col].description : '';
      descTa.value = existingDesc;
      tdDesc.appendChild(descTa);
      tr.appendChild(tdDesc);

      const tdVals = document.createElement('td');
      if (info.categorical && Array.isArray(info.unique_values) && info.unique_values.length){
        const wrap = document.createElement('div');
        (info.unique_values || []).forEach(v => {
          const row = document.createElement('div'); row.className = 'row';
          const lab = document.createElement('span'); lab.textContent = String(v); lab.style.minWidth = '120px';
          const inp = document.createElement('input'); inp.type = 'text'; inp.placeholder = 'Describe';
          inp.className = 'valdesc-input';
          inp.dataset.file = fn; inp.dataset.col = col; inp.dataset.value = String(v);
          const prev = (existing[col] && existing[col].values && typeof existing[col].values === 'object') ? existing[col].values[String(v)] : '';
          inp.value = prev || '';
          row.appendChild(lab); row.appendChild(inp);
          wrap.appendChild(row);
        });
        tdVals.appendChild(wrap);
      } else {
        tdVals.textContent = '—';
      }
      tr.appendChild(tdVals);

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    card.appendChild(table);
    schemaContainer.appendChild(card);
  });
}

/** Build Common Fields editor to manage join relationships. */
function renderCommonFieldsEditor(commonFieldsData){
  schemaContainer.innerHTML = '';
  renderToolbar(schemaContainer);
  
  const intro = document.createElement('p');
  intro.textContent = 'These are potential join columns detected across your files. You can add or remove relationships manually.';
  intro.style.marginBottom = '16px';
  intro.style.color = '#6b7280';
  schemaContainer.appendChild(intro);
  
  // Merge AI-detected and user-defined relationships
  let allRelationships = [];
  if (commonFieldsData && Array.isArray(commonFieldsData.relationships)) {
    allRelationships = [...commonFieldsData.relationships];
  }
  
  // Container for relationships
  const relContainer = document.createElement('div');
  relContainer.id = 'relationshipsContainer';
  relContainer.style.marginBottom = '20px';
  
  if (allRelationships.length === 0) {
    const noRel = document.createElement('p');
    noRel.textContent = 'No common fields detected. Upload multiple files with related columns.';
    noRel.style.fontStyle = 'italic';
    noRel.style.color = '#9ca3af';
    relContainer.appendChild(noRel);
  } else {
    const table = document.createElement('table');
    table.className = 'schema-table';
    table.style.width = '100%';
    
    const thead = document.createElement('thead');
    const thr = document.createElement('tr');
    ['File 1', 'Column 1', '⟷', 'File 2', 'Column 2', 'Confidence', 'Reasons', 'Actions'].forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      if (h === '⟷') th.style.width = '30px';
      if (h === 'Actions') th.style.width = '80px';
      thr.appendChild(th);
    });
    thead.appendChild(thr);
    table.appendChild(thead);
    
    const tbody = document.createElement('tbody');
    allRelationships.forEach((rel, idx) => {
      const tr = document.createElement('tr');
      tr.dataset.relIndex = idx;
      
      // File 1
      const td1 = document.createElement('td');
      td1.textContent = rel.file1 || '';
      td1.style.fontWeight = '500';
      tr.appendChild(td1);
      
      // Column 1
      const td2 = document.createElement('td');
      td2.textContent = rel.column1 || '';
      td2.style.fontFamily = 'monospace';
      td2.style.color = '#667eea';
      tr.appendChild(td2);
      
      // Arrow
      const tdArrow = document.createElement('td');
      tdArrow.textContent = '⟷';
      tdArrow.style.textAlign = 'center';
      tdArrow.style.fontSize = '18px';
      tr.appendChild(tdArrow);
      
      // File 2
      const td3 = document.createElement('td');
      td3.textContent = rel.file2 || '';
      td3.style.fontWeight = '500';
      tr.appendChild(td3);
      
      // Column 2
      const td4 = document.createElement('td');
      td4.textContent = rel.column2 || '';
      td4.style.fontFamily = 'monospace';
      td4.style.color = '#667eea';
      tr.appendChild(td4);
      
      // Confidence
      const tdConf = document.createElement('td');
      const conf = rel.confidence || 0;
      const badge = document.createElement('span');
      badge.textContent = conf + '%';
      badge.style.padding = '4px 8px';
      badge.style.borderRadius = '4px';
      badge.style.fontSize = '12px';
      badge.style.fontWeight = '600';
      if (conf >= 80) {
        badge.style.background = '#d1fae5';
        badge.style.color = '#065f46';
      } else if (conf >= 50) {
        badge.style.background = '#fef3c7';
        badge.style.color = '#92400e';
      } else {
        badge.style.background = '#fee2e2';
        badge.style.color = '#991b1b';
      }
      tdConf.appendChild(badge);
      tr.appendChild(tdConf);
      
      // Reasons
      const tdReasons = document.createElement('td');
      const reasons = Array.isArray(rel.reasons) ? rel.reasons : [];
      tdReasons.textContent = reasons.join(', ') || 'Manual';
      tdReasons.style.fontSize = '12px';
      tdReasons.style.color = '#6b7280';
      tr.appendChild(tdReasons);
      
      // Actions
      const tdActions = document.createElement('td');
      const btnDel = document.createElement('button');
      btnDel.className = 'ghost small';
      btnDel.textContent = '✕';
      btnDel.title = 'Remove this relationship';
      // Use closure to capture the correct relationship to delete
      btnDel.addEventListener('click', ((relToDelete) => {
        return () => {
          if (confirm('Remove this relationship?')) {
            const newRelationships = allRelationships.filter(r => r !== relToDelete);
            renderCommonFieldsEditor({ relationships: newRelationships });
          }
        };
      })(rel));
      tdActions.appendChild(btnDel);
      tr.appendChild(tdActions);
      
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    relContainer.appendChild(table);
  }
  
  schemaContainer.appendChild(relContainer);
  
  // Add manual relationship section
  const addSection = document.createElement('div');
  addSection.style.marginTop = '24px';
  addSection.style.padding = '16px';
  addSection.style.background = '#f9fafb';
  addSection.style.borderRadius = '8px';
  
  const addTitle = document.createElement('h4');
  addTitle.textContent = 'Add Manual Relationship';
  addTitle.style.marginBottom = '12px';
  addSection.appendChild(addTitle);
  
  const addForm = document.createElement('div');
  addForm.className = 'row';
  addForm.style.gap = '8px';
  addForm.style.flexWrap = 'wrap';
  
  // Get list of files
  const fileNames = (currentMeta && currentMeta.files) ? currentMeta.files.map(f => f.file_name) : [];
  
  // File 1 select
  const sel1 = document.createElement('select');
  sel1.id = 'addFile1';
  fileNames.forEach(fn => {
    const opt = document.createElement('option');
    opt.value = fn;
    opt.textContent = fn;
    sel1.appendChild(opt);
  });
  
  // Column 1 select
  const selCol1 = document.createElement('select');
  selCol1.id = 'addCol1';
  
  // File 2 select
  const sel2 = document.createElement('select');
  sel2.id = 'addFile2';
  fileNames.forEach(fn => {
    const opt = document.createElement('option');
    opt.value = fn;
    opt.textContent = fn;
    sel2.appendChild(opt);
  });
  
  // Column 2 select
  const selCol2 = document.createElement('select');
  selCol2.id = 'addCol2';
  
  // Update column selects when file changes
  const updateCols = () => {
    const file1 = sel1.value;
    const file2 = sel2.value;
    
    selCol1.innerHTML = '';
    selCol2.innerHTML = '';
    
    if (file1 && currentMeta && currentMeta.files) {
      const f1 = currentMeta.files.find(f => f.file_name === file1);
      if (f1 && f1.schema && f1.schema.fields) {
        Object.keys(f1.schema.fields).forEach(col => {
          const opt = document.createElement('option');
          opt.value = col;
          opt.textContent = col;
          selCol1.appendChild(opt);
        });
      }
    }
    
    if (file2 && currentMeta && currentMeta.files) {
      const f2 = currentMeta.files.find(f => f.file_name === file2);
      if (f2 && f2.schema && f2.schema.fields) {
        Object.keys(f2.schema.fields).forEach(col => {
          const opt = document.createElement('option');
          opt.value = col;
          opt.textContent = col;
          selCol2.appendChild(opt);
        });
      }
    }
  };
  
  sel1.addEventListener('change', updateCols);
  sel2.addEventListener('change', updateCols);
  updateCols();
  
  const btnAdd = document.createElement('button');
  btnAdd.className = 'ghost';
  btnAdd.textContent = 'Add Relationship';
  btnAdd.addEventListener('click', ()=>{
    const file1 = sel1.value;
    const col1 = selCol1.value;
    const file2 = sel2.value;
    const col2 = selCol2.value;
    
    if (!file1 || !col1 || !file2 || !col2) {
      showToast('Please select both files and columns', true);
      return;
    }
    
    if (file1 === file2) {
      showToast('Please select different files', true);
      return;
    }
    
    // Check if already exists
    const exists = allRelationships.some(r =>
      (r.file1 === file1 && r.column1 === col1 && r.file2 === file2 && r.column2 === col2) ||
      (r.file1 === file2 && r.column1 === col2 && r.file2 === file1 && r.column2 === col1)
    );
    
    if (exists) {
      showToast('This relationship already exists', true);
      return;
    }
    
    const newRel = {
      file1,
      column1: col1,
      file2,
      column2: col2,
      confidence: 100,
      reasons: ['Manually added'],
      auto_detected: false
    };
    
    allRelationships.push(newRel);
    console.log('Added new relationship:', newRel);
    console.log('All relationships now:', allRelationships);
    
    renderCommonFieldsEditor({ relationships: allRelationships });
  });
  
  addForm.appendChild(sel1);
  addForm.appendChild(selCol1);
  const arrow = document.createElement('span');
  arrow.textContent = '⟷';
  arrow.style.fontSize = '18px';
  arrow.style.padding = '0 8px';
  addForm.appendChild(arrow);
  addForm.appendChild(sel2);
  addForm.appendChild(selCol2);
  addForm.appendChild(btnAdd);
  
  addSection.appendChild(addForm);
  schemaContainer.appendChild(addSection);
  
  // Store relationships for saving
  const dataToStore = JSON.stringify(allRelationships);
  schemaContainer.dataset.commonFieldsData = dataToStore;
  console.log('Stored in dataset for save:', dataToStore);
}

// Collect edits from the active editor and POST /schema
document.getElementById('saveSchema').addEventListener('click', async ()=>{
  // Handle common fields mode separately
  if (schemaEditorMode === 'common') {
    try {
      const dataStr = schemaContainer.dataset.commonFieldsData;
      console.log('Reading from dataset:', dataStr);
      const relationships = dataStr ? JSON.parse(dataStr) : [];
      console.log('Parsed relationships to save:', relationships);
      const res = await saveCommonFields(relationships);
      if (res && res.ok) {
        showToast('Common fields saved');
        // Refresh the data
        currentCommonFields = await fetchCommonFields();
      } else {
        const errMsg = (res && res.error) ? res.error : 'Failed to save common fields';
        showToast(errMsg, true);
        console.error('Save common fields error:', res);
      }
    } catch (e) {
      showToast('Error saving common fields: ' + e.message, true);
      console.error('Save common fields exception:', e);
    }
    return;
  }

  let filesPayload = [];

  if (schemaEditorMode === 'form') {
    // Collect from form view, merging with existing schema
    const files = (currentMeta && Array.isArray(currentMeta.files)) ? currentMeta.files : [];
    for (const file of files) {
      const fn = file.file_name;
      const existing = (file.schema && file.schema.fields) ? (file.schema.fields || {}) : {};
      const normalized = {};
      // Start from existing
      for (const [col, val] of Object.entries(existing)) {
        const desc = (val && typeof val.description === 'string') ? val.description : '';
        const values = (val && val.values && typeof val.values === 'object') ? { ...val.values } : undefined;
        normalized[col] = { ...val, description: desc };
        if (values) normalized[col].values = values; else delete normalized[col].values;
      }
      // Apply inputs if present
      const card = schemaContainer.querySelector('div.schema-card[data-file="' + fn + '"]');
      if (card) {
        const rows = card.querySelectorAll('tr[data-col]');
        rows.forEach(row => {
          const col = row.getAttribute('data-col');
          if (!normalized[col]) normalized[col] = {};
          const descEl = row.querySelector('textarea.desc-input');
          if (descEl) normalized[col].description = descEl.value || '';
          const valInputs = row.querySelectorAll('input.valdesc-input[data-value]');
          if (valInputs.length) {
            const map = {};
            valInputs.forEach(inp => {
              const v = inp.getAttribute('data-value');
              const t = (inp.value || '').trim();
              // Always save the value, even if description is empty
              map[v] = t;
            });
            // Always save values object if we have categorical values
            if (Object.keys(map).length) { normalized[col].values = map; }
          }
        });
      }
      filesPayload.push({ file_name: fn, fields: normalized });
    }
  } else {
    // Collect from raw JSON textareas
    const taList = schemaContainer.querySelectorAll('textarea[data-file]');
    if (!taList.length){ alert('Nothing to save.'); return; }
    for (const ta of taList) {
      const fn = ta.dataset.file;
      let obj;
      try {
        obj = JSON.parse(ta.value || '{}');
        if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
          throw new Error('Root JSON must be an object mapping column -> descriptor');
        }
      } catch (e) {
        alert(`Invalid JSON for ${fn}: ${e.message}`);
        return;
      }
      const normalized = {};
      for (const [col, val] of Object.entries(obj)) {
        if (val === null) {
          normalized[col] = { description: '' };
        } else if (typeof val === 'string') {
          normalized[col] = { description: val };
        } else if (typeof val === 'object') {
          const desc = (typeof val.description === 'string') ? val.description : '';
          const values = (val.values && typeof val.values === 'object') ? val.values : null;
          const copy = { ...val, description: desc };
          if (values === null) { delete copy.values; } else { copy.values = values; }
          normalized[col] = copy;
        } else {
          normalized[col] = { description: String(val) };
        }
      }
      filesPayload.push({ file_name: fn, fields: normalized });
    }
  }

  const payload = { files: filesPayload };
  const res = await fetch('/schema', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  const js = await safeJson(res);
  if(js && js.ok){
    showToast('Field descriptions saved');
    modal.classList.add('hidden');
  } else {
    showToast((js && (js.error || js.message)) || 'Failed to save field descriptions.', true);
  }
});

/** Generate Chat page — pre-checks + clear error messages */
document.getElementById('btnGenerate').addEventListener('click', async ()=>{
  const out = document.getElementById('genOut');
  out.textContent = 'Checking…';

  // Pre-check: ensure there is at least one uploaded file in current session
  const meta = await fetchSchema().catch(()=>null);
  const hasFiles = !!(meta && Array.isArray(meta.files) && meta.files.length);
  if (!hasFiles) {
    out.textContent = 'No files found in this session. Please upload files first.';
    return;
  }

  // Validate name and days
  const name = (document.getElementById('chatName').value || '').trim();
  const daysInput = document.getElementById('chatDays');
  const maxDays = parseInt(daysInput.getAttribute('max') || '10', 10);
  const minDays = parseInt(daysInput.getAttribute('min') || '1', 10);
  let days = parseInt(daysInput.value || '0', 10);
  if (!name){ out.textContent = 'Please enter a chat name.'; return; }
  if (isNaN(days) || days < minDays) days = minDays;
  if (days > maxDays) days = maxDays;

  out.textContent = 'Generating chat page…';
  showLoading('Generating chat page...');
  let res, js;
  try {
    res = await fetch('/generate_chatdata', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ name, days }) });
    js = await safeJson(res);
  } catch (e) {
    hideLoading();
    out.textContent = 'Network error: ' + e.message;
    return;
  } finally {
    hideLoading();
  }

  if (res && res.ok && js && js.ok && js.url) {
    const link = document.createElement('a');
    link.href = js.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = js.url;
    out.innerHTML = 'Chat ready: ';
    out.appendChild(link);
    // Share/Public controls for this newly created chat
    try {
      const controls = document.createElement('span');
      controls.style.marginLeft = '8px';
      out.appendChild(controls);
      renderShareControlsInline(controls, js.chat_id);
    } catch {}
  } else {
    // Show most helpful message we can extract
    const msg = (js && (js.detail || js.message)) || ('HTTP ' + (res ? res.status : '?'));
    out.textContent = 'Failed to generate chat data: ' + msg;
  }
});

// Auto-fill button removed — now triggers automatically after upload (see triggerAutoFillAndOpenSchema)

/** Safe JSON helper */
async function safeJson(res){
  let data = null;
  try { data = await res.json(); } catch(_) {}
  return data;
}

function showToast(text, isError=false){
  try {
    const t = document.createElement('div');
    t.textContent = text;
    t.style.position = 'fixed';
    t.style.left = '50%';
    t.style.bottom = '20px';
    t.style.transform = 'translateX(-50%)';
    t.style.background = isError ? '#fef2f2' : '#ecfdf5';
    t.style.color = isError ? '#991b1b' : '#065f46';
    t.style.border = isError ? '1px solid #fecaca' : '1px solid #a7f3d0';
    t.style.padding = '10px 14px';
    t.style.borderRadius = '10px';
    t.style.boxShadow = '0 4px 14px rgba(0,0,0,0.15)';
    t.style.fontSize = '14px';
    t.style.zIndex = '9999';
    document.body.appendChild(t);
    setTimeout(()=>{ t.remove(); }, 1000);
  } catch {}
}

// ---- Chat Schema Editor (for editing descriptions on generated chats) ----
let editingChatId = null;
let editingChatMeta = null;
let editingChatCommonFields = null;

async function fetchChatSchema(chatId) {
  const res = await fetch(`/api/chat/${chatId}/schema`);
  return await safeJson(res);
}

async function saveChatSchema(chatId, files) {
  const res = await fetch(`/api/chat/${chatId}/schema`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ files })
  });
  return await safeJson(res);
}

async function fetchChatCommonFields(chatId) {
  const res = await fetch(`/api/chat/${chatId}/schema_common_fields`);
  return await safeJson(res);
}

async function saveChatCommonFields(chatId, relationships) {
  const res = await fetch(`/api/chat/${chatId}/schema_common_fields`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ relationships })
  });
  return await safeJson(res);
}

async function openChatSchemaEditor(chatId, chatName) {
  showLoading('Loading schema...');
  editingChatId = chatId;
  
  try {
    const [schemaRes, commonRes] = await Promise.all([
      fetchChatSchema(chatId),
      fetchChatCommonFields(chatId)
    ]);
    
    if (!schemaRes || schemaRes.error) {
      hideLoading();
      showToast(schemaRes?.error || 'Failed to load schema', true);
      return;
    }
    
    // Convert to format expected by renderFormSchemaEditor
    editingChatMeta = { files: schemaRes.files || [] };
    editingChatCommonFields = commonRes || { relationships: [], user_defined: [] };
    
    // Build details structure (simplified - no schema_details endpoint for chats)
    const details = { files: [] };
    for (const file of editingChatMeta.files) {
      const cols = {};
      const schema = file.schema || {};
      const fields = schema.fields || {};
      for (const [col, fieldData] of Object.entries(fields)) {
        cols[col] = {
          dtype: 'unknown',
          nunique: null,
          total_rows: null,
          unique_values: Object.keys(fieldData?.values || {}),
          categorical: Object.keys(fieldData?.values || {}).length > 0,
          needs_description: !fieldData?.description,
          missing_value_descriptions: []
        };
      }
      details.files.push({ file_name: file.file_name, columns: cols });
    }
    
    // Update modal title
    const modalHeader = document.querySelector('#schemaModal .modal-header h3');
    if (modalHeader) {
      modalHeader.textContent = `Field Descriptions - ${chatName}`;
    }
    
    // Store original values for restore
    currentMeta = editingChatMeta;
    currentDetails = details;
    currentCommonFields = editingChatCommonFields;
    schemaEditorMode = 'form';
    onlyNeededFilter = false;
    
    // Render the editor
    renderFormSchemaEditor(currentMeta, currentDetails, { onlyNeeded: false });
    
    // Replace save button handler temporarily
    const saveBtn = document.getElementById('saveSchema');
    const originalOnClick = saveBtn.onclick;
    saveBtn.onclick = async () => {
      await saveChatSchemaFromEditor();
    };
    
    // Store original handler for restore
    saveBtn.dataset.originalHandler = 'session';
    
    hideLoading();
    modal.classList.remove('hidden');
    
    // Restore handler when modal closes
    const closeHandler = () => {
      editingChatId = null;
      editingChatMeta = null;
      editingChatCommonFields = null;
      saveBtn.onclick = originalOnClick || (async () => { await saveSchemaForm(); });
      const modalHeader = document.querySelector('#schemaModal .modal-header h3');
      if (modalHeader) modalHeader.textContent = 'Field Descriptions';
      document.getElementById('closeSchema').removeEventListener('click', closeHandler);
    };
    document.getElementById('closeSchema').addEventListener('click', closeHandler);
    
  } catch (e) {
    hideLoading();
    showToast('Error loading schema: ' + e.message, true);
  }
}

async function saveChatSchemaFromEditor() {
  if (!editingChatId) {
    // Fall back to session save
    await saveSchemaForm();
    return;
  }
  
  showLoading('Saving...');
  
  try {
    // Build files payload from form
    const files = [];
    for (const fileEntry of (currentMeta?.files || [])) {
      const fname = fileEntry.file_name;
      const schema = fileEntry.schema || {};
      const fields = {};
      
      // Collect field descriptions from form inputs
      document.querySelectorAll(`[data-file="${fname}"][data-col]`).forEach(inp => {
        const col = inp.dataset.col;
        if (!fields[col]) fields[col] = { description: '', values: {} };
        if (inp.dataset.valkey) {
          fields[col].values[inp.dataset.valkey] = inp.value || '';
        } else {
          fields[col].description = inp.value || '';
        }
      });
      
      // Merge with existing values
      for (const [col, existing] of Object.entries(schema.fields || {})) {
        if (!fields[col]) fields[col] = { description: '', values: {} };
        if (!fields[col].description && existing?.description) {
          fields[col].description = existing.description;
        }
        if (existing?.values) {
          for (const [k, v] of Object.entries(existing.values)) {
            if (!fields[col].values[k]) {
              fields[col].values[k] = v;
            }
          }
        }
      }
      
      files.push({ file_name: fname, fields });
    }
    
    const result = await saveChatSchema(editingChatId, files);
    
    if (result?.ok) {
      showToast('Schema saved successfully');
      modal.classList.add('hidden');
    } else {
      showToast(result?.error || 'Failed to save schema', true);
    }
  } catch (e) {
    showToast('Error saving: ' + e.message, true);
  }
  
  hideLoading();
}
