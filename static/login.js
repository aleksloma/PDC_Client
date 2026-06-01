(function(){
  const year = document.getElementById('year');
  if (year) year.textContent = String(new Date().getFullYear());

  async function safeJson(res){ try { return await res.json(); } catch { return null; } }

  // Check if user is logged in (from page context)
  const isLoggedIn = !!document.getElementById('btnLogout');

  // Subscription state
  var _currentSubSource = 'default';
  var _currentSubPlan = 'Basic';
  var _paddleScheduledChange = null;
  var _paddleScheduledPlan = null;

  // ============================================
  // Shared helpers (toast, loading overlay)
  // ============================================
  function showToast(text, isError = false) {
    const t = document.createElement('div');
    t.textContent = text;
    t.style.cssText = `position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;color:#fff;font-size:14px;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.15);transition:opacity 0.3s;background:${isError ? '#ef4444' : '#10b981'};`;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 3000);
  }

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

  // ============================================
  // Profile Dropdown
  // ============================================
  const profileIconBtn = document.getElementById('btnProfileIcon');
  const profileDropdown = document.getElementById('profileDropdown');

  if (profileIconBtn && profileDropdown) {
    profileIconBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      profileDropdown.classList.toggle('hidden');
    });

    document.addEventListener('click', () => {
      profileDropdown.classList.add('hidden');
    });
  }

  // Avatar initials + color are now set server-side via the shared partial template.
  // No JS color hashing needed.

  // Login dropdown button (when logged out)
  const btnLoginDropdown = document.getElementById('btnLoginDropdown');
  if (btnLoginDropdown) {
    btnLoginDropdown.addEventListener('click', () => {
      profileDropdown.classList.add('hidden');
      openAuthModal('login');
    });
  }

  // ============================================
  // Profile Modal (when logged in)
  // ============================================
  const profileModal = document.getElementById('profileModal');

  // Profile button — opens profile modal
  const btnProfile = document.getElementById('btnProfile');
  if (btnProfile) {
    btnProfile.addEventListener('click', () => {
      profileDropdown.classList.add('hidden');
      if (profileModal) {
        profileModal.classList.remove('hidden');
        loadUserProfile();
      }
    });
  }

  // Subscriptions button — opens profile modal scrolled to subscription section
  const btnSubscriptions = document.getElementById('btnSubscriptions');
  if (btnSubscriptions) {
    btnSubscriptions.addEventListener('click', () => {
      profileDropdown.classList.add('hidden');
      if (profileModal) {
        profileModal.classList.remove('hidden');
        loadUserProfile();
        setTimeout(() => {
          document.querySelector('.subscription-section')?.scrollIntoView({ behavior: 'smooth' });
        }, 100);
      }
    });
  }

  // Logout button
  const btnLogout = document.getElementById('btnLogout');
  if (btnLogout) {
    btnLogout.addEventListener('click', async () => {
      profileDropdown.classList.add('hidden');
      await fetch('/auth/logout', { method: 'POST' });
      window.location.href = '/';
    });
  }

  // Close profile modal
  const closeProfile = document.getElementById('closeProfile');
  if (closeProfile) {
    closeProfile.addEventListener('click', () => {
      profileModal.classList.add('hidden');
    });
  }
  if (profileModal) {
    profileModal.addEventListener('click', (e) => {
      if (e.target === profileModal) profileModal.classList.add('hidden');
    });
  }

  // Save profile button
  const btnSaveProfile = document.getElementById('btnSaveProfile');
  if (btnSaveProfile) btnSaveProfile.addEventListener('click', saveProfile);

  // Change password button
  const btnChangePw = document.getElementById('btnChangePw');
  if (btnChangePw) btnChangePw.addEventListener('click', changePassword);

  // Plan selection buttons
  document.querySelectorAll('.plan-select').forEach(btn => {
    btn.addEventListener('click', async () => {
      var plan = btn.dataset.plan;
      if (plan === 'Enterprise') { window.openAboutContact && window.openAboutContact(); return; }
      await selectPlan(plan);
    });
  });

  // ============================================
  // Profile Management Functions
  // ============================================
  async function loadUserProfile() {
    try {
      const res = await fetch('/auth/profile');
      const profile = await res.json();

      _currentSubSource = profile.subscription_source || 'default';
      _currentSubPlan = profile.subscription_plan || 'Basic';
      _paddleScheduledChange = profile.paddle_scheduled_change || null;
      _paddleScheduledPlan = profile.paddle_scheduled_plan || null;

      // Update profile modal fields
      const profUsername = document.getElementById('profUsername');
      const profFullName = document.getElementById('profFullName');
      const profEmail = document.getElementById('profEmail');
      const currentPlanName = document.getElementById('currentPlanName');

      if (profUsername) profUsername.value = profile.username || '';
      if (profFullName) profFullName.value = profile.full_name || '';
      if (profEmail) profEmail.value = profile.email || '';
      if (currentPlanName) currentPlanName.textContent = profile.subscription_plan || 'Basic';

      // Highlight current plan
      document.querySelectorAll('.plan-card').forEach(card => {
        card.classList.toggle('active', card.dataset.plan === profile.subscription_plan);
      });

      // Update plan button labels
      _updatePlanButtonLabels();

      // Show subscription status info
      var statusEl = document.getElementById('subscriptionStatus');
      if (statusEl) {
        if (_paddleScheduledChange === 'cancel') {
          var expiresAt = profile.paddle_billing_period_end;
          var dateStr = expiresAt ? new Date(expiresAt).toLocaleDateString() : 'end of billing period';
          statusEl.innerHTML = '<span style="color:#f59e0b;">Cancellation scheduled — your ' + _currentSubPlan +
            ' plan stays active until ' + dateStr + '.</span> ' +
            '<button class="plan-select-sm" onclick="window._reactivateSub && window._reactivateSub()">Undo Cancellation</button>';
          statusEl.style.display = 'block';
        } else if (_paddleScheduledChange === 'downgrade' && _paddleScheduledPlan) {
          var expiresAt = profile.paddle_billing_period_end;
          var dateStr = expiresAt ? new Date(expiresAt).toLocaleDateString() : 'next billing date';
          statusEl.innerHTML = '<span style="color:#f59e0b;">Switching to ' + _paddleScheduledPlan +
            ' on ' + dateStr + '. You keep ' + _currentSubPlan + ' features until then.</span> ' +
            '<button class="plan-select-sm" onclick="window._reactivateSub && window._reactivateSub()">Undo Downgrade</button>';
          statusEl.style.display = 'block';
        } else if (_currentSubSource === 'paddle' && _currentSubPlan !== 'Basic') {
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

      // Update profile button text (keep server-side color/initials)
      const nameEl = document.getElementById('profileBtnName');
      const planEl = document.getElementById('profileBtnPlan');
      const displayName = profile.full_name || profile.username || 'User';
      if (nameEl) nameEl.textContent = displayName;
      if (planEl) planEl.textContent = profile.subscription_plan || 'Basic';

      // Disable password section for Google OAuth users
      const isGoogleUser = profile.provider === 'google';
      const pwFields = document.getElementById('passwordFields');
      const pwNotice = document.getElementById('passwordGoogleNotice');
      const pwSection = document.getElementById('passwordSection');
      if (isGoogleUser) {
        if (pwSection) pwSection.classList.add('disabled-section');
        if (pwFields) pwFields.classList.add('hidden');
        if (pwNotice) pwNotice.classList.remove('hidden');
        if (btnChangePw) btnChangePw.classList.add('hidden');
      } else {
        if (pwSection) pwSection.classList.remove('disabled-section');
        if (pwFields) pwFields.classList.remove('hidden');
        if (pwNotice) pwNotice.classList.add('hidden');
        if (btnChangePw) btnChangePw.classList.remove('hidden');
      }
    } catch (e) {
      console.error('Failed to load profile:', e);
    }
  }

  function _updatePlanButtonLabels() {
    document.querySelectorAll('.plan-select').forEach(function(btn) {
      var plan = btn.dataset.plan;
      if (plan === 'Enterprise') { btn.textContent = 'Contact Us'; return; }
      if (plan === _currentSubPlan) {
        btn.textContent = 'Current Plan';
        btn.disabled = (_paddleScheduledChange !== 'cancel' && _paddleScheduledChange !== 'downgrade');
      } else if (_paddleScheduledChange === 'downgrade' && plan === _paddleScheduledPlan) {
        btn.textContent = 'Scheduled';
        btn.disabled = true;
      } else if (plan === 'Basic') {
        btn.textContent = 'Downgrade to Basic';
        btn.disabled = false;
      } else if (_currentSubPlan === 'Basic') {
        btn.textContent = 'Get ' + plan;
        btn.disabled = false;
      } else {
        var rank = { Basic: 0, Standard: 1, Pro: 2, Enterprise: 3 };
        btn.textContent = (rank[plan] > rank[_currentSubPlan]) ? 'Upgrade to ' + plan : 'Switch to ' + plan;
        btn.disabled = false;
      }
    });
    // Highlight Enterprise card if active
    var entCard = document.getElementById('planEnterpriseCard');
    if (entCard) {
      if (_currentSubPlan === 'Enterprise') entCard.classList.add('active');
      else entCard.classList.remove('active');
    }
  }

  // ---------------------------------------------------------------------------
  // Subscription modal helpers (login page)
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
  function _closeSubModal() { _subModal().classList.add('hidden'); }
  function _fmtCurrency(amount, currency) {
    var sym = currency === 'USD' ? '$' : currency + ' ';
    return sym + amount;
  }

  // Expose for global onclick
  window._closeSubModal = _closeSubModal;

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
          '<button class="ghost" onclick="window._closeSubModal()">Close</button>');
      }
    } catch (e) {
      _openSubModal('Error', '<div class="sub-modal-highlight error">Failed to load payment details</div>',
        '<button class="ghost" onclick="window._closeSubModal()">Close</button>');
    }
  }
  window._updatePaymentMethod = _updatePaymentMethod;

  // Expose reactivation for inline button
  window._reactivateSub = async function() {
    var desc;
    if (_paddleScheduledChange === 'downgrade' && _paddleScheduledPlan) {
      desc = '<p>Your plan is scheduled to switch to ' + _paddleScheduledPlan + '.</p>' +
             '<p>Would you like to keep your ' + _currentSubPlan + ' plan instead?</p>';
    } else {
      desc = '<p>Your ' + _currentSubPlan + ' plan is scheduled to cancel.</p>' +
             '<p>Would you like to continue your subscription?</p>';
    }
    _openSubModal('Keep ' + _currentSubPlan + ' Plan', desc,
      '<button class="primary" onclick="window._doReactivate()">Keep My Plan</button>' +
      '<button class="ghost" onclick="window._closeSubModal()">Never Mind</button>'
    );
  };

  window._doReactivate = async function() {
    _subBody().innerHTML = '<div class="sub-modal-spinner"><p>Reactivating...</p></div>';
    _subActions().innerHTML = '';
    try {
      var res = await fetch('/api/paddle/subscription/reactivate', { method: 'POST' });
      var data = await safeJson(res);
      if (data && data.ok) {
        _openSubModal('Plan Reactivated',
          '<div class="sub-modal-highlight success">Your ' + (data.plan || _currentSubPlan) + ' plan will continue.</div>',
          '<button class="primary" onclick="window._closeSubModal(); location.reload();">Done</button>');
      } else {
        _openSubModal('Error',
          '<div class="sub-modal-highlight error">' + ((data && data.error) || 'Failed to reactivate') + '</div>',
          '<button class="ghost" onclick="window._closeSubModal()">Close</button>');
      }
    } catch (e) {
      _openSubModal('Error',
        '<div class="sub-modal-highlight error">Failed to reactivate subscription</div>',
        '<button class="ghost" onclick="window._closeSubModal()">Close</button>');
    }
  };

  async function saveProfile() {
    showLoading('Saving...');

    const newPw = (document.getElementById('profNewPw')?.value || '').trim();
    if (newPw) {
      const confirmPw = (document.getElementById('profConfirmPw')?.value || '').trim();
      if (newPw !== confirmPw) {
        const err = document.getElementById('pwMatchError');
        if (err) err.classList.remove('hidden');
        showToast('Passwords do not match', true);
        hideLoading();
        return;
      }
      const err = document.getElementById('pwMatchError');
      if (err) err.classList.add('hidden');
      try {
        const current = document.getElementById('profCurrentPw')?.value || '';
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
        if (document.getElementById('profCurrentPw')) document.getElementById('profCurrentPw').value = '';
        if (document.getElementById('profNewPw')) document.getElementById('profNewPw').value = '';
        if (document.getElementById('profConfirmPw')) document.getElementById('profConfirmPw').value = '';
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
          email: document.getElementById('profEmail')?.value || '',
          full_name: document.getElementById('profFullName')?.value || '',
          new_username: document.getElementById('profUsername')?.value || ''
        })
      });
      showToast(newPw ? 'Profile and password saved' : 'Profile saved');
      if (profileModal) profileModal.classList.add('hidden');
    } catch (e) {
      showToast('Failed to save profile', true);
    }

    hideLoading();
  }

  async function changePassword() {
    const current = document.getElementById('profCurrentPw')?.value || '';
    const newPw = (document.getElementById('profNewPw')?.value || '').trim();
    const confirmPw = (document.getElementById('profConfirmPw')?.value || '').trim();

    if (!newPw) {
      showToast('Please enter a new password', true);
      return;
    }
    if (newPw !== confirmPw) {
      const err = document.getElementById('pwMatchError');
      if (err) err.classList.remove('hidden');
      showToast('Passwords do not match', true);
      return;
    }
    const err = document.getElementById('pwMatchError');
    if (err) err.classList.add('hidden');

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
        if (document.getElementById('profCurrentPw')) document.getElementById('profCurrentPw').value = '';
        if (document.getElementById('profNewPw')) document.getElementById('profNewPw').value = '';
        if (document.getElementById('profConfirmPw')) document.getElementById('profConfirmPw').value = '';
      }
    } catch (e) {
      showToast('Failed to change password', true);
    }

    hideLoading();
  }

  async function selectPlan(plan) {
    // Enterprise — open About modal to contact section
    if (plan === 'Enterprise') { openAboutContact(); return; }
    if (plan === _currentSubPlan) {
      if (_paddleScheduledChange === 'cancel' || _paddleScheduledChange === 'downgrade') { window._reactivateSub(); }
      return;
    }

    // Show loading modal
    var rankMap = { Basic: 0, Standard: 1, Pro: 2, Enterprise: 3 };
    var actionLabel = (rankMap[plan] > rankMap[_currentSubPlan]) ? 'Upgrading' : 'Switching';
    _openSubModal(actionLabel + ' to ' + plan + '...',
      '<div class="sub-modal-spinner"><p>Calculating your price...</p></div>', '');

    try {
      var res = await fetch('/api/paddle/subscription/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan: plan })
      });
      var data = await safeJson(res);
      if (!data || !data.ok) {
        _openSubModal('Error',
          '<div class="sub-modal-highlight error">' + ((data && data.error) || 'Failed to load pricing') + '</div>',
          '<button class="ghost" onclick="window._closeSubModal()">Close</button>');
        return;
      }
      _showPreviewModal(data);
    } catch (e) {
      _openSubModal('Error',
        '<div class="sub-modal-highlight error">Failed to load pricing details.</div>',
        '<button class="ghost" onclick="window._closeSubModal()">Close</button>');
    }
  }

  function _showPreviewModal(data) {
    var ct = data.change_type;

    // New subscription → Paddle checkout
    if (ct === 'new_subscription') {
      _closeSubModal();
      _openPaddleCheckout(data.new_plan);
      return;
    }

    // Admin direct
    if (ct === 'admin_direct') {
      _openSubModal((data.new_plan === 'Basic' ? 'Downgrade to Basic' : 'Change to ' + data.new_plan),
        '<p>' + data.message + '</p>',
        '<button class="primary" onclick="window._confirmAdminChange(\'' + data.new_plan + '\')">Confirm</button>' +
        '<button class="ghost" onclick="window._closeSubModal()">Cancel</button>');
      return;
    }

    // Upgrade
    if (ct === 'upgrade') {
      var body = '<div class="sub-modal-row"><span class="label">Current plan</span><span class="value">' + data.current_plan + ' (' + data.current_price + ')</span></div>' +
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
        body += '<div class="sub-modal-link">Need to change payment method? <a onclick="window._updatePaymentMethod()">Update payment details</a></div>';
      }
      _openSubModal('Upgrade to ' + data.new_plan, body,
        '<button class="primary" onclick="window._confirmPlanChange(\'upgrade\', \'' + data.new_plan + '\')">Confirm Upgrade</button>' +
        '<button class="ghost" onclick="window._closeSubModal()">Cancel</button>');
      return;
    }

    // Downgrade between paid
    if (ct === 'downgrade') {
      var body = '<div class="sub-modal-row"><span class="label">Current plan</span><span class="value">' + data.current_plan + ' (' + data.current_price + ')</span></div>' +
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
      _openSubModal('Downgrade to ' + data.new_plan, body,
        '<button class="primary" onclick="window._confirmPlanChange(\'downgrade\', \'' + data.new_plan + '\')">Confirm Downgrade</button>' +
        '<button class="ghost" onclick="window._closeSubModal()">Keep ' + data.current_plan + '</button>');
      return;
    }

    // Cancel (Paid → Basic)
    if (ct === 'cancel') {
      var body = '<div class="sub-modal-row"><span class="label">Current plan</span><span class="value">' + data.current_plan + ' (' + data.current_price + ')</span></div>' +
        '<hr class="sub-modal-divider">' +
        '<div class="sub-modal-highlight warning">' + data.message + '</div>' +
        '<p style="font-size:13px;color:#6b7280;margin-top:8px;">After that, you\'ll switch to the free Basic plan with:</p>' +
        '<ul class="sub-modal-features"><li>1 file per upload</li><li>10 messages per day</li><li>1 PDF report/month</li></ul>';
      _openSubModal('Downgrade to Basic', body,
        '<button class="primary" onclick="window._confirmPlanChange(\'cancel\', \'Basic\')">Confirm Downgrade</button>' +
        '<button class="ghost" onclick="window._closeSubModal()">Keep Plan</button>');
      return;
    }
  }

  window._confirmPlanChange = async function(changeType, plan) {
    _subBody().innerHTML = '<div class="sub-modal-spinner"><p>Processing...</p></div>';
    _subActions().innerHTML = '';
    try {
      var endpoint, body = {};
      if (changeType === 'cancel') { endpoint = '/api/paddle/subscription/cancel'; }
      else if (changeType === 'undo_cancel') { endpoint = '/api/paddle/subscription/reactivate'; }
      else { endpoint = '/api/paddle/subscription/update'; body = { plan: plan }; }

      var res = await fetch(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
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
          msg = 'You\'ll switch to ' + plan + ' on ' + dateStr + '. You keep all ' + _currentSubPlan + ' features until then.';
          title = 'Downgrade Scheduled';
        } else {
          msg = 'You\'re now on the ' + plan + ' plan.';
        }
        _openSubModal(title,
          '<div class="sub-modal-highlight success">' + msg + '</div>',
          '<button class="primary" onclick="window._closeSubModal(); location.reload();">Done</button>');
      } else {
        _openSubModal('Error',
          '<div class="sub-modal-highlight error">' + ((data && data.error) || 'Failed to process plan change') + '</div>',
          '<button class="ghost" onclick="window._closeSubModal()">Close</button>');
      }
    } catch (e) {
      _openSubModal('Error',
        '<div class="sub-modal-highlight error">Failed to process plan change.</div>',
        '<button class="ghost" onclick="window._closeSubModal()">Close</button>');
    }
  };

  window._confirmAdminChange = async function(plan) {
    _subBody().innerHTML = '<div class="sub-modal-spinner"><p>Processing...</p></div>';
    _subActions().innerHTML = '';
    try {
      var res = await fetch('/auth/subscription', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan: plan })
      });
      var data = await safeJson(res);
      if (data && data.error) {
        _openSubModal('Error',
          '<div class="sub-modal-highlight error">' + data.error + '</div>',
          '<button class="ghost" onclick="window._closeSubModal()">Close</button>');
      } else {
        _openSubModal('Plan Updated',
          '<div class="sub-modal-highlight success">Switched to ' + plan + ' plan.</div>',
          '<button class="primary" onclick="window._closeSubModal(); location.reload();">Done</button>');
      }
    } catch (e) {
      _openSubModal('Error',
        '<div class="sub-modal-highlight error">Failed to change plan</div>',
        '<button class="ghost" onclick="window._closeSubModal()">Close</button>');
    }
  };

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

  // ============================================
  // Try Now Button
  // ============================================
  const btnTryNow = document.getElementById('btnTryNow');
  if (btnTryNow) {
    btnTryNow.addEventListener('click', () => {
      if (isLoggedIn) {
        window.location.href = '/lab';
      } else {
        openAuthModal('register');
      }
    });
  }

  // ============================================
  // Get Started Free Button (same as Try Now)
  // ============================================
  const btnGetStartedFree = document.getElementById('btnGetStartedFree');
  if (btnGetStartedFree) {
    btnGetStartedFree.addEventListener('click', () => {
      if (isLoggedIn) {
        window.location.href = '/lab';
      } else {
        openAuthModal('register');
      }
    });
  }

  // ============================================
  // Auth Modal
  // ============================================
  const authModal = document.getElementById('authModal');
  const closeAuthModal = document.getElementById('closeAuthModal');
  const authViewRegister = document.getElementById('authViewRegister');
  const authViewLogin = document.getElementById('authViewLogin');
  const switchToLogin = document.getElementById('switchToLogin');
  const switchToRegister = document.getElementById('switchToRegister');

  function openAuthModal(view = 'register') {
    if (!authModal) return;
    authModal.classList.remove('hidden');
    if (view === 'login') {
      authViewRegister.classList.add('hidden');
      authViewLogin.classList.remove('hidden');
    } else {
      authViewRegister.classList.remove('hidden');
      authViewLogin.classList.add('hidden');
    }
  }
  window.openAuthModal = openAuthModal;

  function closeModal() {
    if (authModal) authModal.classList.add('hidden');
  }

  if (closeAuthModal) {
    closeAuthModal.addEventListener('click', closeModal);
  }

  if (authModal) {
    authModal.addEventListener('click', (e) => {
      if (e.target === authModal) closeModal();
    });
  }

  if (switchToLogin) {
    switchToLogin.addEventListener('click', (e) => {
      e.preventDefault();
      authViewRegister.classList.add('hidden');
      authViewLogin.classList.remove('hidden');
    });
  }

  if (switchToRegister) {
    switchToRegister.addEventListener('click', (e) => {
      e.preventDefault();
      authViewLogin.classList.add('hidden');
      authViewRegister.classList.remove('hidden');
    });
  }

  // ============================================
  // Google Auth Buttons
  // ============================================
  const btnGoogleSignUp = document.getElementById('btnGoogleSignUp');
  const btnGoogleSignIn = document.getElementById('btnGoogleSignIn');

  if (btnGoogleSignUp) {
    btnGoogleSignUp.addEventListener('click', () => {
      window.location.href = '/auth/google/login';
    });
  }

  if (btnGoogleSignIn) {
    btnGoogleSignIn.addEventListener('click', () => {
      window.location.href = '/auth/google/login';
    });
  }

  // ============================================
  // Registration Form
  // ============================================
  const btnDoRegister = document.getElementById('btnDoRegister');
  const regFullName = document.getElementById('regFullName');
  const regEmail = document.getElementById('regEmail');
  const regUsername = document.getElementById('regUsername');
  const regPassword = document.getElementById('regPassword');
  const regPasswordConfirm = document.getElementById('regPasswordConfirm');
  const authRegisterMsg = document.getElementById('authRegisterMsg');

  if (btnDoRegister) {
    btnDoRegister.addEventListener('click', async () => {
      const fullName = (regFullName?.value || '').trim();
      const email = (regEmail?.value || '').trim();
      const username = (regUsername?.value || '').trim();
      const password = regPassword?.value || '';
      const passwordConfirm = regPasswordConfirm?.value || '';

      if (!username || !password) {
        showMessage(authRegisterMsg, 'Username and password are required', 'error');
        return;
      }

      if (password !== passwordConfirm) {
        showMessage(authRegisterMsg, 'Passwords do not match', 'error');
        return;
      }

      showMessage(authRegisterMsg, 'Creating account...', '');

      try {
        const res = await fetch('/auth/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username,
            password,
            email: email || null,
            full_name: fullName || null
          })
        });

        const data = await safeJson(res);

        if (res.ok && data?.ok) {
          showMessage(authRegisterMsg, 'Success! Redirecting...', 'success');
          setTimeout(() => {
            window.location.href = '/lab';
          }, 500);
        } else {
          showMessage(authRegisterMsg, data?.error || 'Registration failed', 'error');
        }
      } catch (e) {
        showMessage(authRegisterMsg, 'Network error. Please try again.', 'error');
      }
    });
  }

  // ============================================
  // Login Form
  // ============================================
  const btnDoLogin = document.getElementById('btnDoLogin');
  const loginUsername = document.getElementById('loginUsername');
  const loginPassword = document.getElementById('loginPassword');
  const authLoginMsg = document.getElementById('authLoginMsg');

  if (btnDoLogin) {
    btnDoLogin.addEventListener('click', async () => {
      const username = (loginUsername?.value || '').trim();
      const password = loginPassword?.value || '';

      if (!username || !password) {
        showMessage(authLoginMsg, 'Enter username and password', 'error');
        return;
      }

      showMessage(authLoginMsg, 'Signing in...', '');

      try {
        const res = await fetch('/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password })
        });

        const data = await safeJson(res);

        if (res.ok && data?.ok) {
          showMessage(authLoginMsg, 'Success! Redirecting...', 'success');
          setTimeout(() => {
            window.location.href = '/lab';
          }, 500);
        } else {
          showMessage(authLoginMsg, data?.error || 'Login failed', 'error');
        }
      } catch (e) {
        showMessage(authLoginMsg, 'Network error. Please try again.', 'error');
      }
    });
  }

  // Enter key support for login form
  if (loginPassword) {
    loginPassword.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') btnDoLogin?.click();
    });
  }

  // Enter key support for register form
  if (regPasswordConfirm) {
    regPasswordConfirm.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') btnDoRegister?.click();
    });
  }

  function showMessage(el, text, type) {
    if (!el) return;
    el.textContent = text;
    el.className = 'auth-message';
    if (type) el.classList.add(type);
  }

  // ============================================
  // About Us Modal — close handlers only.
  // The navbar "About Us" button now navigates to /about; the modal markup
  // is retained because window.openAboutContact (Enterprise plan "Contact Us"
  // from the profile modal) and the ?about=contact deep-link still open it.
  // ============================================
  const aboutModal = document.getElementById('aboutModal');
  const closeAbout = document.getElementById('closeAbout');

  if (aboutModal && closeAbout) {
    closeAbout.addEventListener('click', () => {
      aboutModal.classList.add('hidden');
    });

    aboutModal.addEventListener('click', (e) => {
      if (e.target === aboutModal) {
        aboutModal.classList.add('hidden');
      }
    });
  }

  // Enterprise "Contact Us" — open About modal scrolled to contact section
  window.openAboutContact = function() {
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
  };

  // ============================================
  // Scroll-reveal animation for content sections
  // ============================================
  if ('IntersectionObserver' in window) {
    const revealObserver = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          revealObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15 });
    document.querySelectorAll('.scroll-reveal').forEach(el => revealObserver.observe(el));
  } else {
    // No IntersectionObserver support — show everything immediately
    document.querySelectorAll('.scroll-reveal').forEach(el => el.classList.add('is-visible'));
  }

  // ============================================
  // Language selector (i18n) — globe icon dropdown (main page only)
  // ============================================
  if (window.i18n) {
    window.i18n.init();
  }
  const langGlobeBtn = document.getElementById('btnLangGlobe');
  const langDropdown = document.getElementById('langDropdown');
  if (langGlobeBtn && langDropdown) {
    // Toggle on globe click
    langGlobeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const willOpen = langDropdown.classList.contains('hidden');
      langDropdown.classList.toggle('hidden');
      langGlobeBtn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    });

    // Close on outside click
    document.addEventListener('click', (e) => {
      if (langDropdown.classList.contains('hidden')) return;
      if (!langDropdown.contains(e.target) && e.target !== langGlobeBtn && !langGlobeBtn.contains(e.target)) {
        langDropdown.classList.add('hidden');
        langGlobeBtn.setAttribute('aria-expanded', 'false');
      }
    });

    // Language selection
    document.querySelectorAll('.lang-dropdown-item').forEach(item => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        const lang = item.dataset.lang;
        if (!lang || !window.i18n) return;
        window.i18n.setLang(lang);
        document.querySelectorAll('.lang-dropdown-item').forEach(i => i.classList.remove('active'));
        item.classList.add('active');
        langDropdown.classList.add('hidden');
        langGlobeBtn.setAttribute('aria-expanded', 'false');
      });
    });

    // Initial active state from stored language
    if (window.i18n) {
      const _currentLang = window.i18n.currentLang;
      document.querySelectorAll('.lang-dropdown-item').forEach(i => {
        if (i.dataset.lang === _currentLang) i.classList.add('active');
      });
    }
  }

  // ============================================
  // Surface OAuth configuration issues
  // ============================================
  try {
    const params = new URLSearchParams(window.location.search);
    const oauth = params.get('oauth');
    if (oauth === 'google_not_configured') {
      const msg = authRegisterMsg || authLoginMsg;
      if (msg) {
        showMessage(msg, 'Google sign-in is not configured.', 'error');
      }
    }
    // Auto-open About modal to Contact section (from pricing page Enterprise button)
    if (params.get('about') === 'contact') {
      setTimeout(function() { openAboutContact(); }, 300);
    }
  } catch {}
})();
