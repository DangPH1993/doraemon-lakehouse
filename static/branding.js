/* v0.7.59: apply the admin-managed site logo and favicon across public and admin pages. */
(() => {
  const FALLBACK_LOGO = '/static/site-logo.png?v=0.7.59';
  const FALLBACK_FAVICON = '/static/favicon.png?v=0.7.59';
  function apply(cfg) {
    if (!cfg) return;
    const logoUrl = cfg.logo_url || FALLBACK_LOGO;
    const iconUrl = cfg.favicon_url || FALLBACK_FAVICON;
    const wordmark = cfg.logo_mode === 'wordmark';
    document.querySelectorAll('[data-site-logo]').forEach((img) => {
      img.classList.toggle('wordmark', wordmark);
      img.onerror = () => { img.onerror = null; img.src = FALLBACK_LOGO; };
      if (img.getAttribute('src') !== logoUrl) img.setAttribute('src', logoUrl);
    });
    document.querySelectorAll('[data-brand-copy]').forEach((el) => {
      el.hidden = wordmark;
    });
    const favicon = document.querySelector('#siteFavicon');
    if (favicon) {
      if (favicon.getAttribute('href') !== iconUrl) favicon.setAttribute('href', iconUrl);
      favicon.type = 'image/png';
      favicon.setAttribute('sizes', '192x192');
    }
    const apple = document.querySelector('#siteAppleTouchIcon');
    if (apple && apple.getAttribute('href') !== iconUrl) apple.setAttribute('href', iconUrl);
  }
  window.applySiteBranding = apply;
  fetch('/api/public/site-branding', {headers:{Accept:'application/json'}, cache:'no-store'})
    .then((response) => response.ok ? response.json() : null)
    .then(apply)
    .catch(() => {});
})();
