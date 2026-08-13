// The Fitness card's TSS/Duration toggle: is the control offered only when the data
// carries hours, does the sport chip survive a switch of metric, do the sport chips
// filter the right series, and do the season overlays land on race day.
//
// WHY THIS EXISTS. Duration reads durationBySport, a key refresh-site-data.py only
// started publishing on 13 Aug 2026. Two failure modes are worth a test:
//
//   * a published file WITHOUT the key (every file in ClaudeCoach/public/ at the time
//     of writing) must render with the toggle simply absent and CTL showing, not throw
//     and not draw an empty chart under a lit chip. render-test.js covers "does not
//     throw"; this covers "the control is actually gone";
//   * the previous-season overlay is aligned by DAYS TO RACE, which is only correct if
//     each season's series is mapped against ITS OWN race date. Getting that wrong puts
//     last season's taper over this season's mid-build and the comparison is worthless,
//     which is the exact bug the comment in chartFitness records. Asserted here by
//     checking last season's final point sits at x = 0.
//
// Chart.js is replaced by a stub that records the config it was handed, so the canvas
// paths are exercised without a browser. Run:
//
//   node coach/test/duration-chart-test.js
const fs = require('fs');
const path = require('path').resolve(__dirname, '../../') + '/';

function makeEl() {
  return {
    innerHTML: '', textContent: '', dataset: {}, style: {},
    firstChild: {}, lastChild: {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    setAttribute(){}, getAttribute(){ return null; }, focus(){}, addEventListener(){},
    querySelector: () => makeEl(), querySelectorAll: () => [],
    closest: () => null, appendChild(){},
  };
}
const registry = {};
global.document = {
  querySelector: (s) => (registry[s] = registry[s] || makeEl()),
  getElementById: (s) => (registry['#' + s] = registry['#' + s] || makeEl()),
  createElement: () => makeEl(),
  addEventListener(){}, readyState: 'complete', body: makeEl(), activeElement: null,
};
global.window = { console };
global.navigator = { onLine: true };
global.location = { hash: '', replace(){} };
global.localStorage = { getItem: () => null, setItem(){}, removeItem(){} };
global.matchMedia = () => ({ matches: false });
global.addEventListener = () => {};
global.scrollTo = () => {};
global.fetch = () => new Promise(() => {});

// Records what the app asked for and satisfies what attachReadout touches.
let lastChart = null;
global.Chart = function (el, cfg) {
  this.canvas = { addEventListener(){} };
  this.options = cfg.options;
  this.data = cfg.data;
  this.destroy = function () {};
  this.resetZoom = function () {};
  this.getElementsAtEventForMode = function () { return []; };
  this.getDatasetMeta = function () { return { data: [] }; };
  lastChart = this;
};
// init() sets Chart.defaults.font.family, and registers the zoom plugin if present.
global.Chart.defaults = { font: {} };
global.Chart.register = function () {};

const src = fs.readFileSync(path + 'coach/app.js', 'utf8');
const hooked = src.replace(
  '  if (document.readyState === \'loading\') {',
  '  global.__peak = { state: state, renderTrends: renderTrends, drawTrend: drawTrend,\n' +
  '                    trendTabs: trendTabs };\n' +
  '  if (document.readyState === \'loading\') {'
);
eval(hooked);
const peak = global.__peak;

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log('ok   ' + name); return; }
  failures++;
  console.log('FAIL ' + name + (detail ? '  (' + detail + ')' : ''));
}

const real = JSON.parse(fs.readFileSync(
  path + 'ClaudeCoach/public/training-data-jamie.json', 'utf8'));

/* ── 1. old data: no durationBySport at all ──────────────────────────────── */
delete real.durationBySport;   // defensive: the fixture below must be the only source
peak.state.slug = 'jamie';
peak.state.data = real;
peak.state.trend = 'fit';
peak.state.fitMetric = 'dur';  // as if left selected from a previous session
peak.renderTrends();
const oldHtml = registry['#v-trends'].innerHTML;

check('old data offers no metric toggle', oldHtml.indexOf('id="fitMetric"') < 0);
check('old data falls back to the TSS metric', peak.state.fitMetric === 'tss',
  'fitMetric=' + peak.state.fitMetric);
