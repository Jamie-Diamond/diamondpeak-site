/**
 * Find steady-state segments suitable for CdA calculation.
 */

function mean(arr) {
  if (arr.length === 0) return 0;
  return arr.reduce((s, v) => s + v, 0) / arr.length;
}

function stdDev(arr) {
  if (arr.length < 2) return 0;
  const m = mean(arr);
  const variance = arr.reduce((s, v) => s + (v - m) ** 2, 0) / (arr.length - 1);
  return Math.sqrt(variance);
}

function cv(arr) {
  const m = mean(arr);
  if (m === 0) return Infinity;
  return (stdDev(arr) / Math.abs(m)) * 100;
}

// Circular standard deviation for bearings (degrees)
function bearingStdDev(bearings) {
  const rads = bearings.map((b) => (b * Math.PI) / 180);
  const sinSum = rads.reduce((s, r) => s + Math.sin(r), 0) / rads.length;
  const cosSum = rads.reduce((s, r) => s + Math.cos(r), 0) / rads.length;
  const R = Math.sqrt(sinSum ** 2 + cosSum ** 2);
  if (R >= 1) return 0;
  return Math.sqrt(-2 * Math.log(R)) * (180 / Math.PI);
}

function circularMean(bearings) {
  const rads = bearings.map((b) => (b * Math.PI) / 180);
  const sinSum = rads.reduce((s, r) => s + Math.sin(r), 0);
  const cosSum = rads.reduce((s, r) => s + Math.cos(r), 0);
  return ((Math.atan2(sinSum, cosSum) * 180) / Math.PI + 360) % 360;
}

export function detectSegments(trackpoints, filters = {}) {
  const {
    minDuration = 30,
    maxPowerCV = 5,
    maxSpeedCV = 4,
    maxGradientDeg = 3,
    minSpeed_ms = 5,
  } = filters;

  const maxGradient = Math.tan((maxGradientDeg * Math.PI) / 180);
  const windowSize = 15;

  if (trackpoints.length < windowSize) return [];

  // Step 1: Slide window and mark valid
  const valid = new Array(trackpoints.length).fill(false);

  for (let i = 0; i <= trackpoints.length - windowSize; i++) {
    const window = trackpoints.slice(i, i + windowSize);
    // Use smoothed power for steadiness check (less noisy), fall back to raw
    const powers = window.map((p) => p.power_smooth ?? p.power).filter((p) => p != null);
    const speeds = window.map((p) => p.v_ground);
    const gradients = window.map((p) => p.gradient);

    if (powers.length < windowSize * 0.5) continue;

    const cvPower = cv(powers);
    const cvSpeed = cv(speeds);
    const meanGrad = mean(gradients);
    const meanSpeed = mean(speeds);

    if (
      cvPower < maxPowerCV &&
      cvSpeed < maxSpeedCV &&
      Math.abs(meanGrad) < maxGradient &&
      meanSpeed > minSpeed_ms
    ) {
      for (let j = i; j < i + windowSize; j++) {
        valid[j] = true;
      }
    }
  }

  // Step 3: Merge consecutive valid windows (allow 2-point gap)
  const runs = [];
  let runStart = null;
  let gapCount = 0;

  for (let i = 0; i < valid.length; i++) {
    if (valid[i]) {
      if (runStart === null) runStart = i;
      gapCount = 0;
    } else {
      if (runStart !== null) {
        gapCount++;
        if (gapCount > 2) {
          runs.push([runStart, i - gapCount]);
          runStart = null;
          gapCount = 0;
        }
      }
    }
  }
  if (runStart !== null) {
    runs.push([runStart, valid.length - 1 - gapCount]);
  }

  // Step 4: Split on bearing changes
  const splitRuns = [];
  for (const [start, end] of runs) {
    const subWindowSize = 30;
    let segStart = start;
    for (let i = start; i <= end - subWindowSize; i++) {
      const subBearings = [];
      for (let j = i; j < i + subWindowSize && j <= end; j++) {
        subBearings.push(trackpoints[j].bearing);
      }
      if (bearingStdDev(subBearings) > 25) {
        if (i > segStart) {
          splitRuns.push([segStart, i - 1]);
        }
        segStart = i + subWindowSize;
        i = segStart - 1; // skip ahead
      }
    }
    if (segStart <= end) {
      splitRuns.push([segStart, end]);
    }
  }

  // Step 5: Build segment objects, filter by duration
  const segments = [];
  let id = 0;

  for (const [startIdx, endIdx] of splitRuns) {
    const pts = trackpoints.slice(startIdx, endIdx + 1);
    if (pts.length < 2) continue;

    const duration = pts[pts.length - 1].elapsed_s - pts[0].elapsed_s;
    if (duration < minDuration) continue;

    const powers = pts.map((p) => p.power).filter((p) => p != null);
    const speeds = pts.map((p) => p.v_ground);
    const gradients = pts.map((p) => p.gradient);
    const bearings = pts.map((p) => p.bearing);
    const elevations = pts.map((p) => p.ele);

    segments.push({
      id: id++,
      startIdx,
      endIdx,
      startTime: pts[0].time,
      duration_s: duration,
      mean_power: mean(powers),
      mean_speed_ms: mean(speeds),
      mean_speed_kmh: mean(speeds) * 3.6,
      mean_gradient_pct: mean(gradients) * 100,
      mean_bearing: circularMean(bearings),
      mean_elevation: mean(elevations),
      cv_power: cv(powers),
      cv_speed: cv(speeds),
      pointCount: pts.length,
    });
  }

  return segments;
}
