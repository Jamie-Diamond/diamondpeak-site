// Strength-player test: the "next time" suggestion built from the log (double
// progression), and a physio's numbers never being moved.
//
//   node coach/test/strength-player-test.js
const path = require('path').resolve(__dirname, '../../') + '/';
const store = {};
global.window = global;
global.localStorage = { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = String(v); } };
require(path + 'coach/strength-player.js');
const lib = require(path + 'ClaudeCoach/config/exercise-library.json');
const P = global.PeakStrengthPlayer, fails = [];

// suggest() reads the library through open(); give it the library without a DOM.
function withLog(entries) { store['peakStrength.log.v1'] = JSON.stringify([{ date: '2026-10-17', entries }]); }
function sug(id, it) { const e = Object.assign({ id }, lib.exercises[id]); return P.suggest(e, it); }
function eq(name, got, want) { if (JSON.stringify(got) !== JSON.stringify(want)) fails.push(name + ': got ' + JSON.stringify(got) + ', want ' + JSON.stringify(want)); }

// open() is what hands the player its library; a stub document is enough to skip the UI.
global.document = { getElementById: () => null, createElement: () => ({ style: {}, addEventListener() {}, querySelector: () => ({ textContent: '', innerHTML: '', querySelectorAll: () => [], classList: { add() {}, remove() {}, contains() { return false; } } }), querySelectorAll: () => [] }),
  head: { appendChild() {} }, body: { appendChild() {}, style: {} }, addEventListener() {}, removeEventListener() {}, visibilityState: 'hidden' };
global.setInterval = () => 0; global.clearInterval = () => {};
global.requestAnimationFrame = () => 0; global.cancelAnimationFrame = () => {};
global.PeakFigure = { play: () => ({ stop() {}, labels: [] }), drawStatic: () => '', compile: () => ({ keys: [{ t0: 0 }] }) };
P.open({ lib, anims: {}, session: { id: 't', title: 't', parts: [] } });

const squat = { sets: 3, reps: '5' };
withLog([]);
eq('first time has no weight', sug('back_squat', squat).kg, { x: null });

withLog([{ ex: 'back_squat', effort: 'right', sets: [{ side: null, kg: 60, val: 5 }, { side: null, kg: 60, val: 5 }, { side: null, kg: 60, val: 4 }] }]);
eq('right, one set short: same weight', sug('back_squat', squat).kg, { x: 60 });

withLog([{ ex: 'back_squat', effort: 'right', sets: [{ side: null, kg: 60, val: 5 }, { side: null, kg: 60, val: 5 }, { side: null, kg: 60, val: 5 }] }]);
eq('right, top of range: a step up', sug('back_squat', squat).kg, { x: 60 + lib.exercises.back_squat.load_step_kg });

withLog([{ ex: 'back_squat', effort: 'easy', sets: [{ side: null, kg: 60, val: 4 }] }]);
eq('easy: a step up', sug('back_squat', squat).kg, { x: 60 + lib.exercises.back_squat.load_step_kg });

withLog([{ ex: 'back_squat', effort: 'hard', sets: [{ side: null, kg: 60, val: 5 }] }]);
eq('hard: same weight', sug('back_squat', squat).kg, { x: 60 });

withLog([{ ex: 'single_leg_hamstring_curl', effort: 'easy', sets: [{ side: 'L', kg: 27.5, val: 10 }, { side: 'R', kg: 32.5, val: 10 }] }]);
eq('physio: each side keeps its own weight, never progressed',
  sug('single_leg_hamstring_curl', { sets: 'set by physio', reps: 'set by physio' }).kg, { L: 27.5, R: 32.5 });

if (fails.length) { console.log('FAIL\n  ' + fails.join('\n  ')); process.exit(1); }
console.log('ok: next-time suggestions follow the log; physio weights are never moved');
