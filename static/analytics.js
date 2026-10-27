/* Public Google Analytics 4 bootstrap. The measurement ID is supplied by an admin setting. */
(function () {
  'use strict';

  async function initializeGoogleAnalytics() {
    try {
      const response = await fetch('/api/public/analytics-config', {
        method: 'GET',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
        credentials: 'same-origin'
      });
      if (!response.ok) return;
      const config = await response.json();
      const measurementId = String(config.measurement_id || '').trim().toUpperCase();
      if (!/^G-[A-Z0-9]{4,20}$/.test(measurementId)) return;
      if (window.__TINTINTUC_GA4_ID === measurementId) return;

      window.__TINTINTUC_GA4_ID = measurementId;
      window.dataLayer = window.dataLayer || [];
      window.gtag = window.gtag || function () { window.dataLayer.push(arguments); };
      window.gtag('js', new Date());
      window.gtag('config', measurementId, { send_page_view: true });

      const script = document.createElement('script');
      script.async = true;
      script.src = 'https://www.googletagmanager.com/gtag/js?id=' + encodeURIComponent(measurementId);
      script.onerror = function () { console.warn('Google Analytics script could not be loaded.'); };
      document.head.appendChild(script);
    } catch (error) {
      // Analytics is optional: never block or degrade the News Radar page if it is unavailable.
      console.debug('Google Analytics is not configured or could not initialize.');
    }
  }

  initializeGoogleAnalytics();
})();
