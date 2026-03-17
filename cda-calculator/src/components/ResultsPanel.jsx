import React from 'react';
import WindCompass from './WindCompass.jsx';

function cardinalDir(deg) {
  const dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return dirs[Math.round(deg / 22.5) % 16];
}

export default function ResultsPanel({ results, wind }) {
  if (!results) return null;

  const windSpeedKmh = (wind.speed_ms * 3.6).toFixed(1);

  return (
    <div className="results-headline">
      <div className="cda-display">
        <div className="cda-label">CdA</div>
        <div className="cda-value">{results.median.toFixed(3)} m²</div>
        <div className="cda-sub">
          median across {results.count} segments &nbsp;·&nbsp; σ ± {results.stdDev.toFixed(3)}
        </div>
      </div>
      <div className="wind-display">
        <div className="wind-display-row">
          <WindCompass direction={wind.dir_deg} size={48} />
          <div className="wind-display-text">
            <span className="wind-value">{windSpeedKmh} km/h</span>
            <span className="wind-dir">
              from {wind.dir_deg}° ({cardinalDir(wind.dir_deg)})
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
