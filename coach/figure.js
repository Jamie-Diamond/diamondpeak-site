/* Peak figure (17 Oct 2026): a flat-illustrated athlete drawn in code from key
   positions. Every exercise animation shares one look, the form is exactly what we
   set, it works offline and weighs nothing. Jamie chose drawn-in-code over bought or
   AI video: "Drawn in code is fine. If happy can make video later." Each exercise is
   shown from its two most useful angles (usually side and front).

   WORLD: standing height = 1. y is up, the floor is y = 0, the athlete faces +z and
   +x is the athlete's LEFT. Standing tall, the pelvis centre (midpoint of the hip
   joints) is at [0, 0.53, 0] and the ankles are 0.04 above the floor.

   POSE FORMAT (all angles in degrees, every field optional):
     root:   [x, y, z]            pelvis centre
     pelvis: {pitch, yaw, roll}   pitch+ tips the trunk forward (90 = face down, -90 =
                                  lying on the back, head toward -z); roll+ tips to the
                                  athlete's right (90 = lying on the right side); yaw+
                                  turns to the athlete's left
     spine:  {flex, side, twist}  relative to the pelvis, spread over lower and upper
                                  back. flex+ rounds forward (- arches), side+ bends to
                                  the left, twist+ turns the chest to the left
     head:   {flex, turn}         flex+ = chin down
     shoulders: {shrug, reach}    shoulder-blade lift / forward reach, body units (~0.02)
     legL / legR / legs, armL / armR / arms  - `legs`/`arms` set both sides; give the
                                  LEFT side's numbers and the right is mirrored (x
                                  negated). legL/legR then override per side.
       Leg FK: {elev, az, twist, knee, point, roll}
               elev = thigh angle from hanging straight down (90 = thigh forward and
               level, negative = behind); az = which way it swings (0 forward, 90 out to
               the side, 180 back); twist+ turns the knee out; knee+ bends; point+
               points the toes (- pulls them up); roll+ turns the sole outward
       Leg IK: {ankle:T | ball:T, pole:[x,y,z], foot:{pitch, yaw, roll}}
               the foot is placed in the world (flat unless foot.pitch; pitch+ = heel
               up). `ball` pins the ball of the foot instead of the ankle (heel raises).
               pole = direction the knee points (default: body forward).
       Arm FK: {elev, az, twist, elbow}  elev 0 = hanging, 90 = forward and level,
               180 = overhead; az 90 = out to the side; twist+ = rotate out; elbow+ bends
       Arm IK: {hand:T, pole:[x,y,z]}  hand = wrist position; pole = direction the
               elbow points (default: back and slightly out)
       T (an IK target) = a world point [x,y,z], or {rel:'pelvis'|'chest', at:[x,y,z]}
               riding on the body (pelvis: from the pelvis centre in the pelvis frame;
               chest: from the base of the neck in the upper-back frame) - use it for
               hands on a bar across the hips or a weight held at the chest
     ground: ['ballL','ballR',...]  shift the body so the lowest listed point sits on
                                  the floor (groundY to sit on a box instead)
     A key inherits everything from the key before; set a field to null to remove it.
     A limb must use the same mode (FK or IK) in every key of one animation.

   ANIMATION FORMAT (coach/exercise-anims.js):
     { views: ['side', 'front'] | [{yaw, pitch, label}],   // the two angles shown
       props: [...],                                       // see PROPS below
       keys: [{pose, hold, move}, ...] }                   // hold at a key `hold` s,
                                                           // then `move` s to the next;
                                                           // the last key moves back
                                                           // to the first (loop)
   VIEWS: 'side' (camera on the athlete's right, facing screen-right), 'front', 'back',
     'left', 'above' (high side view, for floor work), 'three' (front-right, raised), or
     {yaw, pitch, label}: yaw 0 = front, 90 = athlete's right side; pitch = degrees up.

   PROPS (anchors: 'handL' 'handR' 'hands' 'kneeL' 'kneeR' 'knees' 'ankleL' 'ankleR'
   'footL' 'footR' 'elbowL' 'elbowR' 'back' (across the upper back) 'hips' (across the
   hip crease) 'chest', or a world point [x,y,z]):
     {type:'mat', at:[x,0,z], size:[width, length]}
     {type:'box', min:[x,y,z], max:[x,y,z]}        step, plyo box, seat
     {type:'bench', min, max}                       padded bench on two legs
     {type:'wall', min, max}                        a thin box drawn as a wall
     {type:'dumbbell', hand:'L'|'R'|'both', axis:'forward'|'lateral'|'vertical'|[x,y,z],
       at: anchor (instead of hand, e.g. 'hands' for one held in both hands)}
     {type:'kettlebell', hand:'L'|'R'|'both', hang:'down'|'arm'}   'arm' = bell
       continues along the forearm (swings)
     {type:'barbell', on:'hands'|'back'|'hips', plates:false (a lat or cable bar),
       half: half-length, inner: half-gap between plates}
     {type:'band', from: anchor, to: anchor}        resistance band (orange)
     {type:'bar', from:[x,y,z], to:[x,y,z]}         fixed bar (pull-up bar)
     {type:'cable', from: anchor, to:[x,y,z]}       cable to a pulley
   Boxes, benches and walls are drawn behind the figure; give a box `depthBias: -1`
   to draw it in front. `fit: [[x,y,z],...]` on any prop adds points to the framing. */
