/* Exercise animations for the strength library (17 Oct 2026). One entry per exercise
   id in ClaudeCoach/config/exercise-library.json; drawn by coach/figure.js, which
   documents the pose format. Coordinates: standing height = 1, floor y = 0, athlete
   faces +z, +x is the athlete's left. */
(function (global) {
  'use strict';
  var A = global.PeakAnims = global.PeakAnims || {};

  A.bodyweight_squat = {
    views: ['side', 'front'],
    keys: [
      { hold: 0.4, move: 1.3, pose: {
        root: [0, 0.525, 0],
        legs: { ankle: [0.12, 0.04, 0], foot: { yaw: 12 }, pole: [0.35, 0, 1] },
        arms: { elev: 8, elbow: 10 } } },
      { hold: 0.35, move: 1.0, pose: {
        root: [0, 0.29, -0.16], pelvis: { pitch: 38 }, spine: { flex: -6 }, head: { flex: -20 },
        arms: { elev: 88, elbow: 5 } } }
    ]
  };

  A.glute_bridge = {
    views: ['side', { yaw: 0, pitch: 18, label: 'Front' }],
    props: [{ type: 'mat', at: [0, 0, -0.05], size: [0.6, 1.5] }],
    keys: [
      { hold: 0.4, move: 1.0, pose: {
        root: [0, 0.085, 0], pelvis: { pitch: -90 },
        legs: { ankle: [0.11, 0.04, 0.37], pole: [0.15, 1, 0.3] },
        arms: { hand: [0.24, 0.035, -0.06], pole: [0.3, 1, 0] } } },
      { hold: 0.7, move: 1.3, pose: {
        root: [0, 0.245, -0.035], pelvis: { pitch: -118 }, head: { flex: 28 } } }
    ]
  };

  A.push_up = {
    views: ['side', { yaw: 0, pitch: 12, label: 'Front' }],
    props: [{ type: 'mat', at: [0, 0, 0.4], size: [0.6, 1.5] }],
    keys: [
      { hold: 0.35, move: 1.1, pose: {
        root: [0, 0.4, 0.47], pelvis: { pitch: 74 }, head: { flex: 8 },
        ground: ['ballL', 'ballR', 'toeL', 'toeR'],
        legs: { elev: 0, point: 8 },
        arms: { hand: [0.17, 0.03, 0.77], pole: [0.6, 0.1, -1] } } },
      { hold: 0.25, move: 0.9, pose: {
        root: [0, 0.3, 0.5], pelvis: { pitch: 83 } } }
    ]
  };
  A.back_squat = {
    views: ['side', 'front'],
    props: [{ type: 'barbell', on: 'back' }],
    keys: [
      { hold: 0.4, move: 1.4, pose: {
        root: [0, 0.525, 0],
        legs: { ankle: [0.13, 0.04, 0], foot: { yaw: 14 }, pole: [0.35, 0, 1] },
        arms: { hand: { rel: 'chest', at: [0.25, -0.03, -0.085] }, pole: [0.2, -1, -0.3] } } },
      { hold: 0.3, move: 1.0, pose: {
        root: [0, 0.3, -0.15], pelvis: { pitch: 34 }, spine: { flex: -4 }, head: { flex: -22 } } }
    ]
  };

  A.goblet_squat = {
    views: ['side', 'front'],
    props: [{ type: 'dumbbell', at: 'hands', axis: 'vertical' }],
    keys: [
      { hold: 0.4, move: 1.3, pose: {
        root: [0, 0.525, 0],
        legs: { ankle: [0.13, 0.04, 0], foot: { yaw: 14 }, pole: [0.35, 0, 1] },
        arms: { hand: { rel: 'chest', at: [0.04, -0.15, 0.13] }, pole: [0.3, -1, 0] } } },
      { hold: 0.35, move: 1.0, pose: {
        root: [0, 0.27, -0.12], pelvis: { pitch: 26 }, spine: { flex: -4 }, head: { flex: -14 } } }
    ]
  };

  A.romanian_deadlift = {
    views: ['side', 'front'],
    props: [{ type: 'barbell', on: 'hands' }],
    keys: [
      { hold: 0.4, move: 1.6, pose: {
        root: [0, 0.525, 0],
        legs: { ankle: [0.1, 0.04, 0], knee: null, pole: [0.15, 0, 1] },
        arms: { elev: 4, elbow: 0 } } },
      { hold: 0.3, move: 1.1, pose: {
        root: [0, 0.5, -0.13], pelvis: { pitch: 72 }, spine: { flex: 4 }, head: { flex: -30 },
        arms: { elev: 72 } } }
    ]
  };

  A.hip_thrust = {
    views: ['side', { yaw: 0, pitch: 14, label: 'Front' }],
    props: [{ type: 'bench', min: [-0.4, 0, -0.62], max: [0.4, 0.42, -0.3] }, { type: 'barbell', on: 'hips', half: 0.6 }],
    keys: [
      { hold: 0.35, move: 1.0, pose: {
        root: [0, 0.14, -0.08], pelvis: { pitch: -50 }, spine: { flex: 10 }, head: { flex: 30 },
        legs: { ankle: [0.13, 0.04, 0.3], pole: [0.3, 0.6, 1] },
        arms: { hand: { rel: 'pelvis', at: [0.2, 0.05, 0.1] }, pole: [0.5, 0, -1] } } },
      { hold: 0.7, move: 1.2, pose: {
        root: [0, 0.42, 0.0], pelvis: { pitch: -94 }, spine: { flex: 6 }, head: { flex: 40 } } }
    ]
  };

  A.step_up = {
    views: ['side', 'front'],
    props: [{ type: 'box', min: [-0.25, 0, 0.12], max: [0.25, 0.26, 0.5] }],
    keys: [
      { hold: 0.35, move: 1.2, pose: {
        root: [0, 0.51, -0.08], pelvis: { pitch: 8 },
        legL: { ankle: [0.1, 0.30, 0.3], pole: [0.2, 0, 1] },
        legR: { ankle: [-0.1, 0.04, -0.1], pole: [-0.2, 0, 1] },
        arms: { elev: 10, elbow: 20 } } },
      { hold: 0.4, move: 1.1, pose: {
        root: [0, 0.79, 0.28], pelvis: { pitch: 0 },
        legR: { ankle: [-0.1, 0.33, 0.26] } } }
    ]
  };

  A.band_pull_apart = {
    views: ['front', 'side'],
    props: [{ type: 'band', from: 'handL', to: 'handR' }],
    keys: [
      { hold: 0.3, move: 1.0, pose: {
        legs: { ankle: [0.11, 0.04, 0], pole: [0.2, 0, 1] },
        arms: { elev: 90, az: 10, twist: 0 } } },
      { hold: 0.5, move: 1.0, pose: {
        shoulders: { reach: -0.015 },
        arms: { elev: 90, az: 88 } } }
    ]
  };

  A.bent_over_row = {
    views: ['side', 'front'],
    props: [{ type: 'dumbbell', hand: 'both', axis: 'forward' }],
    keys: [
      { hold: 0.3, move: 0.9, pose: {
        root: [0, 0.47, -0.08], pelvis: { pitch: 55 }, head: { flex: -25 },
        legs: { ankle: [0.11, 0.04, 0], pole: [0.2, 0, 1] },
        arms: { elev: 55, elbow: 0 } } },
      { hold: 0.5, move: 1.3, pose: {
        shoulders: { reach: -0.02 },
        arms: { elev: 10, az: 20, elbow: 95 } } }
    ]
  };

  // ── Single-leg, hinge, plyometric and calf work ──

  A.bulgarian_split_squat = {
    views: ['side', 'front'],
    props: [{ type: 'bench', min: [-0.2, 0, -0.64], max: [0, 0.26, -0.15] }],
    keys: [
      { hold: 0.35, move: 1.5, pose: {
        root: [0, 0.5, 0.17], pelvis: { pitch: 6 },
        legL: { ankle: [0.1, 0.04, 0.33], pole: [0.15, 0, 1] },
        // pitch 160 = laces down, top of the rear foot on the bench
        legR: { ankle: [-0.09, 0.305, -0.17], foot: { pitch: 160 }, pole: [0, -0.4, 1] },
        arms: { elev: 6, elbow: 8 } } },
      { hold: 0.3, move: 1.0, pose: {
        root: [0, 0.3, 0.09], pelvis: { pitch: 18 }, head: { flex: -8 },
        arms: { elev: 18 } } }
    ]
  };

  A.split_squat = {
    views: ['side', 'front'],
    keys: [
      { hold: 0.35, move: 1.4, pose: {
        root: [0, 0.44, 0], pelvis: { pitch: 4 },
        legL: { ankle: [0.1, 0.04, 0.24], pole: [0.15, 0, 1] },
        legR: { ball: [-0.1, 0.01, -0.32], foot: { pitch: 45 }, pole: [0, -0.3, 1] },
        arms: { hand: { rel: 'pelvis', at: [0.15, 0.08, -0.01] }, pole: [1, 0, -0.6] } } },
      { hold: 0.3, move: 1.0, pose: {
        root: [0, 0.3, -0.02], pelvis: { pitch: 6 },
        legR: { foot: { pitch: 58 } } } }
    ]
  };

  A.reverse_lunge = {
    views: ['side', 'front'],
    keys: [
      { hold: 0.4, move: 0.55, pose: {
        root: [0, 0.528, 0], pelvis: { pitch: 0 },
        legL: { ball: [0.1, 0, 0.1], foot: { pitch: 0 }, pole: [0.15, 0, 1] },
        legR: { ankle: [-0.1, 0.04, 0], pole: [-0.15, 0, 1] },
        arms: { hand: { rel: 'pelvis', at: [0.15, 0.08, -0.01] }, pole: [1, 0, -0.6] } } },
      // left foot in the air on its way back (and forward again in the last key)
      { hold: 0, move: 0.7, pose: {
        root: [0, 0.45, -0.12], pelvis: { pitch: 6 },
        legL: { ball: [0.1, 0.04, -0.42], foot: { pitch: 35 } } } },
      { hold: 0.3, move: 0.6, pose: {
        root: [0, 0.3, -0.24], pelvis: { pitch: 8 },
        legL: { ball: [0.1, 0.005, -0.55], foot: { pitch: 55 } } } },
      { hold: 0, move: 0.5, pose: {
        root: [0, 0.48, -0.06], pelvis: { pitch: 4 },
        legL: { ball: [0.1, 0.06, -0.2], foot: { pitch: 20 } } } }
    ]
  };

  A.single_leg_rdl = {
    views: ['side', 'three'],
    props: [{ type: 'dumbbell', hand: 'R', axis: [0, 0, 1] }],
    keys: [
      { hold: 0.4, move: 1.6, pose: {
        root: [-0.04, 0.52, 0], pelvis: { pitch: 2 },
        legR: { ankle: [-0.1, 0.04, 0], pole: [-0.15, 0, 1] },
        legL: { elev: -10, knee: 32, point: -25 },
        arms: { elev: 2, elbow: 5 } } },
      { hold: 0.3, move: 1.1, pose: {
        root: [-0.04, 0.515, -0.08], pelvis: { pitch: 80 }, spine: { flex: 3 }, head: { flex: -12 },
        legL: { elev: 0, knee: 3, point: -5 },
        arms: { elev: 83 } } } // = trunk angle, so the arms hang straight down
    ]
  };

  A.kettlebell_swing = {
    views: ['side', 'front'],
    props: [{ type: 'kettlebell', hand: 'both', hang: 'arm' }],
    keys: [
      // back of the swing: forearms on the inner thighs, bell behind them
      { hold: 0.05, move: 0.42, pose: {
        root: [0, 0.485, -0.15], pelvis: { pitch: 66 }, spine: { flex: 4 }, head: { flex: -18 },
        legs: { ankle: [0.15, 0.04, 0], foot: { yaw: 12 }, pole: [0.4, 0, 1] },
        arms: { elev: 36, az: -30, elbow: 0 } } },
      { hold: 0.12, move: 0.32, pose: {
        root: [0, 0.525, 0], pelvis: { pitch: 0 }, spine: { flex: 0 }, head: { flex: 0 },
        arms: { elev: 82, az: -18 } } },
      // arms meet the hips before the hinge
      { hold: 0, move: 0.26, pose: {
        arms: { elev: 27, az: -53 } } }
    ]
  };

  A.single_leg_glute_bridge = {
    views: ['side', { yaw: 45, pitch: 28, label: 'Angle' }],
    props: [{ type: 'mat', at: [0, 0, -0.05], size: [0.6, 1.5] }],
    keys: [
      { hold: 0.4, move: 1.0, pose: {
        root: [0, 0.085, 0], pelvis: { pitch: -90 },
        legR: { ankle: [-0.1, 0.04, 0.305], pole: [-0.15, 1, 0.3] },
        legL: { elev: 43, knee: 0 }, // free leg straight, in line with the right thigh
        arms: { hand: [0.24, 0.035, -0.06], pole: [0.3, 1, 0] } } },
      { hold: 0.7, move: 1.3, pose: {
        root: [0, 0.193, -0.015], pelvis: { pitch: -108 }, head: { flex: 20 },
        legL: { elev: 0 } } }
    ]
  };

  A.pogo_hops = {
    views: ['side', 'front'],
    keys: [
      { hold: 0.02, move: 0.2, pose: {
        root: [0, 0.551, 0.03],
        legs: { ball: [0.1, 0, 0.1], foot: { pitch: 14 }, pole: [0.15, 0, 1] },
        arms: { elev: 4, elbow: 60 } } },
      { hold: 0.02, move: 0.2, pose: {
        root: [0, 0.631, 0.03],
        legs: { ball: [0.1, 0.06, 0.1], foot: { pitch: 28 } },
        arms: { elev: 14, elbow: 70 } } }
    ]
  };

  A.squat_jump = {
    views: ['side', 'front'],
    keys: [
      { hold: 0.35, move: 0.28, pose: {
        root: [0, 0.39, -0.12], pelvis: { pitch: 30 }, spine: { flex: -4 }, head: { flex: -16 },
        legs: { ball: [0.12, 0, 0.1], foot: { pitch: 0, yaw: 10 }, pole: [0.35, 0, 1] },
        arms: { elev: -20, elbow: 10 } } },
      // take-off through the toes, then the apex, then touch down on the balls of the feet
      { hold: 0, move: 0.2, pose: {
        root: [0, 0.575, 0.02], pelvis: { pitch: 4 }, spine: { flex: 0 }, head: { flex: 0 },
        legs: { foot: { pitch: 32 } },
        arms: { elev: 140 } } },
      { hold: 0.04, move: 0.22, pose: {
        root: [0, 0.715, 0.02],
        legs: { ball: [0.12, 0.14, 0.1], foot: { pitch: 38 } },
        arms: { elev: 160 } } },
      { hold: 0, move: 0.35, pose: {
        root: [0, 0.55, 0.0], pelvis: { pitch: 8 },
        legs: { ball: [0.12, 0, 0.1], foot: { pitch: 22 } },
        arms: { elev: 70 } } }
    ]
  };

  A.calf_raise = {
    views: ['left', 'back'],
    props: [{ type: 'box', min: [-0.22, 0, 0], max: [0.3, 0.12, 0.31] },
      { type: 'wall', min: [-0.55, 0, 0.31], max: [0.55, 1.15, 0.33] }],
    keys: [
      { hold: 0.4, move: 1.0, pose: {
        root: [0.05, 0.608, -0.04],
        // ball of the foot on the step edge, heel below it
        legL: { ball: [0.1, 0.12, 0.015], foot: { pitch: -20 }, pole: [0.15, 0, 1] },
        legR: { elev: 6, az: 30, knee: 75, point: 20 },
        armL: { hand: [0.2, 0.86, 0.27], pole: [0.3, -1, 0] },
        armR: { elev: 4, elbow: 10 } } },
      { hold: 0.45, move: 1.6, pose: {
        root: [0.05, 0.705, 0.0],
        legL: { foot: { pitch: 45 } } } }
    ]
  };

  A.bent_knee_calf_raise = {
    views: ['side', 'back'],
    keys: [
      { hold: 0.3, move: 1.0, pose: {
        root: [0, 0.495, -0.07], pelvis: { pitch: 12 },
        legs: { ball: [0.1, 0, 0.1], foot: { pitch: 0 }, pole: [0.2, 0, 1] },
        arms: { hand: { rel: 'pelvis', at: [0.15, 0.08, -0.01] }, pole: [1, 0, -0.6] } } },
      { hold: 0.4, move: 1.5, pose: {
        root: [0, 0.546, -0.027],
        legs: { foot: { pitch: 36 } } } }
    ]
  };

  // ── Upper body, carries and standing core ──
  // Every pelvis/spine/head/shoulders/foot that a later key changes is declared in the
  // first key: figure.js snaps (does not blend) a sub-object the previous key lacks.

  A.dumbbell_bench_press = {
    views: ['side', { yaw: 0, pitch: 55, label: 'Above' }],
    // 0.26 high = a real ~45 cm flat bench, so the feet sit flat with shins vertical
    props: [{ type: 'bench', min: [-0.13, 0, -0.62], max: [0.13, 0.26, 0.12] },
      { type: 'dumbbell', hand: 'both', axis: 'lateral' }],
    keys: [
      { hold: 0.3, move: 1.6, pose: {
        root: [0, 0.345, 0], pelvis: { pitch: -90 },
        legs: { ankle: [0.2, 0.04, 0.26], pole: [0.4, 1, 0.3] },
        arms: { hand: [0.12, 0.66, -0.27], pole: [0.5, -1, 0.45] } } },
      { hold: 0.3, move: 1.0, pose: {
        arms: { hand: [0.23, 0.43, -0.165] } } }
    ]
  };

  A.overhead_press = {
    views: ['side', 'front'],
    props: [{ type: 'dumbbell', hand: 'both', axis: 'lateral' }],
    keys: [
      { hold: 0.3, move: 1.0, pose: {
        shoulders: { shrug: 0 },
        legs: { ankle: [0.1, 0.04, 0], pole: [0.2, 0, 1] },
        arms: { hand: [0.24, 0.795, 0.06], pole: [0.8, -1, 0.4] } } },
      { hold: 0.3, move: 1.6, pose: {
        shoulders: { shrug: 0.02 },
        arms: { hand: [0.12, 1.11, -0.01] } } }
    ]
  };

  A.lat_pulldown = {
    views: ['side', 'front'],
    props: [{ type: 'bench', min: [-0.17, 0, -0.15], max: [0.17, 0.25, 0.13] },
      { type: 'bench', min: [-0.2, 0.36, 0.17], max: [0.2, 0.41, 0.27] }, // knee pad
      { type: 'barbell', on: 'hands', plates: false, half: 0.36 },
      { type: 'cable', from: 'hands', to: [0, 1.25, 0.08], fit: [[0, 1.28, 0.08]] }],
    keys: [
      { hold: 0.3, move: 1.0, pose: {
        root: [0, 0.325, 0], pelvis: { pitch: -5 }, spine: { flex: 0 }, shoulders: { shrug: 0.025 },
        legs: { ankle: [0.12, 0.04, 0.3], pole: [0.2, 0.3, 1] },
        arms: { hand: [0.27, 0.88, 0.06], pole: [0.6, -1, -0.4] } } },
      { hold: 0.4, move: 1.6, pose: {
        pelvis: { pitch: -10 }, spine: { flex: -6 }, shoulders: { shrug: -0.005 },
        arms: { hand: [0.27, 0.545, 0.06] } } }
    ]
  };

  A.pull_up = {
    views: [{ yaw: 90, pitch: 2, label: 'Side' }, 'front'],
    // wall-mounted bar: the brackets give the side view something to read
    props: [{ type: 'wall', min: [-1, 0, -0.4], max: [1, 2, -0.37] },
      { type: 'box', min: [-0.44, 1.265, -0.37], max: [-0.4, 1.295, 0.015], depthBias: 1 },
      { type: 'box', min: [0.4, 1.265, -0.37], max: [0.44, 1.295, 0.015], depthBias: 1 },
      { type: 'bar', from: [-0.42, 1.28, 0], to: [0.42, 1.28, 0], fit: [[0, 0, 0]] }],
    keys: [
      { hold: 0.4, move: 1.0, pose: {
        root: [0, 0.645, 0], pelvis: { pitch: 0 }, head: { flex: 0 }, shoulders: { shrug: 0.02 },
        legs: { elev: 6, knee: 18, point: 25 },
        arms: { hand: [0.21, 1.245, 0], pole: [0.4, -1, 0.3] } } },
      { hold: 0.4, move: 1.6, pose: {
        root: [0, 0.97, -0.06], pelvis: { pitch: -5 }, head: { flex: -10 }, shoulders: { shrug: -0.01 } } }
    ]
  };

  A.straight_arm_pulldown = {
    views: ['side', 'back'],
    props: [{ type: 'box', min: [-0.03, 0, 0.71], max: [0.03, 1.25, 0.77] },
      { type: 'band', from: 'handL', to: [0, 1.15, 0.7] },
      { type: 'band', from: 'handR', to: [0, 1.15, 0.7] }],
    keys: [
      { hold: 0.3, move: 1.0, pose: {
        root: [0, 0.49, -0.08], pelvis: { pitch: 30 }, head: { flex: -8 },
        legs: { ankle: [0.11, 0.04, 0], pole: [0.2, 0, 1] },
        arms: { elev: 145, az: 12, elbow: 8 } } },
      { hold: 0.4, move: 1.5, pose: {
        arms: { elev: 32, az: 10 } } }
    ]
  };

  A.band_external_rotation = {
    views: ['front', { yaw: 0, pitch: 78, label: 'Above' }],
    props: [{ type: 'box', min: [-0.51, 0, 0.13], max: [-0.45, 0.85, 0.19] },
      { type: 'band', from: 'handL', to: [-0.45, 0.62, 0.16] }],
    keys: [
      { hold: 0.3, move: 1.0, pose: {
        legs: { ankle: [0.11, 0.04, 0], pole: [0.2, 0, 1] },
        armL: { elev: 0, az: 0, twist: -40, elbow: 90 },
        armR: { elev: 4, az: 4, elbow: 12 } } },
      { hold: 0.4, move: 1.5, pose: {
        armL: { twist: 55 } } }
    ]
  };

  A.farmers_carry = {
    views: ['side', 'front'],
    props: [{ type: 'dumbbell', hand: 'both', axis: 'forward' }],
    keys: [
      { hold: 0, move: 0.3, pose: {
        root: [0, 0.525, 0], shoulders: { shrug: -0.005 },
        legs: { ankle: [0.1, 0.04, 0], foot: { pitch: 0 }, pole: [0.15, 0, 1] },
        arms: { elev: 0, az: 7, elbow: 3 } } },
      { hold: 0, move: 0.3, pose: {
        root: [-0.012, 0.53, 0], legL: { ankle: [0.1, 0.1, 0.04], foot: { pitch: 15 } } } },
      { hold: 0, move: 0.3, pose: {
        root: [0, 0.525, 0], legL: { ankle: [0.1, 0.04, 0], foot: { pitch: 0 } } } },
      { hold: 0, move: 0.3, pose: {
        root: [0.012, 0.53, 0], legR: { ankle: [-0.1, 0.1, 0.04], foot: { pitch: 15 } } } }
    ]
  };

  A.pallof_press = {
    views: ['front', { yaw: 0, pitch: 70, label: 'Above' }],
    props: [{ type: 'box', min: [-0.66, 0, 0.09], max: [-0.6, 0.95, 0.15] },
      { type: 'band', from: 'hands', to: [-0.6, 0.72, 0.12] }],
    keys: [
      { hold: 0.4, move: 1.0, pose: {
        root: [0, 0.51, -0.02], pelvis: { pitch: 5 },
        legs: { ankle: [0.13, 0.04, 0], foot: { yaw: 5 }, pole: [0.3, 0, 1] },
        arms: { hand: [0.025, 0.72, 0.14], pole: [0.6, -1, -0.2] } } },
      { hold: 1.5, move: 1.0, pose: {
        arms: { hand: [0.02, 0.73, 0.27] } } }
    ]
  };

  A.banded_lateral_walk = {
    views: ['front', 'side'],
    props: [{ type: 'band', from: 'kneeL', to: 'kneeR' }],
    keys: [
      { hold: 0.15, move: 0.25, pose: {
        root: [0, 0.45, -0.06], pelvis: { pitch: 22 }, spine: { flex: -2 }, head: { flex: -12 },
        legs: { ankle: [0.13, 0.04, 0], pole: [0.3, 0, 1] },
        arms: { elev: 30, az: 8, elbow: 75 } } },
      { hold: 0, move: 0.25, pose: { root: [0.02, 0.45, -0.06], legL: { ankle: [0.205, 0.075, 0] } } },
      { hold: 0.1, move: 0.25, pose: { root: [0.075, 0.45, -0.06], legL: { ankle: [0.28, 0.04, 0] } } },
      { hold: 0, move: 0.25, pose: { root: [0.12, 0.45, -0.06], legR: { ankle: [-0.055, 0.075, 0] } } },
      { hold: 0.15, move: 0.25, pose: { root: [0.15, 0.45, -0.06], legR: { ankle: [0.02, 0.04, 0] } } },
      { hold: 0, move: 0.25, pose: { root: [0.13, 0.45, -0.06], legR: { ankle: [-0.055, 0.075, 0] } } },
      { hold: 0.1, move: 0.25, pose: { root: [0.075, 0.45, -0.06], legR: { ankle: [-0.13, 0.04, 0] } } },
      { hold: 0, move: 0.25, pose: { root: [0.03, 0.45, -0.06], legL: { ankle: [0.205, 0.075, 0] } } }
    ]
  };

  A.single_leg_balance = {
    views: ['side', 'front'],
    keys: [
      { hold: 0.4, move: 1.2, pose: {
        root: [0.05, 0.515, -0.01], pelvis: { pitch: 0 },
        legL: { ankle: [0.07, 0.04, 0], pole: [0.2, 0, 1] },
        legR: { ankle: [-0.07, 0.14, -0.06], pole: [-0.1, 0, 1] },
        arms: { hand: { rel: 'pelvis', at: [0.13, 0.075, 0] }, pole: [1, 0, -0.6] } } },
      { hold: 0.4, move: 1.2, pose: {
        root: [0.05, 0.44, -0.04], pelvis: { pitch: 8 }, legR: { ankle: [-0.04, 0.07, 0.24] } } },
      { hold: 0.4, move: 1.2, pose: {
        root: [0.06, 0.45, -0.03], pelvis: { pitch: 15 }, legR: { ankle: [-0.3, 0.07, 0] } } },
      { hold: 0.4, move: 1.2, pose: {
        root: [0.05, 0.46, 0], pelvis: { pitch: 35 }, legR: { ankle: [-0.08, 0.08, -0.3] } } }
    ]
  };

  // ── Floor core and mobility ──

  A.dead_bug = {
    views: [{ yaw: 90, pitch: 22, label: 'Side' }, { yaw: -45, pitch: 40, label: 'Angle' }],
    props: [{ type: 'mat', at: [0, 0, 0.05], size: [0.6, 1.5] }],
    keys: [
      { hold: 0.3, move: 1.5, pose: {
        root: [0, 0.09, 0], pelvis: { pitch: -90 },
        legs: { elev: 90, knee: 90, point: 10 },
        arms: { elev: 90 } } },
      { hold: 0.4, move: 1.2, pose: {
        armR: { elev: 172 }, legL: { elev: 10, knee: 4 } } },
      { hold: 0.3, move: 1.5, pose: {
        armR: { elev: 90 }, legL: { elev: 90, knee: 90 } } },
      { hold: 0.4, move: 1.2, pose: {
        armL: { elev: 172 }, legR: { elev: 10, knee: 4 } } }
    ]
  };

  A.front_plank = {
    views: [{ yaw: 90, pitch: 10, label: 'Side' }, { yaw: 40, pitch: 25, label: 'Angle' }],
    props: [{ type: 'mat', at: [0, 0, 0.15], size: [0.6, 1.4] }],
    keys: [
      { hold: 0.4, move: 1.0, pose: {
        root: [0, 0.137, 0.21], pelvis: { pitch: 74 },
        legs: { ball: [0.085, 0.03, -0.298], foot: { pitch: 94 }, pole: [0, -1, 0.3] }, // toes tucked, knees down
        arms: { hand: [0.06, 0.03, 0.595], pole: [0.3, -1, -0.6] } } },
      { hold: 2.0, move: 1.0, pose: {
        root: [0, 0.169, 0.203], pelvis: { pitch: 81.2 },
        legs: { foot: { pitch: 75 } } } }
    ]
  };

  A.side_plank = {
    views: [{ yaw: 0, pitch: 8, label: 'Front' }, { yaw: 40, pitch: 30, label: 'Angle' }],
    props: [{ type: 'mat', at: [0.05, 0, 0.05], size: [1.2, 0.5] }],
    keys: [
      { hold: 0.4, move: 1.0, pose: {
        root: [-0.03, 0.145, 0], pelvis: { roll: 76.7 }, spine: { side: 31.5 }, // hips just off the floor
        legL: { ankle: [0.464, 0.112, 0], foot: { roll: 70 } },
        legR: { ankle: [0.44, 0.046, 0], foot: { roll: -70 } },
        armR: { hand: [-0.3, 0.03, 0.15], pole: [0, -1, -1] },
        armL: { hand: { rel: 'pelvis', at: [0.12, 0.06, 0.02] }, pole: [0.2, 1, -0.6] } } },
      { hold: 2.0, move: 1.0, pose: {
        root: [-0.007, 0.243, 0], pelvis: { roll: 70.4 }, spine: { side: 0 } } }
    ]
  };

  A.bird_dog = {
    views: [{ yaw: 90, pitch: 10, label: 'Side' }, { yaw: 125, pitch: 30, label: 'Angle' }],
    props: [{ type: 'mat', at: [0, 0, 0.05], size: [0.6, 1.5] }],
    keys: [
      { hold: 0.3, move: 1.2, pose: {
        root: [0, 0.3, 0], pelvis: { pitch: 76 },
        legs: { elev: 76, knee: 90, point: 85 },
        arms: { elev: 76 } } },
      { hold: 0.5, move: 1.2, pose: {
        armR: { elev: 166 }, legL: { elev: -14, knee: 0, point: 0 } } },
      { hold: 0.3, move: 1.2, pose: {
        armR: { elev: 76 }, legL: { elev: 76, knee: 90, point: 85 } } },
      { hold: 0.5, move: 1.2, pose: {
        armL: { elev: 166 }, legR: { elev: -14, knee: 0, point: 0 } } }
    ]
  };

  A.copenhagen_plank = {
    views: [{ yaw: 0, pitch: 8, label: 'Front' }, { yaw: 40, pitch: 30, label: 'Angle' }],
    // bench built from boxes: the bench prop's dark pad hides a dark shoe resting on it
    props: [{ type: 'box', min: [0.34, 0.37, -0.45], max: [0.6, 0.42, 0.45] },
      { type: 'box', min: [0.43, 0, -0.4], max: [0.51, 0.37, -0.34], color: '#a3aab3' },
      { type: 'box', min: [0.43, 0, 0.28], max: [0.51, 0.37, 0.34], color: '#a3aab3' }],
    keys: [
      { hold: 0.4, move: 1.0, pose: {
        root: [-0.038, 0.24, 0], pelvis: { roll: 87.3 }, spine: { side: 21.8 },
        legL: { ankle: [0.436, 0.46, 0], foot: { roll: 95 } },
        legR: { ankle: [0.435, 0.045, 0], foot: { roll: -90 } },
        armR: { hand: [-0.3, 0.03, 0.15], pole: [0, -1, -1] },
        armL: { hand: { rel: 'pelvis', at: [0.12, 0.06, 0.02] }, pole: [0.2, 1, -0.6] } } },
      { hold: 2.0, move: 1.0, pose: {
        root: [-0.046, 0.361, 0], pelvis: { roll: 94.7 }, spine: { side: 0 },
        legR: { ankle: [0.447, 0.33, 0] } } }
    ]
  };

  A.banded_ankle_eversion = {
    views: [{ yaw: 0, pitch: 40, label: 'Front' }, { yaw: -25, pitch: 50, label: 'Angle' }],
    props: [{ type: 'mat', at: [0, 0, 0.25], size: [0.7, 1.0] },
      { type: 'band', from: 'footL', to: [-0.26, 0.12, 0.86], fit: [[-0.26, 0.12, 0.86]] }],
    keys: [
      { hold: 0.3, move: 1.0, pose: {
        root: [0, 0.09, 0], pelvis: { pitch: -10 }, spine: { flex: 4 },
        legR: { elev: 82, point: -5 },
        legL: { ankle: [0.085, 0.106, 0.4895], pole: [0, 1, 0], foot: { yaw: 0, pitch: -95, roll: 0 } },
        arms: { hand: [0.2, 0.04, -0.08], pole: [0.3, 0, -1] } } },
      // world foot angles: toes turn out 15 deg and the sole everts 20 deg (FK roll alone barely shows)
      { hold: 0.4, move: 1.5, pose: {
        legL: { foot: { yaw: -71.3, pitch: -105.8, roll: 92 } } } }
    ]
  };

  A.leg_swings = {
    views: ['left', 'front'],
    props: [{ type: 'wall', min: [-0.46, 0, -0.5], max: [-0.435, 1.1, 0.5] }],
    keys: [
      { hold: 0.05, move: 0.7, pose: {
        root: [0, 0.529, 0],
        legR: { ankle: [-0.1, 0.04, 0], pole: [-0.15, 0, 1] },
        legL: { elev: 65, knee: 14, point: -15 },
        armR: { hand: [-0.4, 0.78, 0.04], pole: [0, -1, 0] },
        armL: { elev: 20, az: 60, elbow: 20 } } },
      { hold: 0.05, move: 0.7, pose: {
        legL: { elev: -28, knee: 26, point: -10 } } }
    ]
  };

  A.knee_to_wall = {
    views: ['left', { yaw: -140, pitch: 22, label: 'Back' }],
    props: [{ type: 'wall', min: [-0.5, 0, 0.2], max: [0.5, 1.1, 0.23], depthBias: 1 }],
    keys: [
      { hold: 0.4, move: 1.6, pose: {
        root: [0, 0.503, -0.061], pelvis: { pitch: 5 },
        legL: { ankle: [0.1, 0.04, 0], pole: [0.15, 0, 1] },
        legR: { ball: [-0.1, 0.01, -0.2], foot: { pitch: 20 }, pole: [-0.1, -0.3, 1] },
        arms: { hand: [0.17, 0.72, 0.165], pole: [0.4, -1, -0.2] } } },
      { hold: 0.8, move: 1.4, pose: {
        root: [0, 0.434, 0.01], pelvis: { pitch: 8 },
        legR: { foot: { pitch: 45 } } } }
    ]
  };

  A.hip_flexor_stretch = {
    views: ['side', 'front'],
    props: [{ type: 'mat', at: [0, 0, 0], size: [0.5, 0.8] }],
    keys: [
      { hold: 0.4, move: 2.0, pose: {
        root: [0, 0.295, 0],
        legL: { ankle: [0.1, 0.04, 0.27], pole: [0.15, 0, 1] },
        legR: { ankle: [-0.1, 0.045, -0.24], foot: { pitch: 165 }, pole: [0, -1, 1] },
        armR: { elev: 5, az: 10, elbow: 5 },
        armL: { hand: { rel: 'pelvis', at: [0.15, 0.07, 0.03] } } } },
      { hold: 1.5, move: 1.5, pose: {
        root: [0, 0.274, 0.1], pelvis: { pitch: -10 }, spine: { flex: 10, side: 8 },
        armR: { elev: 170, az: 15 } } }
    ]
  };

  A.thoracic_open_book = {
    views: [{ yaw: 90, pitch: 28, label: 'End' }, { yaw: 45, pitch: 48, label: 'Above' }],
    props: [{ type: 'mat', at: [0, 0, 0.05], size: [1.3, 0.8] }],
    keys: [
      { hold: 0.4, move: 2.0, pose: {
        root: [0, 0.13, 0], pelvis: { roll: 90 }, spine: { side: 6 },
        shoulders: { reach: 0.02 },
        legs: { elev: 90, knee: 90, az: -8 },
        armR: { hand: [-0.278, 0.025, 0.345], pole: [-0.5, 1, 0] },
        armL: { elev: 90, az: -20 } } },
      { hold: 1.0, move: 1.8, pose: {
        // flex -15 rolls the chest back so the bottom shoulder stays down
        spine: { twist: 70, flex: -15 }, head: { turn: 60 }, shoulders: { reach: -0.05 },
        armL: { elev: 90, az: 120 } } }
    ]
  };
})(typeof window !== 'undefined' ? window : this);
