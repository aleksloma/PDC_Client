/* /new page — mobile hamburger menu (Pricing / Data Truth / About Us).
   Self-contained, vanilla JS, no dependencies. */
(function () {
  'use strict';

  function init() {
    var btn = document.getElementById('mobileMenuBtn');
    var panel = document.getElementById('mobileMenuPanel');
    if (!btn || !panel) return;

    function setOpen(open) {
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      panel.setAttribute('aria-hidden', open ? 'false' : 'true');
      panel.classList.toggle('open', open);
      panel.classList.toggle('hidden', !open);
    }

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = btn.getAttribute('aria-expanded') === 'true';
      setOpen(!open);
    });

    // Close after a link click (so the route transition feels clean).
    Array.prototype.forEach.call(panel.querySelectorAll('.nav-mobile-link'), function (a) {
      a.addEventListener('click', function () { setOpen(false); });
    });

    // Close when tapping outside the panel/button.
    document.addEventListener('click', function (e) {
      if (panel.classList.contains('open') &&
          !panel.contains(e.target) &&
          !btn.contains(e.target)) {
        setOpen(false);
      }
    });

    // Close on Escape.
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && panel.classList.contains('open')) {
        setOpen(false);
        btn.focus();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
