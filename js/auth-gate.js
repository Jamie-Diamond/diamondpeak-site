/* =============================================
   maji auth-gate — lightweight client-side password wall

   Usage: add to any page's <head>:
     <script src="js/auth-gate.js" data-password-hash="YOUR_SHA256_HASH"></script>

   Generate a hash:  echo -n "yourpassword" | shasum -a 256
   Default password: maji2024
   ============================================= */

(function () {
  'use strict';

  // Default hash = sha256("maji2024")
  var scriptTag = document.currentScript;
  var HASH = scriptTag?.getAttribute('data-password-hash')
    || '5966b4dd75d2433be589e3875cb7cc36025b8b9f9307a08be600b2bb173e4773';
  var SESSION_KEY = 'maji_auth';

  // Check if already authed this session
  if (sessionStorage.getItem(SESSION_KEY) === HASH) return;

  async function sha256(str) {
    var buf = new TextEncoder().encode(str);
    var hash = await crypto.subtle.digest('SHA-256', buf);
    return Array.from(new Uint8Array(hash)).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
  }

  // Hide page content immediately
  document.documentElement.style.overflow = 'hidden';

  function createGate() {
    var overlay = document.createElement('div');
    overlay.id = 'maji-auth-gate';
    overlay.innerHTML = [
      '<div class="mag-backdrop"></div>',
      '<div class="mag-card">',
      '  <div class="mag-lock">',
      '    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">',
      '      <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>',
      '      <path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
      '    </svg>',
      '  </div>',
      '  <h2 class="mag-title">This page is protected</h2>',
      '  <p class="mag-sub">Enter the password to continue</p>',
      '  <form class="mag-form" autocomplete="off">',
      '    <input class="mag-input" type="password" placeholder="Password" autocomplete="off" autofocus />',
      '    <button class="mag-btn" type="submit">Unlock</button>',
      '  </form>',
      '  <p class="mag-error"></p>',
      '  <p class="mag-footer">maji by majaco</p>',
      '</div>',
    ].join('\n');

    var style = document.createElement('style');
    style.textContent = [
      '#maji-auth-gate { position:fixed; inset:0; z-index:999999; display:flex; align-items:center; justify-content:center; font-family:"Plus Jakarta Sans",system-ui,-apple-system,sans-serif; }',
      '.mag-backdrop { position:absolute; inset:0; background:#081C15; }',
      '.mag-card { position:relative; width:100%; max-width:380px; margin:1rem; padding:2.5rem 2rem 2rem; background:#0D2818; border:1px solid rgba(183,228,199,.12); border-radius:16px; text-align:center; animation:magIn .4s cubic-bezier(.16,1,.3,1) both; }',
      '@keyframes magIn { from { opacity:0; transform:translateY(12px) scale(.97); } to { opacity:1; transform:none; } }',
      '.mag-lock { display:inline-flex; align-items:center; justify-content:center; width:52px; height:52px; margin:0 auto 1.25rem; border-radius:50%; background:rgba(45,106,79,.25); color:#74A68D; }',
      '.mag-title { font-size:1.25rem; font-weight:600; color:#fff; margin-bottom:.375rem; letter-spacing:-.01em; }',
      '.mag-sub { font-size:.875rem; color:#74A68D; margin-bottom:1.5rem; }',
      '.mag-form { display:flex; gap:.5rem; }',
      '.mag-input { flex:1; padding:.625rem .875rem; background:rgba(255,255,255,.06); border:1px solid rgba(183,228,199,.15); border-radius:8px; color:#fff; font-size:.9375rem; font-family:inherit; outline:none; transition:border-color .2s; }',
      '.mag-input:focus { border-color:#2D6A4F; }',
      '.mag-input::placeholder { color:rgba(183,228,199,.35); }',
      '.mag-btn { padding:.625rem 1.25rem; background:#2D6A4F; color:#fff; border:none; border-radius:8px; font-size:.875rem; font-weight:600; font-family:inherit; cursor:pointer; transition:background .2s,transform .1s; white-space:nowrap; }',
      '.mag-btn:hover { background:#40916C; }',
      '.mag-btn:active { transform:scale(.97); }',
      '.mag-error { min-height:1.25rem; margin-top:.75rem; font-size:.8125rem; color:#EF4444; transition:opacity .2s; }',
      '.mag-footer { margin-top:1.5rem; font-size:.6875rem; color:rgba(116,166,141,.4); letter-spacing:.04em; text-transform:uppercase; }',
      '.mag-shake { animation:magShake .4s ease; }',
      '@keyframes magShake { 0%,100%{transform:translateX(0)} 20%{transform:translateX(-8px)} 40%{transform:translateX(8px)} 60%{transform:translateX(-4px)} 80%{transform:translateX(4px)} }',
    ].join('\n');

    document.head.appendChild(style);
    document.body.appendChild(overlay);

    var form = overlay.querySelector('.mag-form');
    var input = overlay.querySelector('.mag-input');
    var errEl = overlay.querySelector('.mag-error');
    var card = overlay.querySelector('.mag-card');

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      var val = input.value;
      if (!val) return;

      var h = await sha256(val);
      if (h === HASH) {
        sessionStorage.setItem(SESSION_KEY, HASH);
        overlay.style.transition = 'opacity .25s';
        overlay.style.opacity = '0';
        setTimeout(function () {
          overlay.remove();
          document.documentElement.style.overflow = '';
        }, 260);
      } else {
        errEl.textContent = 'Incorrect password';
        input.value = '';
        input.focus();
        card.classList.remove('mag-shake');
        void card.offsetWidth; // force reflow
        card.classList.add('mag-shake');
      }
    });
  }

  // Wait for body to exist
  if (document.body) {
    createGate();
  } else {
    document.addEventListener('DOMContentLoaded', createGate);
  }
})();
