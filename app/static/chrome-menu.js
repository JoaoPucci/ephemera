// Hamburger drawer toggle for the prototype "?chrome=hamburger" variant.
// Inert when the variant isn't active (the button is display:none) and
// uninstalls cleanly if the button isn't in the DOM.
(() => {
  const btn = document.getElementById('chrome-menu-btn');
  if (!btn) return;

  const root = document.documentElement;

  function setOpen(open) {
    if (open) root.dataset.chromeMenuOpen = 'true';
    else delete root.dataset.chromeMenuOpen;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    setOpen(root.dataset.chromeMenuOpen !== 'true');
  });

  // Outside-click closes the drawer. Chrome items (.top-chrome, .user-btn)
  // are excluded so clicking one of them inside the drawer doesn't immediately
  // dismiss the menu before the action runs.
  document.addEventListener('click', (e) => {
    if (root.dataset.chromeMenuOpen !== 'true') return;
    if (e.target.closest('#chrome-menu-btn')) return;
    if (e.target.closest('.top-chrome')) return;
    if (e.target.closest('.user-btn')) return;
    setOpen(false);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && root.dataset.chromeMenuOpen === 'true') {
      setOpen(false);
      btn.focus();
    }
  });
})();
