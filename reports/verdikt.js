// Verdikt dashboard shared behavior.
// On mobile, inject a hamburger button into the top nav and wire it
// to toggle the .crossnav drawer. Zero effect on desktop (CSS keeps
// the button hidden above 820px).
(function () {
  function init() {
    var nav = document.querySelector('.container.nav');
    var crossnav = document.querySelector('.crossnav');
    if (!nav || !crossnav) return;
    if (nav.querySelector('.nav-hamburger')) return; // already injected

    var btn = document.createElement('button');
    btn.className = 'nav-hamburger';
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Toggle navigation');
    btn.setAttribute('aria-expanded', 'false');
    btn.innerHTML = '<span></span><span></span><span></span>';

    var brand = nav.querySelector('.brand');
    if (brand && brand.nextSibling) {
      nav.insertBefore(btn, brand.nextSibling);
    } else {
      nav.appendChild(btn);
    }

    function close() {
      crossnav.classList.remove('open');
      btn.classList.remove('open');
      btn.setAttribute('aria-expanded', 'false');
    }

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var isOpen = crossnav.classList.toggle('open');
      btn.classList.toggle('open', isOpen);
      btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    });

    // Close when a link is tapped (single-page-app-feel on mobile).
    crossnav.addEventListener('click', function (e) {
      if (e.target && e.target.tagName === 'A') close();
    });

    // Close on outside tap.
    document.addEventListener('click', function (e) {
      if (!crossnav.classList.contains('open')) return;
      if (!nav.contains(e.target)) close();
    });

    // Close on Escape.
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') close();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