peak.drawTrend();
check('old data still draws CTL',
  lastChart.options.scales.y.title.text === 'CTL',
  lastChart.options.scales.y.title.text);
check('there is no separate Hours tab in the picker',
  !peak.trendTabs().some((t) => t.id === 'dur'),
  peak.trendTabs().map((t) => t.id).join(','));

/* ── 2. fixture with durationBySport ─────────────────────────────────────── */
// Real profile, so the race dates driving the alignment are the real ones. Flat series
// per sport: a constant makes "which series did the chip select" unambiguous.
function flat(from, to, hpw) {
  const out = [];
  const d = new Date(from + 'T12:00:00'), end = new Date(to + 'T12:00:00');
  while (d <= end) {
    out.push([d.toISOString().slice(0, 10), hpw]);
    d.setDate(d.getDate() + 1);
  }
  return out;
}
const LEVELS = { Total: 8, Ride: 4, Run: 2.5, Swim: 1.5 };
function season(from, to, scale) {
  const o = {};
  Object.keys(LEVELS).forEach((s) => { o[s] = flat(from, to, LEVELS[s] * scale); });
  return o;
}
const today = new Date();
const todayISO = today.getFullYear() + '-' +
  String(today.getMonth() + 1).padStart(2, '0') + '-' +
  String(today.getDate()).padStart(2, '0');

const fx = JSON.parse(JSON.stringify(real));
fx.durationBySport = {
  current: season('2026-01-01', todayISO, 1),
  // Ends ON last season's race day (profile.prev_race.date = 2025-09-20), which is what
  // makes the x = 0 assertion below a real alignment check.
  prev: season('2025-01-01', fx.profile.prev_race.date, 0.9),
  prev2: season('2023-01-01', fx.profile.prev2_race_date, 0.8),
};
peak.state.data = fx;
peak.state.trend = 'fit';
peak.state.fitMetric = 'dur';
peak.state.fitSport = 'all';

peak.renderTrends();
const html = registry['#v-trends'].innerHTML;
check('the metric toggle is offered', html.indexOf('id="fitMetric"') >= 0);
check('the toggle offers exactly TSS and Duration',
  html.indexOf('>TSS<') >= 0 && html.indexOf('>Duration<') >= 0);
// The metric sits above the sport chips, which is where Jamie asked for it: "under
// fitness and above the sports".
check('the metric sits above the sport chips',
  html.indexOf('id="fitMetric"') < html.indexOf('id="fitSport"'));
check('both controls sit above the chart',
  html.indexOf('id="fitSport"') < html.indexOf('id="c-now"'));
check('Duration metric stays selected', peak.state.fitMetric === 'dur');
// Scoped to the chip bar: 'Total' legitimately appears further down, in the extras
// card, and a whole-page search would pass on that instead of on the chips.
const chipBar = (html.match(/<div class="seg sub" id="fitSport">([\s\S]*?)<\/div>/) ||
                 [, ''])[1];
check('chips offer All + the three sports',
  ['All', 'Ride', 'Run', 'Swim'].every((s) => chipBar.indexOf('>' + s + '<') >= 0),
  chipBar);
check('Total is not offered as a chip', chipBar && chipBar.indexOf('>Total<') < 0);
check('extras card lists Total and the current level',
  html.indexOf('Hours by sport') >= 0 && html.indexOf('>Total<') >= 0 &&
  html.indexOf('8.0h') >= 0);

peak.drawTrend();
const byLabel = {};
lastChart.data.datasets.forEach((s) => { byLabel[s.label] = s; });
check('three seasons drawn', lastChart.data.datasets.length === 3,
  Object.keys(byLabel).join(','));
check('y axis is hours per week',
  lastChart.options.scales.y.title.text === 'HOURS / WEEK',
  lastChart.options.scales.y.title.text);
check('y axis starts at zero', lastChart.options.scales.y.beginAtZero === true);
check('race day marked', (lastChart.options.plugins.vlines.lines || [])
  .some((l) => l.x === 0));

const cur = byLabel['This season'].data;
check('All chip reads the Total series', cur[cur.length - 1].y === 8,
  'y=' + cur[cur.length - 1].y);
check('this season stops before race day', cur[cur.length - 1].x < 0,
  'x=' + cur[cur.length - 1].x);
