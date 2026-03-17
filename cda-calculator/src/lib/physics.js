/**
 * CdA physics model — solve CdA for a single segment given a wind vector.
 */
import { airDensity } from './airDensity.js';

const G = 9.8067;
const DEG2RAD = Math.PI / 180;

/**
 * Compute CdA for a single segment.
 *
 * @param {Object} segment - segment with mean values
 * @param {Object} wind - { speed_ms, dir_deg }
 * @param {Object} params - { mass_kg, Crr, Cm, eta, temp_C }
 * @returns {number|null} CdA in m², or null if v_air <= 0
 */
export function computeCdA(segment, wind, params) {
  const { mean_speed_ms: v, mean_power: P, mean_gradient_pct, mean_bearing, mean_elevation } = segment;
  const { speed_ms: Vw, dir_deg: thetaW } = wind;
  const { mass_kg: m, Crr, Cm, eta, temp_C } = params;

  const grad = mean_gradient_pct / 100;

  // v_air = v_ground + V_wind * cos((theta_wind - bearing) * pi/180)
  // Wind direction is where wind comes FROM, so headwind is when wind dir ≈ bearing
  const v_air = v + Vw * Math.cos((thetaW - mean_bearing) * DEG2RAD);

  if (v_air <= 0) return null;

  const rho = airDensity(mean_elevation, temp_C);

  const atan_grad = Math.atan(grad);
  const gravity_force = m * G * (Crr * Math.cos(atan_grad) + Math.sin(atan_grad));
  const drivetrain_power = P * eta;
  const mechanical_loss = Cm * v;

  const numerator = drivetrain_power - gravity_force * v - mechanical_loss;
  const denominator = 0.5 * rho * v_air * v_air * v;

  if (denominator <= 0) return null;

  const cda = numerator / denominator;

  // Reject clearly unphysical values
  if (cda < 0 || cda > 2) return null;

  return cda;
}

/**
 * Compute CdA for all segments, filtering nulls.
 */
export function computeAllSegments(segments, wind, params) {
  return segments
    .map((segment) => {
      const cda = computeCdA(segment, wind, params);
      return cda !== null ? { segment, cda } : null;
    })
    .filter(Boolean);
}
