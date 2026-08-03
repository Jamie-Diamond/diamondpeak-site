/* ClaudeCoach app — view logic.
 *
 * Static by design: it reads the nightly public subset (public/training-data-<slug>.json)
 * and the published session library (public/session-library.json) and renders five views. There is no
 * backend to talk to (GitHub Pages), so Chat deep-links to Telegram; when FastAPI lands,
 * that one href becomes a route.
 *
 * Deliberately dependency-free apart from Chart.js, which the legacy pages already load
 * from the same CDN so the service worker has it cached either way.
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

  var TELEGRAM = 'https://t.me/ClaudeCoachTri_bot';
  var SPORT = { Swim: 'swim', Ride: 'bike', VirtualRide: 'bike', GravelRide: 'bike', Run: 'run', Brick: 'run' };

  var state = { slug: 'jamie', tab: 'today', data: null, lib: null, chart: null };
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
  function todayISO() { return new Date().toISOString().slice(0, 10); }
  function daysBetween(a, b) {
    return Math.round((new Date(b + 'T12:00:00') - new Date(a + 'T12:00:00')) / 864e5);
  }
  function sportClass(s) { return 'sp-' + (SPORT[s] || 'other'); }
  function signed(n, dp) {
    var v = Number(n).toFixed(dp == null ? 1 : dp);
    return (Number(n) >= 0 ? '+' : '') + v;
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
    if (tab === 'trends') drawTrends();
  }

  /* ── views ───────────────────────────────────────────────────────────── */

  function chatCTA(sub) {
    return '<a class="chat" href="' + TELEGRAM + '" target="_blank" rel="noopener">' +
      '<svg viewBox="0 0 24 24"><path d="M21 4 3 11l5 2 2 5 3-4 5 3z"/></svg>' +
      '<span><span class="t">Ask the coach</span><span class="s">' + esc(sub) + '</span></span>' +
      '<span class="go">→</span></a>';
  }

  function renderToday() {
    var d = state.data, k = d.kpi || {}, p = d.profile || {};
    var t = todayISO();
    var cal = (d.weekCalendar || []);
    var today = cal.filter(function (s) { return s.date === t; });
    var next = cal.filter(function (s) { return s.date > t; }).slice(0, 4);
    var ramp = k.ramp7d;

    var h = '';
    h += '<div class="figures">' +
      fig(Number(k.ctl).toFixed(1), 'Fitness', 'CTL') +
      fig(Number(k.atl).toFixed(1), 'Fatigue', 'ATL') +
      fig(signed(k.tsb), 'Form', 'TSB', k.tsb >= 0 ? 'pos' : (k.tsb < -25 ? 'neg' : 'flat')) +
      '</div>';

    h += '<p class="eyebrow">Today · ' + esc(dow(t)) + ' ' + dnum(t) + '</p>';
    h += '<div class="sheet flush">' + (today.length ? today.map(seshRow).join('') :
      '<div class="empty">Rest day — nothing scheduled</div>') + '</div>';

    if (ramp != null) {
      h += '<p class="eyebrow">7-day ramp</p><div class="sheet">' +
        '<div style="display:flex;align-items:baseline;gap:9px">' +
        '<span style="font-family:\'Libre Baskerville\',serif;font-size:22px;letter-spacing:-.02em">' +
        signed(ramp) + '</span>' +
        '<span style="font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)">CTL / week</span></div>' +
        '<div class="bar' + (Math.abs(ramp) > 5 ? ' warn' : '') + '"><i style="width:' +
        Math.min(100, Math.abs(ramp) / 8 * 100).toFixed(0) + '%"></i></div>' +
        '<p style="margin:8px 0 0;font-size:13px;color:var(--ink-2)">' +
        (Math.abs(ramp) > 5 ? 'Above the 5-point weekly ramp guide.' : 'Inside the weekly ramp guide.') +
        '</p></div>';
    }

    if (d.heatAccl && d.heatAccl.current != null) {
      var ha = d.heatAccl;
      h += '<p class="eyebrow">Heat acclimation</p><div class="sheet">' +
        '<div style="display:flex;align-items:baseline;gap:9px">' +
        '<span style="font-family:\'Libre Baskerville\',serif;font-size:22px;letter-spacing:-.02em">' +
        Number(ha.current).toFixed(0) + '%</span>' +
        '<span style="font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)">peak ' +
        Number(ha.peak).toFixed(0) + '% · ' + ha.entries + ' sessions</span></div>' +
        '<div class="bar"><i style="width:' + Number(ha.current).toFixed(0) + '%"></i></div></div>';
    }

    h += '<p class="eyebrow">Coming up</p><div class="sheet flush">' +
      (next.length ? groupByDay(next) : '<div class="empty">Nothing planned yet</div>') + '</div>';

    h += chatCTA('Telegram · @ClaudeCoachTri_bot');
    $('#v-today').innerHTML = h;
  }

  function fig(n, label, detail, cls) {
    return '<div class="fig"><span class="n ' + (cls || '') + '">' + esc(n) + '</span>' +
      '<span class="l">' + esc(label) + '</span><span class="d">' + esc(detail) + '</span></div>';
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

  function renderCalendar() {
    var cal = (state.data.weekCalendar || []).slice().sort(function (a, b) {
      return a.date < b.date ? -1 : 1;
    });
    var t = todayISO();
    var past = cal.filter(function (s) { return s.date < t; });
    var rest = cal.filter(function (s) { return s.date >= t; });

    var h = '';
    h += '<p class="eyebrow">Planned · from today</p>';
    h += '<div class="sheet flush">' + (rest.length ? groupByDay(rest) :
      '<div class="empty">No sessions on the calendar</div>') + '</div>';
    h += '<p class="eyebrow">Completed</p>';
    h += '<div class="sheet flush">' + (past.length ? groupByDay(past.slice().reverse()) :
      '<div class="empty">Nothing logged in this window</div>') + '</div>';

    var rec = state.data.recent || [];
    if (rec.length) {
      h += '<p class="eyebrow">Recent activity detail</p><div class="sheet flush">' +
        rec.slice(0, 8).map(function (r) {
          return '<div class="sesh done"><span class="tick">✓</span><span class="body">' +
            '<span class="nm"><span class="sp ' + sportClass(r.sport) + '"></span>' + esc(r.name) + '</span>' +
            '<span class="meta">' + esc([dow(r.date) + ' ' + dnum(r.date), r.pace,
              r.hr ? r.hr + ' bpm' : null, r.powNp ? r.powNp + 'w np' : null]
              .filter(Boolean).join(' · ')) + '</span></span>' +
            '<span class="rt"><b>' + hhmm(r.dur) + '</b>' +
            (r.tss != null ? Math.round(r.tss) + ' tss' : '') + '</span></div>';
        }).join('') + '</div>';
    }
    $('#v-cal').innerHTML = h;
  }

  function renderTrends() {
    var d = state.data;
    var h = '';
    h += '<p class="eyebrow">Fitness · this season vs last</p>' +
      '<div class="sheet flush"><div class="chartbox tall"><canvas id="c-fit"></canvas></div></div>';

    var bs = (d.fitnessBySport || {}).current || {};
    var sports = Object.keys(bs);
    if (sports.length) {
      h += '<p class="eyebrow">Fitness by sport</p><div class="sheet flush">' +
        sports.map(function (s) {
          var series = bs[s] || [];
          var last = series.length ? series[series.length - 1][1] : 0;
          var max = series.reduce(function (m, r) { return Math.max(m, r[1]); }, 1);
          return '<div class="sesh"><span class="tick"><span class="sp ' + sportClass(s) +
            '" style="margin:0"></span></span><span class="body"><span class="nm">' + esc(s) +
            '</span><div class="bar"><i style="width:' + (last / max * 100).toFixed(0) +
            '%"></i></div></span><span class="rt"><b>' + Number(last).toFixed(1) +
            '</b>peak ' + Number(max).toFixed(0) + '</span></div>';
        }).join('') + '</div>';
    }

    var pva = d.planVsActual || [];
    if (pva.length) {
      h += '<p class="eyebrow">Plan vs actual · by week</p><div class="sheet flush"><table class="tbl">' +
        '<thead><tr><th>Week</th><th>Type</th><th>Planned</th><th>Actual</th><th>Δ</th></tr></thead><tbody>' +
        pva.slice().reverse().map(function (w) {
          var diff = (w.actual_tss || 0) - (w.planned_tss || 0);
          var pct = w.planned_tss ? Math.round(diff / w.planned_tss * 100) : 0;
          return '<tr><td class="lbl">' + esc(dow(w.week_start) + ' ' + dnum(w.week_start)) +
            '</td><td>' + esc(w.week_type || '—') + '</td><td>' + Math.round(w.planned_tss || 0) +
            '</td><td>' + Math.round(w.actual_tss || 0) + '</td><td class="' +
            (pct >= 0 ? 'pos' : 'neg') + '">' + (pct >= 0 ? '+' : '') + pct + '%</td></tr>';
        }).join('') + '</tbody></table></div>';
    }

    var pc = d.powerCurve || [];
    if (pc.length) {
      h += '<p class="eyebrow">Power curve</p><div class="sheet flush"><table class="tbl">' +
        '<thead><tr><th>Duration</th><th>Now</th><th>Last season</th></tr></thead><tbody>' +
        pc.map(function (r) {
          return '<tr><td class="lbl">' + esc(r.label) + '</td><td class="t">' +
            (r.w != null ? r.w + 'w' : '—') + '</td><td>' +
            (r.wPrev != null ? r.wPrev + 'w' : '—') + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }
    $('#v-trends').innerHTML = h;
  }

  function drawTrends() {
    if (typeof Chart === 'undefined' || !state.data) return;
    var el = document.getElementById('c-fit');
    if (!el) return;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    var mk = function (rows) {
      return (rows || []).map(function (r) { return { x: r[0].slice(5), y: r[1] }; });
    };
    var ink = '#18160f', green = '#1d6840', muted = '#9a9080', rule = '#ddd8cc';
    state.chart = new Chart(el, {
      type: 'line',
      data: {
        datasets: [
          { label: 'This season', data: mk(state.data.fitnessThis), borderColor: green,
            borderWidth: 1.8, pointRadius: 0, tension: 0.28, fill: false },
          { label: 'Last season', data: mk(state.data.fitnessPrev), borderColor: muted,
            borderWidth: 1, pointRadius: 0, tension: 0.28, borderDash: [3, 3], fill: false }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'nearest', intersect: false },
        plugins: {
          legend: { labels: { color: ink, boxWidth: 9, boxHeight: 9,
            font: { family: 'DM Mono', size: 9 } } },
          tooltip: { backgroundColor: ink, titleFont: { family: 'DM Mono', size: 10 },
            bodyFont: { family: 'DM Mono', size: 10 }, displayColors: false }
        },
        scales: {
          x: { type: 'category', ticks: { color: muted, maxTicksLimit: 6,
                 font: { family: 'DM Mono', size: 9 } }, grid: { display: false } },
          y: { ticks: { color: muted, font: { family: 'DM Mono', size: 9 } },
               grid: { color: rule, drawTicks: false } }
        }
      }
    });
  }

  function renderLibrary() {
    var lib = state.lib;
    if (!lib) { $('#v-lib').innerHTML = '<div class="sheet"><div class="skel"></div></div>'; return; }
    var types = lib.session_types || {};
    var groups = Object.keys(types);
    var sel = state.libGroup || groups[0];

    var h = '<p class="eyebrow">Session library · ' +
      groups.reduce(function (a, g) {
        return a + Object.keys(types[g]).filter(function (k) { return k[0] !== '_'; }).length;
      }, 0) + ' sessions</p>';

    h += '<div class="chips">' + groups.map(function (g) {
      return '<button type="button" class="chip" data-g="' + esc(g) + '" aria-pressed="' +
        (g === sel) + '">' + esc(g) + '</button>';
    }).join('') + '</div>';

    var entries = Object.keys(types[sel] || {}).filter(function (k) { return k[0] !== '_'; });
    h += '<div class="lib">' + (entries.length ? entries.map(function (name) {
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
        '<div style="font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)">' +
        esc([s.system, s.min_phase ? 'from ' + s.min_phase : null,
             s.duration ? String(s.duration).replace(/_/g, ' ') : null].filter(Boolean).join(' · ')) +
        '</div>' +
        (prog.length ? '<div class="prog">' + prog.map(function (p) {
          return '<span>' + esc(p) + '</span>';
        }).join('') + '</div>' : '') +
        (s.rest_min ? '<div class="prog"><span>' + s.rest_min + 'min rest</span></div>' : '') +
        (s.note ? '<p class="note">' + esc(s.note) + '</p>' : '') +
        (s.dose ? '<p class="note">' + esc(s.dose) + '</p>' : '') +
        '</div></details>';
    }).join('') : '<div class="empty">No sessions in this group</div>') + '</div>';

    $('#v-lib').innerHTML = h;
    $('#v-lib').querySelector('.chips').onclick = function (e) {
      var b = e.target.closest('.chip');
      if (!b) return;
      state.libGroup = b.dataset.g;
      renderLibrary();
    };
  }

  function renderGoals() {
    var d = state.data, p = d.profile || {}, rp = d.racePredictor || {};
    var h = '';

    h += '<p class="eyebrow">' + esc(p.race_name || 'Race') + ' · ' + esc(p.race_distance || '') + '</p>';
    h += '<div class="sheet">' +
      '<div class="goal"><span class="tag">A</span><span class="v">' + esc(p.a_goal || '—') +
      '</span><span class="n">primary</span></div>' +
      '<div class="goal"><span class="tag">B</span><span class="v">' + esc(p.b_goal || '—') +
      '</span><span class="n">fallback</span></div>' +
      (p.race_date ? '<div class="goal"><span class="tag">—</span><span class="v">' +
        new Date(p.race_date + 'T12:00:00').toLocaleDateString('en-GB',
          { day: 'numeric', month: 'short', year: 'numeric' }) +
        '</span><span class="n">' + daysBetween(todayISO(), p.race_date) + ' days</span></div>' : '') +
      '</div>';

    if (rp.rows && rp.rows.length) {
      h += '<p class="eyebrow">Projection</p><div class="sheet flush"><table class="tbl">' +
        '<thead><tr><th>Scenario</th><th>CTL</th><th>Bike</th><th>Run</th><th>Total</th></tr></thead><tbody>' +
        rp.rows.map(function (r, i) {
          return '<tr' + (i === rp.rows.length - 1 ? ' class="hl"' : '') + '>' +
            '<td class="lbl">' + esc(r.label) + '</td><td>' + Math.round(r.ctl) + '</td>' +
            '<td>' + hhmm(r.bike_min) + '</td><td>' + hhmm(r.run_min) + '</td>' +
            '<td class="t">' + hhmm(r.total_min) + '</td></tr>';
        }).join('') + '</tbody></table></div>' +
        '<p style="margin:-6px 0 15px;font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:.1em;color:var(--muted)">' +
        'Bike IF scales with √CTL from the anchor race.</p>';
    }

    var pr = p.prev_race, tg = p.race_targets;
    if (pr || tg) {
      h += '<p class="eyebrow">Splits · last race vs target</p><div class="sheet flush"><table class="tbl">' +
        '<thead><tr><th>Leg</th><th>' + esc(pr ? pr.date.slice(0, 4) : 'Last') +
        '</th><th>Target</th></tr></thead><tbody>' +
        [['Swim', 'swim_time'], ['T1/T2', 't1t2_time'], ['Bike', 'bike_time'],
         ['Run', 'run_time'], ['Total', 'total_time']].map(function (row) {
          var isTotal = row[0] === 'Total';
          return '<tr' + (isTotal ? ' class="hl"' : '') + '><td class="lbl">' + row[0] + '</td>' +
            '<td>' + esc((pr && pr[row[1]]) || '—') + '</td>' +
            '<td class="' + (isTotal ? 't' : '') + '">' + esc((tg && tg[row[1]]) || '—') + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }

    var cp = d.ctlProjection || {};
    if (cp.target_ctl_min != null) {
      var now = (d.kpi || {}).ctl || 0;
      var lo = cp.target_ctl_min, hi = cp.target_ctl_max;
      h += '<p class="eyebrow">Fitness target for race day</p><div class="sheet">' +
        '<div style="display:flex;align-items:baseline;gap:9px">' +
        '<span style="font-family:\'Libre Baskerville\',serif;font-size:22px;letter-spacing:-.02em">' +
        Number(now).toFixed(1) + '</span>' +
        '<span style="font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)">now · target ' +
        lo + '–' + hi + '</span></div>' +
        '<div class="bar' + (now < lo ? ' warn' : '') + '"><i style="width:' +
        Math.min(100, now / hi * 100).toFixed(0) + '%"></i></div>' +
        '<p style="margin:8px 0 0;font-size:13px;color:var(--ink-2)">' +
        (now >= lo ? 'Inside the target band.' : 'Below the band — ' +
          (lo - now).toFixed(1) + ' CTL to find.') + '</p></div>';
    }

    h += chatCTA('Ask about pacing or the plan');
    $('#v-goals').innerHTML = h;
  }

  /* ── data ────────────────────────────────────────────────────────────── */

  function skeleton() {
    var s = '<div class="sheet">' + '<div class="skel" style="margin:6px 0"></div>'.repeat(3) + '</div>';
    TABS.forEach(function (t) { $('#v-' + t.id).innerHTML = s; });
  }

  function renderAll() {
    renderToday(); renderCalendar(); renderTrends(); renderLibrary(); renderGoals();
    var p = state.data.profile || {};
    if (p.race_date) {
      $('#cd').innerHTML = '<b>' + daysBetween(todayISO(), p.race_date) + '</b>days to ' +
        esc((p.race_name || '').split(' ').slice(0, 2).join(' '));
    }
    if (state.tab === 'trends') drawTrends();
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
    skeleton();
    fetch('public/training-data-' + slug + '.json', { cache: 'no-cache' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) { state.data = j; renderAll(); })
      .catch(function () {
        $('#v-today').innerHTML =
          '<div class="sheet"><div class="empty">Could not load ' + esc(slug) +
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
