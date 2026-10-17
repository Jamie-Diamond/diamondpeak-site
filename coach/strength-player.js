/* Peak strength player (17 Oct 2026, preview). Guides an athlete through a strength
   session: each exercise animated from two angles, the target and how it should feel,
   weight and reps logged for every set (each side separately where the two legs can
   differ), a rest timer, and a suggestion for next time from what was logged.
   Jamie, 17 Oct: "Make sure you log weights and reps, also be clear on how to build,
   what should be comfortable. Make it so if in the gym you can do things out of order
   as you have to deal with equipment availability." So any exercise can be done next,
   and a busy machine can be swapped for a same-job alternative.
   In Peak (app.js) the log comes from and goes to /api/strength/<slug> (history +
   onFinish), which merges it into the watch's Intervals.icu activity. Without them (the
   preview page, strength-lab.html) it lives on the phone (localStorage) and says so. */
(function (global) {
  'use strict';
  var KEY_LOG = 'peakStrength.log.v1', KEY_LIVE = 'peakStrength.live.v1';
  var EFFORT = [
    { id: 'easy', label: 'Easy', sub: 'Could have done 4+ more' },
    { id: 'right', label: 'About right', sub: '2-3 more in the tank' },
    { id: 'hard', label: 'Too hard', sub: '0-1 more, or form slipped' }
  ];
  var LIB, ANIMS, S = null, root = null, anim = null, tick = 0, wake = null, onExit = null;
  var HIST = null, SAVE = null, LIVE = KEY_LIVE;

  // ── helpers ──
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function load(k, d) { try { return JSON.parse(global.localStorage.getItem(k)) || d; } catch (e) { return d; } }
  function save(k, v) { try { global.localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } }
  function num(v, d) { v = parseFloat(v); return isNaN(v) ? d : v; }
  function kgTxt(v) { return v == null || v === '' ? '' : String(Math.round(v * 10) / 10); }
  function ex(id) { var e = LIB.exercises[id] || {}; e.id = id; return e; }
  function mins(s) { return s >= 60 ? (Math.round(s / 6) / 10) + ' min' : s + ' s'; }
  function clock(ms) { var s = Math.max(0, Math.round(ms / 1000)); return Math.floor(s / 60) + ':' + ('0' + s % 60).slice(-2); }
  function parseRange(t) {
    var m = String(t || '').match(/(\d+)(?:\s*[-–]\s*(\d+))?/);
    return m ? { lo: +m[1], hi: +(m[2] || m[1]) } : null;
  }
  function logType(e, it) {
    if (it && it.log) return it.log;
    if (e.log) return e.log;
    if (/\d\s*s\b|sec/.test(String((it && it.reps) || (e.dose && e.dose.reps) || ''))) return 'seconds';
    if (num(e.load_step_kg, 0) > 0) return 'weight_reps';
    return (e.equipment || []).some(function (q) { return /barbell|db_kb|dumbbell|kettlebell|machine|pulldown|cable/.test(q); }) ? 'weight_reps' : 'reps';
  }
  function sides(e) { return e.per_side ? ['L', 'R'] : [null]; }
  function isPhysio(it) { return /physio/i.test(String(it.sets) + String(it.reps)); }

  // ── the log ──
  function history() { return HIST || load(KEY_LOG, []); }
  function localISO(t) { var d = new Date(t || Date.now()); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19); }
  function uid() { return (global.crypto && global.crypto.randomUUID) ? global.crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2, 10); }
  function lastFor(id) {
    var h = history();
    for (var i = h.length - 1; i >= 0; i--) {
      var en = (h[i].entries || []).filter(function (x) { return x.ex === id && x.sets && x.sets.length; })[0];
      if (en) return { date: h[i].date, sets: en.sets, effort: en.effort };
    }
    return null;
  }
  function setsTxt(sets, type) {
    var by = {};
    sets.forEach(function (x) { var k = x.side || ''; (by[k] = by[k] || []).push(x); });
    return Object.keys(by).map(function (k) {
      var ss = by[k], kg = ss[0].kg, same = ss.every(function (x) { return x.kg === kg; });
      var unit = type === 'seconds' ? ' s' : '';
      var reps = ss.map(function (x) { return (x.val == null ? '?' : x.val) + unit; }).join(', ');
      var load = type === 'weight_reps' && kg != null && kg !== '' ? (same ? kgTxt(kg) + ' kg × ' : '') : '';
      if (!same && type === 'weight_reps') reps = ss.map(function (x) { return kgTxt(x.kg) + ' kg × ' + x.val; }).join(', ');
      return (k ? k + ' ' : '') + load + reps;
    }).join(' · ');
  }

  // Next time's target from the last log and how it felt (double progression):
  // easy -> add a weight step; about right -> a rep more per set until the top of the
  // range, then a step; too hard -> same weight (10% lighter if form slipped).
  // No weight to add (bodyweight, band, time) -> reps or seconds up, then the harder version.
  function suggest(e, it) {
    var rng = parseRange(it.reps) || parseRange(e.dose && e.dose.reps) || { lo: 8, hi: 10 };
    var last = lastFor(e.id), type = logType(e, it), step = num(e.load_step_kg, type === 'weight_reps' ? 2.5 : 0);
    var out = { kg: {}, val: {}, lines: [] };
    if (it.physio || isPhysio(it)) {
      // A physio's exercise: their weights stand. Show and pre-fill last time, never progress it.
      sides(e).forEach(function (s) {
        var ss = last ? last.sets.filter(function (x) { return (x.side || null) === s; }) : [];
        out.kg[s || 'x'] = ss.length ? ss[0].kg : null; out.val[s || 'x'] = ss.length ? ss[0].val : null;
      });
      sides(e).forEach(function (s) {
        var k = s || 'x';
        if (out.kg[k] == null) out.kg[k] = it['kg_' + s] != null ? it['kg_' + s] : (it.kg != null ? it.kg : null);
        if (out.val[k] == null) { var pr = parseRange(it.reps); out.val[k] = pr ? pr.lo : null; }
      });
      if (last) out.lines.push('Last time: ' + setsTxt(last.sets, type) + '.');
      out.lines.push('Sets, reps and weight as your physio set them.');
      return out;
    }
    if (!last) {
      out.first = true;
      sides(e).forEach(function (s) { out.val[s || 'x'] = rng.lo; out.kg[s || 'x'] = null; });
      out.lines.push(e.start || (type === 'weight_reps'
        ? 'First time: start light. Find a weight where the last 2 reps of a set are hard but clean.'
        : 'First time: aim for the bottom of the range with good form.'));
      return out;
    }
    var felt = { easy: 'felt easy', right: 'felt about right', hard: 'felt too hard' }[last.effort] || 'no effort given';
    out.lines.push('Last time: ' + setsTxt(last.sets, type) + ' · ' + felt + '.');
    var topped = true;
    sides(e).forEach(function (s) {
      var k = s || 'x', ss = last.sets.filter(function (x) { return (x.side || null) === s; });
      if (!ss.length) { out.val[k] = rng.lo; out.kg[k] = null; topped = false; return; }
      var lk = Math.max.apply(null, ss.map(function (x) { return num(x.kg, 0); }));
      var lv = Math.min.apply(null, ss.map(function (x) { return num(x.val, 0); }));
      var nk = lk, nv = lv;
      if (lv < rng.hi) topped = false;
      if (last.effort === 'easy') { if (step) { nk = lk + step; nv = rng.lo; } else nv = lv + (type === 'seconds' ? 10 : 2); }
      else if (last.effort === 'right') {
        if (lv >= rng.hi && step) { nk = lk + step; nv = rng.lo; } else nv = lv + (type === 'seconds' ? 5 : 1);
      }
      out.kg[k] = type === 'weight_reps' ? nk : null;
      out.val[k] = step ? Math.max(rng.lo, Math.min(nv, rng.hi)) : Math.max(rng.lo, nv);
    });
    var kgs = Object.keys(out.kg).map(function (k) { return out.kg[k]; });
    if (last.effort === 'hard') out.lines.push('Today: keep the same ' + (type === 'weight_reps' ? 'weight' : 'target') +
      (type === 'weight_reps' ? ' (go about 10% lighter if your form slipped).' : '.'));
    else if (type === 'weight_reps' && kgs[0] != null) {
      var up = Object.keys(out.kg).some(function (k) { var ls = last.sets.filter(function (x) { return (x.side || 'x') === k; }); return ls.length && out.kg[k] > Math.max.apply(null, ls.map(function (x) { return num(x.kg, 0); })); });
      out.lines.push('Today: ' + Object.keys(out.kg).map(function (k) { return (k === 'x' ? '' : k + ' ') + kgTxt(out.kg[k]) + ' kg'; }).join(', ') +
        (up ? ', a step up. Aim for ' + rng.lo + '+ on every set.' : ', aim for ' + out.val[Object.keys(out.val)[0]] + ' on every set.'));
    } else if (!step && topped && e.harder && LIB.exercises[e.harder] && last.effort !== 'hard') {
      out.lines.push('You topped the range: try ' + LIB.exercises[e.harder].name + ' instead, or add a few more.');
    } else out.lines.push('Today: aim for ' + out.val[Object.keys(out.val)[0]] + (type === 'seconds' ? ' s' : '') + ' on every set.');
    return out;
  }

  // ── session state ──
  function build(session) {
    var items = [];
    (session.parts || []).forEach(function (p, pi) {
      (p.items || []).forEach(function (it0, ii) {
        if (!it0.ex || !LIB.exercises[it0.ex]) return;
        var it = Object.assign({}, it0, { physio: !!session.physio || !!it0.physio || isPhysio(it0) });
        var e = ex(it.ex), sug = suggest(e, it), n = typeof it.sets === 'number' ? it.sets : (parseInt(it.sets, 10) || 3);
        var rows = [];
        for (var i = 1; i <= n; i++) sides(e).forEach(function (s) {
          rows.push({ set: i, side: s, kg: sug.kg[s || 'x'], val: sug.val[s || 'x'], done: false });
        });
        var alt = it.alt || Object.keys(e.swaps || {}).map(function (t) { return e.swaps[t]; }).filter(function (a) { return a && a !== it.ex && LIB.exercises[a]; })[0] || null;
        var warm = /warm/i.test(p.title || '');
        items.push({ k: pi + '-' + ii, ex: it.ex, part: pi, warm: warm, sets: n, reps: it.reps, rest_s: it.rest_s != null ? it.rest_s : (e.dose && e.dose.rest_s) || 60,
          note: it.note || '', alt: it.physio ? null : alt, physio: it.physio, log: it.log || null,
          kg: it.kg, kg_L: it.kg_L, kg_R: it.kg_R, rows: rows, effort: null });
      });
    });
    return { sid: session.id, title: session.title, parts: (session.parts || []).map(function (p) {
      return { title: p.title, order: p.order || (/warm|power/i.test(p.title) ? 'suggested' : 'any') }; }),
      items: items, cur: items.length ? items[0].k : null, start: Date.now(), startedAt: localISO(),
      id: uid(), event_id: session.event_id || null, physio: !!session.physio, restUntil: 0 };
  }
  function item(k) { return S.items.filter(function (x) { return x.k === k; })[0]; }
  function doneItem(it) { return it.rows.length && it.rows.every(function (r) { return r.done; }) && (it.effort || it.warm); }
  function persist() { save(LIVE, S); }

  // ── rendering ──
  var CSS = '.spl{position:fixed;inset:0;z-index:200;background:#f2f4f6;overflow:auto;-webkit-overflow-scrolling:touch;font-family:inherit;color:#0b0d10}' +
    '.spl *{box-sizing:border-box}' +
    '.spl .hd{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:10px;padding:10px 14px;padding-top:max(10px,env(safe-area-inset-top));background:rgba(242,244,246,.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid rgba(60,60,67,.1)}' +
    '.spl .hd .t{flex:1;min-width:0}.spl .hd b{display:block;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
    '.spl .hd small{font-size:12px;color:rgba(60,60,67,.6)}' +
    '.spl .ib{border:0;background:rgba(118,118,128,.14);min-width:34px;height:34px;border-radius:17px;font:inherit;font-size:14px;font-weight:600;cursor:pointer;color:inherit;padding:0 12px}' +
    '.spl .fin{background:#10656b;color:#fff}' +
    '.spl .wrap{max-width:640px;margin:0 auto;padding:12px 14px 120px}' +
    '.spl .vw{display:grid;grid-template-columns:1fr 1fr;gap:8px}.spl .vw>div{background:#fff;border-radius:16px;aspect-ratio:1;position:relative}' +
    '.spl .vw span{position:absolute;left:10px;top:8px;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:rgba(60,60,67,.55)}' +
    '.spl .vw i{position:absolute;inset:0}' +
    '.spl .ttl{margin:14px 0 8px;position:relative}.spl h3{font-size:23px;font-weight:700;letter-spacing:-.015em;line-height:1.2;}' +
    '.spl .tg{display:inline-block;margin-top:6px;font-size:13px;font-weight:600;color:#10656b;background:rgba(16,101,107,.09);border-radius:8px;padding:4px 9px}' +
    '.spl .info{background:#fff;border-radius:16px;padding:4px 14px;font-size:14px;line-height:1.45}' +
    '.spl .info p{padding:9px 0;border-bottom:1px solid rgba(60,60,67,.08);color:rgba(30,34,40,.85)}' +
    '.spl .info p b{display:block;font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:rgba(60,60,67,.55);margin-bottom:2px}' +
    '.spl details{padding:9px 0}.spl summary{font-weight:600;cursor:pointer;color:#10656b;list-style:none}.spl summary::-webkit-details-marker{display:none}' +
    '.spl summary:after{content:" ›"}.spl details[open] summary:after{content:" ˅"}.spl details ol{padding-left:18px;margin-top:6px}' +
    '.spl .sets{margin-top:12px;background:#fff;border-radius:16px;padding:6px 8px}' +
    '.spl .shd,.spl .set{display:grid;grid-template-columns:34px 1fr 46px;gap:8px;align-items:center}' +
    '.spl .sets.wr .shd,.spl .sets.wr .set{grid-template-columns:34px 1fr 1fr 46px}' +
    '.spl .shd{padding:4px 2px 2px;font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:rgba(60,60,67,.5);text-align:center}' +
    '.spl .set{padding:5px 2px;border-top:1px solid rgba(60,60,67,.07)}' +
    '.spl .set.dn .sn,.spl .set.dn input{color:#10656b}.spl .sn{text-align:center;font-size:13px;font-weight:700;color:rgba(60,60,67,.6)}' +
    '.spl .stp{display:flex;align-items:center;justify-content:space-between;background:rgba(118,118,128,.09);border-radius:12px;height:44px;min-width:0}' +
    '.spl .stp button{border:0;background:none;width:34px;height:44px;font-size:20px;line-height:1;cursor:pointer;color:rgba(60,60,67,.55);flex:none}' +
    '.spl .stp input{width:100%;min-width:0;border:0;background:none;text-align:center;font:inherit;font-size:18px;font-weight:700;padding:0;color:inherit;font-variant-numeric:tabular-nums}' +
    '.spl .ok{width:44px;height:44px;border-radius:50%;border:2px solid rgba(16,101,107,.35);background:#fff;color:rgba(16,101,107,.6);font-size:19px;cursor:pointer;justify-self:center}' +
    '.spl .set.dn .ok{background:#10656b;border-color:#10656b;color:#fff}' +
    '.spl .acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}' +
    '.spl .chip{border:0;background:rgba(118,118,128,.12);color:inherit;border-radius:999px;padding:8px 13px;font:inherit;font-size:13px;font-weight:600;cursor:pointer}' +
    '.spl .chip.wide{display:block;width:100%;margin-top:10px;padding:11px;background:#fff}' +
    '.spl .eff{margin-top:12px;background:#fff;border-radius:16px;padding:12px}' +
    '.spl .eff p{font-size:15px;font-weight:700;margin-bottom:8px}.spl .eff div{display:flex;gap:6px}' +
    '.spl .eff button{flex:1;border:1.5px solid rgba(60,60,67,.14);background:#fff;border-radius:12px;padding:9px 4px;font:inherit;font-size:13px;font-weight:700;cursor:pointer;color:inherit}' +
    '.spl .eff button small{display:block;font-size:11px;font-weight:500;color:rgba(60,60,67,.6);margin-top:2px;line-height:1.3}' +
    '.spl .eff button.on{border-color:#10656b;background:rgba(16,101,107,.08)}' +
    '.spl h4{font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:rgba(60,60,67,.55);margin:20px 0 6px}' +
    '.spl h5{font-size:13px;font-weight:700;margin:14px 0 6px;color:rgba(30,34,40,.8)}' +
    '.spl .lh{display:flex;align-items:center;justify-content:space-between;margin-top:14px}.spl .lh h4{margin:0}' +
    '.spl .hint{font-size:12px;color:rgba(60,60,67,.6);margin:6px 0}' +
    '.spl .li{display:flex;align-items:center;gap:12px;width:100%;border:0;background:#fff;border-radius:14px;padding:6px 14px 6px 6px;margin-bottom:6px;font:inherit;color:inherit;text-align:left;cursor:pointer}' +
    '.spl .li.cur{box-shadow:inset 0 0 0 2px #10656b}.spl .li .th{width:48px;height:48px;border-radius:10px;background:#f3f5f7;flex:none;overflow:hidden}' +
    '.spl .li .n{font-size:14px;font-weight:600;flex:1}.spl .li .st{font-size:12px;font-weight:700;color:rgba(60,60,67,.5)}' +
    '.spl .li.dn{opacity:.6}.spl .li.dn .st{color:#10656b}' +
    '.spl .pb{height:3px;background:rgba(118,118,128,.15)}.spl .pb i{display:block;height:100%;background:#10656b;transition:width .3s}' +
    '.spl .rest{position:fixed;left:0;right:0;bottom:0;z-index:3;display:flex;align-items:center;gap:12px;padding:12px 18px;padding-bottom:max(12px,env(safe-area-inset-bottom));background:#0b0d10;color:#fff}' +
    '.spl .rest b{font-size:26px;font-variant-numeric:tabular-nums;flex:1}.spl .rest small{display:block;font-size:12px;opacity:.7;font-weight:500}' +
    '.spl .rest[hidden]{display:none}.spl .rest.go{background:#10656b}.spl .rest button{border:0;background:rgba(255,255,255,.16);color:#fff;border-radius:17px;height:34px;padding:0 14px;font:inherit;font-weight:600}' +
    '.spl .sum{background:#fff;border-radius:14px;padding:10px 12px;margin-top:8px}.spl .sum b{font-size:15px}.spl .sum p{font-size:13px;color:rgba(30,34,40,.8);margin-top:3px;line-height:1.4}' +
    '.spl .big{display:block;width:100%;border:0;background:#10656b;color:#fff;border-radius:14px;padding:14px;font:inherit;font-size:16px;font-weight:700;margin-top:16px;cursor:pointer}' +
    '.spl .pv{font-size:12px;color:rgba(60,60,67,.6);margin-top:10px;line-height:1.4}';
  function ensureCss() {
    if (document.getElementById('spl-css')) return;
    var st = document.createElement('style'); st.id = 'spl-css'; st.textContent = CSS; document.head.appendChild(st);
  }

  function render(full) {
    var it = item(S.cur), done = S.items.filter(doneItem).length;
    var bar = root.querySelector('.pb i');
    if (bar && bar.style) bar.style.width = Math.round(100 * done / Math.max(1, S.items.length)) + '%';
    root.querySelector('.hd small').textContent = clock(Date.now() - S.start) + ' · ' + done + ' of ' + S.items.length + ' done';
    if (full) renderViews(it);
    root.querySelector('.body').innerHTML = it ? body(it) : '';
    root.querySelector('.list').innerHTML = list();
    thumbs();
  }

  function renderViews(it) {
    if (anim) { anim.stop(); anim = null; }
    var box = root.querySelector('.vw');
    if (!it || !ANIMS[it.ex]) { box.innerHTML = ''; return; }
    var a = ANIMS[it.ex], views = a.views || ['side', 'front'];
    box.innerHTML = views.map(function () { return '<div><i></i><span></span></div>'; }).join('');
    anim = global.PeakFigure.play([].slice.call(box.querySelectorAll('i')), a);
    [].slice.call(box.querySelectorAll('span')).forEach(function (s, i) { s.textContent = anim.labels[i]; });
  }

  function body(it) {
    var e = ex(it.ex), type = logType(e, it), sug = suggest(e, it);
    var pkg = it.kg_L != null || it.kg_R != null ? 'L ' + kgTxt(it.kg_L) + ' / R ' + kgTxt(it.kg_R) + ' kg' : (it.kg != null ? kgTxt(it.kg) + ' kg' : '');
    var target = it.physio ? (/\d/.test(String(it.sets)) ? it.sets + ' × ' + it.reps + (pkg ? ' · ' + pkg : '') : 'As your physio set it') :
      (it.sets + ' × ' + (it.reps || (e.dose && e.dose.reps) || '') + (e.per_side && !/side/.test(it.reps || '') ? ' each side' : '') +
       (it.rest_s ? ' · rest ' + mins(it.rest_s) : ''));
    var feel = it.warm ? 'Comfortable, no strain: this is to get moving.' : (e.effort || 'Last 2 reps hard but clean: you could do 2 more.');
    var plan = it.warm ? '' : sug.lines.join(' ') + (e.progress && !it.physio ? ' ' + e.progress : '');
    var h = '<div class="ttl"><h3>' + esc(e.name) + '</h3><div class="tg">' + esc(target) + '</div></div>' +
      '<div class="info">' + (it.note ? '<p>' + esc(it.note) + '</p>' : '') +
      '<p><b>Feel</b>' + esc(feel) + '</p>' +
      (plan ? '<p><b>' + (it.physio ? 'Set by your physio' : sug.first ? 'Weight' : 'Build') + '</b>' + esc(plan) + '</p>' : '') +
      (e.cues && e.cues.length ? '<details><summary>How to do it</summary><ol>' + e.cues.map(function (c) { return '<li>' + esc(c) + '</li>'; }).join('') + '</ol></details>' : '') +
      '</div>';
    var wr = type === 'weight_reps', unit = type === 'seconds' ? 'Seconds' : 'Reps';
    var stp = function (f, v, lab) {
      return '<div class="stp"><button data-step="' + f + '" data-d="-1" aria-label="Less ' + lab + '">−</button><input data-f="' + f + '" inputmode="' +
        (f === 'kg' ? 'decimal' : 'numeric') + '" value="' + v + '" placeholder="–" aria-label="' + lab + '"><button data-step="' + f + '" data-d="1" aria-label="More ' + lab + '">+</button></div>';
    };
    h += '<div class="sets' + (wr ? ' wr' : '') + '"><div class="shd"><span>Set</span>' + (wr ? '<span>kg</span>' : '') + '<span>' + unit + '</span><span></span></div>' +
      it.rows.map(function (r, i) {
        return '<div class="set' + (r.done ? ' dn' : '') + '" data-row="' + i + '"><span class="sn">' + (r.side || '') + r.set + '</span>' +
          (wr ? stp('kg', kgTxt(r.kg), 'weight in kg') : '') + stp('val', r.val == null ? '' : r.val, unit.toLowerCase()) +
          '<button class="ok" data-done="' + i + '" aria-label="Set done">✓</button></div>';
      }).join('') + '</div><div class="acts"><button class="chip" data-addset>+ Add a set</button>' +
      (it.alt ? '<button class="chip" data-swap>Kit taken? Do ' + esc(ex(it.alt).name) + '</button>' : '') + '</div>';
    if (it.rows.every(function (r) { return r.done; }) && !it.warm) {
      h += '<div class="eff"><p>How did that feel?</p><div>' + EFFORT.map(function (f) {
        return '<button data-eff="' + f.id + '" class="' + (it.effort === f.id ? 'on' : '') + '">' + f.label + '<small>' + f.sub + '</small></button>';
      }).join('') + '</div></div>';
    }
    return h;
  }

  // The rest of the session: the next few by default, every exercise one tap away (out of
  // order is normal in a busy gym).
  function list() {
    var rows = function (its) {
      return its.map(function (x) {
        var d = x.rows.filter(function (r) { return r.done; }).length;
        return '<button class="li' + (x.k === S.cur ? ' cur' : '') + (doneItem(x) ? ' dn' : '') + '" data-go="' + x.k + '"><div class="th" data-th="' + x.ex + '"></div>' +
          '<span class="n">' + esc(ex(x.ex).name) + '</span><span class="st">' + (doneItem(x) ? '✓' : d + '/' + x.rows.length) + '</span></button>';
      }).join('');
    };
    var next = S.items.filter(function (x) { return !doneItem(x) && x.k !== S.cur; });
    var h = '';
    if (!S.showAll) {
      h += next.length ? '<h4>Up next</h4>' + rows(next.slice(0, 3)) : '';
      h += '<button class="chip wide" data-all>All ' + S.items.length + ' exercises · do them in any order</button>';
      return h;
    }
    var rules = ((LIB.template || {}).order || {}).soft_rules || [];
    h += '<div class="lh"><h4>All exercises</h4><button class="chip" data-all>Show less</button></div>' +
      '<div class="hint">Kit busy? Tap any exercise to do it next.' +
      (rules.length ? ' If you can: ' + esc(rules.map(function (r) { return r.replace(/\.$/, '').replace(/^./, function (c) { return c.toLowerCase(); }); }).join('; ')) + '.' : '') + '</div>';
    S.parts.forEach(function (p, pi) {
      var its = S.items.filter(function (x) { return x.part === pi; });
      if (its.length) h += '<h5>' + esc(p.title) + '</h5>' + rows(its);
    });
    return h;
  }
  function thumbs() {
    root.querySelectorAll('[data-th]').forEach(function (el) {
      var a = ANIMS[el.getAttribute('data-th')];
      if (a) el.innerHTML = global.PeakFigure.drawStatic(a, (a.views || ['side'])[0], global.PeakFigure.compile(a).keys[Math.min(1, global.PeakFigure.compile(a).keys.length - 1)].t0);
    });
  }

  // ── rest timer ──
  function startRest(sec) {
    if (!sec) return;
    S.restUntil = Date.now() + sec * 1000; persist(); restBar();
  }
  function restBar() {
    var bar = root.querySelector('.rest'), left = S.restUntil - Date.now();
    if (!S.restUntil) { bar.hidden = true; return; }
    bar.hidden = false;
    if (left > 0) { bar.classList.remove('go'); bar.innerHTML = '<b>' + clock(left) + '<small>Rest</small></b><button data-rest="15">+15 s</button><button data-rest="skip">Skip</button>'; }
    else if (left > -4000) {
      if (!bar.classList.contains('go')) { bar.classList.add('go'); if (navigator.vibrate) navigator.vibrate([200, 100, 200]); }
      bar.innerHTML = '<b>Go<small>Next set</small></b>';
    } else { S.restUntil = 0; bar.hidden = true; persist(); }
  }

  // ── events ──
  function onClick(ev) {
    var t = ev.target.closest('button');
    if (!t || !root.contains(t)) return;
    var it = item(S.cur);
    if (t.hasAttribute('data-close')) return close(true, true);
    if (t.hasAttribute('data-finish')) return finish();
    if (t.hasAttribute('data-done-all')) return close(false, true);
    if (t.hasAttribute('data-all')) { S.showAll = !S.showAll; persist(); render(false); return; }
    if (t.hasAttribute('data-go')) { S.cur = t.getAttribute('data-go'); persist(); render(true); root.scrollTop = 0; return; }
    if (t.hasAttribute('data-rest')) {
      if (t.getAttribute('data-rest') === 'skip') S.restUntil = 0; else S.restUntil += 15000;
      persist(); restBar(); return;
    }
    if (!it) return;
    if (t.hasAttribute('data-step')) {
      var row = it.rows[+t.closest('.set').getAttribute('data-row')], f = t.getAttribute('data-step'), d = +t.getAttribute('data-d');
      var e = ex(it.ex), step = f === 'kg' ? (num(e.load_step_kg, 0) || 2.5) : (logType(e, it) === 'seconds' ? 5 : 1);
      row[f] = Math.max(0, Math.round((num(row[f], 0) + d * step) * 10) / 10);
      carry(it, +t.closest('.set').getAttribute('data-row'), f);
      persist(); render(false); return;
    }
    if (t.hasAttribute('data-done')) {
      var i = +t.getAttribute('data-done'), r = it.rows[i];
      r.done = !r.done;
      if (r.done) {
        // carry this set's numbers to the next empty set on the same side
        it.rows.slice(i + 1).forEach(function (n) { if (!n.done && n.side === r.side) { if (n.kg == null || n.kg === '') n.kg = r.kg; } });
        var next = it.rows[i + 1];
        var sameSet = next && next.set === r.set; // left done, right side of the same set next
        if (!sameSet && it.rows.some(function (n) { return !n.done; })) startRest(it.rest_s);
        if (it.warm && doneItem(it)) {
          var nw = S.items.filter(function (x) { return !doneItem(x); })[0];
          if (nw) { S.cur = nw.k; persist(); render(true); root.scrollTop = 0; return; }
        }
      }
      persist(); render(false); return;
    }
    if (t.hasAttribute('data-addset')) {
      var lastSet = it.rows.length ? it.rows[it.rows.length - 1].set : 0;
      sides(ex(it.ex)).forEach(function (s) {
        var prev = it.rows.filter(function (r) { return r.side === s; }).pop() || {};
        it.rows.push({ set: lastSet + 1, side: s, kg: prev.kg, val: prev.val, done: false });
      });
      it.effort = null; persist(); render(false); return;
    }
    if (t.hasAttribute('data-eff')) {
      it.effort = t.getAttribute('data-eff'); persist();
      var nxt = S.items.filter(function (x) { return !doneItem(x); })[0];
      if (nxt) { S.cur = nxt.k; persist(); render(true); root.scrollTop = 0; } else render(false);
      return;
    }
    if (t.hasAttribute('data-swap')) {
      var was = it.ex; it.ex = it.alt; it.alt = was;
      var e2 = ex(it.ex), sug = suggest(e2, it);
      it.rows = it.rows.filter(function (r) { return r.done; }).length ? it.rows : (function () {
        var rows = [];
        for (var n = 1; n <= it.sets; n++) sides(e2).forEach(function (s) { rows.push({ set: n, side: s, kg: sug.kg[s || 'x'], val: sug.val[s || 'x'], done: false }); });
        return rows;
      })();
      it.effort = null; persist(); render(true); return;
    }
  }
  // A changed weight (or reps) carries to the later sets on that side that haven't been
  // done or set by hand (Jamie, 17 Oct 2026: "if I change the first set to a different
  // weight assume the second set is the same").
  function carry(it, i, f) {
    var r = it.rows[i];
    r['set_' + f] = true;
    it.rows.forEach(function (n, j) {
      if (j <= i || n.done || n.side !== r.side || n['set_' + f]) return;
      n[f] = r[f];
      var el = root && root.querySelector('.set[data-row="' + j + '"] [data-f="' + f + '"]');
      if (el) el.value = f === 'kg' ? kgTxt(r[f]) : (r[f] == null ? '' : r[f]);
    });
  }
  function onInput(ev) {
    var inp = ev.target; if (!inp.matches || !inp.matches('.set input')) return;
    var it = item(S.cur), i = +inp.closest('.set').getAttribute('data-row'), f = inp.getAttribute('data-f');
    it.rows[i][f] = inp.value === '' ? null : num(inp.value, null);
    carry(it, i, f);
    persist();
  }

  function finish() {
    var entries = S.items.filter(function (x) { return x.rows.some(function (r) { return r.done; }); }).map(function (x) {
      return { ex: x.ex, log: logType(ex(x.ex), x), effort: x.effort,
        sets: x.rows.filter(function (r) { return r.done; }).map(function (r) { return { side: r.side, kg: r.kg, val: r.val }; }) };
    });
    if (entries.length) {
      entries.forEach(function (en) { en.name = ex(en.ex).name || en.ex; });
      var rec = { id: S.id || uid(), date: (S.startedAt || localISO(S.start)).slice(0, 10), title: S.title, session_id: S.sid,
        event_id: S.event_id || null, physio: !!S.physio, started_at: S.startedAt || localISO(S.start), ended_at: localISO(),
        minutes: Math.round((Date.now() - S.start) / 60000), entries: entries };
      if (SAVE) { if (HIST) HIST.push(rec); SAVE(rec); }
      else { var h = history(); h.push(rec); save(KEY_LOG, h); }
    }
    S.finished = true; save(LIVE, null);
    if (anim) { anim.stop(); anim = null; }
    S.restUntil = 0; restBar();
    root.querySelector('.vw').innerHTML = '';
    root.querySelector('.list').innerHTML = '';
    root.querySelector('.hd small').textContent = 'Finished · ' + clock(Date.now() - S.start);
    root.querySelector('.hd [data-finish]').hidden = true;
    root.querySelector('.body').innerHTML = '<h3>Session done</h3>' + (entries.length ? entries.map(function (en) {
      var e = ex(en.ex), nx = suggest(e, S.items.filter(function (x) { return x.ex === en.ex; })[0] || {});
      return '<div class="sum"><b>' + esc(e.name) + '</b><p>' + esc(setsTxt(en.sets, en.log)) +
        (en.effort ? ' · ' + esc({ easy: 'easy', right: 'about right', hard: 'too hard' }[en.effort]) : '') + '</p><p><b style="font-size:13px">Next time:</b> ' +
        esc(nx.lines[nx.lines.length - 1].replace(/^Today: /, '').replace(/^Use the sets.*/, 'as your physio sets it.')) + '</p></div>';
    }).join('') : '<p class="pv">Nothing was logged.</p>') +
      '<p class="pv">' + (SAVE ? 'Saved to your log. It is added to the session your watch recorded, or logged as its own session if the watch doesn\'t send one.'
        : 'Preview: this log is saved on this phone only. In Peak it goes to your training log and into the session your watch recorded.') + '</p>' +
      '<button class="big" data-done-all>Done</button>';
    root.scrollTop = 0;
  }

  // keep = leave the session resumable; ui = closed from the player (tell the page)
  function close(keep, ui) {
    if (anim) { anim.stop(); anim = null; }
    global.clearInterval(tick);
    if (wake) { try { wake.release(); } catch (e) { /* already released */ } wake = null; }
    document.removeEventListener('visibilitychange', rewake);
    if (keep && !S.finished) persist();
    root.remove(); root = null; S = null;
    document.body.style.overflow = '';
    if (ui && onExit) onExit();
  }
  function rewake() {
    if (document.visibilityState === 'visible' && navigator.wakeLock && root) navigator.wakeLock.request('screen').then(function (w) { wake = w; }).catch(function () {});
  }

  // open({lib, anims, session, onExit}) starts (or resumes) a session.
  function open(o) {
    LIB = o.lib; ANIMS = o.anims || {}; onExit = o.onExit;
    HIST = o.history || null; SAVE = o.onFinish || null; LIVE = KEY_LIVE + (o.key ? '.' + o.key : '');
    ((LIB.effort_guide || {}).answers || []).forEach(function (a) {
      EFFORT.forEach(function (f) { if (f.id === a.id && a.label) f.sub = a.label; });
    });
    var live = load(LIVE, null);
    S = live && o.session && live.sid === o.session.id ? live : (o.session ? build(o.session) : live);
    if (!S || !S.items) return false;
    persist(); ensureCss();
    root = document.createElement('div');
    root.className = 'spl';
    root.innerHTML = '<div class="hd"><button class="ib" data-close aria-label="Pause and close">×</button><div class="t"><b>' + esc(S.title) +
      '</b><small></small></div><button class="ib fin" data-finish>Finish</button></div><div class="pb"><i></i></div>' +
      '<div class="wrap"><div class="vw"></div><div class="body"></div><div class="list"></div></div><div class="rest" hidden></div>';
    document.body.appendChild(root);
    document.body.style.overflow = 'hidden';
    root.addEventListener('click', onClick);
    root.addEventListener('input', onInput);
    render(true); restBar();
    tick = global.setInterval(function () {
      if (!root || S.finished) return;
      root.querySelector('.hd small').textContent = clock(Date.now() - S.start) + ' · ' + S.items.filter(doneItem).length + ' of ' + S.items.length + ' done';
      restBar();
    }, 500);
    document.addEventListener('visibilitychange', rewake); rewake();
    return true;
  }

  global.PeakStrengthPlayer = {
    open: open,
    close: function () { if (root) close(true, false); },
    isOpen: function () { return !!root; },
    live: function (key) {
      var l = load(KEY_LIVE + (key ? '.' + key : ''), null);
      return l && l.items ? { sid: l.sid, title: l.title, sets: l.items.reduce(function (n, x) { return n + x.rows.filter(function (r) { return r.done; }).length; }, 0) } : null;
    },
    // drop the session in progress without saving it (Jamie, 17 Oct 2026: a button to cancel it)
    discard: function (key) { save(KEY_LIVE + (key ? '.' + key : ''), null); },
    lastFor: lastFor, history: history, suggest: suggest, setsTxt: setsTxt
  };
})(typeof window !== 'undefined' ? window : this);
