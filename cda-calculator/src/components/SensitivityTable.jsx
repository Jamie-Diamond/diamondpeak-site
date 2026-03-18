import React, { useMemo } from 'react';
import { computeAllSegments } from '../lib/physics.js';

function median(arr) {
  const sorted = [...arr].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export default function SensitivityTable({ segments, wind, params, baseCdA }) {
  const perturbations = useMemo(() => {
    if (!segments || !wind || !params || baseCdA == null) return [];

    const perturb = (label, baseVal, unit, delta, makeMod) => {
      const pLo = makeMod(-delta);
      const pHi = makeMod(delta);
      const loResults = computeAllSegments(pLo.segments || segments, pLo.wind, pLo.params);
      const hiResults = computeAllSegments(pHi.segments || segments, pHi.wind, pHi.params);
      const loMedian = loResults.length > 0 ? median(loResults.map((r) => r.cda)) : baseCdA;
      const hiMedian = hiResults.length > 0 ? median(hiResults.map((r) => r.cda)) : baseCdA;
      return {
        label,
        base: `${baseVal}${unit}`,
        loCda: loMedian.toFixed(3),
        hiCda: hiMedian.toFixed(3),
        range: Math.abs(hiMedian - loMedian).toFixed(3),
      };
    };

    return [
      perturb('Mass', params.mass_kg, ' kg', 2, (d) => ({
        wind,
        params: { ...params, mass_kg: params.mass_kg + d },
      })),
      perturb('Crr', params.Crr, '', 0.001, (d) => ({
        wind,
        params: { ...params, Crr: params.Crr + d },
      })),
      perturb('Wind speed', (wind.speed_ms * 3.6).toFixed(1), ' km/h', 2 / 3.6, (d) => ({
        wind: { ...wind, speed_ms: Math.max(0, wind.speed_ms + d) },
        params,
        // Also perturb per-segment wind
        segments: segments.map((s) => s.wind ? { ...s, wind: { ...s.wind, speed_ms: Math.max(0, s.wind.speed_ms + d) } } : s),
      })),
      perturb('Wind dir', wind.dir_deg, '°', 15, (d) => ({
        wind: { ...wind, dir_deg: ((wind.dir_deg + d) % 360 + 360) % 360 },
        params,
        segments: segments.map((s) => s.wind ? { ...s, wind: { ...s.wind, dir_deg: ((s.wind.dir_deg + d) % 360 + 360) % 360 } } : s),
      })),
      perturb('Temperature', params.temp_C, '°C', 5, (d) => ({
        wind,
        params: { ...params, temp_C: params.temp_C + d },
      })),
    ];
  }, [segments, wind, params, baseCdA]);

  if (perturbations.length === 0) return null;

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="section-tag">Sensitivity Analysis</span>
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Parameter</th>
              <th>Base</th>
              <th>−δ CdA</th>
              <th>+δ CdA</th>
              <th>Range</th>
            </tr>
          </thead>
          <tbody>
            {perturbations.map((row) => (
              <tr key={row.label}>
                <td>{row.label}</td>
                <td>{row.base}</td>
                <td>{row.loCda}</td>
                <td>{row.hiCda}</td>
                <td className="cda-cell">{row.range}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
