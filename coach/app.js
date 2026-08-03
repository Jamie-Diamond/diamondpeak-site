/* Peak — view logic.
 *
 * The published data is fetched from ../ClaudeCoach/public/. That directory name is
 * the ENGINE's, not the app's: refresh-site-data.py on the VM writes there, the seven
 * legacy dashboards read from there, and _config.yml's publish-time exclusions are
 * written against it. Moving the app's URL to /coach/ was cheap; moving the data path
 * means touching the nightly pipeline and every legacy page, so it is deliberately a
 * separate change. The path is invisible to anyone using the app.
 *
 * Static by design: it reads the nightly public subset (public/training-data-<slug>.json)
 * and the published session library (public/session-library.json) and renders five views.
 * There is no backend to talk to (GitHub Pages), so Chat deep-links to Telegram; when
 * FastAPI lands, that one href becomes a route.
 *
 * Dependencies are Chart.js plus, for the charts, hammer.js and chartjs-plugin-zoom -
 * all from the CDN the legacy pages already use, so the service worker caches them once.
 * hammer.js is not optional decoration: chartjs-plugin-zoom v2 does pinch and touch-pan
 * through it, and this app is used on a phone.
 *
 * LAYOUT NOTE (why it looks the way it does). The first cut gave every section the same
 * weight - eyebrow, sheet, repeat - which read as busy AND sparse at once: six equal
 * blocks with nothing leading, each with lots of air and little density. So:
 *   Today    one hero (the session), one figure strip, one compact signals row.
 *   Trends   a segmented sub-nav; ONE chart at a time, with its supporting numbers
 *            directly under it, rather than five charts stacked into a long scroll.
 *   Calendar an actual month grid, not two lists.
 */
