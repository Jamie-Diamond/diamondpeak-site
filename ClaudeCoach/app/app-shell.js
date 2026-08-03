/* ClaudeCoach app shell.
 *
 * Injects the tab bar, registers the service worker and shows an offline pill. Written as
 * one dependency-free file included on every page, rather than pasted into seven pages,
 * so the chrome has ONE definition - the pages stay self-contained for their own content.
 *
 * Everything here is additive and fails soft: if this script does not run, the pages are
 * exactly as they were before.
 *
 * The Chat tab deep-links to Telegram rather than opening an in-page chat. That is
 * deliberate for now - the site is static on GitHub Pages with no backend, so there is
 * nothing for a web chat to talk to (see docs/app-transition-plan.md). When the FastAPI
 * backend lands, this one href becomes an internal route and nothing else changes.
 */
(function () {
  'use strict';

  var TELEGRAM = 'https://t.me/ClaudeCoachTri_bot';

  var TABS = [
    {
      label: 'Home',
      href: 'index.html',
      // Inline SVG, not an icon font or sprite: no extra request, and it inherits
      // currentColor so the active state needs no second asset.
      icon: '<path d="M3 10.5 12 4l9 6.5"/><path d="M5.5 9.5V20h13V9.5"/>'
    },
    {
      label: 'Coach',
      href: 'coach.html',
      icon: '<path d="M4 19V6"/><path d="M4 8h9l-1.5 3L13 14H4"/><circle cx="18" cy="18" r="2.5"/>'
    },
    {
      label: 'Trends',
      href: 'training-visualiser.html',
      icon: '<path d="M4 19h16"/><path d="M4 15l4.5-5 3.5 3.5L20 6"/>'
    },
    {
      label: 'Chat',
      href: TELEGRAM,
      external: true,
      cls: 'tab-chat',
      icon: '<path d="M21 4 3 11l5 2 2 5 3-4 5 3z"/>'
    }
  ];

  function currentFile() {
    var p = window.location.pathname;
    var f = p.substring(p.lastIndexOf('/') + 1);
    return f === '' ? 'index.html' : f;
  }

  // Athlete pages have no tab of their own; they are reached from Home, so Home stays lit
  // rather than leaving the bar with nothing active (which reads as a broken state).
  function activeFor(file) {
    if (file === 'coach.html') return 'coach.html';
    if (file === 'training-visualiser.html') return 'training-visualiser.html';
    return 'index.html';
  }

  function buildTabBar() {
    if (document.querySelector('.app-tabbar')) return;

    var active = activeFor(currentFile());
    var nav = document.createElement('nav');
    nav.className = 'app-tabbar';
    nav.setAttribute('role', 'navigation');
    nav.setAttribute('aria-label', 'App sections');

    TABS.forEach(function (t) {
      var a = document.createElement('a');
      a.href = t.href;
      if (t.cls) a.className = t.cls;
      if (t.external) {
        a.target = '_blank';
        a.rel = 'noopener';
      } else if (t.href === active) {
        a.setAttribute('aria-current', 'page');
      }
      a.innerHTML =
        '<svg viewBox="0 0 24 24" aria-hidden="true">' + t.icon + '</svg>' +
        '<span>' + t.label + '</span>';
      nav.appendChild(a);
    });

    document.body.appendChild(nav);
    document.body.classList.add('has-app-shell');
  }

  function offlinePill() {
    var pill = document.createElement('div');
    pill.className = 'app-offline';
    pill.textContent = 'Offline — showing last loaded data';
    pill.setAttribute('role', 'status');
    document.body.appendChild(pill);

    function sync() {
      pill.classList.toggle('show', !navigator.onLine);
    }
    window.addEventListener('online', sync);
    window.addEventListener('offline', sync);
    sync();
  }

  function registerWorker() {
    if (!('serviceWorker' in navigator)) return;
    // Only over HTTPS (or localhost) - a registration attempt on file:// throws.
    if (location.protocol !== 'https:' && location.hostname !== 'localhost') return;
    // Scope is the ClaudeCoach directory, which is why sw.js sits there and not in app/.
    navigator.serviceWorker.register('sw.js', { scope: './' }).catch(function (e) {
      // Never let a failed registration break the page; the site works without it.
      if (window.console) console.warn('[app-shell] service worker not registered:', e);
    });
  }

  function init() {
    try {
      buildTabBar();
      offlinePill();
    } catch (e) {
      if (window.console) console.warn('[app-shell] chrome failed:', e);
    }
    registerWorker();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
