// Render-test: drive coach/app.js's real render paths against every athlete's real
// published data, with a DOM stub just rich enough for the string-building code.
//
// WHY THIS EXISTS. Calum and Kathryn both carry `prev_race: {}` in their profile - an
// empty object, which is TRUTHY - so `if (pr) ... pr.date.slice(0,4)` threw. renderAll
// ran the five views as one statement, so renderGoals throwing meant renderSettings
// never ran; Settings was blank, and Settings was the only route to switching athlete.
// The user was stranded on another athlete's profile with no way back, and the app
// looked fine apart from a missing countdown.
//
// Two things stop that recurring: renderAll isolates each view, and this test fails if
// any athlete's real data cannot be rendered. Run it before deploying app.js:
//
//   node coach/test/render-test.js
//
// It is deliberately dependency-free (no jsdom) so it runs anywhere node does. Chart.js
// is left undefined, so this covers the DOM/string paths, not the canvas ones.
const fs = require('fs');
// Repo root, derived so the test is not tied to one machine.
const path = require('path').resolve(__dirname, '../../') + '/';

function makeEl() {
  const el = {
    innerHTML: '', textContent: '', dataset: {}, style: {}, firstChild: {}, lastChild: {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){return false} },
    setAttribute(){}, getAttribute(){return null}, focus(){}, addEventListener(){},
    querySelector: () => makeEl(), querySelectorAll: () => [],
    closest: () => null, appendChild(){},
  };
  return el;
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
global.Chart = undefined;   // charts are skipped; this tests the DOM/string paths

const src = fs.readFileSync(path + 'coach/app.js', 'utf8');
// Expose the internals the harness needs to drive, without editing the source.
const hooked = src.replace(
  '  if (document.readyState === \'loading\') {',
  '  global.__peak = { state: state, renderAll: renderAll, TABS: TABS, openDetail: openDetail };\n' +
  '  if (document.readyState === \'loading\') {'
);
eval(hooked);

// The athlete's FULL data, found the way api/server.py finds it (private since 29 Sep).
function athleteData(slug) {
  const cands = ['ClaudeCoach/athletes/' + slug + '/training-data.json',
                 'ClaudeCoach/training-data-' + slug + '.json',
                 'ClaudeCoach/public/training-data-' + slug + '.json'];
  const hit = cands.find((c) => fs.existsSync(path + c));
  if (!hit) throw new Error('no training data for ' + slug + ' (run on the VM)');
  return JSON.parse(fs.readFileSync(path + hit, 'utf8'));
}

let failures = 0;
for (const slug of ['jamie', 'kathryn', 'calum']) {
  const data = athleteData(slug);
  global.__peak.state.data = data;
  global.__peak.state.slug = slug;

  // Catch a per-view failure by watching what renderAll writes into a view.
  const errors = [];
  const origError = console.error;
  console.error = (...a) => { errors.push(a.join(' ')); };
  try {
    global.__peak.renderAll();
  } catch (e) {
    errors.push('renderAll threw outright: ' + e.message);
  }
  console.error = origError;

  const cd = registry['#cd'] ? registry['#cd'].innerHTML : '';
  if (errors.length) {
    failures++;
    console.log(`FAIL ${slug}:`);
    errors.forEach((e) => console.log('   ' + e.split('\n')[0]));
  } else {
    console.log(`ok   ${slug}  countdown="${cd.replace(/<[^>]+>/g, '')}"`);
  }
}
// A goal without a race (lib/goals.py, 1 Oct 2026): no current athlete has one, so the
// goal card is driven from Jamie's data with the season swapped for a goal-only one.
{
  const data = athleteData('jamie');
  const goals = [
    { type: 'ftp', label: 'Raise my FTP', started: '2026-10-05', notStarted: false, block: 1, week: 3,
      blockWeeks: 6, kind: 'easy', nextTest: { name: 'FTP test', week_start: '2026-11-09' }, band: null,
      progress: [{ label: 'FTP', start: 231, now: 238, unit: 'W', better: true },
                 { label: 'Fitness', start: 47, now: 49.2, unit: '', better: true }], waiting: false },
    { type: 'maintain', label: 'Keep my fitness', started: '2026-10-12', notStarted: true, block: 1, week: 1,
      blockWeeks: 6, kind: 'load', nextTest: null, band: [44, 50], progress: [], waiting: false },
  ];
  for (const g of goals) {
    data.season = { aRace: null, races: [], phases: [], trackingOnly: false, goal: g };
    global.__peak.state.data = data;
    const errors = [];
    const origError = console.error;
    console.error = (...a) => { errors.push(a.join(' ')); };
    try { global.__peak.renderAll(); } catch (e) { errors.push('renderAll threw: ' + e.message); }
    console.error = origError;
    const html = (registry['#v-goals'] || {}).innerHTML || '';
    const want = [g.label, g.notStarted ? 'Block 1 starts' : 'Block 1 \u00b7 week 3',
                  g.nextTest ? 'Next test: FTP test' : 'Fitness held between 44 and 50'];
    const missing = want.filter((w) => !html.includes(w));
    if (errors.length || missing.length) {
      failures++;
      console.log(`FAIL goal ${g.type}: ` + errors.concat(missing.map((m) => 'missing "' + m + '"')).join('; '));
    } else {
      console.log(`ok   goal ${g.type}`);
    }
  }
}

// Ride indoors (lib/indoor.py, 1 Oct 2026): the button shows on a planned ride under 2 h
// from today on, not on a long ride, and says "outdoors" once a ride is indoors.
{
  const data = athleteData('jamie');
  const tom = new Date(Date.now() + 864e5).toISOString().slice(0, 10);
  data.weekCalendar = [
    { date: tom, sport: 'Ride', name: 'Sweetspot 3x10', duration_min: 60, tss: 60, status: 'planned', event_id: 'e1' },
    { date: tom, sport: 'Run', name: 'Easy run', duration_min: 40, tss: 30, status: 'planned', event_id: 'e2' },
  ];
  data.recent = [];
  global.__peak.state.data = data;
  global.__peak.state.me = { email: 'x', coach: true };
  const drawerHTML = (sport, name) => {
    global.__peak.openDetail(tom, sport, name);
    return (registry['#drawer'] || {}).innerHTML || '';
  };
  const cases = [
    ['short ride offers indoors', drawerHTML('Ride', 'Sweetspot 3x10').includes('Ride indoors')],
    ['a run has no button', !drawerHTML('Run', 'Easy run').includes('Ride indoors')],
  ];
  data.weekCalendar[0].indoor = true;
  cases.push(['an indoor ride offers outdoors', drawerHTML('Ride', 'Sweetspot 3x10').includes('Ride outdoors instead')]);
  data.weekCalendar[0].indoor = false;
  data.weekCalendar[0].duration_min = 150;
  cases.push(['a 2.5 h ride stays outside', !drawerHTML('Ride', 'Sweetspot 3x10').includes('Ride indoors')]);
  for (const [label, ok] of cases) {
    if (!ok) failures++;
    console.log((ok ? 'ok   ' : 'FAIL ') + 'indoor: ' + label);
  }
}

console.log(failures ? `\n${failures} athlete(s) fail to render` : '\nall three render clean');
process.exit(failures ? 1 : 0);