(function () {
  'use strict';

  var ATHLETES = [
    { slug: 'jamie', name: 'Jamie' },
    { slug: 'kathryn', name: 'Kathryn' },
    { slug: 'calum', name: 'Calum' }
  ];

  var TELEGRAM = 'https://t.me/ClaudeCoachTri_bot';

  var TABS = [
    { id: 'today', label: 'Today', icon: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l2.5 2"/>' },
    { id: 'cal', label: 'Calendar', icon: '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3.5v3M16 3.5v3"/>' },
    // Centre, raised and filled: it is the primary action, so it gets the position the
    // thumb reaches without moving. A link out to Telegram until FastAPI lands.
    { id: 'chat', label: 'Chat', href: TELEGRAM, primary: true,
      icon: '<path d="M21 4 3 11l5 2 2 5 3-4 5 3z"/>' },
    { id: 'trends', label: 'Trends', icon: '<path d="M4 19h16"/><path d="M4 15l4.5-5L12 13.5 20 6"/>' },
    { id: 'goals', label: 'Goals', icon: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.4"/><path d="M12 4v2.5M12 17.5V20M4 12h2.5M17.5 12H20"/>' },
    // Settings has no bar slot: it is entered from the masthead gear. It still needs a
    // TABS entry because show()/skeleton() drive every view from this list.
    { id: 'set', label: 'Settings', offBar: true,
      icon: '<circle cx="12" cy="12" r="3.2"/><path d="M12 3.5v2M12 18.5v2M3.5 12h2M18.5 12h2M6 6l1.5 1.5M16.5 16.5 18 18M18 6l-1.5 1.5M7.5 16.5 6 18"/>' }
  ];

  // Each entry is one chart plus the numbers that belong with it.
  var TRENDS = [
    { id: 'fit',  label: 'Fitness' },
    { id: 'load', label: '±7 days' },
    { id: 'heat', label: 'Heat' },
    { id: 'fuel', label: 'Fuel' },
    { id: 'plan', label: 'Plan' }
  ];

  var SPORT = { Swim: 'swim', Ride: 'bike', VirtualRide: 'bike', GravelRide: 'bike',
                Run: 'run', Brick: 'run', WeightTraining: 'strength', Workout: 'strength' };

  var C = { ink: '#18160f', ink2: '#4a4535', muted: '#9a9080', rule: '#ddd8cc',
            green: '#1d6840', blue: '#1a5276', amber: '#b7791f', red: '#b91c1c',
            paper: '#f8f5ef' };

  var state = {
    slug: 'jamie', tab: 'today', data: null, lib: null,
    chart: null, trend: 'fit', fitSport: 'all',
    calMonth: null, calDay: null, libGroup: null
  };

  // Coarse pointer => the tooltip is replaced by a fixed readout line.
  var TOUCH = (function () {
    try { return matchMedia('(hover: none)').matches; } catch (e) { return false; }
  })();

  var $ = function (s) { return document.querySelector(s); };
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  /* ── helpers ─────────────────────────────────────────────────────────── */

  function hhmm(min) {
    if (min == null) return '—';
    var m = Math.round(min), h = Math.floor(m / 60);
    return h ? h + 'h' + String(m % 60).padStart(2, '0') : m + 'm';
  }
  function dow(iso) {
    return new Date(iso + 'T12:00:00').toLocaleDateString('en-GB', { weekday: 'short' });
  }
  function dnum(iso) { return new Date(iso + 'T12:00:00').getDate(); }
  function todayISO() {
    // Local date, not toISOString(): that converts to UTC and rolls the date over
    // an hour early through a British summer evening.
    var d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
           '-' + String(d.getDate()).padStart(2, '0');
  }
  function daysBetween(a, b) {
    return Math.round((new Date(b + 'T12:00:00') - new Date(a + 'T12:00:00')) / 864e5);
  }
  function sportClass(s) { return 'sp-' + (SPORT[s] || 'other'); }
  function signed(n, dp) {
    var v = Number(n).toFixed(dp == null ? 1 : dp);
    return (Number(n) >= 0 ? '+' : '') + v;
  }
  function monthName(y, m) {
    return new Date(y, m, 1).toLocaleDateString('en-GB', { month: 'long', year: 'numeric' });
  }

  // Day-of-year. Three seasons are overlaid on ONE linear x axis so they can be
  // compared, and a linear axis (rather than a time axis) means no date adapter and
  // zoom/pan that behaves. Aligning by calendar date is the right comparison for a
  // triathlon season: the races sit within three weeks of each other.
  function doy0(iso) {
    return Math.round(Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1,
                               +iso.slice(8, 10)) / 864e5);
  }
  function doy(iso) {
    var y = +iso.slice(0, 4);
    return Math.round((Date.UTC(y, +iso.slice(5, 7) - 1, +iso.slice(8, 10)) -
                       Date.UTC(y, 0, 1)) / 864e5) + 1;
  }
  function doyLabel(v, dense) {
    var d = new Date(Date.UTC(2001, 0, 1) + (Math.round(v) - 1) * 864e5);
    return dense
      ? d.getUTCDate() + ' ' + d.toLocaleDateString('en-GB', { month: 'short', timeZone: 'UTC' })
      : d.toLocaleDateString('en-GB', { month: 'short', timeZone: 'UTC' });
  }

  /* ── chrome ──────────────────────────────────────────────────────────── */

  function buildChrome() {
    $('#tabs').innerHTML = TABS.filter(function (t) { return !t.offBar; }).map(function (t) {
      var inner = '<svg viewBox="0 0 24 24" aria-hidden="true">' + t.icon +
        '</svg><span>' + t.label + '</span>';
      var cls = (t.href ? 'out' : '') + (t.primary ? ' primary' : '');
      return t.href
        ? '<a class="' + cls.trim() + '" href="' + t.href + '" target="_blank" rel="noopener">' +
          inner + '</a>'
        : '<button type="button" role="tab" data-tab="' + t.id + '" aria-selected="' +
          (t.id === state.tab) + '">' + inner + '</button>';
    }).join('');

    $('#gear').onclick = function () { show('set'); };
    $('#whoName').onclick = openGate;
    $('#tabs').onclick = function (e) {
      var b = e.target.closest('button');
      if (b) show(b.dataset.tab);
    };

    document.addEventListener('click', function (e) {
      if (e.target.closest('.dr-b') || e.target.closest('.dr-h')) return;
      var row = e.target.closest('.sesh.tap');
      if (row && row.dataset.d) openDetail(row.dataset.d, row.dataset.sp, row.dataset.n);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeDetail();
      if ((e.key === 'Enter' || e.key === ' ') && document.activeElement) {
        var row = document.activeElement.closest && document.activeElement.closest('.sesh.tap');
        if (row && row.dataset.d) { e.preventDefault(); openDetail(row.dataset.d, row.dataset.sp, row.dataset.n); }
      }
    });
    $('#scrim').onclick = closeDetail;

    function net() { $('#off').classList.toggle('on', !navigator.onLine); }
    addEventListener('online', net); addEventListener('offline', net); net();
  }

  function show(tab) {
    state.tab = tab;
    location.hash = tab;
    TABS.forEach(function (t) {
      if (t.href) return;                 // link-out tab: no view to toggle
      $('#v-' + t.id).classList.toggle('on', t.id === tab);
      var b = $('#tabs button[data-tab="' + t.id + '"]');
      if (b) b.setAttribute('aria-selected', String(t.id === tab));
    });
    $('#gear').setAttribute('aria-pressed', String(tab === 'set'));
    scrollTo({ top: 0, behavior: 'instant' });
    // Charts are drawn only while visible: a canvas sized inside a display:none
    // section comes out 0px wide and stays that way.
    if (tab === 'trends') drawTrend();
  }

  /* ── shared bits ─────────────────────────────────────────────────────── */

  function chatCTA(sub) {
    return '<a class="chat" href="' + TELEGRAM + '" target="_blank" rel="noopener">' +
      '<svg viewBox="0 0 24 24"><path d="M21 4 3 11l5 2 2 5 3-4 5 3z"/></svg>' +
      '<span class="txt"><span class="t">Ask the coach</span>' +
      '<span class="s">' + esc(sub) + '</span></span>' +
      '<span class="go">→</span></a>';
  }

  function card(title, body, opts) {
    opts = opts || {};
    return '<section class="card' + (opts.flush ? ' flush' : '') + '">' +
      '<header class="card-h"><h2>' + esc(title) + '</h2>' +
      (opts.action || '') + '</header>' + body +
      (opts.foot ? '<p class="card-f">' + esc(opts.foot) + '</p>' : '') + '</section>';
  }

  function fig(n, label, detail, cls) {
    return '<div class="fig"><span class="n ' + (cls || '') + '">' + esc(n) + '</span>' +
      '<span class="l">' + esc(label) + '</span><span class="d">' + esc(detail) + '</span></div>';
  }

  function mini(v, label, cls) {
    return '<div class="mini' + (cls ? ' ' + cls : '') + '"><b>' + esc(v) + '</b>' +
      '<span>' + esc(label) + '</span></div>';
  }

  function seshRow(s) {
    var done = s.status === 'completed';
    return '<div class="sesh tap' + (done ? ' done' : '') + '" role="button" tabindex="0"' +
      ' data-d="' + esc(s.date || '') + '" data-sp="' + esc(s.sport || '') +
      '" data-n="' + esc(s.name || '') + '">' +
      '<span class="tick">' + (done ? '✓' : '○') + '</span>' +
      '<span class="body"><span class="nm"><span class="sp ' + sportClass(s.sport) + '"></span>' +
      esc(s.name || s.sport) + (s.key ? '<span class="keymark">KEY</span>' : '') + '</span>' +
      (s.detail ? '<span class="meta">' + esc(s.detail) + '</span>' : '') + '</span>' +
      '<span class="rt"><b>' + hhmm(s.duration_min) + '</b>' +
      (s.tss != null ? Math.round(s.tss) + ' tss' : '') + '</span></div>';
  }

  function groupByDay(rows) {
    var t = todayISO(), by = {}, order = [];
    rows.forEach(function (s) {
      if (!by[s.date]) { by[s.date] = []; order.push(s.date); }
      by[s.date].push(s);
    });
    return order.map(function (dt) {
      var items = by[dt];
      var mins = items.reduce(function (a, s) { return a + (s.duration_min || 0); }, 0);
      var tss = items.reduce(function (a, s) { return a + (s.tss || 0); }, 0);
      return '<div class="day"><div class="day-h' + (dt === t ? ' today' : '') + '">' +
        '<span class="dow">' + esc(dow(dt)) + '</span><span class="dnum">' + dnum(dt) + '</span>' +
        '<span class="sum">' + hhmm(mins) + ' · ' + Math.round(tss) + ' tss</span></div>' +
        items.map(seshRow).join('') + '</div>';
    }).join('');
  }

  /* ── Today ───────────────────────────────────────────────────────────── */

  function renderToday() {
    var d = state.data, k = d.kpi || {};
    var t = todayISO();
    var cal = (d.weekCalendar || []);
    var today = cal.filter(function (s) { return s.date === t; });
    var next = cal.filter(function (s) { return s.date > t; }).slice(0, 4);
    var ha = d.heatAccl || {};
    var ramp = k.ramp7d;

    var h = '';

    // The hero is the session, because that is the question the app is opened to
    // answer. The numbers sit under it, not above it.
    h += '<section class="hero">' +
      '<p class="hero-k">' + esc(dow(t)) + ' ' + dnum(t) + ' · today</p>' +
      (today.length
        ? today.map(function (s) {
            return '<h2 class="hero-t"><span class="sp ' + sportClass(s.sport) + '"></span>' +
              esc(s.name || s.sport) + '</h2>' +
              '<p class="hero-m">' + esc([hhmm(s.duration_min),
                s.tss != null ? Math.round(s.tss) + ' tss' : null,
                s.detail].filter(Boolean).join(' · ')) + '</p>' +
              (s.status === 'completed' ? '<p class="hero-done">✓ Completed</p>' : '');
          }).join('<hr class="hero-r">')
        : '<h2 class="hero-t">Rest day</h2><p class="hero-m">Nothing scheduled.</p>') +
      '</section>';

    h += '<div class="figures">' +
      fig(Number(k.ctl).toFixed(1), 'Fitness', 'CTL') +
      fig(Number(k.atl).toFixed(1), 'Fatigue', 'ATL') +
      fig(signed(k.tsb), 'Form', 'TSB', k.tsb >= 0 ? 'pos' : (k.tsb < -25 ? 'neg' : 'flat')) +
      '</div>';

    // Ramp and heat were two full cards each. They are two numbers - so they are
    // two numbers, on one row, with the warning state carried by colour.
    var signals = '';
    if (ramp != null) {
      signals += mini(signed(ramp), 'ramp / wk', Math.abs(ramp) > 5 ? 'warn' : '');
    }
    if (ha.current != null) {
      signals += mini(Number(ha.current).toFixed(0) + '%', 'heat accl');
    }
    if (d.resolvedFtp) signals += mini(d.resolvedFtp + 'w', 'ftp');
    if (signals) h += '<div class="minis">' + signals + '</div>';
    if (ramp != null && Math.abs(ramp) > 5) {
      h += '<p class="flag">Ramp is above the 5-point weekly guide.</p>';
    }

    h += card('Coming up', '<div class="body-flush">' +
      (next.length ? groupByDay(next) : '<div class="empty">Nothing planned yet</div>') +
      '</div>', { flush: true });

    $('#v-today').innerHTML = h;
  }

  /* ── Calendar (month grid) ───────────────────────────────────────────── */

  function calSessions() {
    // The grid draws from the plan AND from logged activity, because a month has
    // both in it. weekCalendar is authoritative where the two overlap (it carries
    // planned/completed status); recent[] fills in days the plan window misses.
    var d = state.data;
    var by = {};
    (d.weekCalendar || []).forEach(function (s) {
      (by[s.date] = by[s.date] || []).push(s);
    });
    (d.recent || []).forEach(function (r) {
      if (by[r.date]) return;
      (by[r.date] = by[r.date] || []).push({
        date: r.date, name: r.name, sport: r.sport, duration_min: r.dur,
        tss: r.tss, status: 'completed',
        detail: [r.pace, r.hr ? r.hr + ' bpm' : null,
                 r.powNp ? r.powNp + 'w np' : null].filter(Boolean).join(' · ')
      });
    });
    return by;
  }

  function renderCalendar() {
    var t = todayISO();
    if (!state.calMonth) state.calMonth = t.slice(0, 7);
    var by = calSessions();
    var y = +state.calMonth.slice(0, 4), m = +state.calMonth.slice(5, 7) - 1;

    var first = new Date(y, m, 1);
    var days = new Date(y, m + 1, 0).getDate();
    var lead = (first.getDay() + 6) % 7;          // Monday-first
    var cells = [];
    for (var i = 0; i < lead; i++) cells.push(null);
    for (var dd = 1; dd <= days; dd++) {
      cells.push(y + '-' + String(m + 1).padStart(2, '0') + '-' + String(dd).padStart(2, '0'));
    }
    while (cells.length % 7) cells.push(null);

    var mins = 0, tss = 0, sessions = 0, rest = 0;
    cells.forEach(function (iso) {
      if (!iso) return;
      var items = by[iso] || [];
      if (!items.length) { if (iso <= t) rest++; return; }
      sessions += items.length;
      items.forEach(function (s) { mins += s.duration_min || 0; tss += s.tss || 0; });
    });

    var grid = '<div class="cal-dows">' +
      ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map(function (x) { return '<span>' + x + '</span>'; }).join('') +
      '</div><div class="cal-grid">' + cells.map(function (iso) {
        if (!iso) return '<span class="cal-cell pad"></span>';
        var items = by[iso] || [];
        var cls = 'cal-cell';
        if (iso === t) cls += ' today';
        if (iso === state.calDay) cls += ' sel';
        if (!items.length && iso < t) cls += ' rest';
        return '<button type="button" class="' + cls + '" data-d="' + iso + '">' +
          '<span class="n">' + dnum(iso) + '</span>' +
          '<span class="dots">' + items.slice(0, 4).map(function (s) {
            return '<i class="dot ' + sportClass(s.sport) +
              (s.status === 'completed' ? ' done' : '') + '"></i>';
          }).join('') + '</span></button>';
      }).join('') + '</div>';

    var nav = '<div class="cal-nav">' +
      '<button type="button" class="cal-mo" data-mo="-1" aria-label="Previous month">‹</button>' +
      '<button type="button" class="cal-mo" data-mo="1" aria-label="Next month">›</button></div>';

    var h = card(monthName(y, m), grid, { action: nav });

    h += '<div class="minis">' + mini(hhmm(mins), 'this month') +
      mini(Math.round(tss), 'tss') + mini(sessions, 'sessions') +
      mini(rest, 'rest days') + '</div>';

    h += weeklyTotals(by, y, m, t);

    var sel = state.calDay && by[state.calDay] ? by[state.calDay] : null;
    if (state.calDay) {
      h += card(dow(state.calDay) + ' ' + dnum(state.calDay) + ' ' +
        new Date(state.calDay + 'T12:00:00').toLocaleDateString('en-GB', { month: 'short' }),
        '<div class="body-flush">' + (sel ? sel.map(seshRow).join('') :
          '<div class="empty">Rest day — nothing recorded</div>') + '</div>', { flush: true });
    } else {
      h += '<p class="hint">Tap a day for its sessions.</p>';
    }

    $('#v-cal').innerHTML = h;

    $('#v-cal').querySelector('.cal-grid').onclick = function (e) {
      var b = e.target.closest('.cal-cell');
      if (!b || !b.dataset.d) return;
      state.calDay = (state.calDay === b.dataset.d) ? null : b.dataset.d;
      renderCalendar();
    };
    $('#v-cal').querySelector('.cal-nav').onclick = function (e) {
      var b = e.target.closest('.cal-mo');
      if (!b) return;
      var nm = new Date(y, m + (+b.dataset.mo), 1);
      state.calMonth = nm.getFullYear() + '-' + String(nm.getMonth() + 1).padStart(2, '0');
      state.calDay = null;
      renderCalendar();
    };
  }

  /* ── Trends ──────────────────────────────────────────────────────────── */

  // Per-week hours / TSS / hours-by-sport for the displayed month. Future weeks are
  // the same figures taken from the plan rather than from completed work, and are
  // labelled as such - a planned week and a done week must never read alike.
  function weeklyTotals(by, y, m, today) {
    var monday = new Date(y, m, 1);
    monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
    var last = new Date(y, m + 1, 0);

    var weeks = [];
    for (var cur = new Date(monday); cur <= last; cur.setDate(cur.getDate() + 7)) {
      var ws = new Date(cur);
      var row = { start: ws, mins: 0, tss: 0, sports: {}, planned: 0, done: 0 };
      for (var i = 0; i < 7; i++) {
        var dd = new Date(ws); dd.setDate(dd.getDate() + i);
        var iso = dd.getFullYear() + '-' + String(dd.getMonth() + 1).padStart(2, '0') +
                  '-' + String(dd.getDate()).padStart(2, '0');
        (by[iso] || []).forEach(function (x) {
          var mn = x.duration_min || 0;
          row.mins += mn;
          row.tss += x.tss || 0;
          var fam = SPORT[x.sport] || 'other';
          row.sports[fam] = (row.sports[fam] || 0) + mn;
          if (x.status === 'completed') row.done += mn; else row.planned += mn;
        });
      }
      if (row.mins > 0) weeks.push(row);
    }
    if (!weeks.length) return '';

    var NAME = { swim: 'Swim', bike: 'Bike', run: 'Run', strength: 'Str', other: 'Other' };
    var body = weeks.map(function (w) {
      var iso = w.start.getFullYear() + '-' + String(w.start.getMonth() + 1).padStart(2, '0') +
                '-' + String(w.start.getDate()).padStart(2, '0');
      var future = w.planned > w.done;
      var split = ['swim', 'bike', 'run', 'strength', 'other'].filter(function (k) {
        return w.sports[k];
      }).map(function (k) {
        return '<span><i class="sp sp-' + k + '"></i>' + NAME[k] + ' ' + hhmm(w.sports[k]) + '</span>';
      }).join('');
      return '<div class="wk"><div class="wk-h"><span class="wk-d">w/c ' +
        w.start.getDate() + ' ' +
        w.start.toLocaleDateString('en-GB', { month: 'short' }) + '</span>' +
        (future ? '<span class="wk-tag">planned</span>' : '') +
        '<span class="wk-n">' + hhmm(w.mins) + '</span>' +
        '<span class="wk-t">' + Math.round(w.tss) + ' tss</span></div>' +
        '<div class="wk-s">' + split + '</div></div>';
    }).join('');

    return card('By week', '<div class="body-flush">' + body + '</div>', { flush: true });
  }

  function renderTrends() {
    var d = state.data;
    var sel = state.trend;

    var seg = '<div class="seg" id="trendSeg" role="tablist">' + TRENDS.map(function (x) {
      return '<button type="button" role="tab" data-t="' + x.id + '" aria-selected="' +
        (x.id === sel) + '">' + esc(x.label) + '</button>';
    }).join('') + '</div>';

    var META = {
      fit:  { title: 'Fitness · three seasons',
              foot: 'CTL by calendar date. Pinch, scroll or drag to zoom; the shaded band is the race-day target.' },
      load: { title: 'Seven days either side',
              foot: 'Bars are daily TSS, faded where still planned. The line is form (TSB).' },
      heat: { title: 'Heat acclimation',
              foot: 'Score decays with a 21-day constant. Dots are logged heat exposures.' },
      fuel: { title: 'Fuelling capacity',
              foot: 'Best carbohydrate rate achieved in the trailing 14 days, bike and run ' +
                    'separately. A gap means nothing was logged in that window.' },
      plan: { title: 'Plan vs actual',
              foot: 'Weekly planned and completed TSS.' }
    }[sel];

    // Overall CTL or one discipline. fitnessBySport carries Ride/Run/Swim for all
    // three seasons, so the whole chart - seasons, race alignment and all - just
    // switches which series it reads.
    var sportBar = '';
    if (sel === 'fit') {
      var avail = Object.keys((d.fitnessBySport || {}).current || {});
      if (avail.length) {
        sportBar = '<div class="seg sub" id="fitSport">' +
          [['all', 'All']].concat(avail.map(function (x) { return [x, x]; }))
          .map(function (o) {
            return '<button type="button" data-s="' + esc(o[0]) + '" aria-selected="' +
              (state.fitSport === o[0]) + '">' + esc(o[1]) + '</button>';
          }).join('') + '</div>';
      }
    }

    var zoomable = (sel === 'fit' || sel === 'heat');
    var action = zoomable
      ? '<button type="button" class="card-a" id="zreset">Reset zoom</button>' : '';

    var h = seg + sportBar + card(META.title +
      (sel === 'fit' && state.fitSport !== 'all' ? ' · ' + state.fitSport : ''),
      '<div class="readout" id="ro"><b>—</b><span></span></div>' +
      '<div class="chartbox tall"><canvas id="c-now"></canvas></div>',
      { action: action, foot: META.foot });

    h += trendExtras(sel, d);
    $('#v-trends').innerHTML = h;

    $('#trendSeg').onclick = function (e) {
      var b = e.target.closest('button');
      if (!b) return;
      state.trend = b.dataset.t;
      renderTrends();
      drawTrend();
    };
    var fs = $('#fitSport');
    if (fs) fs.onclick = function (e) {
      var b = e.target.closest('button');
      if (!b) return;
      state.fitSport = b.dataset.s;
      renderTrends();
      drawTrend();
    };

    var zr = $('#zreset');
    if (zr) zr.onclick = function () { if (state.chart && state.chart.resetZoom) state.chart.resetZoom(); };
  }

  // The numbers that belong with each chart, directly under it. This is where the
  // density goes - a chart alone on a screen is the "sparse" half of the problem.
  function trendExtras(sel, d) {
    var h = '';

    if (sel === 'fit') {
      var bs = (d.fitnessBySport || {}).current || {};
      var sports = Object.keys(bs);
      if (sports.length) {
        h += card('Fitness by sport', '<div class="body-flush">' + sports.map(function (s) {
          var series = bs[s] || [];
          var last = series.length ? series[series.length - 1][1] : 0;
          var max = series.reduce(function (m, r) { return Math.max(m, r[1]); }, 1);
          return '<div class="sesh"><span class="tick"><span class="sp ' + sportClass(s) +
            '" style="margin:0"></span></span><span class="body"><span class="nm">' + esc(s) +
            '</span><div class="bar"><i style="width:' + (last / max * 100).toFixed(0) +
            '%"></i></div></span><span class="rt"><b>' + Number(last).toFixed(1) +
            '</b>peak ' + Number(max).toFixed(0) + '</span></div>';
        }).join('') + '</div>', { flush: true });
      }
      var pc = d.powerCurve || [];
      if (pc.length) {
        var pw = d.powerCurveWindow || {};
        var days = pw.days || 90;
        h += card('Power curve', '<table class="tbl">' +
          '<thead><tr><th>Duration</th><th>Last ' + days + 'd</th>' +
          '<th>Year ago</th><th>Δ</th></tr></thead><tbody>' +
          pc.map(function (r) {
            var dl = (r.w != null && r.wPrev) ? Math.round((r.w - r.wPrev) / r.wPrev * 100) : null;
            return '<tr><td class="lbl">' + esc(r.label) + '</td><td class="t">' +
              (r.w != null ? r.w + 'w' : '—') + '</td><td>' +
              (r.wPrev != null ? r.wPrev + 'w' : '—') + '</td><td class="' +
              (dl == null ? '' : (dl >= 0 ? 'pos' : 'neg')) + '">' +
              (dl == null ? '—' : (dl >= 0 ? '+' : '') + dl + '%') + '</td></tr>';
          }).join('') + '</tbody></table>',
          { flush: true,
            foot: pw.label ? 'Best of ' + pw.now_from + ' to ' + pw.now_to +
                             ', against the same ' + days + ' days a year earlier (' +
                             pw.prev_from + ' to ' + pw.prev_to + ').'
                           : 'Best efforts over the last ' + days + ' days.' });
      }
    }

    if (sel === 'load') {
      var lc = d.loadChart || [];
      var done = lc.filter(function (r) { return !r.projected; });
      var sum = function (rows) {
        return rows.reduce(function (a, r) {
          return a + (r.activities || []).reduce(function (b, x) { return b + (x.tss || 0); }, 0);
        }, 0);
      };
      h += '<div class="minis">' +
        mini(Math.round(sum(done)), 'tss done') +
        mini(Math.round(sum(lc.filter(function (r) { return r.projected; }))), 'tss planned') +
        mini(done.filter(function (r) { return !(r.activities || []).length; }).length, 'rest days') +
        '</div>';
      var withAct = lc.filter(function (r) { return (r.activities || []).length; });
      if (withAct.length) {
        h += card('Day by day', '<div class="body-flush">' + withAct.map(function (r) {
          return '<div class="day"><div class="day-h' + (r.date === todayISO() ? ' today' : '') + '">' +
            '<span class="dow">' + esc(dow(r.date)) + '</span><span class="dnum">' + dnum(r.date) +
            '</span><span class="sum">tsb ' + signed(r.tsb) +
            (r.projected ? ' · planned' : '') + '</span></div>' +
            (r.activities || []).map(function (a) {
              return '<div class="sesh' + (a.status === 'completed' ? ' done' : '') + '">' +
                '<span class="tick">' + (a.status === 'completed' ? '✓' : '○') + '</span>' +
                '<span class="body"><span class="nm"><span class="sp ' + sportClass(a.sport) +
                '"></span>' + esc(a.sport) + '</span></span>' +
                '<span class="rt"><b>' + hhmm(a.dur) + '</b>' +
                (a.tss != null ? Math.round(a.tss) + ' tss' : '') + '</span></div>';
            }).join('') + '</div>';
        }).join('') + '</div>', { flush: true });
      }
    }

    if (sel === 'heat') {
      var ha = d.heatAccl || {}, hp = d.heatProtocol || {};
      h += '<div class="minis">' +
        mini(ha.current != null ? Number(ha.current).toFixed(0) + '%' : '—', 'now') +
        mini(ha.peak != null ? Number(ha.peak).toFixed(0) + '%' : '—', 'peak') +
        mini(hp.sessions_cumulative != null ? hp.sessions_cumulative : (ha.entries || '—'), 'sessions') +
        mini(hp.sessions_this_week != null ? hp.sessions_this_week : '—', 'this week') +
        '</div>';
      if (hp.target_min != null) {
        var n = hp.sessions_this_week || 0;
        h += '<p class="flag' + (n >= hp.target_min ? ' ok' : '') + '">Weekly target ' +
          hp.target_min + '–' + hp.target_max + ' exposures · ' + n + ' logged.</p>';
      }
      var ev = (ha.events || []).slice().reverse().slice(0, 14);
      if (ev.length) {
        // heat.py emits [date, dose, PCT, label] - pct is the acclimation score on
        // that date, NOT a temperature. This column read "Temp 8.2°" for a 45°C hot
        // bath, which is how Jamie spotted it.
        h += card('Recent exposures', '<table class="tbl">' +
          '<thead><tr><th>Date</th><th>Method</th><th>Dose</th><th>Score</th></tr></thead><tbody>' +
          ev.map(function (e) {
            return '<tr><td class="lbl">' + esc(dow(e[0]) + ' ' + dnum(e[0])) + '</td><td>' +
              esc(e[3] || '—') + '</td><td>' +
              (e[1] != null ? Number(e[1]).toFixed(2) : '—') + '</td><td class="t">' +
              (e[2] != null ? Number(e[2]).toFixed(0) + '%' : '—') + '</td></tr>';
          }).join('') + '</tbody></table>',
          { flush: true, foot: 'Dose is the credit for that exposure; score is the ' +
                              'acclimation level it left you at. Neither is a temperature.' });
      }
    }

    if (sel === 'fuel') {
      var f = fuelSeries(d);
      if (!f) {
        h += '<p class="hint">No fuelling logged yet. Tell the coach what you took ' +
             'on and it lands here.</p>';
      } else {
        var latest = function (rows) {
          for (var i = rows.length - 1; i >= 0; i--) if (rows[i].y != null) return rows[i].y;
          return null;
        };
        var peak = function (rows) {
          return rows.reduce(function (m, r) {
            return r.y != null && (m == null || r.y > m) ? r.y : m;
          }, null);
        };
        var fmt = function (v) { return v == null ? '—' : Number(v).toFixed(0); };
        h += '<div class="minis">' +
          mini(fmt(latest(f.Ride)), 'bike now') +
          mini(fmt(peak(f.Ride)), 'bike best') +
          mini(fmt(latest(f.Run)), 'run now') +
          mini(fmt(peak(f.Run)), 'run best') + '</div>';
        // Duration and total grams, not just the rate: 120 g/hr held for an hour and
        // 120 g/hr held for five hours are different achievements, and the rate alone
        // hides which one happened (Jamie, 3 Aug 2026).
        h += card('Logged sessions', '<table class="tbl">' +
          '<thead><tr><th>Date</th><th>Sport</th><th>Time</th><th>Total</th><th>g/hr</th></tr></thead><tbody>' +
          f.points.slice().reverse().slice(0, 24).map(function (r) {
            var grams = (r.g_per_hr != null && r.dur) ? Math.round(r.g_per_hr * r.dur / 60) : null;
            return '<tr><td class="lbl">' + esc(dow(r.date) + ' ' + dnum(r.date)) + '</td><td>' +
              esc(r.sport || '—') + '</td><td>' + hhmm(r.dur) + '</td><td>' +
              (grams != null ? grams + ' g' : '—') + '</td><td class="t">' +
              Number(r.g_per_hr).toFixed(0) + '</td></tr>';
          }).join('') + '</tbody></table>',
          { flush: true, foot: 'A rate held for four hours is a different result from ' +
                              'the same rate held for one, so the duration is shown with it.' });
      }
    }

    if (sel === 'plan') {
      var pva = d.planVsActual || [];
      if (pva.length) {
        h += card('By week', '<table class="tbl">' +
          '<thead><tr><th>Week</th><th>Type</th><th>Plan</th><th>Actual</th><th>Δ</th></tr></thead><tbody>' +
          pva.slice().reverse().map(function (w) {
            var diff = (w.actual_tss || 0) - (w.planned_tss || 0);
            var pct = w.planned_tss ? Math.round(diff / w.planned_tss * 100) : 0;
            return '<tr><td class="lbl">' + esc(dow(w.week_start) + ' ' + dnum(w.week_start)) +
              '</td><td>' + esc(w.week_type || '—') + '</td><td>' + Math.round(w.planned_tss || 0) +
              '</td><td>' + Math.round(w.actual_tss || 0) + '</td><td class="' +
              (pct >= 0 ? 'pos' : 'neg') + '">' + (pct >= 0 ? '+' : '') + pct + '%</td></tr>';
          }).join('') + '</tbody></table>', { flush: true });
      }
    }

    return h;
  }

  /* ── charts ──────────────────────────────────────────────────────────── */

  // Vertical dated markers (race days, today). Written inline rather than pulling in
  // the annotation plugin for four lines of canvas work.
  var vlines = {
    id: 'vlines',
    afterDatasetsDraw: function (chart, args, opts) {
      var lines = (opts && opts.lines) || [];
      if (!lines.length) return;
      var x = chart.scales.x, ya = chart.chartArea, g = chart.ctx;
      lines.forEach(function (L) {
        if (L.x < x.min || L.x > x.max) return;
        var px = x.getPixelForValue(L.x);
        g.save();
        g.strokeStyle = L.color || C.muted;
        g.lineWidth = 1;
        g.setLineDash([2, 3]);
        g.beginPath(); g.moveTo(px, ya.top); g.lineTo(px, ya.bottom); g.stroke();
        if (L.label) {
          g.setLineDash([]);
          g.fillStyle = L.color || C.muted;
          g.font = '9px "DM Mono", monospace';
          g.textAlign = 'left';
          var w = g.measureText(L.label).width;
          var lx = px + 4 + w > ya.right ? px - 4 - w : px + 4;
          g.fillText(L.label, lx, ya.top + 10);
        }
        g.restore();
      });
    }
  };

  function axes(dense, yTitle) {
    return {
      x: {
        type: 'linear',
        ticks: {
          color: C.muted, maxTicksLimit: 7, autoSkip: true,
          font: { family: 'DM Mono', size: 9 },
          callback: function (v) { return doyLabel(v, dense); }
        },
        grid: { display: false },
        border: { color: C.rule }
      },
      y: {
        title: yTitle ? { display: true, text: yTitle, color: C.muted,
                          font: { family: 'DM Mono', size: 9 } } : { display: false },
        ticks: { color: C.muted, font: { family: 'DM Mono', size: 9 }, maxTicksLimit: 6 },
        grid: { color: C.rule, drawTicks: false },
        border: { display: false }
      }
    };
  }

  function baseOpts() {
    return {
      responsive: true, maintainAspectRatio: false,
      // Charts are read by touching them, so a fat tap should still hit a point.
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      plugins: {
        legend: {
          labels: {
            color: C.ink, boxWidth: 8, boxHeight: 8, usePointStyle: true, padding: 12,
            font: { family: 'DM Mono', size: 9 },
            // Datasets whose label starts with _ are structural (the lower edge of a
            // band, for instance) and must not appear as legend entries.
            filter: function (item) { return item.text && item.text[0] !== '_'; }
          }
        },
        // Off on touch devices - see setReadout(). Kept for mouse, where a tooltip
        // near the cursor is genuinely the best affordance.
        tooltip: {
          enabled: !TOUCH,
          backgroundColor: C.ink, borderColor: C.ink, padding: 9,
          titleFont: { family: 'DM Mono', size: 10 },
          bodyFont: { family: 'DM Mono', size: 10 },
          displayColors: false
        }
      }
    };
  }

  function zoomOpts() {
    return {
      pan: { enabled: true, mode: 'x', modifierKey: null },
      zoom: {
        wheel: { enabled: true, speed: 0.08 },
        pinch: { enabled: true },
        drag: { enabled: false },
        mode: 'x'
      },
      limits: { x: { min: 'original', max: 'original', minRange: 14 } }
    };
  }

  function readout(txt, sub) {
    var el = $('#ro');
    if (!el) return;
    el.firstChild.textContent = txt || '—';
    el.lastChild.textContent = sub || '';
  }

  // Wire a chart so dragging across it writes into the readout. Uses the SAME
  // callbacks the tooltip would have used, so mouse and touch never disagree.
  function attachReadout(chart) {
    if (!chart) return;
    var cbs = (chart.options.plugins.tooltip || {}).callbacks || {};
    var canvas = chart.canvas;

    var report = function (evt) {
      var pts = chart.getElementsAtEventForMode(evt, 'index', { intersect: false }, true);
      if (!pts.length) return;
      var items = pts.map(function (pt) {
        var ds = chart.data.datasets[pt.datasetIndex];
        return {
          dataset: ds, datasetIndex: pt.datasetIndex, dataIndex: pt.index,
          raw: ds.data[pt.index],
          parsed: chart.getDatasetMeta(pt.datasetIndex).data[pt.index].parsed
        };
      }).filter(function (it) {
        return it.dataset.label && it.dataset.label[0] !== '_' && it.parsed.y != null;
      });
      if (!items.length) return;
      var title = cbs.title ? cbs.title(items) : '';
      var lines = [];
      items.forEach(function (it) {
        var out = cbs.label ? cbs.label(it) : null;
        if (out == null) return;
        lines = lines.concat(out);
      });
      readout(title, lines.filter(Boolean).join('  ·  '));
    };

    ['touchstart', 'touchmove', 'mousemove'].forEach(function (ev) {
      canvas.addEventListener(ev, report, { passive: true });
    });
  }

  function drawTrend() {
    if (typeof Chart === 'undefined' || !state.data) return;
    var el = document.getElementById('c-now');
    if (!el) return;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    var fn = { fit: chartFitness, load: chartLoad, heat: chartHeat,
               fuel: chartFuel, plan: chartPlan }[state.trend];
    if (fn) state.chart = fn(el, state.data);
    attachReadout(state.chart);
  }

  function chartFitness(el, d) {
    var p = d.profile || {}, cp = d.ctlProjection || {};

    // x is DAYS RELATIVE TO RACE DAY, not day-of-year. Aligning three seasons by
    // calendar date put Barcelona's race (7 Oct) 18 days right of Italy's (19 Sep),
    // so the taper and the peak of one season sat over mid-build of another and the
    // comparison was worthless. Race day is 0 for every season; tick labels are
    // still calendar dates, read off THIS season, which is what was asked for.
    var raceThis = cp.race_date || p.race_date;
    var racePrev = (p.prev_race && p.prev_race.date) || p.prev_race_date;
    var racePrev2 = p.prev2_race_date;

    // 'all' = overall CTL; otherwise the per-sport series for the same season.
    var pick3 = function (which) {
      if (state.fitSport === 'all') {
        return { current: d.fitnessThis, prev: d.fitnessPrev, prev2: d.fitnessPrev2 }[which];
      }
      var bs = d.fitnessBySport || {};
      return ((bs[which] || {})[state.fitSport]) || [];
    };

    var rel = function (rows, race) {
      if (!race) return [];
      var r0 = doy0(race);
      return (rows || []).map(function (x) { return { x: doy0(x[0]) - r0, y: x[1] }; });
    };
    var relObj = function (rows, race) {
      if (!race) return [];
      var r0 = doy0(race);
      return (rows || []).map(function (x) { return { x: doy0(x.date) - r0, y: x.ctl }; });
    };

    var ds = [];

    if (cp.target_ctl_min != null && raceThis && state.fitSport === 'all') {
      var x0 = doy0(todayISO()) - doy0(raceThis);
      ds.push({
        label: 'Target band', order: 9,
        data: [{ x: x0, y: cp.target_ctl_max }, { x: 0, y: cp.target_ctl_max }],
        borderColor: 'rgba(29,104,64,.35)', borderWidth: 1, borderDash: [4, 3],
        pointRadius: 0, fill: '+1', backgroundColor: 'rgba(29,104,64,.09)'
      });
      ds.push({
        label: '_band-lo', order: 10,
        data: [{ x: x0, y: cp.target_ctl_min }, { x: 0, y: cp.target_ctl_min }],
        borderColor: 'rgba(29,104,64,.35)', borderWidth: 1, borderDash: [4, 3],
        pointRadius: 0, fill: false
      });
    }

    if ((pick3('prev2') || []).length && racePrev2) {
      ds.push({
        label: (p.prev2_race_name || '2023').replace(' IM', " '23"), order: 6,
        data: rel(pick3('prev2'), racePrev2), borderColor: C.blue, borderWidth: 1,
        borderDash: [5, 3], pointRadius: 0, tension: 0.3, fill: false
      });
    }
    if ((pick3('prev') || []).length && racePrev) {
      ds.push({
        label: 'Last season', order: 5,
        data: rel(pick3('prev'), racePrev), borderColor: C.muted, borderWidth: 1,
        borderDash: [2, 3], pointRadius: 0, tension: 0.3, fill: false
      });
    }
    if ((cp.planned_build || []).length && state.fitSport === 'all') {
      ds.push({
        label: 'Planned', order: 3,
        data: relObj(cp.planned_build, raceThis), borderColor: C.green, borderWidth: 1.4,
        borderDash: [3, 3], pointRadius: 0, tension: 0.25, fill: false
      });
    }
    ds.push({
      label: 'This season', order: 1,
      data: rel(pick3('current'), raceThis), borderColor: C.green, borderWidth: 2.2,
      pointRadius: 0, tension: 0.25, fill: false
    });
    if ((cp.target_milestones || []).length && raceThis && state.fitSport === 'all') {
      var r0m = doy0(raceThis);
      ds.push({
        label: 'Milestones', order: 0, showLine: false,
        data: cp.target_milestones.map(function (m) {
          return { x: doy0(m.date) - r0m, y: m.ctl, lbl: m.label };
        }),
        borderColor: C.amber, backgroundColor: C.amber,
        pointRadius: 3.4, pointStyle: 'rectRot'
      });
    }

    // Ticks read as calendar dates on THIS season's timeline.
    var label = function (v) {
      if (!raceThis) return Math.round(v) + 'd';
      var t = new Date(raceThis + 'T12:00:00');
      t.setDate(t.getDate() + Math.round(v));
      return t.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    };

    var o = baseOpts();
    o.scales = {
      x: {
        type: 'linear',
        // Default to the run-in rather than the whole three-year span: on a phone a
        // full season of three overlaid lines is unreadable. Zoom out reaches the rest.
        min: -175, max: 12,
        ticks: { color: C.muted, maxTicksLimit: 6, autoSkip: true,
                 font: { family: 'DM Mono', size: 9 },
                 callback: function (v) { return label(v); } },
        grid: { display: false }, border: { color: C.rule }
      },
      y: {
        title: { display: true, text: 'CTL', color: C.muted,
                 font: { family: 'DM Mono', size: 9 } },
        ticks: { color: C.muted, font: { family: 'DM Mono', size: 9 }, maxTicksLimit: 6 },
        grid: { color: C.rule, drawTicks: false }, border: { display: false }
      }
    };
    o.plugins.zoom = zoomOpts();
    o.plugins.zoom.limits = { x: { min: -400, max: 30, minRange: 21 } };
    o.plugins.vlines = { lines: [{ x: 0, label: 'RACE DAY', color: C.green }] };
    o.plugins.tooltip.callbacks = {
      title: function (items) {
        var v = Math.round(items[0].parsed.x);
        return label(v) + ' · ' + (v === 0 ? 'race day' : Math.abs(v) + 'd ' +
          (v < 0 ? 'to race' : 'after'));
      },
      label: function (it) {
        var raw = it.raw || {};
        return (it.dataset.label || '') + ': ' + Number(it.parsed.y).toFixed(1) +
          (raw.lbl ? ' · ' + raw.lbl : '');
      }
    };
    return new Chart(el, { type: 'line', data: { datasets: ds }, options: o, plugins: [vlines] });
  }

  function chartLoad(el, d) {
    // Matched to the bot's load_chart (telegram/charts.py), which is the version
    // Jamie reads and the one he confirmed is right. Three things this had wrong:
    // one undifferentiated total bar instead of a stack BY SPORT, no rolling 7-day
    // average load line, and a bare TSB line with no fresh/load/heavy banding.
    var lc = (d.loadChart || []).slice();
    var t = todayISO();
    var SPORTS = ['Ride', 'Run', 'Swim', 'Strength', 'Other'];
    var BASE = { Ride: '29,104,64', Run: '192,57,43', Swim: '26,82,118',
                 Strength: '127,140,141', Other: '176,170,160' };
    var norm = function (sp) {
      var k = SPORT[sp] || 'other';
      return { bike: 'Ride', run: 'Run', swim: 'Swim', strength: 'Strength' }[k] || 'Other';
    };

    var totals = lc.map(function (r) {
      return (r.activities || []).reduce(function (a, x) { return a + (x.tss || 0); }, 0);
    });
    // Trailing 7-day mean of daily load, on the TSS axis because it shares the unit.
    var roll = totals.map(function (_, i) {
      var w = totals.slice(Math.max(0, i - 6), i + 1);
      return w.reduce(function (a, b) { return a + b; }, 0) / w.length;
    });

    var ds = [];
    SPORTS.forEach(function (sport) {
      var vals = lc.map(function (r) {
        return (r.activities || []).reduce(function (a, x) {
          return a + (norm(x.sport) === sport ? (x.tss || 0) : 0);
        }, 0);
      });
      if (!vals.some(function (v) { return v > 0; })) return;
      ds.push({
        type: 'bar', label: sport, stack: 'tss', yAxisID: 'y', order: 3,
        data: vals,
        backgroundColor: lc.map(function (r, i) {
          if (!vals[i]) return 'transparent';
          var planned = r.projected || r.date > t ||
            (r.activities || []).some(function (x) {
              return norm(x.sport) === sport && x.status === 'planned';
            });
          return 'rgba(' + BASE[sport] + ',' + (planned ? 0.3 : 0.87) + ')';
        }),
        borderWidth: 0, barPercentage: 0.8, categoryPercentage: 0.92
      });
    });
    ds.push({
      type: 'line', label: '7d avg load', yAxisID: 'y', order: 2, data: roll,
      borderColor: 'rgba(24,22,15,.55)', borderWidth: 1.6, borderDash: [5, 3],
      pointRadius: 0, tension: 0.3, fill: false
    });
    ds.push({
      type: 'line', label: 'TSB', yAxisID: 'y1', order: 1,
      data: lc.map(function (r) { return r.tsb; }),
      borderColor: C.ink2, borderWidth: 1.4, tension: 0.3, fill: false,
      pointRadius: 3, pointBorderColor: '#fff', pointBorderWidth: 1,
      pointBackgroundColor: lc.map(function (r) {
        var v = r.tsb == null ? 0 : r.tsb;
        return v > 5 ? '#2e9c8e' : (v >= -20 ? '#c9871f' : '#c0392b');
      })
    });

    // TSB zone bands, drawn behind everything on the right-hand scale.
    var bands = {
      id: 'tsbBands',
      beforeDatasetsDraw: function (chart) {
        var y1 = chart.scales.y1, ca = chart.chartArea, g = chart.ctx;
        if (!y1) return;
        [[5, Infinity, 'rgba(46,156,142,.09)'], [0, 5, 'rgba(120,200,140,.10)'],
         [-20, 0, 'rgba(200,160,60,.08)'], [-Infinity, -20, 'rgba(192,57,43,.09)']]
          .forEach(function (b) {
            var lo = Math.max(b[0], y1.min), hi = Math.min(b[1], y1.max);
            if (hi <= lo) return;
            var yTop = y1.getPixelForValue(hi), yBot = y1.getPixelForValue(lo);
            g.save(); g.fillStyle = b[2];
            g.fillRect(ca.left, yTop, ca.right - ca.left, yBot - yTop);
            g.restore();
          });
      }
    };

    var o = baseOpts();
    o.scales = {
      x: {
        stacked: true, type: 'category',
        ticks: { color: C.muted, autoSkip: false, maxRotation: 0,
                 font: { family: 'DM Mono', size: 9 } },
        grid: { display: false }, border: { color: C.rule }
      },
      y: {
        stacked: true, position: 'left', beginAtZero: true,
        title: { display: true, text: 'TSS', color: C.muted, font: { family: 'DM Mono', size: 9 } },
        ticks: { color: C.muted, font: { family: 'DM Mono', size: 9 }, maxTicksLimit: 5 },
        grid: { color: C.rule, drawTicks: false }, border: { display: false }
      },
      y1: {
        position: 'right',
        title: { display: true, text: 'TSB', color: C.muted, font: { family: 'DM Mono', size: 9 } },
        ticks: { color: C.muted, font: { family: 'DM Mono', size: 9 }, maxTicksLimit: 5 },
        grid: { display: false }, border: { display: false }
      }
    };
    o.plugins.vlines = { lines: [] };
    o.plugins.tooltip.callbacks = {
      title: function (items) {
        var r = lc[items[0].dataIndex];
        return r ? dow(r.date) + ' ' + dnum(r.date) +
          (r.projected || r.date > t ? ' · planned' : '') : '';
      },
      label: function (it) {
        if (it.dataset.yAxisID === 'y1') return 'TSB ' + signed(it.parsed.y);
        if (it.dataset.label === '7d avg load') return '7d avg ' + Math.round(it.parsed.y);
        if (!it.parsed.y) return null;
        return it.dataset.label + ' ' + Math.round(it.parsed.y) + ' tss';
      },
      footer: function (items) {
        return 'Day total ' + Math.round(totals[items[0].dataIndex]) + ' tss';
      }
    };
    o.plugins.tooltip.footerFont = { family: 'DM Mono', size: 9 };

    return new Chart(el, {
      data: {
        labels: lc.map(function (r) { return dow(r.date).charAt(0) + dnum(r.date); }),
        datasets: ds
      },
      options: o
    });
  }

  function chartHeat(el, d) {
    var ha = d.heatAccl || {};
    var o = baseOpts();
    o.scales = axes(false, 'Score %');
    o.scales.y.min = 0;
    o.plugins.zoom = zoomOpts();
    o.plugins.tooltip.callbacks = {
      title: function (items) { return doyLabel(items[0].parsed.x, true); },
      label: function (it) {
        var raw = it.raw || {};
        return raw.lbl
          ? raw.lbl + (raw.pct != null ? ' · score ' + Number(raw.pct).toFixed(0) + '%' : '')
          : 'Score ' + Number(it.parsed.y).toFixed(0) + '%';
      }
    };
    var ds = [{
      label: 'Acclimation', order: 1,
      data: (ha.daily || []).map(function (r) { return { x: doy(r[0]), y: r[1] }; }),
      borderColor: C.red, borderWidth: 1.8, pointRadius: 0, tension: 0.25,
      fill: true, backgroundColor: 'rgba(185,28,28,.07)'
    }];
    if ((ha.events || []).length) {
      var byDate = {};
      (ha.daily || []).forEach(function (r) { byDate[r[0]] = r[1]; });
      ds.push({
        label: 'Exposures', order: 0, showLine: false,
        data: ha.events.map(function (e) {
          return { x: doy(e[0]), y: byDate[e[0]] != null ? byDate[e[0]] : 0,
                   lbl: e[3], pct: e[2] };
        }),
        borderColor: C.amber, backgroundColor: C.amber, pointRadius: 3
      });
    }
    return new Chart(el, { type: 'line', data: { datasets: ds }, options: o });
  }

  // Rolling 14-day MAX g/hr per sport. A raw per-ride scatter answers "what did I
  // take on that day", which is noisy and not the question - fuelling is a trainable
  // capacity, so the useful figure is the best rate demonstrated in the last fortnight
  // (Jamie, 3 Aug 2026). Split by sport because gut tolerance on the bike and on the
  // run are different problems and a combined line hides the run, which is the harder one.
  var FUEL_WIN = 14;

  function fuelSeries(d) {
    var cb = (((d.progressData || {}).carb) || []).slice()
      .filter(function (r) { return r.date && r.g_per_hr != null; })
      .sort(function (a, b) { return a.date < b.date ? -1 : 1; });
    if (!cb.length) return null;

    var fam = function (sp) { return SPORT[sp] === 'run' ? 'Run' : (SPORT[sp] === 'bike' ? 'Ride' : null); };
    var d0 = doy0(cb[0].date), d1 = doy0(todayISO());
    if (d1 < doy0(cb[cb.length - 1].date)) d1 = doy0(cb[cb.length - 1].date);

    var out = { Ride: [], Run: [], points: cb, from: cb[0].date };
    for (var x = d0; x <= d1; x++) {
      ['Ride', 'Run'].forEach(function (sport) {
        var best = null, bestDur = null;
        cb.forEach(function (r) {
          if (fam(r.sport) !== sport) return;
          var rd = doy0(r.date);
          if (rd > x || rd <= x - FUEL_WIN) return;   // trailing 14-day window
          if (best == null || r.g_per_hr > best) { best = r.g_per_hr; bestDur = r.dur; }
        });
        out[sport].push({ x: x, y: best, dur: bestDur });  // null = nothing in the window
      });
    }
    return out;
  }

  function chartFuel(el, d) {
    var f = fuelSeries(d);
    if (!f) return null;

    var label = function (v) {
      var t = new Date((Math.round(v)) * 864e5);
      return t.getUTCDate() + ' ' +
        t.toLocaleDateString('en-GB', { month: 'short', timeZone: 'UTC' });
    };

    var o = baseOpts();
    o.spanGaps = false;
    o.scales = {
      x: {
        type: 'linear',
        ticks: { color: C.muted, maxTicksLimit: 6, autoSkip: true,
                 font: { family: 'DM Mono', size: 9 },
                 callback: function (v) { return label(v); } },
        grid: { display: false }, border: { color: C.rule }
      },
      y: {
        beginAtZero: true,
        title: { display: true, text: 'max g/hr · 14d', color: C.muted,
                 font: { family: 'DM Mono', size: 9 } },
        ticks: { color: C.muted, font: { family: 'DM Mono', size: 9 }, maxTicksLimit: 5 },
        grid: { color: C.rule, drawTicks: false }, border: { display: false }
      }
    };
    o.plugins.zoom = zoomOpts();
    o.plugins.zoom.limits = { x: { min: 'original', max: 'original', minRange: 21 } };
    o.plugins.tooltip.callbacks = {
      title: function (items) { return label(items[0].parsed.x); },
      label: function (it) {
        var r = (it.raw || {});
        return it.dataset.label + ': ' + Number(it.parsed.y).toFixed(0) + ' g/hr' +
          (r.dur ? ' over ' + hhmm(r.dur) : '');
      }
    };

    return new Chart(el, {
      type: 'line',
      data: {
        datasets: [
          { label: 'Bike', data: f.Ride, borderColor: C.green, borderWidth: 2,
            pointRadius: 0, tension: 0.2, fill: false, stepped: 'after' },
          { label: 'Run', data: f.Run, borderColor: C.red, borderWidth: 2,
            pointRadius: 0, tension: 0.2, fill: false, stepped: 'after' }
        ]
      },
      options: o
    });
  }

  function chartPlan(el, d) {
    var pva = (d.planVsActual || []).slice();
    var o = baseOpts();
    o.scales = {
      x: {
        type: 'linear', offset: true,
        ticks: {
          color: C.muted, autoSkip: true, maxRotation: 0, maxTicksLimit: 7,
          font: { family: 'DM Mono', size: 9 },
          callback: function (v) {
            var r = pva[Math.round(v)];
            return r ? dnum(r.week_start) + ' ' +
              new Date(r.week_start + 'T12:00:00').toLocaleDateString('en-GB', { month: 'short' }) : '';
          }
        },
        grid: { display: false }, border: { color: C.rule }
      },
      y: {
        beginAtZero: true,
        title: { display: true, text: 'TSS', color: C.muted, font: { family: 'DM Mono', size: 9 } },
        ticks: { color: C.muted, font: { family: 'DM Mono', size: 9 }, maxTicksLimit: 5 },
        grid: { color: C.rule, drawTicks: false }, border: { display: false }
      }
    };
    o.plugins.tooltip.callbacks = {
      title: function (items) {
        var r = pva[items[0].dataIndex];
        return r ? 'Week of ' + dnum(r.week_start) + ' · ' + (r.week_type || '—') : '';
      },
      label: function (it) {
        var r = pva[it.dataIndex] || {};
        var pct = r.planned_tss ? Math.round(((r.actual_tss || 0) - r.planned_tss) / r.planned_tss * 100) : 0;
        return it.dataset.label + ' ' + Math.round(it.parsed.y) +
          (it.dataset.label === 'Actual' ? ' (' + (pct >= 0 ? '+' : '') + pct + '%)' : '');
      }
    };
    return new Chart(el, {
      type: 'bar',
      data: {
        labels: pva.map(function (r, i) { return i; }),
        datasets: [
          { label: 'Planned', data: pva.map(function (r) { return r.planned_tss || 0; }),
            backgroundColor: 'rgba(154,144,128,.55)', borderWidth: 0, borderRadius: 2 },
          { label: 'Actual', data: pva.map(function (r) { return r.actual_tss || 0; }),
            backgroundColor: 'rgba(29,104,64,.75)', borderWidth: 0, borderRadius: 2 }
        ]
      },
      options: o
    });
  }

  /* ── Library ─────────────────────────────────────────────────────────── */

  /* ── session library ─────────────────────────────────────────────────── */
  /* Lives inside Settings rather than owning a tab: it is reference material, looked
   * up occasionally, and it was crowding the bar against things used daily. */

  function libraryBlock() {
    var lib = state.lib;
    if (!lib) return '';
    var types = lib.session_types || {};
    var groups = Object.keys(types);
    var sel = state.libGroup || groups[0];
    var total = groups.reduce(function (a, g) {
      return a + Object.keys(types[g]).filter(function (k) { return k[0] !== '_'; }).length;
    }, 0);

    var seg = '<div class="seg wrap" id="libSeg">' + groups.map(function (g) {
      return '<button type="button" data-g="' + esc(g) + '" aria-selected="' +
        (g === sel) + '">' + esc(g.replace(/_/g, ' ')) + '</button>';
    }).join('') + '</div>';

    var entries = Object.keys(types[sel] || {}).filter(function (k) { return k[0] !== '_'; });
    return card('Session library · ' + total, seg +
      '<div class="lib">' + (entries.length ? entries.map(function (name) {
        var x = types[sel][name] || {};
        var prog = (x.progression || []).map(function (q) {
          if (q.reps && q.min) return q.reps + '×' + q.min + 'min';
          if (q.bike_min) return q.bike_min + '/' + q.run_min + 'min';
          return null;
        }).filter(Boolean);
        return '<details><summary><span class="nm">' + esc(name.replace(/_/g, ' ')) + '</span>' +
          '<span class="zn">' + esc([x.zone, x.if != null ? 'IF ' + x.if : null]
            .filter(Boolean).join(' · ')) + '</span></summary><div class="inner">' +
          '<div class="kv">' + esc([x.system, x.min_phase ? 'from ' + x.min_phase : null,
            x.duration ? String(x.duration).replace(/_/g, ' ') : null].filter(Boolean).join(' · ')) +
          '</div>' +
          (prog.length ? '<div class="prog">' + prog.map(function (q) {
            return '<span>' + esc(q) + '</span>';
          }).join('') + '</div>' : '') +
          (x.note ? '<p class="note">' + esc(x.note) + '</p>' : '') +
          '</div></details>';
      }).join('') : '<div class="empty">No sessions in this group</div>') + '</div>');
  }

  /* ── Goals ───────────────────────────────────────────────────────────── */

  function renderGoals() {
    var d = state.data, p = d.profile || {}, rp = d.racePredictor || {};
    var h = '';

    h += '<section class="hero">' +
      '<p class="hero-k">' + esc(p.race_distance || 'Race') +
      (p.race_date ? ' · ' + daysBetween(todayISO(), p.race_date) + ' days' : '') + '</p>' +
      '<h2 class="hero-t">' + esc(p.race_name || 'Race') + '</h2>' +
      (p.race_date ? '<p class="hero-m">' +
        new Date(p.race_date + 'T12:00:00').toLocaleDateString('en-GB',
          { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }) + '</p>' : '') +
      '</section>';

    h += '<div class="goals">' +
      '<div class="goal"><span class="tag">A</span><span class="v">' + esc(p.a_goal || '—') +
      '</span><span class="n">primary</span></div>' +
      '<div class="goal"><span class="tag">B</span><span class="v">' + esc(p.b_goal || '—') +
      '</span><span class="n">fallback</span></div></div>';

    var cp = d.ctlProjection || {};
    if (cp.target_ctl_min != null) {
      var now = (d.kpi || {}).ctl || 0, lo = cp.target_ctl_min, hi = cp.target_ctl_max;
      h += card('Fitness target for race day',
        // One wrapper, not two siblings: .card pads each direct child, so two
        // children here would stack padding into a 26px gap mid-card.
        '<div><div class="statline"><b>' + Number(now).toFixed(1) + '</b>' +
        '<span>now · target ' + lo + '–' + hi + '</span></div>' +
        '<div class="bar' + (now < lo ? ' warn' : '') + '"><i style="width:' +
        Math.min(100, now / hi * 100).toFixed(0) + '%"></i></div></div>',
        { foot: now >= lo ? 'Inside the target band.'
                          : 'Below the band — ' + (lo - now).toFixed(1) + ' CTL to find.' });
    }

    if (rp.rows && rp.rows.length) {
      h += card('Projection', '<table class="tbl">' +
        '<thead><tr><th>Scenario</th><th>CTL</th><th>Bike</th><th>Run</th><th>Total</th></tr></thead><tbody>' +
        rp.rows.map(function (r, i) {
          return '<tr' + (i === rp.rows.length - 1 ? ' class="hl"' : '') + '>' +
            '<td class="lbl">' + esc(r.label) + '</td><td>' + Math.round(r.ctl) + '</td>' +
            '<td>' + hhmm(r.bike_min) + '</td><td>' + hhmm(r.run_min) + '</td>' +
            '<td class="t">' + hhmm(r.total_min) + '</td></tr>';
        }).join('') + '</tbody></table>',
        { flush: true, foot: 'Bike IF scales with √CTL from the anchor race.' });
    }

    // An empty object is truthy, and Calum's profile carries prev_race:{} and
    // prev2_race:{}. Test for the field actually dereferenced, not for the key.
    var has = function (o) { return o && o.date ? o : null; };
    var pr = has(p.prev_race), pr2 = has(p.prev2_race);
    var tg = (p.race_targets && Object.keys(p.race_targets).length) ? p.race_targets : null;
    if (pr || pr2 || tg) {
      var cols = [];
      if (pr2) cols.push([pr2.date.slice(0, 4), pr2]);
      if (pr) cols.push([pr.date.slice(0, 4), pr]);
      cols.push(['Target', tg || {}]);
      h += card('Splits · progression to target', '<table class="tbl">' +
        '<thead><tr><th>Leg</th>' + cols.map(function (c) {
          return '<th>' + esc(c[0]) + '</th>';
        }).join('') + '</tr></thead><tbody>' +
        [['Swim', 'swim_time'], ['T1/T2', 't1t2_time'], ['Bike', 'bike_time'],
         ['Run', 'run_time'], ['Total', 'total_time']].map(function (row) {
          var isTotal = row[0] === 'Total';
          return '<tr' + (isTotal ? ' class="hl"' : '') + '><td class="lbl">' + row[0] + '</td>' +
            cols.map(function (c, i) {
              var last = i === cols.length - 1;
              return '<td class="' + (isTotal && last ? 't' : '') + '">' +
                esc(c[1][row[1]] || '—') + '</td>';
            }).join('') + '</tr>';
        }).join('') + '</tbody></table>',
        { flush: true,
          foot: pr2 ? 'Barcelona 2023 reconstructed from the Intervals.icu multisport ' +
                      'legs; its stored date was wrong by six days and is corrected.' : '' });
    }

    h += chatCTA('Ask about pacing or the plan');
    $('#v-goals').innerHTML = h;
  }

  /* ── activity detail ─────────────────────────────────────────────────── */
  /* Joined BY DATE, because the public subset carries no activity id - the id is
   * withheld deliberately (it re-identifies an Intervals.icu/Strava record), so
   * date plus sport is the only key available. Consequence worth knowing: two rides
   * on one day share their fuelling and heat rows. */

  function detailFor(dateISO, sport, name) {
    var d = state.data || {};
    var fam = SPORT[sport] || null;
    var same = function (a, b) { return a && b && (SPORT[a] || a) === (SPORT[b] || b); };

    var act = (d.recent || []).filter(function (r) {
      return r.date === dateISO && (!sport || same(r.sport, sport));
    })[0];
    var plan = (d.weekCalendar || []).filter(function (r) {
      return r.date === dateISO && (!sport || same(r.sport, sport));
    })[0];
    var log = (d.sessionLog || []).filter(function (r) {
      return r.date === dateISO && (!sport || same(r.sport, sport));
    })[0];
    var carb = (((d.progressData || {}).carb) || []).filter(function (r) {
      return r.date === dateISO && (!fam || same(r.sport, sport));
    })[0];
    var heat = ((d.heatAccl || {}).events || []).filter(function (e) {
      return e[0] === dateISO;
    })[0];

    return { act: act, plan: plan, log: log, carb: carb, heat: heat,
             date: dateISO, sport: sport, name: name };
  }

  function kvRows(rows) {
    var live = rows.filter(function (r) { return r; });
    if (!live.length) return '';
    return '<table class="tbl"><tbody>' + live.map(function (r) {
      return '<tr><td class="lbl">' + esc(r[0]) + '</td><td class="' +
        (r[2] ? 't' : '') + '">' + esc(r[1]) + '</td></tr>';
    }).join('') + '</tbody></table>';
  }

  function openDetail(dateISO, sport, name) {
    var x = detailFor(dateISO, sport, name);
    var a = x.act || {}, pl = x.plan || {}, lg = x.log || {};
    var done = !!x.act || pl.status === 'completed';

    var h = '<header class="dr-h"><div><p class="dr-k">' +
      esc(dow(dateISO) + ' ' + dnum(dateISO) + ' ' +
          new Date(dateISO + 'T12:00:00').toLocaleDateString('en-GB', { month: 'long' })) +
      (done ? ' · completed' : ' · planned') + '</p>' +
      '<h2 class="dr-t"><span class="sp ' + sportClass(sport) + '"></span>' +
      esc(name || pl.name || a.name || sport || 'Session') + '</h2></div>' +
      '<button type="button" class="dr-x" id="drX" aria-label="Close">✕</button></header>';

    h += '<div class="dr-b">';

    h += card('Summary', kvRows([
      ['Duration', hhmm(a.dur != null ? a.dur : (pl.duration_min != null ? pl.duration_min : lg.duration_min)), true],
      (a.dist || lg.distance_km) ? ['Distance', Number(a.dist || lg.distance_km).toFixed(1) + ' km'] : null,
      (a.pace || lg.pace_per_100m) ? ['Pace', a.pace || lg.pace_per_100m] : null,
      (a.hr || lg.avg_hr) ? ['Avg HR', (a.hr || lg.avg_hr) + ' bpm'] : null,
      (a.powNp || lg.norm_power) ? ['Normalised power', (a.powNp || lg.norm_power) + ' w'] : null,
      (a.powAvg || lg.avg_power) ? ['Average power', (a.powAvg || lg.avg_power) + ' w'] : null,
      (a.tss != null || pl.tss != null || lg.tss != null)
        ? ['Training load', Math.round(a.tss != null ? a.tss : (lg.tss != null ? lg.tss : pl.tss)) + ' tss', true] : null,
      lg.rpe != null ? ['RPE', lg.rpe + ' / 10'] : null,
      pl.detail ? ['Prescription', pl.detail] : null
    ]) || '<div class="empty">No summary recorded</div>', { flush: true });

    // Fuelling. Every row is shown even when empty: a blank is information (it says
    // log it), whereas hiding the row makes a gap look like a feature that is missing.
    h += card('Fuelling', kvRows([
      ['Carbohydrate', x.carb ? Number(x.carb.g_per_hr).toFixed(0) + ' g/hr' : 'not logged',
       !!x.carb],
      ['Water', lg.hydration_ml != null ? lg.hydration_ml + ' ml' : 'not logged'],
      ['Sodium', lg.nutrition_mg_sodium != null ? lg.nutrition_mg_sodium + ' mg' : 'not logged',
       lg.nutrition_mg_sodium != null],
      (x.carb && x.carb.dur) ? ['Over', hhmm(x.carb.dur)] : null,
      (x.carb && x.carb.dur && x.carb.g_per_hr != null)
        ? ['Total carbohydrate', Math.round(x.carb.g_per_hr * x.carb.dur / 60) + ' g'] : null
    ]), { foot: 'Sodium is captured from your reply to the post-session question. ' +
                'Sweat sodium concentration is a separate test, still to be booked.' });

    h += card('Heat', x.heat
      ? kvRows([
          ['Exposure', x.heat[3] || 'logged', true],
          x.heat[1] != null ? ['Dose', Number(x.heat[1]).toFixed(2)] : null,
          x.heat[2] != null ? ['Acclimation after', Number(x.heat[2]).toFixed(0) + '%'] : null
        ])
      : '<div class="empty">No heat exposure logged this day</div>', { flush: true });

    h += '</div>';

    var dr = $('#drawer');
    dr.innerHTML = h;
    dr.classList.add('on');
    document.body.classList.add('drawn');
    $('#drX').onclick = closeDetail;
    dr.querySelector('.dr-h').focus && dr.querySelector('.dr-x').focus();
  }

  function closeDetail() {
    $('#drawer').classList.remove('on');
    document.body.classList.remove('drawn');
  }

  /* ── Settings ────────────────────────────────────────────────────────── */

  function renderSettings() {
    var d = state.data || {};
    var cur = state.slug;

    var h = card('Athlete', '<div class="body-flush">' + ATHLETES.map(function (a) {
      return '<button type="button" class="pickrow' + (a.slug === cur ? ' on' : '') +
        '" data-slug="' + a.slug + '">' +
        '<span class="gate-mark">' + esc(a.name.charAt(0)) + '</span>' +
        '<span class="gate-row-t"><b>' + esc(a.name) + '</b><span>' +
        (a.slug === cur ? 'showing now' : 'switch to this profile') + '</span></span>' +
        '<span class="gate-go">' + (a.slug === cur ? '✓' : '→') + '</span></button>';
    }).join('') + '</div>', { flush: true });

    h += card('Data', '<table class="tbl"><tbody>' +
      '<tr><td class="lbl">Last refreshed</td><td class="t">' +
      esc(d.generated || '—') + '</td></tr>' +
      '<tr><td class="lbl">Refresh schedule</td><td>nightly, 06:20</td></tr>' +
      '<tr><td class="lbl">Threshold power</td><td>' +
      (d.resolvedFtp ? d.resolvedFtp + 'w' : '—') + '</td></tr>' +
      '</tbody></table>',
      { flush: true, foot: 'Figures come from the nightly published subset. ' +
                          'Body weight, HRV and resting HR are never published.' });

    h += card('App', '<table class="tbl"><tbody>' +
      '<tr><td class="lbl">Coach chat</td><td>Telegram</td></tr>' +
      '<tr><td class="lbl">Offline</td><td>' +
      (('serviceWorker' in navigator) ? 'cached for offline use' : 'not supported') +
      '</td></tr></tbody></table>', { flush: true });

    h += libraryBlock();

    var TOOLS = [
      ['Fuelling calculator', '../cycling/fuelling-calculator.html', 'carbs, fluid and sodium per hour'],
      ['Sweat rate', '../cycling/sweat-rate-calculator.html', 'fluid loss from a weigh-in'],
      ['Race predictor', '../cycling/triathlon-race-predictor.html', 'split and finish estimates'],
      ['Pacing strategy', '../cycling/pacing-strategy-builder.html', 'build a race pacing plan'],
      ['FTP calculator', '../cycling/ftp-calculator.html', 'threshold from a test effort'],
      ['TSS calculator', '../cycling/tss-calculator.html', 'load for a single session'],
      ['Run pace converter', '../cycling/run-pace-converter.html', 'pace, speed and splits'],
      ['VO2max estimator', '../cycling/vo2max-estimator.html', 'from a time trial'],
      ['Power to weight', '../cycling/power-to-weight.html', 'w/kg across durations'],
      ['CdA calculator', '../cda-calculator/', 'aerodynamic drag from field data'],
      ['Wetsuit decision', '../cycling/cervia-wetsuit.html', 'water temperature call'],
      ['Gear ratios', '../cycling/gear-ratio-calculator.html', 'cadence and speed by gear']
    ];
    h += card('Tools', '<div class="body-flush">' + TOOLS.map(function (x) {
      return '<a class="toolrow" href="' + x[1] + '">' +
        '<span class="gate-row-t"><b>' + esc(x[0]) + '</b><span>' + esc(x[2]) + '</span></span>' +
        '<span class="gate-go">\u2197</span></a>';
    }).join('') + '</div>', { flush: true, foot: 'Calculators on diamondpeak.uk.' });

    h += card('Session', '<div class="body-flush">' +
      '<button type="button" class="pickrow" id="logout">' +
      '<span class="gate-mark">⏻</span>' +
      '<span class="gate-row-t"><b>Log out</b><span>forget this device and choose again</span></span>' +
      '<span class="gate-go">→</span></button></div>', { flush: true });

    $('#v-set').innerHTML = h;
    // Scoped to the view, not to the first .body-flush: several cards use that class
    // now, and binding to the first one silently stops working when a card is added
    // above it.
    $('#v-set').onclick = function (e) {
      var b = e.target.closest('.pickrow[data-slug]');
      if (b) pick(b.dataset.slug);
    };
    // Clearing the stored slug is the whole of "logging out" - there is no session to
    // end, because there is no auth. It returns to the picker, which is what was
    // actually missing: you could switch athlete but never get back to the front door.
    $('#logout').onclick = function () {
      try { localStorage.removeItem(KEY); } catch (e) { /* nothing to clear */ }
      openGate();
    };

    var ls = $('#libSeg');
    if (ls) ls.onclick = function (e) {
      var b = e.target.closest('button');
      if (!b) return;
      state.libGroup = b.dataset.g;
      renderSettings();
    };
  }

  /* ── data ────────────────────────────────────────────────────────────── */

  function skeleton() {
    var s = '<div class="card">' + '<div class="skel"></div>'.repeat(3) + '</div>';
    TABS.forEach(function (t) { if (!t.href) $('#v-' + t.id).innerHTML = s; });
  }

  function renderAll() {
    // Each view is isolated. Previously these ran as one statement, so the first
    // exception skipped every later view: renderGoals threw on Calum, Settings never
    // rendered, and Settings is the only route to switching athlete - which stranded
    // the user on a profile with no way out. A broken view should cost that view only.
    [['today', renderToday], ['cal', renderCalendar], ['trends', renderTrends],
     ['goals', renderGoals], ['set', renderSettings]].forEach(function (v) {
      try {
        v[1]();
      } catch (err) {
        if (window.console) console.error('[peak] ' + v[0] + ' failed to render:', err);
        var el = $('#v-' + v[0]);
        if (el) {
          el.innerHTML = '<div class="card"><div class="empty">This view could not be ' +
            'built from the current data.</div></div>';
        }
      }
    });

    var p = state.data.profile || {};
    // Set unconditionally: guarding on race_date left the PREVIOUS athlete's countdown
    // on screen after a switch.
    if (p.race_date) {
      $('#cd').innerHTML = '<b>' + daysBetween(todayISO(), p.race_date) + '</b>days to ' +
        esc((p.race_name || '').split(' ').slice(0, 2).join(' '));
    } else {
      $('#cd').innerHTML = '<b>—</b>no race set';
    }
    if (state.tab === 'trends') drawTrend();
  }

  /* ── entry screen ────────────────────────────────────────────────────── */
  /* A profile picker, not a login. The data behind it is published to a public
   * URL, so a client-side gate would protect nothing; it is here because an app
   * opens on "who are you", and because remembering the answer removes the
   * segmented control from every subsequent screen. */

  var KEY = 'cc.athlete';

  function stored() {
    try {
      var v = localStorage.getItem(KEY);
      return ATHLETES.some(function (a) { return a.slug === v; }) ? v : null;
    } catch (e) { return null; }  // private mode / storage disabled
  }

  function buildGate() {
    $('#gateList').innerHTML = ATHLETES.map(function (a) {
      return '<button type="button" class="gate-row" data-slug="' + a.slug + '">' +
        '<span class="gate-mark">' + esc(a.name.charAt(0)) + '</span>' +
        '<span class="gate-row-t"><b>' + esc(a.name) + '</b>' +
        '<span>Training almanac</span></span>' +
        '<span class="gate-go" aria-hidden="true">→</span></button>';
    }).join('');

    $('#gateList').onclick = function (e) {
      var b = e.target.closest('.gate-row');
      if (!b) return;
      b.classList.add('picked');
      // Let the mark fill before the screen goes: the pick should feel confirmed
      // rather than dismissed.
      setTimeout(function () { pick(b.dataset.slug); }, 160);
    };

  }

  function openGate() {
    document.body.classList.add('gated');
    var g = $('#gate');
    g.classList.remove('out');
    Array.prototype.forEach.call(g.querySelectorAll('.gate-row'), function (r) {
      r.classList.remove('picked');
    });
    var first = g.querySelector('.gate-row');
    if (first) first.focus();
  }

  function closeGate() {
    $('#gate').classList.add('out');
    document.body.classList.remove('gated');
  }

  function pick(slug) {
    try { localStorage.setItem(KEY, slug); } catch (e) { /* choice just won't persist */ }
    closeGate();
    if (state.tab === 'set') show('today');
    if (slug !== state.slug || !state.data) load(slug);
  }

  function load(slug) {
    state.slug = slug;
    var a = ATHLETES.filter(function (x) { return x.slug === slug; })[0];
    $('#whoName').innerHTML = '<span>' + esc(a ? a.name : slug) + '</span>' +
      '<span class="chg">change</span>';
    state.calMonth = null; state.calDay = null;
    skeleton();
    fetch('../ClaudeCoach/public/training-data-' + slug + '.json', { cache: 'no-cache' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) { state.data = j; renderAll(); })
      .catch(function () {
        $('#v-today').innerHTML =
          '<div class="card"><div class="empty">Could not load ' + esc(slug) +
          '’s data. It refreshes nightly.</div></div>' + chatCTA('Ask the coach instead');
      });
  }

  function loadLibrary() {
    fetch('../ClaudeCoach/public/session-library.json', { cache: 'default' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { state.lib = j; if (state.lib && state.data) renderSettings(); })
      .catch(function () { /* library is a nice-to-have; the rest of the app stands */ });
  }

  function init() {
    if (typeof Chart !== 'undefined') {
      Chart.defaults.font.family = 'DM Mono, monospace';
      Chart.defaults.font.size = 9;
      Chart.defaults.color = C.muted;
    }
    buildChrome();
    buildGate();
    var h = (location.hash || '').replace('#', '');
    if (TABS.some(function (t) { return t.id === h; })) state.tab = h;
    show(state.tab);

    // A remembered profile skips the gate entirely - being asked who you are on
    // every launch is the thing that makes a web app feel like a website.
    var s = stored();
    if (s) { closeGate(); load(s); } else { openGate(); }

    loadLibrary();
    if ('serviceWorker' in navigator && location.protocol === 'https:') {
      navigator.serviceWorker.register('sw.js', { scope: './' }).catch(function () {});
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else { init(); }
})();