(function (global) {
  'use strict';

  // ── vectors and 3x3 matrices (row-major) ─────────────────────────────────
  function add(a, b) { return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]; }
  function sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
  function mul(a, s) { return [a[0] * s, a[1] * s, a[2] * s]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function len(a) { return Math.sqrt(dot(a, a)); }
  function norm(a) { var l = len(a) || 1; return mul(a, 1 / l); }
  function lerp(a, b, f) { return a + (b - a) * f; }
  var D2R = Math.PI / 180;
  function mm(A, B) {
    var o = [];
    for (var i = 0; i < 3; i++) for (var j = 0; j < 3; j++)
      o.push(A[i * 3] * B[j] + A[i * 3 + 1] * B[3 + j] + A[i * 3 + 2] * B[6 + j]);
    return o;
  }
  function mv(A, v) {
    return [A[0] * v[0] + A[1] * v[1] + A[2] * v[2], A[3] * v[0] + A[4] * v[1] + A[5] * v[2],
            A[6] * v[0] + A[7] * v[1] + A[8] * v[2]];
  }
  function rx(d) { var c = Math.cos(d * D2R), s = Math.sin(d * D2R); return [1, 0, 0, 0, c, -s, 0, s, c]; }
  function ry(d) { var c = Math.cos(d * D2R), s = Math.sin(d * D2R); return [c, 0, s, 0, 1, 0, -s, 0, c]; }
  function rz(d) { var c = Math.cos(d * D2R), s = Math.sin(d * D2R); return [c, -s, 0, s, c, 0, 0, 0, 1]; }
  var I3 = [1, 0, 0, 0, 1, 0, 0, 0, 1];

  // ── body (units of standing height) ──────────────────────────────────────
  var B = {
    hipW: 0.085, thigh: 0.245, shank: 0.245, ankleH: 0.04, heel: 0.045, ball: 0.10, toe: 0.13,
    lumbar: 0.15, thorax: 0.15, shW: 0.13, shDrop: 0.035, neck: 0.05, headR: 0.062,
    upper: 0.18, fore: 0.15
  };

  // Two-bone IK: joint a, target t, bone lengths l1/l2, pole = direction the middle
  // joint points. Returns the middle joint, the end point and whether t was reachable.
  function ik2(a, t, l1, l2, pole) {
    var d0 = sub(t, a), dl = len(d0), u = dl > 1e-6 ? mul(d0, 1 / dl) : [0, -1, 0];
    var reach = dl <= l1 + l2 + 0.004 && dl >= Math.abs(l1 - l2) - 0.004;
    var d = Math.max(Math.abs(l1 - l2) + 1e-4, Math.min(l1 + l2 - 1e-4, dl));
    var w = sub(pole, mul(u, dot(pole, u)));
    if (len(w) < 1e-6) w = Math.abs(u[1]) < 0.9 ? cross(u, [0, 1, 0]) : cross(u, [1, 0, 0]);
    w = norm(w);
    var ca = Math.max(-1, Math.min(1, (l1 * l1 + d * d - l2 * l2) / (2 * l1 * d)));
    var sa = Math.sqrt(1 - ca * ca);
    return { mid: add(a, add(mul(u, l1 * ca), mul(w, l1 * sa))), end: add(a, mul(u, d)), ok: reach };
  }

  function num(v, d) { return typeof v === 'number' ? v : d; }
  // An IK target is a world point [x,y,z] or {rel, at} riding on the body:
  // rel 'pelvis' = offset from the pelvis centre in the pelvis frame, 'chest' = from
  // the base of the neck in the upper-back frame.
  function isTarget(t) { return Array.isArray(t) || (t && Array.isArray(t.at)); }
  function target(J, t) {
    if (Array.isArray(t)) return t;
    if (t.rel === 'chest') return add(J.neck, mv(J.T, t.at));
    return add(J.root, mv(J.P, t.at));
  }

  // ── pose solving ─────────────────────────────────────────────────────────
  // `ground: ['ballL', 'ballR']` lifts or lowers the whole body until the lowest of
  // those points touches the floor (or `groundY`), so FK bodies need no hand-tuned height.
  function solve(p) {
    if (!p.ground) return solve1(p);
    var J0 = solve1(p), lo = Math.min.apply(null, p.ground.map(function (k) { return J0[k][1]; }));
    var r = p.root || [0, 0.53, 0];
    return solve1(merge(p, { root: [r[0], r[1] + num(p.groundY, 0) - lo, r[2]], ground: null }));
  }
  function solve1(p) {
    var J = { warn: [] };
    var pv = p.pelvis || {}, sp = p.spine || {}, hd = p.head || {}, sh = p.shoulders || {};
    var root = p.root || [0, 0.53, 0];
    var P = mm(ry(num(pv.yaw, 0)), mm(rx(num(pv.pitch, 0)), rz(num(pv.roll, 0))));
    var Rs = mm(ry(num(sp.twist, 0) / 2), mm(rx(num(sp.flex, 0) / 2), rz(-num(sp.side, 0) / 2)));
    var L1 = mm(P, Rs), T = mm(L1, Rs);
    var mid = add(root, mv(L1, [0, B.lumbar, 0]));
    var neck = add(mid, mv(T, [0, B.thorax, 0]));
    var H = mm(T, mm(rx(num(hd.flex, 0)), ry(num(hd.turn, 0))));
    var neckTop = add(neck, mv(H, [0, B.neck, 0]));
    J.root = root; J.P = P; J.L1 = L1; J.T = T; J.H = H; J.mid = mid; J.neck = neck; J.neckTop = neckTop;
    J.head = add(neckTop, mv(H, [0, B.headR * 0.85, 0.012]));
    J.yaw = num(pv.yaw, 0);
    ['L', 'R'].forEach(function (s) {
      leg(J, s, p['leg' + s] || {});
      arm(J, s, p['arm' + s] || {}, sh);
    });
    return J;
  }

  function leg(J, side, g) {
    var s = side === 'L' ? 1 : -1, hip = add(J.root, mv(J.P, [s * B.hipW, 0, 0])), knee, ankle, F;
    if (isTarget(g.ankle) || isTarget(g.ball)) {
      var ft = g.foot || {};
      F = mm(ry(J.yaw + s * num(ft.yaw, 0)), mm(rx(num(ft.pitch, 0)), rz(s * num(ft.roll, 0))));
      ankle = g.ankle ? target(J, g.ankle) : sub(target(J, g.ball), mv(F, [0, -B.ankleH, B.ball]));
      var pole = g.pole || mv(J.P, [s * 0.15, 0, 1]);
      var r = ik2(hip, ankle, B.thigh, B.shank, pole);
      if (!r.ok) J.warn.push(ankle);
      knee = r.mid; ankle = r.end;
    } else {
      var Lf = mm(J.P, mm(ry(s * num(g.az, 0)), mm(rx(-num(g.elev, 0)), ry(s * num(g.twist, 0)))));
      knee = add(hip, mv(Lf, [0, -B.thigh, 0]));
      var Sf = mm(Lf, rx(num(g.knee, 0)));
      ankle = add(knee, mv(Sf, [0, -B.shank, 0]));
      F = mm(Sf, mm(rx(num(g.point, 0)), rz(s * num(g.roll, 0))));
    }
    J['hip' + side] = hip; J['knee' + side] = knee; J['ankle' + side] = ankle;
    J['heel' + side] = add(ankle, mv(F, [0, -B.ankleH * 0.9, -B.heel]));
    J['ball' + side] = add(ankle, mv(F, [0, -B.ankleH, B.ball]));
    J['toe' + side] = add(ankle, mv(F, [0, -B.ankleH * 0.6, B.toe]));
    J['F' + side] = F;
  }

  function arm(J, side, g, sh) {
    var s = side === 'L' ? 1 : -1, T = J.T;
    var shoulder = add(J.neck, mv(T, [s * B.shW, -B.shDrop + num(sh.shrug, 0), num(sh.reach, 0)]));
    var elbow, wrist, dir;
    if (isTarget(g.hand)) {
      var pole = g.pole || mv(T, [s * 0.35, -0.25, -1]), ht = target(J, g.hand);
      var r = ik2(shoulder, ht, B.upper, B.fore, pole);
      if (!r.ok) J.warn.push(ht);
      elbow = r.mid; wrist = r.end;
    } else {
      var Af = mm(T, mm(ry(s * num(g.az, 0)), mm(rx(-num(g.elev, 0)), ry(s * num(g.twist, 0)))));
      elbow = add(shoulder, mv(Af, [0, -B.upper, 0]));
      wrist = add(elbow, mv(mm(Af, rx(-num(g.elbow, 0))), [0, -B.fore, 0]));
    }
    dir = norm(sub(wrist, elbow));
    J['sh' + side] = shoulder; J['elbow' + side] = elbow; J['wrist' + side] = wrist;
    J['hand' + side] = add(wrist, mul(dir, 0.035));
  }

  // ── keys: inherit, mirror, blend ─────────────────────────────────────────
  function isObj(v) { return v && typeof v === 'object' && !Array.isArray(v); }
  function merge(a, b) {
    var o = {}, k;
    for (k in a) o[k] = a[k];
    for (k in b) {
      if (b[k] === null) delete o[k];
      else if (isObj(b[k]) && isObj(o[k])) o[k] = merge(o[k], b[k]);
      else o[k] = b[k];
    }
    return o;
  }
  function mirror(v) {
    if (Array.isArray(v)) return v.length === 3 ? [-v[0], v[1], v[2]] : v.slice();
    if (isObj(v)) { var o = {}; for (var k in v) o[k] = mirror(v[k]); return o; }
    return v;
  }
  // Both-sides shorthand -> explicit legL/legR/armL/armR, with FK defaults filled in.
  function expand(raw) {
    var p = merge({}, raw);
    [['legs', 'leg'], ['arms', 'arm']].forEach(function (pair) {
      var both = p[pair[0]]; delete p[pair[0]];
      ['L', 'R'].forEach(function (s) {
        var base = both ? (s === 'L' ? both : mirror(both)) : {};
        p[pair[1] + s] = merge(base, p[pair[1] + s] || {});
      });
    });
    return p;
  }
  function blend(a, b, f) {
    if (typeof a === 'number' && typeof b === 'number') return lerp(a, b, f);
    if (Array.isArray(a) && Array.isArray(b)) return a.map(function (x, i) { return blend(x, b[i], f); });
    if (isObj(a) || isObj(b)) {
      a = a || {}; b = b || {};
      var o = {}, k;
      // A part only one key sets (say pelvis tilt) blends from/to its neutral 0, not a jump.
      for (k in a) o[k] = k in b ? blend(a[k], b[k], f) : (typeof a[k] === 'number' ? lerp(a[k], 0, f) : isObj(a[k]) ? blend(a[k], {}, f) : a[k]);
      for (k in b) if (!(k in a)) o[k] = typeof b[k] === 'number' ? lerp(0, b[k], f) : isObj(b[k]) ? blend({}, b[k], f) : b[k];
      return o;
    }
    return f < 0.5 ? a : b;
  }
  function ease(f) { return 0.5 - 0.5 * Math.cos(Math.PI * f); }

  function compile(anim) {
    if (anim._c) return anim._c;
    var raw = {}, keys = [], total = 0;
    (anim.keys || [{ pose: {} }]).forEach(function (k) {
      raw = merge(raw, k.pose || {});
      var hold = num(k.hold, 0.35), move = num(k.move, 1.0);
      keys.push({ pose: expand(raw), hold: hold, move: move, t0: total });
      total += hold + move;
    });
    if (keys.length === 1) { keys[0].move = 0; total = keys[0].hold || 1; }
    anim._c = { keys: keys, total: total || 1 };
    return anim._c;
  }
  function poseAt(anim, t) {
    var c = compile(anim), ks = c.keys;
    t = ((t % c.total) + c.total) % c.total;
    for (var i = 0; i < ks.length; i++) {
      var k = ks[i], next = ks[(i + 1) % ks.length];
      if (t < k.t0 + k.hold) return k.pose;
      if (t < k.t0 + k.hold + k.move) return blend(k.pose, next.pose, ease((t - k.t0 - k.hold) / k.move));
    }
    return ks[0].pose;
  }

  // ── camera ───────────────────────────────────────────────────────────────
  var VIEWS = {
    side: { yaw: 90, pitch: 6, label: 'Side' },
    front: { yaw: 0, pitch: 6, label: 'Front' },
    back: { yaw: 180, pitch: 6, label: 'Back' },
    left: { yaw: -90, pitch: 6, label: 'Side' },
    above: { yaw: 90, pitch: 55, label: 'Above' },
    three: { yaw: 40, pitch: 16, label: 'Angle' }
  };
  function camera(v) {
    v = typeof v === 'string' ? VIEWS[v] : v;
    var y = v.yaw * D2R, p = v.pitch * D2R;
    var c = [-Math.sin(y) * Math.cos(p), Math.sin(p), Math.cos(y) * Math.cos(p)];
    var d = mul(c, -1), up = [0, 1, 0];
    var u = norm(sub(up, mul(d, dot(up, d))));
    return { d: d, u: u, r: cross(d, u), label: v.label || '' };
  }
  function proj(cam, p) { return [dot(p, cam.r), -dot(p, cam.u), dot(p, cam.d)]; }

  // ── colours ──────────────────────────────────────────────────────────────
  var C = {
    bg: '#f3f5f7', skin: '#b67c58', hair: '#2a201b', shirt: '#14636a', legs: '#3a4255',
    shoe: '#2b2e34', sole: '#dfe3e8', metal: '#59606a', plate: '#2e333a', pad: '#56657a',
    frame: '#a3aab3', box: '#c9cfd7', boxTop: '#dce1e7', mat: '#dae0e7', band: '#e4572e',
    wall: '#e3e7ec', shadow: 'rgba(20,30,45,.09)', warn: '#e0245e'
  };
  function hex(c) { return [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16)]; }
  function shade(c, f) {
    if (c.charAt(0) !== '#' || f <= 0) return c;
    var a = hex(c), b = hex(C.bg);
    return 'rgb(' + a.map(function (x, i) { return Math.round(x + (b[i] - x) * f); }).join(',') + ')';
  }

  // ── 2D shapes ────────────────────────────────────────────────────────────
  function f3(n) { return Math.round(n * 1000) / 1000; }
  function capsule(a, b, ra, rb, fill) {
    var dx = b[0] - a[0], dy = b[1] - a[1], l = Math.sqrt(dx * dx + dy * dy);
    if (l < 1e-4) return '<circle cx="' + f3(a[0]) + '" cy="' + f3(a[1]) + '" r="' + f3(Math.max(ra, rb)) + '" fill="' + fill + '"/>';
    var nx = -dy / l, ny = dx / l;
    return '<path fill="' + fill + '" d="M' + f3(a[0] + nx * ra) + ' ' + f3(a[1] + ny * ra) +
      'L' + f3(b[0] + nx * rb) + ' ' + f3(b[1] + ny * rb) +
      'A' + f3(rb) + ' ' + f3(rb) + ' 0 0 0 ' + f3(b[0] - nx * rb) + ' ' + f3(b[1] - ny * rb) +
      'L' + f3(a[0] - nx * ra) + ' ' + f3(a[1] - ny * ra) +
      'A' + f3(ra) + ' ' + f3(ra) + ' 0 0 0 ' + f3(a[0] + nx * ra) + ' ' + f3(a[1] + ny * ra) + 'Z"/>';
  }
  function hull(pts) {
    pts = pts.slice().sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    function cr(o, a, b) { return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]); }
    var lo = [], up = [], i;
    for (i = 0; i < pts.length; i++) {
      while (lo.length >= 2 && cr(lo[lo.length - 2], lo[lo.length - 1], pts[i]) <= 0) lo.pop();
      lo.push(pts[i]);
    }
    for (i = pts.length - 1; i >= 0; i--) {
      while (up.length >= 2 && cr(up[up.length - 2], up[up.length - 1], pts[i]) <= 0) up.pop();
      up.push(pts[i]);
    }
    return lo.slice(0, -1).concat(up.slice(0, -1));
  }
  function poly(pts, fill, round) {
    return '<path fill="' + fill + '"' + (round ? ' stroke="' + fill + '" stroke-width="' + round + '" stroke-linejoin="round"' : '') +
      ' d="M' + pts.map(function (p) { return f3(p[0]) + ' ' + f3(p[1]); }).join('L') + 'Z"/>';
  }
  function circle(c, r, fill) { return '<circle cx="' + f3(c[0]) + '" cy="' + f3(c[1]) + '" r="' + f3(r) + '" fill="' + fill + '"/>'; }
  function line(a, b, w, col) {
    return '<path d="M' + f3(a[0]) + ' ' + f3(a[1]) + 'L' + f3(b[0]) + ' ' + f3(b[1]) + '" stroke="' + col +
      '" stroke-width="' + f3(w) + '" stroke-linecap="round" fill="none"/>';
  }
  function perp(axis) {
    var a = norm(Math.abs(axis[1]) < 0.9 ? cross(axis, [0, 1, 0]) : cross(axis, [1, 0, 0]));
    return [a, norm(cross(axis, a))];
  }
  // A solid cylinder (weight plate, dumbbell head) as the hull of its two end circles.
  function cylinder(cam, c, axis, r, h) {
    axis = norm(axis);
    var ab = perp(axis), pts = [];
    [-h / 2, h / 2].forEach(function (o) {
      var cc = add(c, mul(axis, o));
      for (var i = 0; i < 16; i++) {
        var t = i / 16 * Math.PI * 2;
        pts.push(proj(cam, add(cc, add(mul(ab[0], r * Math.cos(t)), mul(ab[1], r * Math.sin(t))))));
      }
    });
    return hull(pts);
  }
  function boxPts(cam, mn, mx) {
    var pts = [];
    for (var i = 0; i < 8; i++)
      pts.push(proj(cam, [i & 1 ? mx[0] : mn[0], i & 2 ? mx[1] : mn[1], i & 4 ? mx[2] : mn[2]]));
    return pts;
  }

  // ── drawing ──────────────────────────────────────────────────────────────
  function depthOf(cam, p) { return dot(p, cam.d); }

  function figureItems(J, cam) {
    var items = [], P = function (p) { return proj(cam, p); };
    var torsoD = depthOf(cam, J.mid);
    // The limbs on the far side are paled, by how squarely the camera sees that side.
    var latFar = dot(mv(J.P, [1, 0, 0]), cam.d), sideShade = { L: Math.max(0, latFar) * 0.38, R: Math.max(0, -latFar) * 0.38 };
    var curSide = 'L';
    function far() { return sideShade[curSide]; }
    function seg(a, b, ra, rb, col, bias) {
      var m = mul(add(a, b), 0.5);
      items.push({ d: depthOf(cam, m) + (bias || 0), s: capsule(P(a), P(b), ra, rb, shade(col, far(m))) });
    }
    // torso group: pelvis (tights), lower and upper shirt, neck - drawn as one layer
    function pts(M, o, list) { return list.map(function (q) { return P(add(o, mv(M, q))); }); }
    var pel = [], low = [], up = [], i;
    [1, -1].forEach(function (s) {
      pel.push([s * 0.10, 0.06, 0.05], [s * 0.10, 0.06, -0.065], [s * 0.105, -0.02, 0.05], [s * 0.10, -0.03, -0.09],
        [s * 0.07, -0.085, -0.055], [s * 0.06, -0.07, 0.035]);
      low.push([s * 0.098, 0.035, 0.058], [s * 0.098, 0.035, -0.07]);
      up.push([s * 0.092, 0, 0.065], [s * 0.092, 0, -0.065], [s * 0.112, 0.075, 0.085], [s * 0.112, 0.075, -0.07],
        [s * 0.125, B.thorax - 0.025, 0.05], [s * 0.125, B.thorax - 0.025, -0.06], [s * 0.05, B.thorax + 0.012, 0.03],
        [s * 0.05, B.thorax + 0.012, -0.035]);
    });
    var lowPts = pts(J.L1, J.root, low).concat(pts(J.T, J.mid, [[0.092, 0, 0.065], [0.092, 0, -0.065], [-0.092, 0, 0.065], [-0.092, 0, -0.065]]));
    var g = poly(hull(pts(J.P, J.root, pel)), C.legs, 0.03) +
      poly(hull(lowPts), C.shirt, 0.03) +
      poly(hull(pts(J.T, J.mid, up)), C.shirt, 0.03) +
      capsule(P(J.neck), P(J.neckTop), 0.034, 0.03, C.skin);
    items.push({ d: torsoD, s: g });
    // head: hair sits behind/above the face; draw order follows which way it faces
    var hc = P(J.head), fwd = mv(J.H, [0, 0, 1]), upv = mv(J.H, [0, 1, 0]);
    var hairC = P(add(J.head, add(mul(fwd, -0.014), mul(upv, 0.01)))), faceC = P(add(J.head, mul(fwd, 0.006)));
    var bun = P(add(J.head, add(mul(fwd, -0.06), mul(upv, 0.035))));
    var faceAway = dot(fwd, cam.d) > 0.35;
    var hair = circle(bun, 0.03, C.hair) + circle(hairC, B.headR + 0.004, C.hair);
    var face = circle(faceC, B.headR, C.skin);
    items.push({ d: depthOf(cam, J.head), s: faceAway ? face + hair : hair + face });
    void hc;
    // limbs
    ['L', 'R'].forEach(function (s) {
      curSide = s;
      seg(J['hip' + s], J['knee' + s], 0.064, 0.047, C.legs);
      seg(J['knee' + s], J['ankle' + s], 0.046, 0.031, C.legs);
      var heel = J['heel' + s], ball = J['ball' + s], toe = J['toe' + s], an = J['ankle' + s];
      var mf = mul(add(heel, ball), 0.5), fd = depthOf(cam, mf);
      items.push({ d: fd, s: capsule(P(an), P(mf), 0.034, 0.03, shade(C.shoe, far(mf))) +
        capsule(P(heel), P(toe), 0.026, 0.02, shade(C.shoe, far(mf))) });
      seg(J['sh' + s], J['elbow' + s], 0.037, 0.03, C.skin, -0.001);
      var sleeve = add(J['sh' + s], mul(sub(J['elbow' + s], J['sh' + s]), 0.5));
      seg(J['sh' + s], sleeve, 0.045, 0.04, C.shirt, -0.002);
      seg(J['elbow' + s], J['wrist' + s], 0.03, 0.024, C.skin);
      items.push({ d: depthOf(cam, J['hand' + s]) - 0.001, s: circle(P(J['hand' + s]), 0.03, shade(C.skin, far(J['hand' + s]))) });
    });
    return items;
  }

  // Where on the body a prop attaches.
  function anchor(J, a) {
    if (Array.isArray(a)) return a;
    var map = { handL: 'handL', handR: 'handR', kneeL: 'kneeL', kneeR: 'kneeR', ankleL: 'ankleL', ankleR: 'ankleR',
      footL: 'ballL', footR: 'ballR', wristL: 'wristL', wristR: 'wristR', elbowL: 'elbowL', elbowR: 'elbowR' };
    if (map[a]) return J[map[a]];
    if (a === 'hands') return mul(add(J.handL, J.handR), 0.5);
    if (a === 'knees') return mul(add(J.kneeL, J.kneeR), 0.5);
    if (a === 'back') return add(J.neck, mv(J.T, [0, -0.04, -0.075]));
    if (a === 'hips') return add(J.root, mv(J.P, [0, 0.035, 0.085]));
    if (a === 'chest') return add(J.neck, mv(J.T, [0, -0.09, 0.15]));
    return J.root;
  }
  function axisOf(J, ax, side) {
    if (Array.isArray(ax)) return ax;
    var s = side === 'R' ? -1 : 1;
    if (ax === 'forward') return mv(J.T, [0, 0, 1]);
    if (ax === 'vertical') return [0, 1, 0];
    if (ax === 'world-x') return [1, 0, 0];
    if (ax === 'forearm') { var w = J['wrist' + (side || 'L')], e = J['elbow' + (side || 'L')]; return norm(sub(w, e)); }
    return mul(mv(J.T, [1, 0, 0]), s); // 'lateral'
  }

  function propItems(J, cam, props) {
    var items = [];
    function P(p) { return proj(cam, p); }
    function push(pt, s, bias) { items.push({ d: depthOf(cam, pt) + (bias || 0), s: s }); }
    (props || []).forEach(function (pr) {
      var t = pr.type;
      if (t === 'mat') {
        var c = pr.at || [0, 0, 0], w = (pr.size || [0.5, 1.4])[0] / 2, l = (pr.size || [0.5, 1.4])[1] / 2;
        var corners = [[c[0] - w, 0.003, c[2] - l], [c[0] + w, 0.003, c[2] - l], [c[0] + w, 0.003, c[2] + l], [c[0] - w, 0.003, c[2] + l]];
        items.push({ d: 1e3, s: poly(corners.map(P), C.mat, 0.02) });
      } else if (t === 'box' || t === 'bench' || t === 'wall') {
        var mn = pr.min, mx = pr.max, col = t === 'wall' ? C.wall : (pr.color || C.box);
        var center = mul(add(mn, mx), 0.5);
        var s = '';
        if (t === 'bench') {
          var padH = 0.05, legW = 0.05;
          [[mn[0] + 0.03, mn[2] + 0.08], [mn[0] + 0.03, mx[2] - 0.08 - legW]].forEach(function () {});
          var lz = [mn[2] + 0.06, mx[2] - 0.06 - legW];
          lz.forEach(function (z) {
            s += poly(hull(boxPts(cam, [center[0] - 0.08, 0, z], [center[0] + 0.08, mx[1] - padH, z + legW])), C.frame, 0.01);
          });
          s += poly(hull(boxPts(cam, [mn[0], mx[1] - padH, mn[2]], mx)), C.pad, 0.012);
        } else {
          s += poly(hull(boxPts(cam, mn, mx)), col, 0.01);
          if (cam.u[1] < 0.999 && t !== 'wall') {
            var top = [[mn[0], mx[1], mn[2]], [mx[0], mx[1], mn[2]], [mx[0], mx[1], mx[2]], [mn[0], mx[1], mx[2]]];
            s += poly(top.map(P), C.boxTop, 0.01);
          }
        }
        push(center, s, pr.depthBias || 0.15);
      } else if (t === 'dumbbell') {
        (pr.hand === 'both' ? ['L', 'R'] : [pr.hand || 'L']).forEach(function (sd) {
          var c = pr.at ? anchor(J, pr.at) : J['hand' + sd], ax = norm(axisOf(J, pr.axis || 'forward', sd));
          var sd2 = '', hw = 0.07;
          var ends = [add(c, mul(ax, -hw)), add(c, mul(ax, hw))];
          sd2 += line(P(ends[0]), P(ends[1]), 0.022, C.metal);
          var heads = ends.map(function (e, i) { return add(e, mul(ax, (i ? 1 : -1) * 0.02)); });
          heads.sort(function (a, b) { return depthOf(cam, b) - depthOf(cam, a); });
          heads.forEach(function (hc) { sd2 += poly(cylinder(cam, hc, ax, pr.r || 0.045, 0.04), C.plate, 0.004); });
          push(c, sd2, -0.004);
        });
      } else if (t === 'kettlebell') {
        var hc = pr.at ? anchor(J, pr.at) : (pr.hand === 'L' || pr.hand === 'R' ? J['hand' + pr.hand] : anchor(J, 'hands'));
        var hang = pr.hang === 'arm' ? norm(sub(J.wristL, J.elbowL)) : (Array.isArray(pr.hang) ? norm(pr.hang) : [0, -1, 0]);
        var bc = add(hc, mul(hang, 0.085));
        push(bc, line(P(hc), P(add(hc, mul(hang, 0.03))), 0.03, C.plate) + circle(P(bc), 0.06, C.plate), -0.006);
      } else if (t === 'barbell') {
        var at = pr.on === 'back' ? anchor(J, 'back') : pr.on === 'hips' ? anchor(J, 'hips') : anchor(J, 'hands');
        var bx = pr.on === 'hands' || !pr.on ? norm(sub(J.handL, J.handR)) : mv(J.T, [1, 0, 0]);
        if (pr.on === 'hips') bx = mv(J.P, [1, 0, 0]);
        var half = pr.half || 0.6, inner = pr.inner || 0.36, rr = pr.plate || 0.12;
        var sb = line(P(add(at, mul(bx, -half))), P(add(at, mul(bx, half))), 0.018, C.metal);
        var ps = [-1, 1].map(function (sg) { return add(at, mul(bx, sg * (inner + 0.04))); });
        // Seen end-on (side view) a real plate would hide the lifter, so it goes see-through.
        var op = f3(1 - 0.62 * Math.abs(dot(norm(bx), cam.d)));
        if (pr.plates !== false) ps.forEach(function (pc) {
          push(pc, '<g opacity="' + op + '">' + poly(cylinder(cam, pc, bx, rr, 0.07), C.plate, 0.004) + '</g>', -0.03);
        });
        push(at, sb, -0.02);
      } else if (t === 'band') {
        var a = anchor(J, pr.from), b = anchor(J, pr.to);
        push(mul(add(a, b), 0.5), line(P(a), P(b), pr.width || 0.016, C.band), -0.05);
        if (Array.isArray(pr.to)) push(b, circle(P(b), 0.02, C.metal), 0);
      } else if (t === 'bar') {
        var a2 = anchor(J, pr.from), b2 = anchor(J, pr.to);
        push(mul(add(a2, b2), 0.5), line(P(a2), P(b2), pr.width || 0.022, C.metal), pr.depthBias || 0);
      } else if (t === 'cable') {
        var a3 = anchor(J, pr.from), b3 = anchor(J, pr.to);
        push(mul(add(a3, b3), 0.5), line(P(a3), P(b3), 0.007, '#6b727c') + circle(P(b3), 0.03, C.metal), 0.2);
      }
    });
    return items;
  }

  function shadowItem(J, cam) {
    var keys = ['heelL', 'heelR', 'toeL', 'toeR', 'root', 'neck', 'head', 'handL', 'handR', 'kneeL', 'kneeR'];
    var xs = [], ys = [];
    keys.forEach(function (k) { var q = proj(cam, [J[k][0], 0, J[k][2]]); xs.push(q[0]); ys.push(q[1]); });
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    var cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, rxx = (x1 - x0) / 2 + 0.12, ryy = Math.max(0.018, (y1 - y0) / 2 + 0.03);
    return { d: 2e3, s: '<ellipse cx="' + f3(cx) + '" cy="' + f3(cy) + '" rx="' + f3(rxx) + '" ry="' + f3(ryy) + '" fill="' + C.shadow + '"/>' };
  }

  function frame(J, cam, anim, dev) {
    var items = [shadowItem(J, cam)].concat(propItems(J, cam, anim.props), figureItems(J, cam));
    items.sort(function (a, b) { return b.d - a.d; });
    var s = items.map(function (i) { return i.s; }).join('');
    if (dev) {
      J.warn.forEach(function (w) { s += '<circle cx="' + f3(proj(cam, w)[0]) + '" cy="' + f3(proj(cam, w)[1]) + '" r=".03" fill="none" stroke="' + C.warn + '" stroke-width=".012"/>'; });
      ['heelL', 'heelR', 'toeL', 'toeR', 'handL', 'handR', 'head', 'root'].forEach(function (k) {
        if (J[k][1] < -0.012) s += circle(proj(cam, J[k]), 0.02, C.warn);
      });
    }
    return s;
  }

  // A fixed viewBox per view, fitted round the whole loop, so the figure never jumps.
  function fit(anim, cam) {
    var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, c = compile(anim);
    for (var i = 0; i <= 24; i++) {
      var J = solve(poseAt(anim, c.total * i / 24));
      ['head', 'neck', 'mid', 'root', 'handL', 'handR', 'elbowL', 'elbowR', 'kneeL', 'kneeR', 'heelL', 'heelR', 'toeL', 'toeR', 'shL', 'shR']
        .forEach(function (k) {
          var q = proj(cam, J[k]), r = k === 'head' ? 0.08 : 0.06;
          x0 = Math.min(x0, q[0] - r); x1 = Math.max(x1, q[0] + r); y0 = Math.min(y0, q[1] - r); y1 = Math.max(y1, q[1] + r);
        });
      (anim.props || []).forEach(function (pr) {
        if (pr.type === 'box' || pr.type === 'bench') boxPts(cam, pr.min, pr.max).forEach(function (q) {
          x0 = Math.min(x0, q[0]); x1 = Math.max(x1, q[0]); y0 = Math.min(y0, q[1]); y1 = Math.max(y1, q[1]);
        });
        if (pr.fit) pr.fit.forEach(function (w) {
          var q = proj(cam, w); x0 = Math.min(x0, q[0]); x1 = Math.max(x1, q[0]); y0 = Math.min(y0, q[1]); y1 = Math.max(y1, q[1]);
        });
      });
    }
    var pad = 0.06, w = x1 - x0 + 2 * pad, h = y1 - y0 + 2 * pad, side = Math.max(w, h, 0.9);
    var cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    return [cx - side / 2, cy - side / 2, side, side];
  }

  function svgOpen(vb) {
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="' + vb.map(f3).join(' ') +
      '" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%;display:block">';
  }

  // Static SVG string of one view at time t (seconds into the loop).
  function drawStatic(anim, view, t, dev) {
    var cam = camera(view), J = solve(poseAt(anim, t || 0));
    return svgOpen(fit(anim, cam)) + frame(J, cam, anim, dev) + '</svg>';
  }

  // Live animation of one or more views into `els` (array of elements, one per view).
  var reduceMotion = global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function play(els, anim, opts) {
    opts = opts || {};
    var views = opts.views || anim.views || ['side', 'front'];
    var cams = views.map(camera), boxes = cams.map(function (c) { return fit(anim, c); });
    var groups = els.map(function (el, i) {
      el.innerHTML = svgOpen(boxes[i]) + '<g></g></svg>';
      return el.querySelector('g');
    });
    var start = null, raf = 0, stopped = false, last = 0, minDt = 1000 / (opts.fps || 60);
    var c = compile(anim);
    function draw(t) {
      var J = solve(poseAt(anim, t));
      groups.forEach(function (g, i) { g.innerHTML = frame(J, cams[i], anim, opts.dev); });
    }
    function tick(now) {
      if (stopped) return;
      if (start === null) start = now;
      if (now - last >= minDt - 1) { last = now; draw((now - start) / 1000); }
      raf = global.requestAnimationFrame(tick);
    }
    if (reduceMotion || opts.still) draw(c.keys[Math.min(1, c.keys.length - 1)].t0);
    else raf = global.requestAnimationFrame(tick);
    return {
      stop: function () { stopped = true; global.cancelAnimationFrame(raf); },
      labels: cams.map(function (c2) { return c2.label; })
    };
  }

  global.PeakFigure = {
    solve: solve, poseAt: poseAt, compile: compile, camera: camera, drawStatic: drawStatic, play: play,
    VIEWS: VIEWS, BODY: B, COLOURS: C
  };
})(typeof window !== 'undefined' ? window : this);
