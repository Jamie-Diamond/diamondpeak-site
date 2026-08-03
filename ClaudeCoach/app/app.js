/* ClaudeCoach app — view logic.
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

  var TABS = [
    { id: 'today', label: 'Today', icon: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l2.5 2"/>' },
    { id: 'cal', label: 'Calendar', icon: '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3.5v3M16 3.5v3"/>' },
    { id: 'trends', label: 'Trends', icon: '<path d="M4 19h16"/><path d="M4 15l4.5-5L12 13.5 20 6"/>' },
    { id: 'lib', label: 'Library', icon: '<path d="M5 4.5h6a2 2 0 0 1 2 2V20a1.6 1.6 0 0 0-1.6-1.6H5z"/><path d="M19 4.5h-6a2 2 0 0 0-2 2V20a1.6 1.6 0 0 1 1.6-1.6H19z"/>' },
    { id: 'goals', label: 'Goals', icon: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.4"/><path d="M12 4v2.5M12 17.5V20M4 12h2.5M17.5 12H20"/>' }
  ];

  // Each entry is one chart plus the numbers that belong with it.
  var TRENDS = [
    { id: 'fit',  label: 'Fitness' },
    { id: 'load', label: '±7 days' },
    { id: 'heat', label: 'Heat' },
    { id: 'fuel', label: 'Fuel' },
    { id: 'plan', label: 'Plan' }
  ];

  var TELEGRAM = 'https://t.me/ClaudeCoachTri_bot';
  var SPORT = { Swim: 'swim', Ride: 'bike', VirtualRide: 'bike', GravelRide: 'bike',
                Run: 'run', Brick: 'run', WeightTraining: 'strength', Workout: 'strength' };

  var C = { ink: '#18160f', ink2: '#4a4535', muted: '#9a9080', rule: '#ddd8cc',
            green: '#1d6840', blue: '#1a5276', amber: '#b7791f', red: '#b91c1c',
            paper: '#f8f5ef' };

  var state = {
    slug: 'jamie', tab: 'today', data: null, lib: null,
    chart: null, trend: 'fit', calMonth: null, calDay: null, libGroup: null
  };

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
    $('#tabs').innerHTML = TABS.map(function (t) {
      return '<button type="button" role="tab" data-tab="' + t.id + '" aria-selected="' +
        (t.id === state.tab) + '"><svg viewBox="0 0 24 24" aria-hidden="true">' + t.icon +
        '</svg><span>' + t.label + '</span></button>';
    }).join('');
    $('#tabs').onclick = function (e) {
      var b = e.target.closest('button');
      if (b) show(b.dataset.tab);
    };

    function net() { $('#off').classList.toggle('on', !navigator.onLine); }
    addEventListener('online', net); addEventListener('offline', net); net();
  }

  function show(tab) {
    state.tab = tab;
    location.hash = tab;
    TABS.forEach(function (t) {
      $('#v-' + t.id).classList.toggle('on', t.id === tab);
      var b = $('#tabs button[data-tab="' + t.id + '"]');
      if (b) b.setAttribute('aria-selected', String(t.id === tab));
    });
    scrollTo({ top: 0, behavior: 'instant' });
    // Charts are drawn only while visible: a canvas sized inside a display:none
    // section comes out 0px wide and stays that way.
    if (tab === 'trends') drawTrend();
  }

  /* ── shared bits ─────────────────────────────────────────────────────── */

  function chatCTA(sub) {
    return '<a class="chat" href="' + TELEGRAM + '" target="_blank" rel="noopener">' +
      '<svg viewBox="0 0 24 24"><path d="M21 4 3 11l5 2 2 5 3-4 5 3z"/></svg>' +
      '<span><span class="t">Ask the coach</span><span class="s">' + esc(sub) + '</span></span>' +
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
    return '<div class="sesh' + (done ? ' done' : '') + '">' +
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

    h += chatCTA('Telegram · @ClaudeCoachTri_bot');
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
      fuel: { title: 'Carbohydrate intake',
              foot: 'Grams per hour, rides only. The line is the mean across logged rides.' },
      plan: { title: 'Plan vs actual',
              foot: 'Weekly planned and completed TSS.' }
    }[sel];

    var zoomable = (sel === 'fit' || sel === 'heat');
    var action = zoomable
      ? '<button type="button" class="card-a" id="zreset">Reset zoom</button>' : '';

    var h = seg + card(META.title,
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
        h += card('Power curve', '<table class="tbl">' +
          '<thead><tr><th>Duration</th><th>Now</th><th>Last season</th></tr></thead><tbody>' +
          pc.map(function (r) {
            return '<tr><td class="lbl">' + esc(r.label) + '</td><td class="t">' +
              (r.w != null ? r.w + 'w' : '—') + '</td><td>' +
              (r.wPrev != null ? r.wPrev + 'w' : '—') + '</td></tr>';
          }).join('') + '</tbody></table>', { flush: true });
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
      var ev = (ha.events || []).slice().reverse().slice(0, 12);
      if (ev.length) {
        h += card('Recent exposures', '<table class="tbl">' +
          '<thead><tr><th>Date</th><th>Method</th><th>Temp</th></tr></thead><tbody>' +
          ev.map(function (e) {
            return '<tr><td class="lbl">' + esc(dow(e[0]) + ' ' + dnum(e[0])) + '</td><td>' +
              esc(e[3] || '—') + '</td><td class="t">' +
              (e[2] != null ? Number(e[2]).toFixed(1) + '°' : '—') + '</td></tr>';
          }).join('') + '</tbody></table>', { flush: true });
      }
    }

    if (sel === 'fuel') {
      var cb = ((d.progressData || {}).carb) || [];
      if (!cb.length) {
        h += '<p class="hint">No fuelling logged yet. Tell the coach what you took on and it lands here.</p>';
      } else {
        var rates = cb.map(function (r) { return r.g_per_hr; }).filter(function (v) { return v != null; });
        var avg = rates.reduce(function (a, b) { return a + b; }, 0) / (rates.length || 1);
        h += '<div class="minis">' +
          mini(avg.toFixed(0), 'g/hr mean') +
          mini(Math.max.apply(null, rates).toFixed(0), 'best') +
          mini(Math.min.apply(null, rates).toFixed(0), 'lowest') +
          mini(cb.length, 'rides') + '</div>';
        h += card('Logged rides', '<table class="tbl">' +
          '<thead><tr><th>Date</th><th>Ride</th><th>g/hr</th></tr></thead><tbody>' +
          cb.slice().reverse().map(function (r) {
            return '<tr><td class="lbl">' + esc(dow(r.date) + ' ' + dnum(r.date)) + '</td><td>' +
              esc(r.name || r.sport) + '</td><td class="t">' +
              (r.g_per_hr != null ? Number(r.g_per_hr).toFixed(0) : '—') + '</td></tr>';
          }).join('') + '</tbody></table>', { flush: true });
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
        tooltip: {
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

  function drawTrend() {
    if (typeof Chart === 'undefined' || !state.data) return;
    var el = document.getElementById('c-now');
    if (!el) return;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    var fn = { fit: chartFitness, load: chartLoad, heat: chartHeat,
               fuel: chartFuel, plan: chartPlan }[state.trend];
    if (fn) state.chart = fn(el, state.data);
  }

  function chartFitness(el, d) {
    var p = d.profile || {}, cp = d.ctlProjection || {};
    var xy = function (rows) {
      return (rows || []).map(function (r) { return { x: doy(r[0]), y: r[1] }; });
    };
    var xyObj = function (rows) {
      return (rows || []).map(function (r) { return { x: doy(r.date), y: r.ctl }; });
    };

    var ds = [];

    // Race-day target band first so it sits behind every line.
    if (cp.target_ctl_min != null && cp.race_date) {
      var x0 = doy(todayISO()), x1 = doy(cp.race_date);
      ds.push({
        label: 'Target band', order: 9,
        data: [{ x: x0, y: cp.target_ctl_max }, { x: x1, y: cp.target_ctl_max }],
        borderColor: 'rgba(29,104,64,.35)', borderWidth: 1, borderDash: [4, 3],
        pointRadius: 0, fill: '+1', backgroundColor: 'rgba(29,104,64,.09)'
      });
      ds.push({
        label: '_band-lo', order: 10,
        data: [{ x: x0, y: cp.target_ctl_min }, { x: x1, y: cp.target_ctl_min }],
        borderColor: 'rgba(29,104,64,.35)', borderWidth: 1, borderDash: [4, 3],
        pointRadius: 0, fill: false
      });
    }

    // Barcelona 2023 and last season, thin and dashed - reference, not subject.
    if ((d.fitnessPrev2 || []).length) {
      ds.push({
        label: (p.prev2_race_name || '2023').replace(' IM', " '23"), order: 6,
        data: xy(d.fitnessPrev2), borderColor: C.blue, borderWidth: 1,
        borderDash: [5, 3], pointRadius: 0, tension: 0.3, fill: false
      });
    }
    if ((d.fitnessPrev || []).length) {
      ds.push({
        label: 'Last season', order: 5,
        data: xy(d.fitnessPrev), borderColor: C.muted, borderWidth: 1,
        borderDash: [2, 3], pointRadius: 0, tension: 0.3, fill: false
      });
    }
    if ((cp.planned_build || []).length) {
      ds.push({
        label: 'Planned build', order: 3,
        data: xyObj(cp.planned_build), borderColor: C.green, borderWidth: 1.4,
        borderDash: [3, 3], pointRadius: 0, tension: 0.25, fill: false
      });
    }
    ds.push({
      label: 'This season', order: 1,
      data: xy(d.fitnessThis), borderColor: C.green, borderWidth: 2,
      pointRadius: 0, tension: 0.25, fill: false
    });
    if ((cp.target_milestones || []).length) {
      ds.push({
        label: 'Milestones', order: 0, showLine: false,
        data: cp.target_milestones.map(function (m) {
          return { x: doy(m.date), y: m.ctl, lbl: m.label };
        }),
        borderColor: C.amber, backgroundColor: C.amber,
        pointRadius: 3.4, pointStyle: 'rectRot'
      });
    }

    var lines = [];
    if (cp.race_date) {
      lines.push({ x: doy(cp.race_date),
                   label: (p.race_name || 'Race').replace(/^IM /, ''), color: C.green });
    }
    if (p.prev2_race_date) {
      lines.push({ x: doy(p.prev2_race_date),
                   label: (p.prev2_race_name || '2023').replace(' IM', " '23"), color: C.blue });
    }

    var o = baseOpts();
    o.scales = axes(false, 'CTL');
    o.plugins.zoom = zoomOpts();
    o.plugins.vlines = { lines: lines };
    o.plugins.tooltip.callbacks = {
      title: function (items) { return doyLabel(items[0].parsed.x, true); },
      label: function (it) {
        var raw = it.raw || {};
        return (it.dataset.label || '') + ': ' + Number(it.parsed.y).toFixed(1) +
          (raw.lbl ? ' · ' + raw.lbl : '');
      }
    };
    return new Chart(el, { type: 'line', data: { datasets: ds }, options: o, plugins: [vlines] });
  }

  function chartLoad(el, d) {
    var lc = (d.loadChart || []).slice();
    var t = todayISO();
    var tssOf = function (r) {
      return (r.activities || []).reduce(function (a, x) { return a + (x.tss || 0); }, 0);
    };
    var o = baseOpts();
    o.scales = {
      x: {
        type: 'linear', offset: true,
        ticks: {
          color: C.muted, autoSkip: false, maxRotation: 0,
          font: { family: 'DM Mono', size: 9 },
          callback: function (v) {
            var r = lc[Math.round(v)];
            return r ? dow(r.date).charAt(0) + dnum(r.date) : '';
          }
        },
        grid: { display: false }, border: { color: C.rule }
      },
      y: {
        position: 'left', beginAtZero: true,
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
    o.plugins.tooltip.callbacks = {
      title: function (items) {
        var r = lc[items[0].dataIndex];
        return r ? dow(r.date) + ' ' + dnum(r.date) + (r.projected ? ' (planned)' : '') : '';
      },
      label: function (it) {
        if (it.dataset.yAxisID === 'y1') return 'TSB ' + signed(it.parsed.y);
        var r = lc[it.dataIndex];
        var names = (r.activities || []).map(function (a) {
          return a.sport + ' ' + hhmm(a.dur);
        });
        return ['TSS ' + Math.round(it.parsed.y)].concat(names);
      }
    };
    return new Chart(el, {
      data: {
        labels: lc.map(function (r, i) { return i; }),
        datasets: [
          {
            type: 'bar', label: 'TSS', yAxisID: 'y', order: 2,
            data: lc.map(tssOf),
            backgroundColor: lc.map(function (r) {
              return r.projected || r.date > t ? 'rgba(29,104,64,.22)' : 'rgba(29,104,64,.72)';
            }),
            borderWidth: 0, borderRadius: 2, barPercentage: 0.62, categoryPercentage: 0.9
          },
          {
            type: 'line', label: 'TSB', yAxisID: 'y1', order: 1,
            data: lc.map(function (r) { return r.tsb; }),
            borderColor: C.ink2, borderWidth: 1.5, pointRadius: 0, tension: 0.3, fill: false
          }
        ]
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
          ? raw.lbl + (raw.temp != null ? ' · ' + Number(raw.temp).toFixed(1) + '°' : '')
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
                   lbl: e[3], temp: e[2] };
        }),
        borderColor: C.amber, backgroundColor: C.amber, pointRadius: 3
      });
    }
    return new Chart(el, { type: 'line', data: { datasets: ds }, options: o });
  }

  function chartFuel(el, d) {
    var cb = (((d.progressData || {}).carb) || []).slice()
      .sort(function (a, b) { return a.date < b.date ? -1 : 1; });
    var rates = cb.map(function (r) { return r.g_per_hr; });
    var avg = rates.length ? rates.reduce(function (a, b) { return a + b; }, 0) / rates.length : 0;
    var o = baseOpts();
    o.scales = {
      x: {
        type: 'linear', offset: true,
        ticks: {
          color: C.muted, autoSkip: true, maxRotation: 0, maxTicksLimit: 6,
          font: { family: 'DM Mono', size: 9 },
          callback: function (v) {
            var r = cb[Math.round(v)];
            return r ? dnum(r.date) + ' ' +
              new Date(r.date + 'T12:00:00').toLocaleDateString('en-GB', { month: 'short' }) : '';
          }
        },
        grid: { display: false }, border: { color: C.rule }
      },
      y: {
        beginAtZero: true,
        title: { display: true, text: 'g carb / hr', color: C.muted,
                 font: { family: 'DM Mono', size: 9 } },
        ticks: { color: C.muted, font: { family: 'DM Mono', size: 9 }, maxTicksLimit: 5 },
        grid: { color: C.rule, drawTicks: false }, border: { display: false }
      }
    };
    o.plugins.tooltip.callbacks = {
      title: function (items) {
        var r = cb[items[0].dataIndex];
        return r ? dow(r.date) + ' ' + dnum(r.date) : '';
      },
      label: function (it) {
        var r = cb[it.dataIndex] || {};
        return [Number(it.parsed.y).toFixed(0) + ' g/hr',
                r.name || '', r.dur ? hhmm(r.dur) : ''].filter(Boolean);
      }
    };
    return new Chart(el, {
      data: {
        labels: cb.map(function (r, i) { return i; }),
        datasets: [
          {
            type: 'bar', label: 'g/hr', order: 2, data: rates,
            backgroundColor: 'rgba(26,82,118,.68)', borderWidth: 0,
            borderRadius: 2, barPercentage: 0.55, categoryPercentage: 0.9
          },
          {
            type: 'line', label: 'Mean ' + avg.toFixed(0), order: 1,
            data: cb.map(function () { return avg; }),
            borderColor: C.amber, borderWidth: 1, borderDash: [4, 3],
            pointRadius: 0, fill: false
          }
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

  function renderLibrary() {
    var lib = state.lib;
    if (!lib) { $('#v-lib').innerHTML = '<div class="card"><div class="skel"></div></div>'; return; }
    var types = lib.session_types || {};
    var groups = Object.keys(types);
    var sel = state.libGroup || groups[0];
    var total = groups.reduce(function (a, g) {
      return a + Object.keys(types[g]).filter(function (k) { return k[0] !== '_'; }).length;
    }, 0);

    var h = '<div class="seg wrap" id="libSeg">' + groups.map(function (g) {
      return '<button type="button" data-g="' + esc(g) + '" aria-selected="' +
        (g === sel) + '">' + esc(g) + '</button>';
    }).join('') + '</div>';

    var entries = Object.keys(types[sel] || {}).filter(function (k) { return k[0] !== '_'; });
    h += card(sel.replace(/_/g, ' ') + ' · ' + entries.length + ' of ' + total,
      '<div class="lib">' + (entries.length ? entries.map(function (name) {
        var s = types[sel][name] || {};
        var prog = (s.progression || []).map(function (p) {
          if (p.reps && p.min) return p.reps + '×' + p.min + 'min';
          if (p.bike_min) return p.bike_min + '/' + p.run_min + 'min';
          return null;
        }).filter(Boolean);
        return '<details><summary><span class="nm">' + esc(name.replace(/_/g, ' ')) + '</span>' +
          '<span class="zn">' + esc([s.zone, s.if != null ? 'IF ' + s.if : null]
            .filter(Boolean).join(' · ')) + '</span></summary>' +
          '<div class="inner">' +
          '<div class="kv">' + esc([s.system, s.min_phase ? 'from ' + s.min_phase : null,
               s.duration ? String(s.duration).replace(/_/g, ' ') : null].filter(Boolean).join(' · ')) +
          '</div>' +
          (prog.length ? '<div class="prog">' + prog.map(function (p) {
            return '<span>' + esc(p) + '</span>';
          }).join('') + '</div>' : '') +
          (s.rest_min ? '<div class="prog"><span>' + s.rest_min + 'min rest</span></div>' : '') +
          (s.note ? '<p class="note">' + esc(s.note) + '</p>' : '') +
          (s.dose ? '<p class="note">' + esc(s.dose) + '</p>' : '') +
          '</div></details>';
      }).join('') : '<div class="empty">No sessions in this group</div>') + '</div>', { flush: true });

    $('#v-lib').innerHTML = h;
    $('#libSeg').onclick = function (e) {
      var b = e.target.closest('button');
      if (!b) return;
      state.libGroup = b.dataset.g;
      renderLibrary();
    };
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

    var pr = p.prev_race, tg = p.race_targets;
    if (pr || tg) {
      h += card('Splits · last race vs target', '<table class="tbl">' +
        '<thead><tr><th>Leg</th><th>' + esc(pr ? pr.date.slice(0, 4) : 'Last') +
        '</th><th>Target</th></tr></thead><tbody>' +
        [['Swim', 'swim_time'], ['T1/T2', 't1t2_time'], ['Bike', 'bike_time'],
         ['Run', 'run_time'], ['Total', 'total_time']].map(function (row) {
          var isTotal = row[0] === 'Total';
          return '<tr' + (isTotal ? ' class="hl"' : '') + '><td class="lbl">' + row[0] + '</td>' +
            '<td>' + esc((pr && pr[row[1]]) || '—') + '</td>' +
            '<td class="' + (isTotal ? 't' : '') + '">' + esc((tg && tg[row[1]]) || '—') + '</td></tr>';
        }).join('') + '</tbody></table>', { flush: true });
    }

    h += chatCTA('Ask about pacing or the plan');
    $('#v-goals').innerHTML = h;
  }

  /* ── data ────────────────────────────────────────────────────────────── */

  function skeleton() {
    var s = '<div class="card">' + '<div class="skel"></div>'.repeat(3) + '</div>';
    TABS.forEach(function (t) { $('#v-' + t.id).innerHTML = s; });
  }

  function renderAll() {
    renderToday(); renderCalendar(); renderTrends(); renderLibrary(); renderGoals();
    var p = state.data.profile || {};
    if (p.race_date) {
      $('#cd').innerHTML = '<b>' + daysBetween(todayISO(), p.race_date) + '</b>days to ' +
        esc((p.race_name || '').split(' ').slice(0, 2).join(' '));
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

    $('#switch').onclick = openGate;
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
    if (slug !== state.slug || !state.data) load(slug);
  }

  function load(slug) {
    state.slug = slug;
    var a = ATHLETES.filter(function (x) { return x.slug === slug; })[0];
    $('#whoName').textContent = a ? a.name : slug;
    state.calMonth = null; state.calDay = null;
    skeleton();
    fetch('public/training-data-' + slug + '.json', { cache: 'no-cache' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) { state.data = j; renderAll(); })
      .catch(function () {
        $('#v-today').innerHTML =
          '<div class="card"><div class="empty">Could not load ' + esc(slug) +
          '’s data. It refreshes nightly.</div></div>' + chatCTA('Ask the coach instead');
      });
  }

  function loadLibrary() {
    fetch('public/session-library.json', { cache: 'default' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { state.lib = j; if (state.lib) renderLibrary(); })
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
