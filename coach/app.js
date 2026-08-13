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
    // Chat used to sit raised in the CENTRE of the bar, which only works on an odd
    // number of slots. Adding the opt-in Food tab makes it six for Jamie, so chat moved
    // out to a corner button (offBar) and the bar became an ordinary even row. It keeps
    // the thumb-reachable position without dictating the bar's arithmetic.
    { id: 'trends', label: 'Trends', icon: '<path d="M4 19h16"/><path d="M4 15l4.5-5L12 13.5 20 6"/>' },
    { id: 'goals', label: 'Goals', icon: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.4"/><path d="M12 4v2.5M12 17.5V20M4 12h2.5M17.5 12H20"/>' },
    // Food is OPT-IN per athlete and hidden unless enabled, so the bar is five slots
    // for Kathryn and Calum and six for Jamie. See nutritionOn().
    { id: 'food', label: 'Food', optIn: 'nutrition',
      icon: '<path d="M7 3.5v7a2.5 2.5 0 0 0 5 0v-7"/><path d="M9.5 10.5V20"/><path d="M17 3.5c1.6 1 2.4 2.6 2.4 4.6 0 1.7-.8 2.9-2.4 3.4V20"/>' },
    // Settings has no bar slot: it is entered from the masthead gear. It still needs a
    // TABS entry because show()/skeleton() drive every view from this list.
    { id: 'set', label: 'Settings', offBar: true,
      icon: '<path d="M10.3 3.2h3.4l.5 2.2 1.9.8 1.9-1.2 2.4 2.4-1.2 1.9.8 1.9 2.2.5v3.4l-2.2.5-.8 1.9 1.2 1.9-2.4 2.4-1.9-1.2-1.9.8-.5 2.2h-3.4l-.5-2.2-1.9-.8-1.9 1.2-2.4-2.4 1.2-1.9-.8-1.9-2.2-.5v-3.4l2.2-.5.8-1.9-1.2-1.9 2.4-2.41.9 1.2 1.9-.8z"/><circle cx="12" cy="12" r="3.1"/>' }
  ];

  /* Opt-in views. `nutrition_enabled` is published per athlete by
     publish-nutrition-data.py, which writes nothing at all unless that athlete's
     profile carries nutrition_tracker: true - so the default for a new athlete is off
     by omission rather than by a list of exclusions. A local toggle in Settings can
     hide it for someone it IS published for, but can never reveal a tab whose data was
     never published: there would be nothing to render. */
  function nutritionOn() {
    if (!(state.nutr && state.nutr.nutrition_enabled)) return false;
    var pref = null;
    try { pref = localStorage.getItem('cc.food.' + state.slug); } catch (e) { pref = null; }
    return pref === null ? true : pref === '1';
  }

  function tabAllowed(t) {
    if (t.optIn === 'nutrition') return nutritionOn();
    return true;
  }

  // Each entry is one chart plus the numbers that belong with it.
  var TRENDS = [
    { id: 'fit',  label: 'Fitness' },
    // Volume next to fitness, deliberately adjacent: CTL says how hard the training
    // was, hours say how much of it there was, and the pair is read together. Same
    // seasons, same sport chips, same race alignment - see chartDuration.
    { id: 'dur',  label: 'Hours' },
    { id: 'load', label: '±7 days' },
    { id: 'heat', label: 'Heat' },
    { id: 'fuel', label: 'Fuel' },
    { id: 'plan', label: 'Plan' },
    // Jamie asked for exactly this on 17 Jul and the bot could not do it: "Can you make
    // me a graph of my long run distance by week up to race week... we will probably go
    // to 35k and stop. And have a taper." He then derived it by hand, message by message.
    { id: 'longrun', label: 'Run km' },
    { id: 'zones', label: 'Zones' }
  ];

  var SPORT = { Swim: 'swim', Ride: 'bike', VirtualRide: 'bike', GravelRide: 'bike',
                Run: 'run', Brick: 'run', WeightTraining: 'strength', Workout: 'strength' };

  var C = { ink: '#14181d', ink2: '#3d4650', muted: '#8b949e', rule: '#d8dce1',
            accent: '#10656b', blue: '#1d4e73', amber: '#a86a12', red: '#b3241f',
            paper: '#f6f7f8' };

  var state = {
    slug: 'jamie', tab: 'today', data: null, lib: null,
    chart: null, todayChart: null, trend: 'fit', fitSport: 'all', durSport: 'all',
    calMonth: null, calDay: null, libGroup: null,
    lrMode: 'long', pcMode: 'avg', zoneWin: 'week'
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

  // The published blocks label a sport for display ('Bike'), while an activity carries
  // the intervals.icu name ('Ride'). SPORT knows the second set only, so the fallback is
  // the lower-cased label - not a second mapping, which is how the two would drift.
  function family(s) {
    return SPORT[s] || String(s == null ? '' : s).toLowerCase();
  }
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

  /* ── focus sports ────────────────────────────────────────────────────── */
  /* Which disciplines this athlete is actually training for. Calum rides only; Jamie
   * and Kathryn do all three. It is a FOCUS, not a filter on reality: strength, hikes
   * and a one-off swim are still training that happened, so the Calendar, the ±7 day
   * chart, Today and the week totals never consult this. It only decides what gets its
   * own sub-nav entry, its own card, or its own trend tab. */

  var FOCUS = ['swim', 'bike', 'run'];
  var SKEY = 'cc.sports.';

  // Stored as a comma list rather than JSON: there is nothing to nest, and a corrupt
  // value degrades to "no override" instead of throwing on parse.
  function storedSports() {
    try {
      var raw = localStorage.getItem(SKEY + state.slug);
      if (!raw) return null;
      var got = raw.split(',');
      var out = FOCUS.filter(function (s) { return got.indexOf(s) >= 0; });
      return out.length ? out : null;
    } catch (e) { return null; }   // private mode / storage disabled
  }

  // ONE place the rule lives: stored override, else the published default, else all
  // three. Always returned in FOCUS order, so no consumer has to sort.
  function focusSports() {
    var own = storedSports();
    if (own) return own;
    var pub = ((state.data || {}).sports || []).map(function (s) { return family(s); });
    var out = FOCUS.filter(function (s) { return pub.indexOf(s) >= 0; });
    return out.length ? out : FOCUS.slice();
  }

  /* ── chrome ──────────────────────────────────────────────────────────── */

  function buildTabs() {
    $('#tabs').innerHTML = TABS.filter(function (t) {
      return !t.offBar && tabAllowed(t);
    }).map(function (t) {
      var inner = '<svg viewBox="0 0 24 24" aria-hidden="true">' + t.icon +
        '</svg><span>' + t.label + '</span>';
      var cls = (t.href ? 'out' : '') + (t.primary ? ' primary' : '');
      return t.href
        ? '<a class="' + cls.trim() + '" href="' + t.href + '" target="_blank" rel="noopener">' +
          inner + '</a>'
        : '<button type="button" role="tab" data-tab="' + t.id + '" aria-selected="' +
          (t.id === state.tab) + '">' + inner + '</button>';
    }).join('');
  }

  function buildChrome() {
    buildTabs();
    // The corner chat button. A link out to Telegram until FastAPI lands.
    var fab = document.getElementById('chatFab');
    if (!fab) {
      fab = document.createElement('a');
      fab.id = 'chatFab';
      fab.className = 'fab';
      fab.target = '_blank';
      fab.rel = 'noopener';
      fab.setAttribute('aria-label', 'Chat to the coach');
      fab.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' +
        '<path d="M21 4 3 11l5 2 2 5 3-4 5 3z"/></svg>';
      document.body.appendChild(fab);
    }
    fab.href = TELEGRAM;

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
      var v = $('#v-' + t.id);
      // Null-guarded: a TABS entry whose section is missing must not throw here. An
      // exception in this loop skips every LATER tab, and Settings is last, which is
      // the only route to switching athlete - that combination has stranded the app
      // before.
      if (!v) return;
      v.classList.toggle('on', t.id === tab);
      var b = $('#tabs button[data-tab="' + t.id + '"]');
      if (b) b.setAttribute('aria-selected', String(t.id === tab));
    });
    $('#gear').setAttribute('aria-pressed', String(tab === 'set'));
    scrollTo({ top: 0, behavior: 'instant' });
    // Charts are drawn only while visible: a canvas sized inside a display:none
    // section comes out 0px wide and stays that way.
    if (tab === 'trends') drawTrend();
    if (tab === 'today') drawToday();
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

  // A secondary segmented control under the Trends sub-nav. The id doubles as the
  // state key it writes (see SUBSEG wiring in renderTrends), so adding a toggle is one
  // call here plus one state field - not another bespoke click handler.
  function subSeg(id, current, opts) {
    return '<div class="seg sub" id="' + id + '">' + opts.map(function (o) {
      return '<button type="button" data-s="' + esc(o[0]) + '" aria-selected="' +
        (current === o[0]) + '">' + esc(o[1]) + '</button>';
    }).join('') + '</div>';
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

  // Monday of the week containing `iso`.
  function weekStart(iso) {
    var dt = new Date(iso + 'T12:00:00');
    dt.setDate(dt.getDate() - ((dt.getDay() + 6) % 7));
    return dt.getFullYear() + '-' + String(dt.getMonth() + 1).padStart(2, '0') +
           '-' + String(dt.getDate()).padStart(2, '0');
  }

  function weekSoFar(d) {
    var t = todayISO(), ws = weekStart(t);
    var done = { mins: 0, tss: 0, n: 0 }, left = { mins: 0, tss: 0, n: 0 };
    var daysLoaded = {};

    (d.recent || []).forEach(function (r) {
      if (r.date < ws || r.date > t) return;
      done.mins += r.dur || 0; done.tss += r.tss || 0; done.n++;
      // Any logged activity means the day was trained, TSS or not: a strength session
      // or a technique swim can score zero load and is still not a rest day. This also
      // matches the Calendar's rule (a covered day with no items), so the two agree.
      if ((r.tss || 0) > 0 || (r.dur || 0) > 0) daysLoaded[r.date] = 1;
    });
    (d.weekCalendar || []).forEach(function (x) {
      if (x.date < ws) return;
      if (x.date > t && x.status !== 'completed') {
        left.mins += x.duration_min || 0; left.tss += x.tss || 0; left.n++;
      }
    });

    // Target for the week from planVsActual, which is the same figure the planner used.
    var wk = (d.planVsActual || []).filter(function (w) { return w.week_start === ws; })[0];
    var target = wk ? wk.planned_tss : null;
    var pct = (target && target > 0) ? Math.round(done.tss / target * 100) : null;

    // Days ELAPSED, not 7. Dividing into the whole week counted Friday, Saturday and
    // Sunday as rest days on a Thursday, so a week with one real rest day reported four
    // (Jamie, 6 Aug 2026). "So far" has to mean so far.
    var elapsed = daysBetween(ws, t) + 1;              // Monday..today inclusive
    var rest = Math.max(0, elapsed - Object.keys(daysLoaded).length);

    var h = '<div class="minis">' +
      mini(hhmm(done.mins), 'hours done') +
      mini(Math.round(done.tss), 'tss done') +
      mini(target ? Math.round(target) : '—', 'tss target') +
      mini(pct != null ? pct + '%' : '—', 'of target',
           pct != null && pct > 110 ? 'warn' : '') +
      '</div>';

    var bits = [done.n + ' done'];
    if (left.n) bits.push(left.n + ' left (' + hhmm(left.mins) + ')');
    bits.push(rest > 0 ? rest + ' rest day' + (rest === 1 ? '' : 's') + ' so far'
                       : 'no rest day yet this week');

    /* The rolling energy figure USED to be appended here, as "+73 kcal/day average"
       against the day's deficit-adjusted target. It measured adherence, not balance, and
       carried the opposite sign to the word beside it - so a day eaten 73 kcal OVER the
       plan read as a deficit. It now lives on the Food tab, next to the calories it is
       computed from, in the two places Jamie asked for it. Not restated here: a figure
       repeated on a card that does not own it is how the two drift apart. */
    return card('This week', '<div><p class="wsum">' + esc(bits.join(' · ')) + '</p>' +
      (target ? '<div class="bar' + (pct > 110 ? ' warn' : '') + '"><i style="width:' +
        Math.min(100, pct || 0) + '%"></i></div>' : '') + '</div>',
      { foot: 'As of ' + esc(d.generated || 'unknown') +
              (d.refreshCadence ? '. Refreshes ' + d.refreshCadence + '.' : '.') }) + h;
  }

  /* Detail for one session, hidden until its heading is tapped. Every field is emitted
     only if PRESENT: a planned session has an aim and no numbers, a completed one has
     numbers and often no aim, and printing a label with nothing after it reads as missing
     data rather than as a field that does not apply here. */
  function sessionDetail(s, si) {
    var rows = [];
    function add(label, value) {
      if (value == null || value === '') { return; }
      rows.push('<p class="hero-m"><span class="mut">' + esc(label) + '</span> ' +
                esc(String(value)) + '</p>');
    }
    add('Planned load', s.tss != null ? Math.round(s.tss) + ' TSS' : null);
    add('Duration', s.duration_min ? hhmm(s.duration_min) : null);
    add('Distance', s.distance_km != null ? Number(s.distance_km).toFixed(1) + ' km' : null);
    add('Status', s.status);
    var aim = s.description || s.aim || s.notes;
    if (aim) {
      rows.push('<p class="hero-m">' + esc(String(aim).slice(0, 600)) + '</p>');
    }
    if (!rows.length) {
      rows.push('<p class="hero-m mut">Nothing further recorded for this one yet.</p>');
    }
    return '<div class="sessdet" data-sessdet="' + si + '" hidden>' +
      rows.join('') + '</div>';
  }

  function toggleSession(si) {
    var el = document.querySelector('[data-sessdet="' + si + '"]');
    if (el) { el.hidden = !el.hidden; }
  }

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
        ? today.map(function (s, si) {
            return '<h2 class="hero-t" data-sess="' + si + '">' +
              '<span class="sp ' + sportClass(s.sport) + '"></span>' +
              esc(s.name || s.sport) + '</h2>' +
              '<p class="hero-m">' + esc([hhmm(s.duration_min),
                s.tss != null ? Math.round(s.tss) + ' tss' : null,
                s.detail].filter(Boolean).join(' · ')) + '</p>' +
              (s.status === 'completed' ? '<p class="hero-done">✓ Completed</p>' : '') +
              sessionDetail(s, si);
          }).join('<hr class="hero-r">')
        : '<h2 class="hero-t">Rest day</h2><p class="hero-m">Nothing scheduled.</p>') +
      '</section>';

    h += '<div class="figures">' +
      fig(Number(k.ctl).toFixed(1), 'Fitness', 'CTL') +
      fig(Number(k.atl).toFixed(1), 'Fatigue', 'ATL') +
      fig(signed(k.tsb), 'Form', 'TSB', k.tsb >= 0 ? 'pos' : (k.tsb <= -20 ? 'neg' : 'flat')) +
      '</div>';

    // Ramp and heat were two full cards each. They are two numbers - so they are
    // two numbers, on one row, with the warning state carried by colour.
    // The guide is per-athlete config (rampCap), not a hard-coded 5.
    var cap = d.rampCap != null ? d.rampCap : 5;
    var signals = '';
    if (ramp != null) {
      signals += mini(signed(ramp) + ' / ' + cap.toFixed(0), 'ramp vs target',
                      Math.abs(ramp) > cap ? 'warn' : '');
    }
    if (ha.current != null) {
      signals += mini(Number(ha.current).toFixed(0) + '%', 'heat accl');
    }
    if (d.resolvedFtp) signals += mini(d.resolvedFtp + 'w', 'ftp');
    if (signals) h += '<div class="minis">' + signals + '</div>';
    if (ramp != null && Math.abs(ramp) > cap) {
      h += '<p class="flag">Ramp ' + signed(ramp) + ' is above the ' + cap.toFixed(0) +
        '-point weekly guide.</p>';
    }

    h += weekSoFar(d);

    h += card('Coming up', '<div class="body-flush">' +
      (next.length ? groupByDay(next) : '<div class="empty">Nothing planned yet</div>') +
      '</div>', { flush: true });

    // The ±7 day chart, same one as Trends, at the bottom of Today: it answers "how
    // heavy has this week been and what is left" without changing tab.
    if ((d.loadChart || []).length) {
      h += card('Seven days either side',
        '<div class="readout" id="ro-today"><b>—</b><span></span></div>' +
        '<div class="chartbox"><canvas id="c-today"></canvas></div>',
        { foot: 'Daily TSS by sport, faded where still planned. Line is form (TSB).' });
    }

    $('#v-today').innerHTML = h;
    var th = $('#v-today');
    if (th && !th.dataset.sessNav) {
      th.dataset.sessNav = '1';
      th.addEventListener('click', function (e) {
        try {
          var head = e.target.closest('[data-sess]');
          if (head) { toggleSession(head.getAttribute('data-sess')); }
        } catch (err) {
          if (window.console) { console.error('[peak] session toggle:', err); }
        }
      });
    }
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

  // The data window is NOT the month. recent[] covers roughly the last three weeks and
  // weekCalendar the plan window, so anything outside that has no data - and a day with
  // no data is NOT a rest day. Counting it as one told Jamie that Kathryn took 19 rest
  // days in July, which is invented, not measured.
  function coverage(by) {
    var d = state.data || {};
    var dates = []
      .concat((d.recent || []).map(function (r) { return r.date; }))
      .concat((d.weekCalendar || []).map(function (r) { return r.date; }))
      .concat((d.loadChart || []).map(function (r) { return r.date; }))
      .concat(Object.keys(by || {}))
      .filter(Boolean)
      .sort();
    if (!dates.length) return null;
    return { from: dates[0], to: dates[dates.length - 1] };
  }

  function renderCalendar() {
    var t = todayISO();
    if (!state.calMonth) state.calMonth = t.slice(0, 7);
    var by = calSessions();
    var cov = coverage(by);
    var y = +state.calMonth.slice(0, 4), m = +state.calMonth.slice(5, 7) - 1;

    // Whole weeks (Mon-first) that touch this month, so a row is always seven days.
    var monday = new Date(y, m, 1);
    monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
    var lastDay = new Date(y, m + 1, 0);

    var iso = function (dt) {
      return dt.getFullYear() + '-' + String(dt.getMonth() + 1).padStart(2, '0') +
             '-' + String(dt.getDate()).padStart(2, '0');
    };
    // ISO week number, so "wk 31" means what a coach means by it.
    var isoWeek = function (dt) {
      var d0 = new Date(Date.UTC(dt.getFullYear(), dt.getMonth(), dt.getDate()));
      var day = d0.getUTCDay() || 7;
      d0.setUTCDate(d0.getUTCDate() + 4 - day);
      var y0 = new Date(Date.UTC(d0.getUTCFullYear(), 0, 1));
      return Math.ceil((((d0 - y0) / 864e5) + 1) / 7);
    };

    var NAME = { swim: 'Swim', bike: 'Bike', run: 'Run', strength: 'Str', other: 'Other' };
    var mTot = { mins: 0, tss: 0, sessions: 0, rest: 0, nodata: 0 };
    var rows = [];

    for (var cur = new Date(monday); cur <= lastDay; cur.setDate(cur.getDate() + 7)) {
      var ws = new Date(cur);
      var wk = { start: new Date(ws), num: isoWeek(ws), mins: 0, tss: 0,
                 sports: {}, planned: 0, done: 0, days: [] };
      for (var i = 0; i < 7; i++) {
        var dd = new Date(ws); dd.setDate(dd.getDate() + i);
        var key = iso(dd);
        var items = by[key] || [];
        var inMonth = dd.getMonth() === m;
        var covered = cov && key >= cov.from && key <= cov.to;
        wk.days.push({ iso: key, n: dd.getDate(), items: items,
                       inMonth: inMonth, covered: covered });
        items.forEach(function (x) {
          var mn = x.duration_min || 0;
          wk.mins += mn; wk.tss += x.tss || 0;
          var fam = SPORT[x.sport] || 'other';
          wk.sports[fam] = (wk.sports[fam] || 0) + mn;
          if (x.status === 'completed') wk.done += mn; else wk.planned += mn;
          if (inMonth) { mTot.mins += mn; mTot.tss += x.tss || 0; mTot.sessions++; }
        });
        if (inMonth && !items.length) {
          // Only a covered day with nothing on it is a rest day.
          if (covered && key <= t) mTot.rest++;
          else if (!covered) mTot.nodata++;
        }
      }
      rows.push(wk);
    }

    var grid = rows.map(function (wk) {
      var future = wk.planned > wk.done;
      var split = ['swim', 'bike', 'run', 'strength', 'other'].filter(function (k) {
        return wk.sports[k];
      }).map(function (k) {
        return '<span><i class="sp sp-' + k + '"></i>' + hhmm(wk.sports[k]) + '</span>';
      }).join('');
      return '<div class="wkrow">' +
        '<div class="wkrow-t"><span class="wkn">wk ' + wk.num + '</span>' +
        '<span class="wkdays">' + wk.days.map(function (dy) {
          var cls = 'wkd';
          if (!dy.inMonth) cls += ' out';
          if (dy.iso === t) cls += ' today';
          if (dy.iso === state.calDay) cls += ' sel';
          if (!dy.items.length && dy.covered && dy.iso <= t) cls += ' rest';
          if (!dy.covered) cls += ' nodata';
          return '<button type="button" class="' + cls + '" data-d="' + dy.iso + '">' +
            '<span class="n">' + dy.n + '</span><span class="dots">' +
            dy.items.slice(0, 3).map(function (x) {
              return '<i class="dot ' + sportClass(x.sport) +
                (x.status === 'completed' ? ' done' : '') + '"></i>';
            }).join('') + '</span></button>';
        }).join('') + '</span>' +
        '<span class="wksum"><b>' + (wk.mins ? hhmm(wk.mins) : '—') + '</b>' +
        (wk.tss ? Math.round(wk.tss) + ' tss' : '') + '</span></div>' +
        (split ? '<div class="wkrow-s">' + split +
          (future ? '<span class="wk-tag">planned</span>' : '') + '</div>' : '') +
        '</div>';
    }).join('');

    // Mirrors .wkrow-t exactly - same wrapper classes, same three boxes - so the day
    // headers are positioned by the same rules as the day cells. Two parallel layouts
    // that merely look similar is why they drifted apart.
    var head = '<div class="cal-dows"><span class="wkn"></span>' +
      '<span class="wkdays">' + ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map(function (x) {
        return '<span>' + x + '</span>';
      }).join('') + '</span><span class="wksum"></span></div>';

    var nav = '<div class="cal-nav">' +
      '<button type="button" class="cal-mo" data-mo="-1" aria-label="Previous month">‹</button>' +
      '<button type="button" class="cal-mo" data-mo="1" aria-label="Next month">›</button></div>';

    var h = card(monthName(y, m), head + '<div class="wkrows">' + grid + '</div>',
      { action: nav });

    h += '<div class="minis">' + mini(hhmm(mTot.mins), 'this month') +
      mini(Math.round(mTot.tss), 'tss') + mini(mTot.sessions, 'sessions') +
      mini(mTot.rest, 'rest days') + '</div>';

    // Say what is not known rather than folding it into a number.
    if (mTot.nodata) {
      h += '<p class="hint">' + mTot.nodata + ' day' + (mTot.nodata === 1 ? '' : 's') +
        ' this month are outside the published window' +
        (cov ? ' (' + cov.from + ' to ' + cov.to + ')' : '') +
        ' — not counted as rest.</p>';
    }

    var sel = state.calDay && by[state.calDay] ? by[state.calDay] : null;
    if (state.calDay) {
      h += card(dow(state.calDay) + ' ' + dnum(state.calDay) + ' ' +
        new Date(state.calDay + 'T12:00:00').toLocaleDateString('en-GB', { month: 'short' }),
        '<div class="body-flush">' + (sel ? sel.map(seshRow).join('') :
          '<div class="empty">' + (cov && state.calDay >= cov.from && state.calDay <= cov.to
            ? 'Rest day — nothing recorded'
            : 'Outside the published data window') + '</div>') + '</div>', { flush: true });
    } else {
      h += '<p class="hint">Tap a day for its sessions.</p>';
    }

    $('#v-cal').innerHTML = h;

    $('#v-cal').querySelector('.wkrows').onclick = function (e) {
      var b = e.target.closest('.wkd');
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

  /* Rolling hours/week, published as durationBySport by refresh-site-data.py in the
   * same {current,prev,prev2} x {Total,Ride,Run,Swim} shape as fitnessBySport. 'Total'
   * is every activity logged, so it is the counterpart of overall CTL rather than a
   * fourth sport, and durSports() keeps it out of the chip list.
   *
   * A published file written before this existed has no durationBySport at all, hence
   * the guards: with no data the Hours tab leaves the sub-nav entirely, exactly as
   * 'Run km' does for an athlete who does not run. */
  function durBySport(d) { return (d || state.data || {}).durationBySport || {}; }

  function durSeries(d, which, sport) {
    var season = durBySport(d)[which] || {};
    return season[sport === 'all' ? 'Total' : sport] || [];
  }

  function hasDuration(d) {
    var cur = durBySport(d).current || {};
    return Object.keys(cur).some(function (k) { return (cur[k] || []).length > 0; });
  }

  // Chip-able sports: the three disciplines this athlete is focused on, whichever of
  // them the data actually carries. Total is excluded by name, not by relying on it
  // failing the focus test.
  function durSports(d) {
    var fs = focusSports();
    return Object.keys(durBySport(d).current || {}).filter(function (s) {
      return s !== 'Total' && (durSeries(d, 'current', s) || []).length &&
             fs.indexOf(family(s)) >= 0;
    });
  }

  // 'Run km' is a run tab and nothing else: with run out of focus it is an empty chart
  // and a dead sub-nav entry, so it leaves the list entirely.
  function trendTabs() {
    var fs = focusSports();
    return TRENDS.filter(function (t) {
      if (t.id === 'dur') return hasDuration(state.data);
      return t.id !== 'longrun' || fs.indexOf('run') >= 0;
    });
  }

  // Both renderTrends and drawTrend go through this: show('trends') draws without
  // re-rendering, so a selection left pointing at a dropped tab has to be corrected in
  // one place or the chart and the sub-nav disagree.
  function normaliseTrend() {
    var fs = focusSports();
    if (!trendTabs().some(function (t) { return t.id === state.trend; })) state.trend = 'fit';
    if (state.fitSport !== 'all' && fs.indexOf(family(state.fitSport)) < 0) {
      state.fitSport = 'all';
    }
    // Same correction for the Hours chips, and additionally for a sport the duration
    // data does not carry - switching athlete can leave a chip selected that the new
    // athlete has no series for, which would draw an empty chart under a lit chip.
    if (state.durSport !== 'all' && durSports().indexOf(state.durSport) < 0) {
      state.durSport = 'all';
    }
  }

  function renderTrends() {
    var d = state.data;
    normaliseTrend();
    var sel = state.trend;

    var seg = '<div class="seg" id="trendSeg" role="tablist">' + trendTabs().map(function (x) {
      return '<button type="button" role="tab" data-t="' + x.id + '" aria-selected="' +
        (x.id === sel) + '">' + esc(x.label) + '</button>';
    }).join('') + '</div>';

    var META = {
      fit:  { title: 'Fitness · three seasons',
              foot: 'CTL by calendar date. Pinch, scroll or drag to zoom; the shaded band is the race-day target.' },
      dur:  { title: 'Hours · three seasons',
              foot: 'Rolling training hours per week, smoothed on the same 42-day ' +
                    'constant as CTL and aligned on race day. Volume, not intensity: ' +
                    'read it next to Fitness, not instead of it.' },
      load: { title: 'Seven days either side',
              foot: 'Bars are daily TSS, faded where still planned. The line is form (TSB).' },
      heat: { title: 'Heat acclimation',
              foot: 'Score decays with a 21-day constant. Dots are logged heat exposures.' },
      fuel: { title: 'Fuelling capacity',
              foot: 'Best carbohydrate rate achieved in the trailing 14 days, bike and run ' +
                    'separately. A gap means nothing was logged in that window.' },
      plan: { title: 'Plan vs actual',
              foot: 'Weekly planned and completed TSS.' },
      longrun: state.lrMode === 'total'
        ? { title: 'Total run km by week',
            foot: 'Every run in the week added together, with the week-on-week build. ' +
                  '10% is the usual ceiling for a build week; the dashed line is the race.' }
        : { title: 'Longest run by week',
            foot: 'The longest single run in each week, with the week-on-week build. ' +
                  '10% is the usual ceiling for a build week; the dashed line is the race.' },
      zones: { title: 'Time in zones vs target',
               foot: 'Bike by power, run by pace, swim by pace against CSS. Bars are the ' +
                     'share of moving time in each band; the notch is the blueprint target ' +
                     'for the current phase.' }
    }[sel];

    // Overall CTL or one discipline. fitnessBySport carries Ride/Run/Swim for all
    // three seasons, so the whole chart - seasons, race alignment and all - just
    // switches which series it reads.
    var sportBar = '';
    if (sel === 'fit') {
      var fs = focusSports();
      var avail = Object.keys((d.fitnessBySport || {}).current || {}).filter(function (s) {
        return fs.indexOf(family(s)) >= 0;
      });
      if (avail.length) {
        sportBar = subSeg('fitSport', state.fitSport,
          [['all', 'All']].concat(avail.map(function (x) { return [x, x]; })));
      }
    }
    if (sel === 'dur') {
      var dsports = durSports(d);
      if (dsports.length) {
        sportBar = subSeg('durSport', state.durSport,
          [['all', 'All']].concat(dsports.map(function (x) { return [x, x]; })));
      }
    }
    if (sel === 'longrun') {
      sportBar = subSeg('lrMode', state.lrMode,
        [['long', 'Long run'], ['total', 'Week total']]);
    }
    if (sel === 'zones') {
      // Only the windows the data carries, and marked with the window actually being
      // shown: `phase` is absent for an athlete with no phase set, and a button that
      // highlights a window the view then falls back out of is a lie about the numbers.
      var wins = zoneWindows(d);
      if (wins.length) sportBar = subSeg('zoneWin', zoneWinSel(d), wins);
    }

    var zoomable = (sel === 'fit' || sel === 'dur' || sel === 'heat');
    var action = zoomable
      ? '<button type="button" class="card-a" id="zreset">Reset zoom</button>' : '';

    var h = seg + sportBar + card(META.title +
      (sel === 'fit' && state.fitSport !== 'all' ? ' · ' + state.fitSport : '') +
      (sel === 'dur' && state.durSport !== 'all' ? ' · ' + state.durSport : ''),
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
    // Every sub-segment writes the state field its container id names.
    ['fitSport', 'durSport', 'lrMode', 'pcMode', 'zoneWin'].forEach(function (key) {
      var el = $('#' + key);
      if (!el) return;
      el.onclick = function (e) {
        var b = e.target.closest('button');
        if (!b) return;
        state[key] = b.dataset.s;
        renderTrends();
        drawTrend();
      };
    });

    var zr = $('#zreset');
    if (zr) zr.onclick = function () { if (state.chart && state.chart.resetZoom) state.chart.resetZoom(); };
  }

  // The numbers that belong with each chart, directly under it. This is where the
  // density goes - a chart alone on a screen is the "sparse" half of the problem.
  function trendExtras(sel, d) {
    var h = '';

    if (sel === 'fit') {
      var fs = focusSports();
      var bs = (d.fitnessBySport || {}).current || {};
      var sports = Object.keys(bs).filter(function (s) { return fs.indexOf(family(s)) >= 0; });
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
      // Best efforts in watts: a bike block. It stays out of the view for an athlete
      // whose focus does not include the bike, even though the rides are still logged.
      var pc = fs.indexOf('bike') >= 0 ? (d.powerCurve || []) : [];
      if (pc.length) {
        var pw = d.powerCurveWindow || {};
        var days = pw.days || 90;
        // Average or normalised. NP is the honest number for a long race effort: over
        // four hours a lumpy ride and a smooth one can share an average and be entirely
        // different rides, and it is the smooth one that gets you off the bike able to
        // run. Intervals.icu has no NP curve, so these come from the power streams and
        // populate a few rides per refresh - hence np_pending.
        var npMode = state.pcMode === 'np';
        var hasNp = pc.some(function (r) { return r.np != null; });
        var pick = function (r) { return npMode ? r.np : r.w; };
        var pickPrev = function (r) { return npMode ? r.npPrev : r.wPrev; };
        var bar = hasNp || npMode
          ? subSeg('pcMode', state.pcMode, [['avg', 'Average'], ['np', 'Normalised']])
          : '';
        var pending = pw.np_pending;

        var table = '<table class="tbl">' +
          '<thead><tr><th>Duration</th><th>Last ' + days + 'd</th>' +
          '<th>Year ago</th><th>Δ</th></tr></thead><tbody>' +
          pc.map(function (r) {
            var now = pick(r), was = pickPrev(r);
            var dl = (now != null && was) ? Math.round((now - was) / was * 100) : null;
            return '<tr><td class="lbl">' + esc(r.label) + '</td><td class="t">' +
              (now != null ? now + 'w' : '—') + '</td><td>' +
              (was != null ? was + 'w' : '—') + '</td><td class="' +
              (dl == null ? '' : (dl >= 0 ? 'pos' : 'neg')) + '">' +
              (dl == null ? '—' : (dl >= 0 ? '+' : '') + dl + '%') + '</td></tr>';
          }).join('') + '</tbody></table>';

        // Twelve rows of dashes reads as "you have never ridden that long", which is a
        // different and wrong statement. Until one duration has an NP, the column says
        // what it is doing instead of pretending to be a table.
        var body = bar + ((npMode && !hasNp)
          ? '<div class="empty">Normalised curve still building' +
            (pending ? ' · ' + pending + ' ride' + (pending === 1 ? '' : 's') + ' to go' : '') +
            '</div>'
          : table);

        var pcFoot = pw.label
          ? 'Best of ' + pw.now_from + ' to ' + pw.now_to + ', against the same ' +
            days + ' days a year earlier (' + pw.prev_from + ' to ' + pw.prev_to + ').'
          : 'Best efforts over the last ' + days + ' days.';
        if (npMode) {
          pcFoot += ' Normalised power: ' + (pw.np_basis ||
            'the 30-second rolling fourth-power mean over every window of that length') +
            '.';
          // Counted, so a curve that is thin at the long end is read as work in progress
          // rather than as a ceiling.
          if (pending) {
            pcFoot += ' Still building: ' + pending + ' ride' +
              (pending === 1 ? '' : 's') + ' not yet processed, so the longer ' +
              'durations may fill in over the next few refreshes.';
          }
        }
        h += card('Power curve', body, { flush: true, foot: pcFoot });
      }
    }

    if (sel === 'dur') {
      // The same block the fitness tab carries, in hours instead of CTL: where the
      // volume sits now against this season's own peak, per sport. Total is included
      // here (unlike the chips, where it is the 'All' state) because the split only
      // reads as a split next to what it adds up to.
      var dbs = durBySport(d).current || {};
      var rows = ['Total'].concat(durSports(d)).filter(function (s) {
        return (dbs[s] || []).length;
      });
      if (rows.length) {
        var peakAll = rows.reduce(function (m, s) {
          return (dbs[s] || []).reduce(function (mm, r) { return Math.max(mm, r[1]); }, m);
        }, 0.1);
        h += card('Hours by sport', '<div class="body-flush">' + rows.map(function (s) {
          var series = dbs[s] || [];
          var last = series.length ? series[series.length - 1][1] : 0;
          var peak = series.reduce(function (m, r) { return Math.max(m, r[1]); }, 0);
          // One shared scale, so the bars compare sports rather than each sport
          // against itself - a 3h/wk swim must not draw as long as a 9h/wk bike.
          return '<div class="sesh"><span class="tick">' +
            (s === 'Total' ? '<b>Σ</b>' : '<span class="sp ' + sportClass(s) +
              '" style="margin:0"></span>') +
            '</span><span class="body"><span class="nm">' + esc(s) +
            '</span><div class="bar"><i style="width:' + (last / peakAll * 100).toFixed(0) +
            '%"></i></div></span><span class="rt"><b>' + Number(last).toFixed(1) +
            'h</b>peak ' + Number(peak).toFixed(1) + 'h</span></div>';
        }).join('') + '</div>', { flush: true,
          foot: 'Hours per week now, and the highest this season, on the same 42-day ' +
                'smoothing as the chart. Total counts every activity logged, including ' +
                'strength and anything outside the three disciplines.' });
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
        // Rows are every session with ANY intake logged, not just the ones with carbs.
        // Keying off carb sessions alone dropped a water-only session (Kathryn had one),
        // which is the same class of error as truncating the list: real data, no row to
        // put it on.
        var logBy = {};
        (d.sessionLog || []).forEach(function (e) { logBy[e.date] = e; });
        var carbBy = {};
        f.points.forEach(function (r) { carbBy[r.date] = r; });

        var dates = {};
        Object.keys(carbBy).forEach(function (k) { dates[k] = 1; });
        (d.sessionLog || []).forEach(function (e) {
          if (e.hydration_ml != null || e.nutrition_mg_sodium != null ||
              e.nutrition_g_carb != null) dates[e.date] = 1;
        });
        var rows = Object.keys(dates).sort().reverse();

        h += card('Logged sessions · ' + rows.length,
          '<div class="scroller"><table class="tbl">' +
          '<thead><tr><th>Date</th><th>Time</th><th>Carb</th><th>g/hr</th>' +
          '<th>Water</th><th>Na</th></tr></thead><tbody>' +
          rows.map(function (dt) {
            var r = carbBy[dt] || {}, lg = logBy[dt] || {};
            var dur = r.dur != null ? r.dur : lg.duration_min;
            var grams = (r.g_per_hr != null && r.dur)
              ? Math.round(r.g_per_hr * r.dur / 60)
              : (lg.nutrition_g_carb != null ? Math.round(lg.nutrition_g_carb) : null);
            return '<tr><td class="lbl">' + esc(dow(dt) + ' ' + dnum(dt)) + '</td><td>' +
              hhmm(dur) + '</td><td>' + (grams != null ? grams + 'g' : '—') +
              '</td><td class="t">' +
              (r.g_per_hr != null ? Number(r.g_per_hr).toFixed(0) : '—') + '</td><td>' +
              (lg.hydration_ml != null ? Math.round(lg.hydration_ml / 100) / 10 + 'L' : '—') +
              '</td><td>' + (lg.nutrition_mg_sodium != null
                ? Math.round(lg.nutrition_mg_sodium) + 'mg' : '—') + '</td></tr>';
          }).join('') + '</tbody></table></div>',
          { flush: true, foot: 'A rate held for four hours is a different result from the ' +
                              'same rate held for one, so duration is shown with it. Water ' +
                              'and sodium appear once logged - sodium capture is new, so ' +
                              'older sessions have none.' });
      }
    }

    if (sel === 'longrun') {
      var total = state.lrMode === 'total';
      var lr = longRunWeeks(d, state.lrMode);
      if (!lr.length) {
        h += '<p class="hint">No runs with distance in the published window.</p>';
      } else {
        var peak = lr.reduce(function (m, r) { return r.km > m.km ? r : m; }, lr[0]);
        var over = lr.filter(function (r) { return r.pct != null && r.pct > 10; }).length;
        h += '<div class="minis">' +
          mini(lr[lr.length - 1].km.toFixed(1), 'latest km') +
          mini(peak.km.toFixed(1), total ? 'biggest week' : 'longest') +
          mini(lr.length, 'weeks') +
          mini(over, 'builds over 10%', over ? 'warn' : '') + '</div>';
        h += card('Week by week', '<div class="scroller"><table class="tbl">' +
          '<thead><tr><th>Week</th><th>' + (total ? 'Total' : 'Longest') +
          '</th><th>Build</th><th>' + (total ? 'Runs' : 'Session') +
          '</th></tr></thead><tbody>' +
          lr.slice().reverse().map(function (r) {
            return '<tr><td class="lbl">' + esc(r.ws.slice(5)) + '</td><td class="t">' +
              r.km.toFixed(1) + '</td><td class="' +
              (r.pct == null ? '' : (r.pct > 10 ? 'neg' : 'pos')) + '">' +
              (r.pct == null ? '—' : (r.pct >= 0 ? '+' : '') + r.pct + '%') + '</td><td>' +
              (total ? r.n + ' · longest ' + r.longest.toFixed(1) + 'km'
                     : esc((r.name || '').slice(0, 26))) + '</td></tr>';
          }).join('') + '</tbody></table></div>',
          { flush: true, foot: 'Amber bars and red percentages are weeks that grew the ' +
                              (total ? 'weekly total' : 'long run') + ' by more than 10%.' });
      }
    }

    if (sel === 'zones') h += zoneExtras(d);

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

  function readout(txt, sub, target) {
    var el = $(target || '#ro');
    if (!el) return;
    el.firstChild.textContent = txt || '—';
    el.lastChild.textContent = sub || '';
  }

  // Wire a chart so dragging across it writes into the readout. Uses the SAME
  // callbacks the tooltip would have used, so mouse and touch never disagree.
  function attachReadout(chart, roTarget) {
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
      readout(title, lines.filter(Boolean).join('  ·  '), roTarget);
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
    normaliseTrend();
    var fn = { fit: chartFitness, dur: chartDuration, load: chartLoad, heat: chartHeat,
               fuel: chartFuel, plan: chartPlan, longrun: chartLongRun,
               zones: chartZones }[state.trend];
    if (fn) state.chart = fn(el, state.data);
    attachReadout(state.chart, '#ro');
  }

  /* The three race dates the season overlays are aligned on, and the mapping that does
   * it. Extracted from chartFitness so the Hours chart cannot align its seasons even
   * slightly differently: two copies of this would be two charts that disagree about
   * where last season's taper was. See the note in chartFitness on why the axis is
   * days-to-race and not calendar day-of-year. */
  function seasonRaces(d) {
    var p = d.profile || {}, cp = d.ctlProjection || {};
    return {
      current: cp.race_date || p.race_date,
      prev: (p.prev_race && p.prev_race.date) || p.prev_race_date,
      prev2: p.prev2_race_date
    };
  }

  // [[date, value], ...] -> [{x: days from race, y: value}, ...]. Empty for a season
  // with no race date, which is what drops that overlay rather than mis-placing it.
  function relToRace(rows, race) {
    if (!race) return [];
    var r0 = doy0(race);
    return (rows || []).map(function (x) { return { x: doy0(x[0]) - r0, y: x[1] }; });
  }

  // Tick labels are real calendar dates read off THIS season's timeline.
  function raceAxisLabel(race, v) {
    if (!race) return Math.round(v) + 'd';
    var t = new Date(race + 'T12:00:00');
    t.setDate(t.getDate() + Math.round(v));
    return t.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  }

  function chartFitness(el, d) {
    var p = d.profile || {}, cp = d.ctlProjection || {};

    // x is DAYS RELATIVE TO RACE DAY, not day-of-year. Aligning three seasons by
    // calendar date put Barcelona's race (7 Oct) 18 days right of Italy's (19 Sep),
    // so the taper and the peak of one season sat over mid-build of another and the
    // comparison was worthless. Race day is 0 for every season; tick labels are
    // still calendar dates, read off THIS season, which is what was asked for.
    var races = seasonRaces(d);
    var raceThis = races.current, racePrev = races.prev, racePrev2 = races.prev2;

    // 'all' = overall CTL; otherwise the per-sport series for the same season.
    var pick3 = function (which) {
      if (state.fitSport === 'all') {
        return { current: d.fitnessThis, prev: d.fitnessPrev, prev2: d.fitnessPrev2 }[which];
      }
      var bs = d.fitnessBySport || {};
      return ((bs[which] || {})[state.fitSport]) || [];
    };

    var rel = relToRace;
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
        borderColor: 'rgba(16,101,107,.35)', borderWidth: 1, borderDash: [4, 3],
        pointRadius: 0, fill: '+1', backgroundColor: 'rgba(16,101,107,.09)'
      });
      ds.push({
        label: '_band-lo', order: 10,
        data: [{ x: x0, y: cp.target_ctl_min }, { x: 0, y: cp.target_ctl_min }],
        borderColor: 'rgba(16,101,107,.35)', borderWidth: 1, borderDash: [4, 3],
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
        data: relObj(cp.planned_build, raceThis), borderColor: C.accent, borderWidth: 1.4,
        borderDash: [3, 3], pointRadius: 0, tension: 0.25, fill: false
      });
    }
    ds.push({
      label: 'This season', order: 1,
      data: rel(pick3('current'), raceThis), borderColor: C.accent, borderWidth: 2.2,
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

    var label = function (v) { return raceAxisLabel(raceThis, v); };

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
    o.plugins.vlines = { lines: [{ x: 0, label: 'RACE DAY', color: C.accent }] };
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

  /* Rolling training hours per week, three seasons, same interaction grammar as
   * chartFitness: the same chips, the same days-to-race x axis, the same muted dashed
   * overlays. What it deliberately does NOT carry is the target band, the planned build
   * and the milestones - those are CTL targets from the blueprint, and there is no
   * published hours target to draw against. Inventing one would be inventing data.
   *
   * The smoothing is already done: refresh-site-data.py publishes hours/week per day,
   * matching where chartFitness gets its CTL from, so neither chart smooths in the app
   * and neither can drift from the numbers the bot's charts quote. */
  function chartDuration(el, d) {
    var p = d.profile || {};
    var races = seasonRaces(d);
    var sport = state.durSport;

    var ds = [];
    if (relToRace(durSeries(d, 'prev2', sport), races.prev2).length) {
      ds.push({
        label: (p.prev2_race_name || '2023').replace(' IM', " '23"), order: 6,
        data: relToRace(durSeries(d, 'prev2', sport), races.prev2),
        borderColor: C.blue, borderWidth: 1,
        borderDash: [5, 3], pointRadius: 0, tension: 0.3, fill: false
      });
    }
    if (relToRace(durSeries(d, 'prev', sport), races.prev).length) {
      ds.push({
        label: 'Last season', order: 5,
        data: relToRace(durSeries(d, 'prev', sport), races.prev),
        borderColor: C.muted, borderWidth: 1,
        borderDash: [2, 3], pointRadius: 0, tension: 0.3, fill: false
      });
    }
    ds.push({
      label: 'This season', order: 1,
      data: relToRace(durSeries(d, 'current', sport), races.current),
      borderColor: C.accent, borderWidth: 2.2,
      pointRadius: 0, tension: 0.25, fill: false
    });

    var label = function (v) { return raceAxisLabel(races.current, v); };

    var o = baseOpts();
    o.scales = {
      x: {
        type: 'linear', min: -175, max: 12,
        ticks: { color: C.muted, maxTicksLimit: 6, autoSkip: true,
                 font: { family: 'DM Mono', size: 9 },
                 callback: function (v) { return label(v); } },
        grid: { display: false }, border: { color: C.rule }
      },
      y: {
        beginAtZero: true,
        title: { display: true, text: 'HOURS / WEEK', color: C.muted,
                 font: { family: 'DM Mono', size: 9 } },
        ticks: { color: C.muted, font: { family: 'DM Mono', size: 9 }, maxTicksLimit: 6 },
        grid: { color: C.rule, drawTicks: false }, border: { display: false }
      }
    };
    o.plugins.zoom = zoomOpts();
    o.plugins.zoom.limits = { x: { min: -400, max: 30, minRange: 21 } };
    o.plugins.vlines = { lines: [{ x: 0, label: 'RACE DAY', color: C.accent }] };
    o.plugins.tooltip.callbacks = {
      title: function (items) {
        var v = Math.round(items[0].parsed.x);
        return label(v) + ' · ' + (v === 0 ? 'race day' : Math.abs(v) + 'd ' +
          (v < 0 ? 'to race' : 'after'));
      },
      label: function (it) {
        return (it.dataset.label || '') + ': ' +
          Number(it.parsed.y).toFixed(1) + ' h/wk';
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
    var BASE = { Ride: '16,101,107', Run: '186,60,50', Swim: '29,78,115',
                 Strength: '120,132,142', Other: '166,172,180' };
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
      borderColor: 'rgba(20,24,29,.55)', borderWidth: 1.6, borderDash: [5, 3],
      pointRadius: 0, tension: 0.3, fill: false
    });
    ds.push({
      type: 'line', label: 'TSB', yAxisID: 'y1', order: 1,
      data: lc.map(function (r) { return r.tsb; }),
      borderColor: C.ink2, borderWidth: 1.4, tension: 0.3, fill: false,
      pointRadius: 3, pointBorderColor: '#fff', pointBorderWidth: 1,
      pointBackgroundColor: lc.map(function (r) {
        var v = r.tsb == null ? 0 : r.tsb;
        return v > 5 ? '#2aa6a0' : (v >= -20 ? '#c08428' : '#ba3c32');
      })
    });

    // TSB zone bands, drawn behind everything on the right-hand scale.
    var bands = {
      id: 'tsbBands',
      beforeDatasetsDraw: function (chart) {
        var y1 = chart.scales.y1, ca = chart.chartArea, g = chart.ctx;
        if (!y1) return;
        [[5, Infinity, 'rgba(42,166,160,.09)'], [0, 5, 'rgba(110,190,170,.10)'],
         [-20, 0, 'rgba(196,158,70,.08)'], [-Infinity, -20, 'rgba(186,60,50,.09)']]
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
      fill: true, backgroundColor: 'rgba(179,36,31,.07)'
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
          { label: 'Bike', data: f.Ride, borderColor: C.accent, borderWidth: 2,
            pointRadius: 0, tension: 0.2, fill: false, stepped: 'after' },
          { label: 'Run', data: f.Run, borderColor: C.red, borderWidth: 2,
            pointRadius: 0, tension: 0.2, fill: false, stepped: 'after' }
        ]
      },
      options: o
    });
  }

  // Run distance per ISO week, from completed activities. Carries BOTH figures per week -
  // the longest single run and the week's total - because they answer different questions
  // and the 10% build rule applies to each on its own terms. `km` is whichever the toggle
  // has selected, so every consumer (chart, table, minis, build %) reads one field and
  // the percentages recompute against the right series rather than the long-run one.
  function longRunWeeks(d, mode) {
    var by = {};
    (d.recent || []).forEach(function (r) {
      if ((SPORT[r.sport] || '') !== 'run') return;
      var km = r.dist;
      if (!km) return;
      var ws = weekStart(r.date);
      var w = by[ws] || (by[ws] = { ws: ws, longest: 0, total: 0, n: 0,
                                    date: null, name: null });
      w.total += km;
      w.n++;
      if (km > w.longest) { w.longest = km; w.date = r.date; w.name = r.name; }
    });
    var rows = Object.keys(by).sort().map(function (k) { return by[k]; });
    var field = mode === 'total' ? 'total' : 'longest';
    rows.forEach(function (r, i) {
      r.km = r[field];
      var prev = i > 0 ? rows[i - 1][field] : null;
      r.pct = prev ? Math.round((r.km - prev) / prev * 100) : null;
    });
    return rows;
  }

  function chartLongRun(el, d) {
    var rows = longRunWeeks(d, state.lrMode);
    if (!rows.length) return null;
    var race = (d.profile || {}).race_date;

    var o = baseOpts();
    o.scales = {
      x: {
        type: 'linear', offset: true,
        ticks: { color: C.muted, maxTicksLimit: 7, autoSkip: true, maxRotation: 0,
                 font: { family: 'DM Mono', size: 9 },
                 callback: function (v) {
                   var r = rows[Math.round(v)];
                   if (!r) return '';
                   var dt = new Date(r.ws + 'T12:00:00');
                   return dt.getDate() + ' ' +
                     dt.toLocaleDateString('en-GB', { month: 'short' });
                 } },
        grid: { display: false }, border: { color: C.rule }
      },
      y: {
        beginAtZero: true,
        title: { display: true, text: 'km', color: C.muted, font: { family: 'DM Mono', size: 9 } },
        ticks: { color: C.muted, font: { family: 'DM Mono', size: 9 }, maxTicksLimit: 6 },
        grid: { color: C.rule, drawTicks: false }, border: { display: false }
      }
    };
    o.plugins.tooltip.callbacks = {
      title: function (items) {
        var r = rows[items[0].dataIndex];
        return r ? 'w/c ' + r.ws + (r.pct != null ? ' · ' + (r.pct >= 0 ? '+' : '') + r.pct + '%' : '') : '';
      },
      label: function (it) {
        var r = rows[it.dataIndex] || {};
        return state.lrMode === 'total'
          ? [Number(it.parsed.y).toFixed(1) + ' km total',
             r.n + ' run' + (r.n === 1 ? '' : 's') + ' · longest ' + r.longest.toFixed(1) + ' km']
          : [Number(it.parsed.y).toFixed(1) + ' km', r.name || ''].filter(Boolean);
      }
    };
    // Race week marked on the same axis the bars sit on.
    var lines = [];
    if (race) {
      var rw = weekStart(race);
      var idx = rows.map(function (r) { return r.ws; }).indexOf(rw);
      if (idx >= 0) lines.push({ x: idx, label: 'RACE', color: C.accent });
    }
    o.plugins.vlines = { lines: lines };

    return new Chart(el, {
      type: 'bar',
      data: {
        labels: rows.map(function (r, i) { return i; }),
        datasets: [{
          label: state.lrMode === 'total' ? 'Week total' : 'Longest run',
          data: rows.map(function (r) { return r.km; }),
          backgroundColor: rows.map(function (r) {
            return r.pct != null && r.pct > 10 ? 'rgba(168,106,18,.8)' : 'rgba(16,101,107,.75)';
          }),
          borderWidth: 0, borderRadius: 2, barPercentage: 0.7
        }]
      },
      options: o, plugins: [vlines]
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
            backgroundColor: 'rgba(139,148,158,.55)', borderWidth: 0, borderRadius: 2 },
          { label: 'Actual', data: pva.map(function (r) { return r.actual_tss || 0; }),
            backgroundColor: 'rgba(16,101,107,.75)', borderWidth: 0, borderRadius: 2 }
        ]
      },
      options: o
    });
  }

  /* ── Zones ───────────────────────────────────────────────────────────── */
  /* Time in zones against the blueprint's target for the phase. Everything here reads
   * the data rather than assuming a shape: the number of bands and their names differ by
   * sport (the swim splits Z1-2 / Z3-4 / Z5 where the bike splits Z1-2 / Z3 / Z4-5), and
   * a phase may state no target at all. Hard-coding three buckets would print a 0%
   * target in a taper, which is a target the blueprint never set. */

  // Deviation the blueprint tolerates. Beyond this a band is off plan in EITHER
  // direction, which is why the delta is coloured by size and not by sign: 10pp more
  // Z4-5 than prescribed is not a good week, and a green + would say it was.
  var ZONE_TOL = 5;

  var ZONE_WINS = [['week', 'This week'], ['r4', '4 weeks'], ['phase', 'Phase']];

  // Coverage below this gets a caveat on the card. Deliberately NOT the published
  // min_coverage (60): that is the threshold at which the PUBLISHER gives up on the
  // preferred signal and falls back to heart rate, so reusing it here would leave
  // everything it chose to keep - Kathryn's bike at 61.7% classified - uncaveated,
  // which is the one case most in need of the caveat.
  var ZONE_COV_WARN = 90;

  // A window counts as available only if it actually HAS focus-sport data in it. The
  // window objects always exist, and an empty one is `{}`, which is truthy - so testing
  // for the key offered Calum a "This week" tab that resolved to nothing and left him
  // on "no zone time recorded" while four weeks of his rides sat in the next window.
  function zoneWindows(d) {
    var zd = (d || {}).zoneDistribution || {};
    var sp = zd.sports || {};
    var fs = focusSports();
    return ZONE_WINS.filter(function (o) {
      var block = sp[o[0]];
      if (!block) return false;
      return Object.keys(block).some(function (name) {
        return fs.indexOf(family(name)) >= 0 &&
               ((block[name] || {}).minutes || 0) > 0;
      });
    });
  }

  // Selected window, falling back through r4 to week.
  function zoneWinSel(d) {
    var avail = zoneWindows(d).map(function (o) { return o[0]; });
    if (!avail.length) return null;
    if (avail.indexOf(state.zoneWin) >= 0) return state.zoneWin;
    if (avail.indexOf('r4') >= 0) return 'r4';
    if (avail.indexOf('week') >= 0) return 'week';
    return avail[0];
  }

  // One shape for the chart and the table to share, so a band can never be drawn with
  // one target and tabulated with another. `targeted` says whether a comparison exists
  // at all; with no bands the raw per-zone split stands in, which is the honest answer
  // to "what did I do" when there is nothing to compare it against.
  function zoneData(d) {
    var zd = (d || {}).zoneDistribution;
    if (!zd) return null;
    var win = zoneWinSel(d);
    if (!win) return null;
    var block = (zd.sports || {})[win] || {};
    var fs = focusSports();
    var out = [];

    Object.keys(block).forEach(function (name) {
      if (fs.indexOf(family(name)) < 0) return;
      var s = block[name] || {};
      // `bands: []` and `bands: null` mean the same thing and neither can be trusted on
      // its own: the publishing sanitiser's Records spec turns a None into an empty
      // array, so the field that actually says whether the blueprint set a target for
      // this phase is target_stated. Both are tested, because an empty band list is
      // nothing to compare against however it was spelled.
      var bands = (s.bands && s.bands.length) ? s.bands : null;
      var stated = !!bands && s.target_stated !== false;
      var rows = [];

      if (stated) {
        // Percentages are of CLASSIFIED time, so the fallback denominator is the sum of
        // the band minutes - not s.minutes, which includes the sessions that carried no
        // zone data and would quietly shrink every share.
        var cls = bands.reduce(function (a, b) { return a + (b.minutes || 0); }, 0);
        bands.forEach(function (b) {
          rows.push({
            label: b.label, minutes: b.minutes, target: b.target == null ? null : b.target,
            actual: b.actual != null ? b.actual : (cls > 0 ? (b.minutes || 0) / cls * 100 : null)
          });
        });
      } else if (s.zones) {
        var z = s.zones;
        var tot = Object.keys(z).reduce(function (a, k) { return a + (z[k] || 0); }, 0);
        Object.keys(z).sort().forEach(function (k) {
          rows.push({ label: k, minutes: z[k], target: null,
                      actual: tot > 0 ? (z[k] || 0) / tot * 100 : null });
        });
      } else if (bands) {
        // No target stated and no raw split published either: the bands still carry real
        // minutes, so they are shown with their targets dropped rather than the sport
        // vanishing from a view that is about where its time went.
        var cls2 = bands.reduce(function (a, b) { return a + (b.minutes || 0); }, 0);
        bands.forEach(function (b) {
          rows.push({ label: b.label, minutes: b.minutes, target: null,
                      actual: b.actual != null ? b.actual
                        : (cls2 > 0 ? (b.minutes || 0) / cls2 * 100 : null) });
        });
      }
      if (!rows.length) return;

      out.push({ sport: name, minutes: s.minutes, sessions: s.sessions,
                 unclassified: s.unclassified_sessions || 0,
                 // PER-SPORT, not the top-level map. The published basis falls back to
                 // heart rate per sport when the preferred signal is too sparse to
                 // classify: Kathryn's runs do (her ICU run threshold was set recently,
                 // so almost no historical run carries pace zones) and Jamie's do not.
                 // Labelling both off the page-level preferred map would state "pace"
                 // over a split that is actually heart rate, for one athlete and not
                 // the other, which is worse than showing no basis at all.
                 basis: s.basis || null,
                 coverage: s.coverage == null ? null : s.coverage,
                 targeted: stated, rows: rows });
    });

    if (!out.length) return null;
    // No top-level basis map here on purpose: it is the PREFERRED basis and the publisher
    // falls back per sport, so it disagrees with what was actually measured. It states
    // 'pace' for Kathryn's runs, which are classified by heart rate (1 of 44 runs carried
    // pace zones), and 'pace' for Jamie's, which are grade-adjusted pace.
    return { win: win, meta: (zd.windows || {})[win] || {}, phase: zd.phase || null,
             sports: out };
  }

  function zoneExtras(d) {
    if (!(d || {}).zoneDistribution) {
      return '<p class="hint">Time in zones appears here once the nightly refresh has ' +
             'published it.</p>';
    }
    var z = zoneData(d);
    if (!z) {
      return '<p class="hint">No zone time recorded for your focus sports in this ' +
             'window.</p>';
    }

    var wl = ZONE_WINS.filter(function (o) { return o[0] === z.win; })[0];
    var bits = [];
    if (z.phase) bits.push(z.phase + ' phase');
    bits.push(z.meta.label || (wl ? wl[1] : z.win));
    if (z.meta.from && z.meta.to) bits.push(z.meta.from + ' to ' + z.meta.to);
    var h = '<p class="hint">' + esc(bits.join(' · ')) + '</p>';

    var tot = z.sports.reduce(function (a, s) {
      return { min: a.min + (s.minutes || 0), n: a.n + (s.sessions || 0),
               un: a.un + s.unclassified };
    }, { min: 0, n: 0, un: 0 });
    h += '<div class="minis">' +
      mini(hhmm(tot.min), 'in zones') +
      mini(tot.n, 'sessions') +
      mini(tot.un, 'no zone data', tot.un ? 'warn' : '') + '</div>';

    z.sports.forEach(function (s) {
      var head = [s.sport, hhmm(s.minutes),
                  s.sessions != null
                    ? s.sessions + ' session' + (s.sessions === 1 ? '' : 's') : null]
        .filter(Boolean).join(' · ');

      var body = '<table class="tbl"><thead><tr><th>Band</th><th>Actual</th>' +
        (s.targeted ? '<th>Target</th><th>Δ pp</th>' : '') +
        '<th>Time</th></tr></thead><tbody>' +
        s.rows.map(function (r) {
          // Rounded BEFORE it is signed: an actual of 69.7 against a target of 70 is
          // 0pp off, and signed(-0.3, 0) prints it as '-0pp'.
          var dl = (r.target != null && r.actual != null)
            ? Math.round(r.actual - r.target) : null;
          return '<tr><td class="lbl">' + esc(r.label) + '</td><td class="t">' +
            (r.actual != null ? Math.round(r.actual) + '%' : '–') + '</td>' +
            (s.targeted
              ? '<td>' + (r.target != null ? Math.round(r.target) + '%' : '–') + '</td>' +
                '<td class="' + (dl == null ? ''
                  : (Math.abs(dl) <= ZONE_TOL ? 'pos' : 'neg')) + '">' +
                (dl == null ? '–' : signed(dl, 0) + 'pp') + '</td>'
              : '') +
            '<td>' + hhmm(r.minutes) + '</td></tr>';
        }).join('') + '</tbody></table>';

      var foot = [];
      // The sport's OWN basis, always stated, and nothing stood in for it: "Run by heart
      // rate" against "Run by grade-adjusted pace" is the difference between a Z2 share
      // you can act on and one you cannot, and the two sit side by side across athletes.
      if (s.basis) foot.push(s.sport + ' by ' + s.basis + '.');
      // A split drawn from two thirds of the time is not the window, and saying 48%
      // Z1-2 without saying that is how a partial figure gets read as the whole one.
      if (s.coverage != null && s.coverage < ZONE_COV_WARN) {
        foot.push('Only ' + Math.round(s.coverage) + '% of ' + s.sport +
          ' time could be classified, so this is that share, not the whole window.');
      }
      if (!s.targeted) {
        foot.push('The blueprint sets no target for ' + s.sport +
          (z.phase ? ' in the ' + z.phase + ' phase' : '') +
          ', so this is the split as it happened with nothing to compare it against.');
      }
      if (s.unclassified) {
        foot.push(s.unclassified + ' session' + (s.unclassified === 1 ? '' : 's') +
          ' had no zone data' + (s.unclassified === 1 ? ' and is' : ' and are') +
          ' not in these figures.');
      }
      h += card(head, body, { flush: true, foot: foot.join(' ') });
    });

    return h;
  }

  // The target, drawn on the bar it belongs to. A second bar per band would be two
  // things to read; the question is the GAP between what was done and what was asked
  // for, and a notch on the same line IS that gap.
  var znotch = {
    id: 'znotch',
    afterDatasetsDraw: function (chart, args, opts) {
      var t = (opts && opts.targets) || [];
      if (!t.length) return;
      var x = chart.scales.x, meta = chart.getDatasetMeta(0), g = chart.ctx;
      t.forEach(function (v, i) {
        var bar = meta.data[i];
        if (v == null || !bar) return;
        var half = ((bar.height || 12) / 2) + 2.5;
        var px = x.getPixelForValue(v);
        g.save();
        g.strokeStyle = C.ink;
        g.lineWidth = 1.6;
        g.beginPath(); g.moveTo(px, bar.y - half); g.lineTo(px, bar.y + half); g.stroke();
        g.restore();
      });
    }
  };

  function chartZones(el, d) {
    var z = zoneData(d);
    if (!z) return null;

    // Flattened to one bar per sport-and-band. Grouping by band across sports needs the
    // bands to line up, and they do not: the swim's second band is Z3-4 where the bike's
    // is Z3, so a shared dataset would have put two different bands on one legend entry.
    var rows = [];
    z.sports.forEach(function (s) {
      s.rows.forEach(function (r, i) {
        rows.push({ sport: s.sport, label: r.label, actual: r.actual, target: r.target,
                    minutes: r.minutes, i: i, n: s.rows.length });
      });
    });

    var HEAT = ['16,101,107', '168,106,18', '179,36,31'];
    // Positional, not by name: the first band is the easy one and the last is the
    // hardest whatever they are called, so a three-band bike and a five-zone raw split
    // both read calm to hot without this function knowing a single zone name.
    var tone = function (r) {
      if (r.n < 2 || r.i === 0) return HEAT[0];
      return r.i === r.n - 1 ? HEAT[2] : HEAT[1];
    };

    var peak = rows.reduce(function (m, r) {
      return Math.max(m, r.actual || 0, r.target || 0);
    }, 0);

    var o = baseOpts();
    o.indexAxis = 'y';
    // baseOpts() compares along x, right for every other chart because they are all
    // vertical. On a horizontal bar that resolves the index against the VALUE axis, so
    // dragging down the rows never changes the row the readout names.
    o.interaction.axis = 'y';
    o.plugins.legend.display = false;      // one dataset; the row labels carry the bands
    o.scales = {
      x: {
        beginAtZero: true, max: Math.min(100, Math.ceil((peak + 6) / 10) * 10),
        title: { display: true, text: '% of classified time', color: C.muted,
                 font: { family: 'DM Mono', size: 9 } },
        ticks: { color: C.muted, font: { family: 'DM Mono', size: 9 }, maxTicksLimit: 6,
                 callback: function (v) { return v + '%'; } },
        grid: { color: C.rule, drawTicks: false }, border: { display: false }
      },
      y: {
        // No autoSkip: a dropped row label leaves a bar belonging to nothing.
        ticks: { color: C.ink2, font: { family: 'DM Mono', size: 9 }, autoSkip: false },
        grid: { display: false }, border: { color: C.rule }
      }
    };
    o.plugins.znotch = { targets: rows.map(function (r) { return r.target; }) };
    o.plugins.tooltip.callbacks = {
      title: function (items) {
        var r = rows[items[0].dataIndex] || {};
        return (r.sport || '') + ' ' + (r.label || '');
      },
      label: function (it) {
        var r = rows[it.dataIndex] || {};
        return [Math.round(it.parsed.x) + '% · ' + hhmm(r.minutes),
                r.target != null
                  ? 'target ' + Math.round(r.target) + '% · ' +
                    signed(Math.round((r.actual || 0) - r.target), 0) + 'pp'
                  : 'no target this phase'];
      }
    };

    return new Chart(el, {
      type: 'bar',
      data: {
        labels: rows.map(function (r) { return r.sport + ' ' + r.label; }),
        datasets: [{
          label: 'Share of time',
          data: rows.map(function (r) { return r.actual; }),
          backgroundColor: rows.map(function (r) { return 'rgba(' + tone(r) + ',.78)'; }),
          borderWidth: 0, borderRadius: 2, barPercentage: 0.72, categoryPercentage: 0.88
        }]
      },
      options: o, plugins: [znotch]
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

    var onDate = (d.recent || []).filter(function (r) { return r.date === dateISO; });
    var act = onDate.filter(function (r) { return !sport || same(r.sport, sport); })[0]
      // Fall back to any activity that day: plan and activity sport names differ
      // ("Bike" vs "Ride", "Brick" vs "Run"), and a route is better than a blank.
      || onDate.filter(function (r) { return r.shape; })[0]
      || onDate[0];
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

    // Route outline. A unit-box shape (no coordinates - see lib/route_shape.py), so it
    // draws straight into a 0..1 viewBox with no projection work here.
    if (a.shape && a.shape.length > 3) {
      h += '<div class="route"><svg viewBox="-0.04 -0.04 1.08 1.08" ' +
        'preserveAspectRatio="xMidYMid meet" aria-hidden="true">' +
        '<polyline points="' + a.shape.map(function (pt) {
          return pt[0] + ',' + pt[1];
        }).join(' ') + '"/>' +
        '<circle cx="' + a.shape[0][0] + '" cy="' + a.shape[0][1] + '" r="0.032"/>' +
        '</svg><span class="route-c">' +
        esc([a.dist ? Number(a.dist).toFixed(1) + ' km' : null,
             a.dur ? hhmm(a.dur) : null].filter(Boolean).join(' · ')) +
        '</span></div>';
    }

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


  /* ── Food ────────────────────────────────────────────────────────────────
     Three views in one page, per spec 13: today against its zones, the rolling
     week, and the block.

     The one rule this renderer must not break: a CEILING is not a progress bar.
     Fibre before a long session, and fat on a crowded day, are limits. Drawing them
     as bars filling toward a goal makes a low number read as failure when it is
     compliance - the exact misreading spec 4.1 warns about. So a ceiling gets its
     own treatment: a limit line and a plain "within" or "over", never a fill.

     There is deliberately no body-fat chart. BIA fat is weight divided by an assumed
     hydration constant, so it tracks the scale at r = 0.999 and a trend line would be
     a weight chart wearing a fat label. publish-nutrition-data.py does not even emit
     the series. */

  /* One zone row. Three things share this space and must stay distinguishable:
     - the FILL, measured against the zone MINIMUM (a floor is satisfied at its floor;
       measuring to the midpoint would make a met floor look unmet)
     - the PACE MARKER, a dashed line at the calorie-progress point, answering "what
       should I reach for". It does NOT answer "am I in trouble" - only the projection
       does, and conflating them is how this page would start crying wolf.
     - the ZONE, as text on the right

     Colour carries meaning and nothing else. Grey is the default INCLUDING ahead or
     behind pace, because being ahead on fat at 2pm is what a normal day looks like.
     Red is earned only when a macro cannot reach its zone or has passed a ceiling. */
  function zoneRow(label, consumed, z, pacePct, req, extra) {
    if (!z) return '';
    var c = Math.round(consumed || 0), lo = Math.round(z.low), hi = Math.round(z.high);
    var bias = z.bias, ceiling = bias === 'ceiling', floor = bias === 'floor';
    var dens = req ? req.density : null;

    var pct = lo ? Math.min(100, (c / lo) * 100) : (c ? 100 : 0);
    var tone = '';                                  // grey unless earned
    var right, delta;
    if (ceiling) {
      right = 'ceiling ' + hi + ' g';
      if (c > hi) { tone = 'bad'; delta = Math.round(c - hi) + ' g over'; }
      else { delta = Math.round(hi - c) + ' g of room'; }
      pct = hi ? Math.min(100, (c / hi) * 100) : 0;
    } else if (floor) {
      right = 'floor ' + lo + ' g';
      delta = c >= lo ? 'met' : Math.round(lo - c) + ' g to go';
      if (c >= lo) tone = 'good';
    } else {
      right = lo + '-' + hi + ' g';
      if (c > hi) { tone = 'bad'; delta = Math.round(c - hi) + ' g over'; }
      else if (c >= lo) { tone = 'good'; delta = 'in zone'; }
      else { delta = Math.round(lo - c) + ' g to go'; }
    }
    if (dens === 'avoid' && !ceiling) tone = tone || '';

    var marker = (pacePct != null && !ceiling)
      ? '<b class="zpace" style="left:' + Math.min(100, pacePct).toFixed(1) + '%"></b>' : '';
    return '<div class="zrow">' +
      '<span class="zl">' + esc(label) +
        '<em>' + esc(extra || bias) + '</em></span>' +
      '<span class="zt' + (ceiling ? ' zt-ceil' : '') + '">' +
        '<i class="' + tone + '" style="width:' + pct.toFixed(1) + '%"></i>' +
        (ceiling ? '<b class="zlim"></b>' : '') + marker + '</span>' +
      '<span class="zv"><b>' + c + '</b><span>' + esc(right) + '</span>' +
        '<span class="' + tone + '">' + esc(delta) + '</span></span></div>';
  }

  var MEAL_ORDER = [['breakfast', 'Breakfast'], ['lunch', 'Lunch'],
                    ['snacks', 'Snacks & fuel'], ['dinner', 'Dinner']];
  var MACRO_LABEL = { protein_g: 'Protein', carb_g: 'Carbs', fat_g: 'Fat',
                      fibre_g: 'Fibre' };
  /* Written out rather than derived, because deriving it is what collided Fat with
     Fibre. Distinct by construction. */
  var MACRO_SHORT = { protein_g: 'p', carb_g: 'c', fat_g: 'fat', fibre_g: 'fibre' };

  /* The carb row has to distinguish the two, because one number cannot answer both
     questions: 900 g on a long-run day is not 900 g of food. Before the session it names
     what is reserved for it; afterwards, what was actually taken. */
  /* TWO GOALS, TWO ROWS. A note on one row was not clear enough, and the bar still
     measured against the 897 g total - so the number he acts on when deciding what to eat
     was the one he could not see. Food and in-run fuel are separate goals: the second is a
     RATE taken on the move, and no amount of dinner substitutes for it. */
  function carbRows(day, t, z, r) {
    var cp = day.carb_plan;
    if (!cp || !cp.in_session_planned_g || !z || !z.carb_g) {
      return zoneRow('Carbs', t.carb_g, z && z.carb_g, day.pace_pct,
                     r && r.macros.carb_g, carbNote(day));
    }
    var taken = (day.carb_split && day.carb_split.in_session_g) || 0;
    var food = Math.max(0, (t.carb_g || 0) - taken);
    return zoneRow('Carbs from food', food,
                   { low: cp.out_of_session_low, high: cp.out_of_session_high,
                     bias: z.carb_g.bias }, null, r && r.macros.carb_g,
                   'out of session') +
      zoneRow('Carbs in-run', taken,
              { low: cp.in_session_planned_g, high: cp.in_session_planned_g,
                bias: 'floor' }, null, null,
              'prescribed for the session');
  }

  function carbNote(day) {
    var cp = day.carb_plan, split = day.carb_split;
    if (split && split.in_session_g) {
      return Math.round(split.in_session_g) + ' g in-run';
    }
    if (cp && cp.in_session_planned_g) {
      return Math.round(cp.in_session_planned_g) + ' g in-run fuel, prescribed \u2013 ' +
        Math.round(cp.out_of_session_low) + ' g from food';
    }
    return '';
  }

  /* THE ROLLING ENERGY BALANCE, stated in WORDS rather than in a sign. What this replaces
     printed "+73 kcal/day average" under the word deficit, where the plus actually meant he
     had eaten 73 OVER his target: the sign said one thing and the label said the opposite.
     A surplus now reads as a surplus and a deficit as a deficit, so there is nothing left
     to misread by sign.

     Every read is guarded. JSON published before these keys existed carries none of them,
     and then the line does not appear at all rather than printing NaN or undefined. No
     arithmetic beyond picking the word and the direction: the figures, their denominators
     and their coverage are all computed in publish-nutrition-data.py, which is the only
     place that knows which days are settled. */
  function balanceLine(wk, label, terse) {
    var s = (wk && wk.summary) || null;
    if (!s || s.mean_deficit_vs_maintenance_kcal_day == null) { return ''; }
    // Positive is a real deficit, as published: maintenance less what was eaten.
    var d = Math.round(s.mean_deficit_vs_maintenance_kcal_day);
    // `terse` drops "vs maintenance" for the top of the day card, where this sits under a
    // 34px headline in 10px mono and a long line wraps to a second row on a phone. The
    // basis is still stated once on the page, in the Block card where there is room.
    var bits = [label + ': ' + (d === 0
      ? 'level with maintenance'
      : (d > 0 ? 'deficit ' : 'surplus ') + Math.abs(d).toLocaleString() +
        ' kcal/day' + (terse ? '' : ' vs maintenance'))];
    var kg = s.implied_kg_per_week;
    // Rounded to 0.00 is not a trend, and printing "0.00 kg/wk down" would give a
    // direction to a number that has none.
    if (kg != null && Math.abs(kg) >= 0.01) {
      bits.push('about ' + Math.abs(kg).toFixed(2) + ' kg/wk ' + (kg > 0 ? 'down' : 'up'));
    }
    // Coverage travels WITH the figure and is published beside it, so a two-day average
    // can never be read as a week.
    if (s.deficit_coverage) { bits.push(s.deficit_coverage); }
    return bits.join(' · ');
  }

  /* A ZEROED DEFICIT MUST NOT BE SILENT. When the engine holds the target at maintenance
     it says why, and until now that sentence never left the VM: the page showed a target
     equal to maintenance and no reason.

     Gated on the WARNING, never on deficit_applied_kcal === 0. A deficit that was never
     enabled is also zero - which is most of this athlete's days - and triggering on the
     zero would have the app assert a suppression that did not happen. Only these two
     messages are surfaced: "deficit capped at ..." is engine chatter about a deficit that
     WAS applied, and belongs in the logs. */
  var DEFICIT_HELD = /^deficit (suppressed|dropped)/;
  function deficitHeldNote(z) {
    var w = (z && z.warnings) || [];
    for (var i = 0; i < w.length; i++) {
      if (DEFICIT_HELD.test(w[i])) { return w[i]; }
    }
    return '';
  }

  function dayLabel(iso) {
    // Built from the ISO string, never from new Date(iso) alone: a bare date parses as UTC
    // and renders as the previous day for anyone west of it.
    var p = String(iso || '').split('-');
    if (p.length !== 3) { return iso || ''; }
    var d = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
    return d.toLocaleDateString(undefined,
      { weekday: 'short', day: 'numeric', month: 'short' });
  }

  function stepFoodDay(dir) {
    state.foodDayOffset = Math.max(0, (state.foodDayOffset || 0) + (dir === 'older' ? 1 : -1));
    renderFood();
  }

  function renderFood() {
    var n = state.nutr;
    var host = $('#v-food');
    if (!host) return;
    // Delegated ONCE on the host, which renderFood replaces wholesale every call: a handler
    // bound to the buttons themselves would be discarded on the next render.
    if (!host.dataset.dayNav) {
      host.dataset.dayNav = '1';
      host.addEventListener('click', function (e) {
        var row = e.target.closest('[data-food-date]');
        if (row) {
          state.foodDate = row.getAttribute('data-food-date');
          renderFood();
          return;
        }
        var b = e.target.closest('[data-food-day]');
        if (b && !b.disabled) { stepFoodDay(b.getAttribute('data-food-day')); }
      });
    }
    if (!n || !n.days || !n.days.length) {
      host.innerHTML = '<div class="card"><div class="empty">Nothing logged yet. ' +
        'Tell the nutrition bot what you ate and it appears here.</div></div>';
      return;
    }
    // The offset is clamped on every read rather than trusted: it survives in state across
    // a data refresh, and a stale one would index past the array and blank the tab.
    var logged = n.days.filter(function (d) { return (d.items || []).length; });
    if (!logged.length) { logged = n.days.slice(-1); }
    if (state.foodDayOffset == null) { state.foodDayOffset = 0; }
    state.foodDayOffset = Math.max(0, Math.min(state.foodDayOffset, logged.length - 1));
    // A DATE, not an index: an index into a filtered list goes stale the moment the data
    // refreshes and would quietly show a different day than the one he tapped. A date that
    // is no longer present simply falls back to today.
    var day = null;
    if (state.foodDate) {
      day = logged.filter(function (x) { return x.date === state.foodDate; })[0] || null;
    }
    if (!day) { day = logged[logged.length - 1 - state.foodDayOffset]; }
    var isToday = day === logged[logged.length - 1];
    // The older/newer buttons are gone - Jamie would rather pick the day from the rolling
    // table than step one at a time. The offset itself stays, because that is what a row in
    // that table will set.
    var z = day.zones, t = day.totals || {}, r = day.requirement;
    var h = '';


    /* 1. Reach for. First on the page and the whole point of it: what the next meal
          has to be, stated as an instruction rather than as a fault. Composed in
          Python, never phrased here. */
    if (r) {
      h += '<section class="reach' + (r.at_target ? ' done' : '') + '">' +
        '<span class="reach-k">Reach for</span>' +
        '<h2>' + esc(r.headline.replace(/^Reach for /, '')) + '</h2>' +
        '<p>' + esc(r.reason) + '</p></section>';
    }

    /* 2. Position. Where you are, not where you are going: the zones carry the
          targets and repeating them here would be two sources for one number. */
    /* First letter of the label was the shorthand, which made Fat and Fibre BOTH "f" -
       two different numbers wearing the same name, on the one row you glance at. */
    var chips = ['protein_g', 'carb_g', 'fat_g', 'fibre_g'].map(function (k) {
      return '<span class="chip"><b>' + Math.round(t[k] || 0) + '</b>' +
        esc(MACRO_SHORT[k]) + '</span>';
    }).join('');
    /* Jamie, 13 Aug 2026: "move this somewhere near the total cals at the top. can repeat
       at the bottom with the weight tracking." The rolling figure belongs beside the
       calories it is made of - one day's total is not a verdict, and this is the number he
       is actually judging by. It is repeated once, in the Block card, and nowhere else. */
    var bal = balanceLine(n.week, '7-day', true);
    // ONLY for the day this card claims to describe. Any row in the Recent-days table can
    // be selected, and the selection survives a refresh - so a day-scoped warning printed
    // unconditionally would put Monday's "resting HR elevated" under a heading that says
    // "So far today". The suppression belongs to a day, and the reader is told which.
    var held = isToday ? deficitHeldNote(z) : '';
    h += card('So far today',
      '<div class="fnow"><span class="fnow-n">' +
      Math.round(t.kcal || 0).toLocaleString() + '</span>' +
      '<span class="fnow-u">kcal</span><div class="chips">' + chips + '</div>' +
      (r ? '<span class="fnow-r">' + Math.round(r.remaining_kcal).toLocaleString() +
        ' kcal left</span>' : '') +
      (bal ? '<span class="fnow-r">' + esc(bal) + '</span>' : '') +
      (held ? '<span class="fnow-r">' + esc(held) + '</span>' : '') + '</div>' +
      '<div class="zones">' +
      zoneRow('Protein', t.protein_g, z && z.protein_g, day.pace_pct,
              r && r.macros.protein_g) +
      carbRows(day, t, z, r) +
      zoneRow('Fat', t.fat_g, z && z.fat_g, day.pace_pct, r && r.macros.fat_g) +
      zoneRow('Fibre', t.fibre_g, z && z.fibre_g, day.pace_pct,
              r && r.macros.fibre_g,
              /* The ceiling is about TIMING. Saying so is the difference between a coach
                 and a food diary: without it, a 40 g dinner after the run reads as 20 g
                 over a limit, which is the app telling him off for doing it right. */
              (z && z.fibre_g && z.fibre_g.after_session)
                ? 'ceiling until the run \u2013 then ' +
                  Math.round(z.fibre_g.after_session.low) + ' g floor after'
                : '') +
      '</div>',
      );

    /* 3b. In-run fuelling, assessed apart from the day. Jamie: "I can over carb in the
           day and under in the run and it looks fine." A rate cannot be made good at
           dinner, so it gets its own verdict rather than living inside the carb zone. */
    var ins = day.in_session;
    if (ins) {
      var vlabel = { on_target: 'on target', acceptable: 'acceptable', under: 'under' };
      h += card('In-run fuelling',
        '<div class="figures in-card">' +
        fig(ins.g_per_hr.toFixed(0), 'g/hr taken', ins.sport + ', ' +
            ins.session_minutes + ' min') +
        fig(ins.target_g_hr.toFixed(0), 'g/hr prescribed', 'ramping from your logs') +
        fig(ins.shortfall_g ? '-' + ins.shortfall_g : 'ok', 'shortfall',
            ins.shortfall_g ? 'grams over the session' : 'rate met',
            ins.verdict === 'under' ? 'neg' : (ins.verdict === 'on_target' ? 'pos' : '')) +
        '</div>' +
        '<p class="insnote' + (ins.verdict === 'under' ? ' miss' : '') + '">' +
        esc(ins.verdict === 'under'
            ? 'Under the prescribed rate. This is a delivery rate, not a budget, so it '
              + 'cannot be recovered by eating more later.'
            : 'Rate ' + (vlabel[ins.verdict] || ins.verdict) +
              '. Counted in the day total as well, but judged on its own.') +
        '</p>');
    }

    /* 4. What the rest of the day has to look like. Required density against a normal
          meal's density is what turns grams into "high protein, low fat". */
    if (r && !r.at_target) {
      var rows = Object.keys(MACRO_LABEL).filter(function (k) {
        return r.macros[k];
      }).map(function (k) {
        var v = r.macros[k];
        var need = v.bias === 'ceiling'
          ? '<span class="mut">max ' + Math.round(v.headroom_g || 0) + ' g</span>'
          : (v.still_needed_g ? Math.round(v.still_needed_g) + ' g' :
             '<span class="mut">met</span>');
        var cmp = v.required_share
          ? Math.round(v.required_share * 100) + '% <span class="mut">vs ' +
            (v.normal_share ? Math.round(v.normal_share * 100) + '%' : 'n/a') + '</span>'
          : '<span class="mut">-</span>';
        return '<tr><td class="lbl">' + esc(MACRO_LABEL[k]) + '</td>' +
          '<td class="nw">' + need + '</td><td class="nw">' + cmp + '</td>' +
          '<td><span class="dens ' + esc(v.density) + '">' + esc(v.density) +
          '</span></td></tr>';
      }).join('');
      h += card('What the rest of today has to look like',
        '<div class="tblwrap"><table class="tbl dtbl"><thead><tr><th>Macro</th>' +
        '<th>Still needed</th>' +
        // Stacked, not stretched: on a phone this header was what pushed the table past
        // the viewport - nowrap cells plus a long heading, with only vertical overflow
        // handled anywhere. The comparison still has to be stated, just not on one line.
        '<th>Share left<span class="mut nlbl">vs normal meal</span></th>' +
        '<th></th></tr></thead>' +
        '<tbody>' + rows + '</tbody></table></div>');
    }

    /* 5. The log, by meal. Unlogged meals stay visible and greyed: an empty Dinner
          row is information. */
    var meals = day.meals || {};
    var mealHtml = MEAL_ORDER.map(function (pair) {
      var items = meals[pair[0]] || [];
      var sub = items.reduce(function (a, i) { return a + (i.kcal || 0); }, 0);
      if (!items.length) {
        return '<div class="meal empty-meal"><div class="meal-h"><b>' + esc(pair[1]) +
          '</b><span>not logged</span></div></div>';
      }
      return '<div class="meal"><div class="meal-h"><b>' + esc(pair[1]) +
        '</b><span>' + Math.round(sub).toLocaleString() + ' kcal</span></div>' +
        items.map(function (i) {
          return '<div class="mi' + (i.confidence === 'estimate' ? ' est' : '') +
            (i.in_session ? ' insess' : '') + '">' +
            '<span class="mi-n">' + esc(i.name || '') +
            (i.in_session ? ' <em>in session</em>' : '') +
            (i.confidence === 'estimate' ? ' <em>est</em>' : '') + '</span>' +
            '<span class="mi-m">' + Math.round(i.kcal || 0) + ' · ' +
            Math.round(i.protein_g || 0) + 'p · ' + Math.round(i.carb_g || 0) + 'c · ' +
            Math.round(i.fat_g || 0) + 'f</span></div>';
        }).join('') + '</div>';
    }).join('');
    var notes = [];
    if (t.non_counting_protein_g) {
      notes.push('+' + Math.round(t.non_counting_protein_g) +
                 ' g collagen, not counted toward protein');
    }
    if (day.fuel_from_coach) {
      notes.push('Includes ' + Math.round(t.in_session_carb_g) +
                 ' g of ride fuel from the coach bot');
    }
    if (t.dietary_sodium_mg) {
      notes.push('Sodium ' + Math.round(t.dietary_sodium_mg).toLocaleString() +
                 ' mg, no target set');
    }
    (z && z.modifiers ? z.modifiers : []).forEach(function (m) { notes.push(m); });
    h += card('Log', '<div class="meals">' + mealHtml + '</div>' +
      (notes.length ? '<ul class="notelist"><li>' + notes.map(esc).join('</li><li>') +
        '</li></ul>' : ''));

    /* 5b. THE WEEK. Jamie: "missing one day s fine missing every day is a problem." A
           single day cannot show that, so this is one row per day with the counts
           underneath. Consistency is the thing being measured, not any one number. */
    var wk = n.week;
    if (wk && wk.days && wk.days.length) {
      var sm = wk.summary || {};
      var rows = wk.days.map(function (d) {
        if (!d.logged) {
          return '<tr class="miss" data-food-date="' + esc(d.date) + '">' +
            '<td class="lbl">' + esc(d.dow) + '</td>' +
            '<td colspan="4"><span class="mut">not sampled</span></td></tr>';
        }
        var pm = d.protein_met ? 'ok' : 'low';
        var fk = d.fibre_ok === false ? 'bad' : '';
        var fibLabel = (d.fibre_bias === 'ceiling' ? '\u2264' : '') +
          Math.round(d.fibre_g || 0);
        var run = d.in_run_verdict
          ? '<span class="dens ' + (d.in_run_verdict === 'under' ? 'avoid' : 'high') +
            '">' + Math.round(d.in_run_g_hr) + ' g/hr</span>'
          : '<span class="mut">-</span>';
        return '<tr data-food-date="' + esc(d.date) + '"' +
          (d.date === day.date ? ' class="picked"' : '') + '>' +
          '<td class="lbl">' + esc(d.dow) + '</td>' +
          '<td class="nw">' + Math.round(d.kcal || 0).toLocaleString() +
          '<span class="mut"> / ' + Math.round(d.kcal_target || 0).toLocaleString() +
          '</span></td>' +
          '<td class="nw ' + pm + '">' + Math.round(d.protein_g || 0) + 'p</td>' +
          '<td class="nw ' + fk + '">' + esc(fibLabel) + 'f</td>' +
          '<td class="nw">' + run + '</td></tr>';
      }).join('');
      // Days SAMPLED, never "missed", and never coloured: logging is occasional by
      // design, so an unlogged day is a choice rather than a failure. The count is here
      // to size the other figures, not to judge.
      h += card('Recent days',
        '<div class="figures in-card">' +
        // No expectation of frequency: he dips in when he wants a check, not weekly.
        fig(sm.days_logged, 'days sampled', 'in the last ' + sm.days_in_window) +
        fig(sm.days_logged
            ? sm.protein_met_days + '/' + sm.days_logged : '-', 'protein floor met',
            'of the days sampled') +
        fig(sm.in_run_sessions
            ? sm.in_run_on_target + '/' + sm.in_run_sessions : '-',
            'in-run fuelling', sm.in_run_sessions ? 'at or above the rate' : 'no long sessions') +
        '</div>' +
        '<table class="tbl wktbl"><thead><tr><th>Day</th><th>Energy</th>' +
        '<th>Protein</th><th>Fibre</th><th>In-run</th></tr></thead><tbody>' +
        rows + '</tbody></table>');
    }

    /* 6. Footer: plants and provenance. Per-item confidence is already on each row
          above; this is the summary, not the substitute. */
    var p = n.plants || {}, pv = day.provenance || {};
    h += card('Week and provenance',
      '<div class="figures in-card">' +
      // No number while the scores are missing. A caveated wrong number gets read as
      // the number.
      /* whole_7d, not unique_7d: the old headline counted a pinch of cumin at ingredient 14
         of a dip exactly like a portion of spinach, so two logged days read 29. This counts
         species eaten as FOOD, with seasonings and traces shown beside it rather than folded
         into it - they are really in the food, they are just not portions. */
      fig(p.whole_7d != null ? p.whole_7d : 'n/a', 'plant species',
          p.whole_7d != null ? 'aiming around ' + (p.target || 30) : 'not yet countable',
          p.whole_7d != null ? '' : 'flat') +
      (p.trace_7d ? fig(p.trace_7d, 'seasonings + traces',
                        'in the label, not a portion') : '') +
      fig(p.new_today != null ? p.new_today : '-', 'new today', 'variety, not a score') +
      fig((n.weight && n.weight.rolling_7d_mean_kg)
          ? n.weight.rolling_7d_mean_kg.toFixed(1) : '-', 'kg', 'morning 7-day mean') +
      '</div>' +
      (p.whole_7d == null
        ? '<p class="prov warn">No plant count yet. ' +
          esc(String(p.unscored_species)) + ' species in this window were logged before ' +
          'the matched score was stored, so refined derivatives such as sunflower oil, ' +
          'sugar and soy lecithin cannot be told apart from whole plants. A count will ' +
          'appear once the window holds only correctly scored days.</p>'
        : '') +
      '<p class="prov">' +
      esc((pv.label || 0) + ' label-verified, ' + (pv.database || 0) + ' from a database, ' +
          (pv.estimate || 0) + ' estimated' +
          (pv.estimate ? ' at roughly ' + pv.estimate_error_band : '')) + '</p>' +
      (p.species && p.species.length
        ? '<p class="plantlist">' + esc(p.species.join(' · ')) + '</p>' : ''));

    /* Block. Kept last: it is context, not the day's decision. */
    var b = n.block || {}, w = n.weight || {}, proj = w.projection;
    var blockBody = '<div class="figures in-card">' +
      fig(b.days_to_race != null ? b.days_to_race : '-', 'days to race',
          b.race_name || '') +
      fig(w.race_target_kg || '-', 'kg target', 'from your profile') +
      fig(proj ? proj.projected_race_kg : '-', 'kg projected',
          proj ? (proj.reaches_target ? 'meets it' : proj.shortfall_kg + ' kg short') : '') +
      '</div>';
    if (proj && !proj.reaches_target) {
      // The target is never shown without the shortfall: an unreachable number on its
      // own is worse than no number.
      blockBody += '<p class="proj miss">Reaching ' + w.race_target_kg +
        ' kg would need about ' + proj.required_daily_kcal_to_reach.toLocaleString() +
        ' kcal/day every day, which the safety limits block.</p>';
    }
    /* The same figure again, here beside the weight, because this is where it gets checked.
       Named as IMPLIED and not as measured: it is arithmetic on a food log with a 10-15%
       estimate band on some items, and the morning weight above is the only measured
       quantity in the chain. No observed kg/week beside it - the published JSON carries the
       morning series but no trend figure, and deriving one here would be the page doing its
       own science, which is exactly how a plausible wrong number gets onto it. */
    // The full wording here, basis included: this card has the room the day headline does
    // not, and the basis has to be stated somewhere on the page.
    var balFull = balanceLine(n.week, '7-day');
    if (balFull) {
      blockBody += '<p class="prov">' + esc(balFull) +
        '. Implied by the food log; the morning weight is the measured check.</p>';
    }
    // No body-fat trend, and no note explaining its absence: nobody needs telling
    // what is not on a page. The reason lives in Settings.
    h += card('Block', blockBody);

    host.innerHTML = h;
  }

  /* ── Settings ────────────────────────────────────────────────────────── */

  function renderSettings() {
    var d = state.data || {};
    var p = d.profile || {};
    var cur = state.slug;

    var h = card('Athlete', '<div class="body-flush">' + ATHLETES.map(function (a) {
      return '<button type="button" class="pickrow' + (a.slug === cur ? ' on' : '') +
        '" data-slug="' + a.slug + '">' +
        '<span class="gate-mark">' + esc(a.name.charAt(0)) + '</span>' +
        '<span class="gate-row-t"><b>' + esc(a.name) + '</b><span>' +
        (a.slug === cur ? 'showing now' : 'switch to this profile') + '</span></span>' +
        '<span class="gate-go">' + (a.slug === cur ? '✓' : '→') + '</span></button>';
    }).join('') + '</div>', { flush: true });

    // Multi-select, so these are toggle rows rather than a segmented control: a seg
    // reads as "pick one". data-sport, not data-slug, keeps them clear of the athlete
    // handler bound on the whole view.
    var fs = focusSports();
    var SPNAME = { swim: 'Swim', bike: 'Bike', run: 'Run' };
    h += card('Focus sports', '<div class="body-flush" id="sportPick">' +
      FOCUS.map(function (s) {
        var on = fs.indexOf(s) >= 0;
        return '<button type="button" class="pickrow' + (on ? ' on' : '') +
          '" data-sport="' + s + '" aria-pressed="' + on + '">' +
          '<span class="gate-mark"><span class="sp sp-' + s + '" style="margin:0"></span></span>' +
          '<span class="gate-row-t"><b>' + esc(SPNAME[s]) + '</b><span>' +
          (on ? 'in focus' : 'logged, but not a focus') + '</span></span>' +
          '<span class="gate-go">' + (on ? '✓' : '+') + '</span></button>';
      }).join('') + '</div>',
      { flush: true,
        foot: 'Sets which sports get their own fitness series, zone card and trend tab. ' +
              'Strength and everything else still appear in the calendar, the week ' +
              'totals and the load chart, because they are training you did. The last ' +
              'sport cannot be turned off - an app focused on nothing has nothing to show.' });

    // Only offered when the data exists. A toggle that cannot reveal anything is
    // worse than no toggle: it implies the feature is broken rather than not published.
    if (state.nutr && state.nutr.nutrition_enabled) {
      var fon = nutritionOn();
      h += card('Food tracking',
        '<div class="body-flush" id="foodPick">' +
        '<button type="button" class="pickrow' + (fon ? ' on' : '') +
        '" data-food="1" aria-pressed="' + fon + '">' +
        '<span class="gate-mark">F</span>' +
        '<span class="gate-row-t"><b>Food tab</b><span>' +
        (fon ? 'showing in the bar' : 'hidden') + '</span></span>' +
        '<span class="gate-go">' + (fon ? '✓' : '+') + '</span></button></div>',
        { flush: true,
          foot: 'Hides or shows the Food tab on this device. It is opt-in per athlete ' +
                'and off unless nutrition data is published for them, so turning it ' +
                'on here cannot reveal anyone else\u2019s log.' });
    }

    if (state.nutr && state.nutr.nutrition_enabled) {
      h += card('About the food numbers',
        '<ul class="notelist about">' +
        '<li>A <b>ceiling</b> is a limit, so coming in under one is the point. A ' +
        '<b>floor</b> is a minimum, and going past it is not an event.</li>' +
        '<li>The dashed mark on a bar is where a macro would sit if it tracked ' +
        'calories exactly. It says what to reach for, not that anything is wrong.</li>' +
        '<li>30 plants a week comes from an observational study (McDonald et al., ' +
        '2018) comparing 30+ against 10 or fewer. It is a variety prompt, not a ' +
        'threshold: 28 against 32 means nothing.</li>' +
        '<li>Sodium has no personal target because there is no sweat test, so the ' +
        'assumed band is ' + esc(state.nutr.sodium.assumed_band_mg_l.join(' to ')) +
        ' mg/L.</li>' +
        '<li>There is no body-fat trend. BIA body fat tracks the scale at r = 0.999, ' +
        'so a trend line would be a weight chart with a different label.</li>' +
        '<li>Collagen is excluded from the protein figure: no tryptophan and little ' +
        'leucine, so counting it would show a target met that was not.</li>' +
        '<li>Meals are grouped by the clock, not by anything you told it.</li></ul>');
    }

    h += card('Data', '<table class="tbl"><tbody>' +
      '<tr><td class="lbl">Last refreshed</td><td class="t">' +
      esc(d.generated || '—') + '</td></tr>' +
      (d.refreshCadence
        ? '<tr><td class="lbl">Refresh schedule</td><td>' + esc(d.refreshCadence) + '</td></tr>'
        : '') +
      '<tr><td class="lbl">Threshold power</td><td>' +
      (d.resolvedFtp ? d.resolvedFtp + 'w' : '—') + '</td></tr>' +
      '</tbody></table>',
      { flush: true, foot: 'Figures come from the nightly published subset. ' +
                          'Body weight, HRV and resting HR are never published.' });

    var css = (d.swimLog || []).filter(function (x) {
      return /css|test/i.test(x.name || '');
    }).slice(-4).reverse();

    h += card('Where the numbers come from', '<table class="tbl"><tbody>' +
      [['Fitness (CTL)', (d.kpi || {}).ctl, 'Intervals.icu wellness, as of ' + (d.generated || '?')],
       ['Threshold power', d.resolvedFtp ? d.resolvedFtp + 'w' : null,
        p.ftp_watts === d.resolvedFtp ? 'profile value, confirmed against season eFTP'
                                      : 'season eFTP — overrides a stale profile value'],
       ['Threshold run pace', p.run_threshold_pace_per_km, 'profile — set from testing, not auto-derived'],
       ['Swim CSS', p.swim_css_per_100m,
        css.length ? 'profile — last test ' + css[0].date + ' (' + esc(css[0].name || '') + ')'
                   : 'profile — no CSS test found in the published swim log'],
       ['Ramp guide', d.rampCap != null ? '+' + d.rampCap + ' CTL/wk' : null,
        'your configured max_ctl_ramp_per_week, not a global default'],
       ['Heat decay', (d.heatAccl || {}).tau_days ? (d.heatAccl.tau_days) + ' day tau' : null,
        'exponential decay; score falls to ~37% of peak after one tau with no exposure'],
       ['Power curve window', (d.powerCurveWindow || {}).days ?
          (d.powerCurveWindow.days) + ' days' : null, (d.powerCurveWindow || {}).label || '']
      ].filter(function (r) { return r[1] != null && r[1] !== ''; }).map(function (r) {
        return '<tr><td class="lbl">' + esc(r[0]) + '</td><td class="t">' + esc(r[1]) +
          '</td></tr><tr class="prov"><td colspan="2">' + esc(r[2]) + '</td></tr>';
      }).join('') + '</tbody></table>',
      { flush: true, foot: 'Every figure the coach quotes is derived from these. If one ' +
                          'looks wrong, this is the input to challenge.' });

    if (css.length) {
      h += card('CSS test history', '<table class="tbl">' +
        '<thead><tr><th>Date</th><th>Session</th><th>Pace</th></tr></thead><tbody>' +
        css.map(function (x) {
          return '<tr><td class="lbl">' + esc(x.date) + '</td><td>' + esc(x.name || '—') +
            '</td><td class="t">' + esc(x.pace_per_100m || '—') + '</td></tr>';
        }).join('') + '</tbody></table>', { flush: true });
    }

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

    // Deselecting the last sport is refused rather than silently read as "all", because a
    // control showing nothing selected while the app behaves as though everything is
    // selected misreports its own state.
    $('#sportPick').onclick = function (e) {
      var b = e.target.closest('.pickrow[data-sport]');
      if (!b) return;
      var cur = focusSports(), s = b.dataset.sport;
      var next = cur.indexOf(s) >= 0
        ? cur.filter(function (x) { return x !== s; })
        : FOCUS.filter(function (x) { return x === s || cur.indexOf(x) >= 0; });
      if (!next.length) return;
      try { localStorage.setItem(SKEY + state.slug, next.join(',')); }
      catch (err) { /* choice just won't persist */ }
      renderSettings();
      renderTrends();
      // Only while Trends is on screen: a canvas sized inside a display:none section
      // comes out 0px wide and stays that way.
      if (state.tab === 'trends') drawTrend();
    };

    var fp = $('#foodPick');
    if (fp) fp.onclick = function (e) {
      if (!e.target.closest('.pickrow[data-food]')) return;
      var next = nutritionOn() ? '0' : '1';
      try { localStorage.setItem('cc.food.' + state.slug, next); }
      catch (err) { /* choice just won't persist */ }
      renderSettings();
      buildTabs();
      // If the tab being hidden is the one on screen, move somewhere that still exists.
      if (next === '0' && state.tab === 'food') show('today');
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
    TABS.forEach(function (t) {
      var v = t.href ? null : $('#v-' + t.id);
      if (v) v.innerHTML = s;
    });
  }

  function renderAll() {
    // Each view is isolated. Previously these ran as one statement, so the first
    // exception skipped every later view: renderGoals threw on Calum, Settings never
    // rendered, and Settings is the only route to switching athlete - which stranded
    // the user on a profile with no way out. A broken view should cost that view only.
    [['today', renderToday], ['cal', renderCalendar], ['trends', renderTrends],
     ['goals', renderGoals], ['food', renderFood],
     ['set', renderSettings]].forEach(function (v) {
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
    drawToday();
  }

  // Today's copy of the load chart. Separate instance from the Trends one: two charts
  // can be mounted at once, so one shared `state.chart` would destroy the wrong canvas.
  function drawToday() {
    if (typeof Chart === 'undefined' || !state.data) return;
    var el = document.getElementById('c-today');
    if (!el) return;
    if (state.todayChart) { state.todayChart.destroy(); state.todayChart = null; }
    try {
      state.todayChart = chartLoad(el, state.data);
      attachReadout(state.todayChart, '#ro-today');
    } catch (err) {
      if (window.console) console.error('[peak] today load chart failed:', err);
    }
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
      .then(loadNutrition)
      .catch(function () {
        $('#v-today').innerHTML =
          '<div class="card"><div class="empty">Could not load ' + esc(slug) +
          '’s data. It refreshes nightly.</div></div>' + chatCTA('Ask the coach instead');
      });
  }

  /* Opt-in and per athlete, so a 404 is the NORMAL case for anyone without the flag
     and must not surface as an error. */
  function loadNutrition() {
    state.nutr = null;
    fetch('../ClaudeCoach/public/nutrition-' + state.slug + '.json?v=' + Date.now(),
          { cache: 'no-cache' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        state.nutr = j;
        // The tab bar is built before this resolves, so it has to be rebuilt once the
        // opt-in answer is known - otherwise Food never appears without a reload.
        buildTabs();
        renderFood();
        renderSettings();
      })
      .catch(function () { /* absent by design for athletes without the flag */ });
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
