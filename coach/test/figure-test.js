// Figure-test: every exercise in the strength library has an animation, and every
// animation draws cleanly through its whole loop - no NaN in the SVG, no hand or foot
// asked to reach somewhere the limb can't, nothing sunk through the floor. A pose
// that looks fine at its keys can still break between them, so 200 frames are checked.
//
//   node coach/test/figure-test.js
//
// Dependency-free, like render-test.js.
const path = require('path').resolve(__dirname, '../../') + '/';
global.window = global;
require(path + 'coach/figure.js');
require(path + 'coach/exercise-anims.js');
const lib = require(path + 'ClaudeCoach/config/exercise-library.json');

const A = global.PeakAnims, F = global.PeakFigure, fails = [];
const ids = Object.keys(lib.exercises);
ids.filter((id) => !A[id]).forEach((id) => fails.push(id + ': no animation'));
Object.keys(A).filter((id) => !lib.exercises[id]).forEach((id) => fails.push(id + ': animation for an exercise not in the library'));

ids.filter((id) => A[id]).forEach((id) => {
  const an = A[id], total = F.compile(an).total, views = an.views || ['side', 'front'];
  if (views.length !== 2) fails.push(id + ': needs exactly two views, has ' + views.length);
  for (let i = 0; i < 200; i++) {
    const t = total * i / 200, J = F.solve(F.poseAt(an, t));
    if (J.warn.length) { fails.push(id + ': limb cannot reach its target at t=' + t.toFixed(2)); break; }
    const low = ['heelL', 'heelR', 'toeL', 'toeR', 'handL', 'handR', 'head', 'root'].filter((k) => J[k][1] < -0.012);
    if (low.length) { fails.push(id + ': ' + low.join(',') + ' below the floor at t=' + t.toFixed(2)); break; }
    if (i % 20 === 0 && views.some((v) => /NaN/.test(F.drawStatic(an, v, t)))) { fails.push(id + ': NaN at t=' + t.toFixed(2)); break; }
  }
});

if (fails.length) { console.log('FAIL\n  ' + fails.join('\n  ')); process.exit(1); }
console.log('ok: ' + ids.length + ' exercises animate cleanly from two angles');