const prev = byLabel['Last season'].data;
check('last season is aligned on its own race day',
  prev[prev.length - 1].x === 0, 'x=' + prev[prev.length - 1].x);
check('last season is muted and dashed',
  byLabel['Last season'].borderDash && byLabel['Last season'].borderWidth === 1);

/* ── 3. the chip actually filters, and survives a switch of metric ───────── */
peak.state.fitSport = 'Swim';
peak.renderTrends();
peak.drawTrend();
const swim = lastChart.data.datasets.find((s) => s.label === 'This season').data;
check('Swim chip reads the Swim series', swim[swim.length - 1].y === 1.5,
  'y=' + swim[swim.length - 1].y);
check('chip is named in the card title',
  registry['#v-trends'].innerHTML.indexOf('· Swim') >= 0);

// Switching metric with a sport selected: the sport must come with it, in both
// directions. This is the "remembering the selection across toggles" requirement, and
// the reason both charts read one state.fitSport rather than a field each.
peak.state.fitMetric = 'tss';
peak.renderTrends();
peak.drawTrend();
check('the sport chip survives the switch to TSS',
  peak.state.fitSport === 'Swim' &&
  lastChart.options.scales.y.title.text === 'CTL',
  'sport=' + peak.state.fitSport);
const swimCtl = lastChart.data.datasets.find((s) => s.label === 'This season').data;
check('TSS with a sport chip reads the per-sport CTL series',
  swimCtl.length > 0 && swimCtl[swimCtl.length - 1].y !==
    fx.fitnessThis[fx.fitnessThis.length - 1][1]);
peak.state.fitMetric = 'dur';
peak.renderTrends();
peak.drawTrend();
check('and survives the switch back to Duration',
  peak.state.fitSport === 'Swim' &&
  lastChart.data.datasets.find((s) => s.label === 'This season')
    .data.slice(-1)[0].y === 1.5);

/* ── 4. the Fitness chart still works ────────────────────────────────────── */
// seasonRaces(), relToRace() and raceAxisLabel() were lifted OUT of chartFitness so the
// two renderings share one alignment rule. render-test.js leaves Chart undefined, so it
// never runs either chart function: without this block the refactor of the chart Jamie
// reads daily would be covered by nothing but node --check.
peak.state.data = fx;
peak.state.trend = 'fit';
peak.state.fitMetric = 'tss';
peak.state.fitSport = 'all';
peak.renderTrends();
check('Fitness chips still render',
  registry['#v-trends'].innerHTML.indexOf('id="fitSport"') >= 0);
check('Fitness extras still show the CTL split, not the hours one',
  registry['#v-trends'].innerHTML.indexOf('Fitness by sport') >= 0 &&
  registry['#v-trends'].innerHTML.indexOf('Hours by sport') < 0);
peak.drawTrend();
const fitBy = {};
lastChart.data.datasets.forEach((s) => { fitBy[s.label] = s; });
check('Fitness y axis is still CTL',
  lastChart.options.scales.y.title.text === 'CTL',
  lastChart.options.scales.y.title.text);
check('Fitness still draws this season and both overlays',
  ['This season', 'Last season'].every((l) => fitBy[l] && fitBy[l].data.length),
  Object.keys(fitBy).join(','));
const fitPrev = fitBy['Last season'].data;
check('Fitness last season is still aligned on its own race day',
  fitPrev[fitPrev.length - 1].x === 0, 'x=' + fitPrev[fitPrev.length - 1].x);
check('Fitness still carries the blueprint-only layers the Hours chart omits',
  ['Target band', '_band-lo', 'Planned', 'Milestones'].every((l) => fitBy[l]),
  Object.keys(fitBy).join(','));

/* ── 5. a season with no race date drops rather than mis-aligns ──────────── */
const fx2 = JSON.parse(JSON.stringify(fx));
delete fx2.profile.prev_race;
delete fx2.profile.prev_race_date;
peak.state.data = fx2;
peak.state.fitMetric = 'dur';
peak.state.fitSport = 'all';
peak.renderTrends();
peak.drawTrend();
check('season without a race date is dropped',
  !lastChart.data.datasets.some((s) => s.label === 'Last season'),
  lastChart.data.datasets.map((s) => s.label).join(','));

console.log(failures ? '\n' + failures + ' check(s) failed' : '\nall checks pass');
process.exit(failures ? 1 : 0);
