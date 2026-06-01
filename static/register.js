(function(){
  const u = document.getElementById('regUsername');
  const f = document.getElementById('regFullName');
  const e = document.getElementById('regEmail');
  const p = document.getElementById('regPassword');
  const b = document.getElementById('btnDoRegister');
  const c = document.getElementById('btnCancel');
  const msg = document.getElementById('regMsg');

  async function safeJson(res){ try { return await res.json(); } catch { return null; } }

  async function submit(){
    const payload = {
      username: (u.value||'').trim(),
      password: p.value||'',
      email: (e.value||'').trim() || null,
      full_name: (f.value||'').trim() || null,
    };
    if (!payload.username || !payload.password){ msg.textContent = 'Username and password are required.'; return; }
    msg.textContent = 'Creating account…';
    const res = await fetch('/auth/register', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const js = await safeJson(res);
    if (res.ok && js && js.ok){
      msg.textContent = 'Done. You can close this window.';
      try { if (window.opener) window.opener.location.href = '/lab'; } catch {}
      window.close();
    } else {
      msg.textContent = (js && js.error) || 'Registration failed';
    }
  }

  b.addEventListener('click', submit);
  c.addEventListener('click', ()=>{ window.close(); });
})();
