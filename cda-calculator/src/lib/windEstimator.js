/**
 * Wind back-calculation via grid search + Nelder-Mead optimiser.
 * Minimises CdA variance across segments.
 */
import { computeCdA } from './physics.js';

function median(arr) {
  const sorted = [...arr].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function iqrFilter(values) {
  if (values.length < 4) return values;
  const sorted = [...values].sort((a, b) => a - b);
  const q1 = sorted[Math.floor(sorted.length * 0.25)];
  const q3 = sorted[Math.floor(sorted.length * 0.75)];
  const iqr = q3 - q1;
  const lo = q1 - 1.5 * iqr;
  const hi = q3 + 1.5 * iqr;
  return values.filter((v) => v >= lo && v <= hi);
}

function variance(arr) {
  if (arr.length < 2) return Infinity;
  const m = arr.reduce((s, v) => s + v, 0) / arr.length;
  return arr.reduce((s, v) => s + (v - m) ** 2, 0) / (arr.length - 1);
}

function objective(segments, params, Vw, thetaW) {
  const wind = { speed_ms: Vw, dir_deg: ((thetaW % 360) + 360) % 360 };
  const cdas = [];
  for (const seg of segments) {
    const cda = computeCdA(seg, wind, params);
    if (cda !== null) cdas.push(cda);
  }
  if (cdas.length < 3) return Infinity;
  const filtered = iqrFilter(cdas);
  if (filtered.length < 3) return Infinity;
  return variance(filtered);
}

function bearingOctant(deg) {
  return Math.floor(((deg + 22.5) % 360) / 45);
}

function stdDev(arr) {
  if (arr.length < 2) return 0;
  const m = arr.reduce((s, v) => s + v, 0) / arr.length;
  return Math.sqrt(arr.reduce((s, v) => s + (v - m) ** 2, 0) / (arr.length - 1));
}

function cardinalDir(deg) {
  const dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return dirs[Math.round(deg / 22.5) % 16];
}

/**
 * Nelder-Mead optimisation (2D) — implemented from scratch.
 */
function nelderMead(fn, initial, { maxIter = 300, tol = 1e-7 } = {}) {
  const alpha = 1;  // reflect
  const gamma = 2;  // expand
  const rho = 0.5;  // contract
  const sigma = 0.5; // shrink

  let simplex = initial.map((pt) => ({ x: [...pt], f: fn(pt[0], pt[1]) }));

  for (let iter = 0; iter < maxIter; iter++) {
    simplex.sort((a, b) => a.f - b.f);

    // Check convergence
    const fRange = simplex[simplex.length - 1].f - simplex[0].f;
    if (fRange < tol) break;

    const n = simplex[0].x.length;
    const best = simplex[0];
    const worst = simplex[simplex.length - 1];
    const secondWorst = simplex[simplex.length - 2];

    // Centroid (excluding worst)
    const centroid = new Array(n).fill(0);
    for (let i = 0; i < simplex.length - 1; i++) {
      for (let j = 0; j < n; j++) centroid[j] += simplex[i].x[j];
    }
    for (let j = 0; j < n; j++) centroid[j] /= (simplex.length - 1);

    // Reflect
    const reflected = centroid.map((c, j) => c + alpha * (c - worst.x[j]));
    reflected[0] = Math.max(0, Math.min(15, reflected[0])); // clamp V
    const fr = fn(reflected[0], reflected[1]);

    if (fr < secondWorst.f && fr >= best.f) {
      simplex[simplex.length - 1] = { x: reflected, f: fr };
      continue;
    }

    if (fr < best.f) {
      // Expand
      const expanded = centroid.map((c, j) => c + gamma * (reflected[j] - c));
      expanded[0] = Math.max(0, Math.min(15, expanded[0]));
      const fe = fn(expanded[0], expanded[1]);
      simplex[simplex.length - 1] = fe < fr ? { x: expanded, f: fe } : { x: reflected, f: fr };
      continue;
    }

    // Contract
    const contracted = centroid.map((c, j) => c + rho * (worst.x[j] - c));
    contracted[0] = Math.max(0, Math.min(15, contracted[0]));
    const fc = fn(contracted[0], contracted[1]);
    if (fc < worst.f) {
      simplex[simplex.length - 1] = { x: contracted, f: fc };
      continue;
    }

    // Shrink
    for (let i = 1; i < simplex.length; i++) {
      for (let j = 0; j < n; j++) {
        simplex[i].x[j] = best.x[j] + sigma * (simplex[i].x[j] - best.x[j]);
      }
      simplex[i].x[0] = Math.max(0, Math.min(15, simplex[i].x[0]));
      simplex[i].f = fn(simplex[i].x[0], simplex[i].x[1]);
    }
  }

  simplex.sort((a, b) => a.f - b.f);
  return { x: simplex[0].x, f: simplex[0].f };
}

export function estimateWind(segments, params) {
  // Pre-check: ≥8 segments, ≥4 octants
  if (segments.length < 8) {
    return {
      feasible: false,
      reason: `Only ${segments.length} segments available (minimum 8 required)`,
    };
  }

  const octants = new Set(segments.map((s) => bearingOctant(s.mean_bearing)));
  if (octants.size < 4) {
    return {
      feasible: false,
      reason: `Segments only cover ${octants.size} bearing octants (minimum 4 required for wind estimation)`,
    };
  }

  const fn = (Vw, thetaW) => objective(segments, params, Vw, thetaW);

  // Step 1: Coarse grid search
  let bestV = 0;
  let bestTheta = 0;
  let bestObj = Infinity;

  for (let v = 0; v <= 12; v += 1) {
    for (let theta = 0; theta < 360; theta += 22.5) {
      const obj = fn(v, theta);
      if (obj < bestObj) {
        bestObj = obj;
        bestV = v;
        bestTheta = theta;
      }
    }
  }

  // Step 2: Nelder-Mead refinement
  const result = nelderMead(fn, [
    [bestV, bestTheta],
    [bestV + 0.5, bestTheta + 15],
    [bestV + 0.3, bestTheta - 15],
  ]);

  const windSpeed = Math.max(0, Math.min(15, result.x[0]));
  const windDir = ((result.x[1] % 360) + 360) % 360;
  const finalVariance = result.f;

  // Confidence score
  const bearings = segments.map((s) => s.mean_bearing);
  const bearingSpreadScore = Math.min(stdDev(bearings) / 90, 1.0) * 40;
  const segmentCountScore = Math.min(segments.length / 20, 1.0) * 30;
  const residualScore = Math.max(0, 1 - finalVariance / 0.01) * 30;
  const confidence = bearingSpreadScore + segmentCountScore + residualScore;

  let confidenceLabel;
  if (confidence < 40) confidenceLabel = 'Low';
  else if (confidence <= 70) confidenceLabel = 'Medium';
  else confidenceLabel = 'High';

  return {
    feasible: true,
    wind_speed_ms: windSpeed,
    wind_dir_deg: Math.round(windDir),
    wind_speed_kmh: Math.round(windSpeed * 3.6 * 10) / 10,
    wind_dir_cardinal: cardinalDir(windDir),
    confidence_pct: Math.round(confidence),
    confidence_label: confidenceLabel,
    segments_used: segments.length,
    octants_covered: octants.size,
  };
}
