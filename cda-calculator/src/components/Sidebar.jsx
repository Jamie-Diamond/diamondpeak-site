import React, { useState, useEffect, useCallback } from 'react';
import WindCompass from './WindCompass.jsx';
import { loadSidebarInputs, saveSidebarInputs, clearCachedRides } from '../lib/storage.js';

const DEFAULTS = {
  mass_kg: 80,
  Crr: 0.004,
  Cm: 3.0,
  eta: 0.976,
  temp_C: 15,
  windMode: 'auto',
  wind_speed_kmh: 0,
  wind_dir_deg: 0,
  minDuration: 20,
  maxPowerCV: 15,
  maxSpeedCV: 8,
  maxGradientDeg: 3,
  minSpeed_ms: 5,
};

export default function Sidebar({
  onCalculate,
  onEstimateWind,
  windResult,
  hasSegments,
  hasRide,
  calculating,
  onParamsChange,
  rideData,
}) {
  const [inputs, setInputs] = useState(() => {
    const saved = loadSidebarInputs();
    if (saved) {
      // Reset stale filter defaults from earlier versions
      if (saved.maxPowerCV === 5) saved.maxPowerCV = DEFAULTS.maxPowerCV;
      if (saved.maxSpeedCV === 4) saved.maxSpeedCV = DEFAULTS.maxSpeedCV;
      if (saved.minDuration === 30) saved.minDuration = DEFAULTS.minDuration;
      if (saved.windMode === 'manual') saved.windMode = DEFAULTS.windMode;
    }
    return saved ? { ...DEFAULTS, ...saved } : DEFAULTS;
  });
  const [filtersOpen, setFiltersOpen] = useState(false);

  useEffect(() => {
    saveSidebarInputs(inputs);
    if (onParamsChange) onParamsChange(inputs);
  }, [inputs, onParamsChange]);

  // Auto-populate temperature from GPX data
  useEffect(() => {
    if (rideData?.hasTemp && rideData.meanTemp_C != null) {
      setInputs((prev) => ({
        ...prev,
        temp_C: Math.round(rideData.meanTemp_C),
      }));
    }
  }, [rideData]);

  const set = useCallback((key, value) => {
    setInputs((prev) => ({ ...prev, [key]: value }));
  }, []);

  const handleCalculate = () => {
    const params = {
      mass_kg: inputs.mass_kg,
      Crr: inputs.Crr,
      Cm: inputs.Cm,
      eta: inputs.eta,
      temp_C: inputs.temp_C,
    };
    const filters = {
      minDuration: inputs.minDuration,
      maxPowerCV: inputs.maxPowerCV,
      maxSpeedCV: inputs.maxSpeedCV,
      maxGradientDeg: inputs.maxGradientDeg,
      minSpeed_ms: inputs.minSpeed_ms,
    };

    if (inputs.windMode === 'auto') {
      // Auto mode: estimate wind first, then calculate
      onEstimateWind(params, filters, true); // true = autoCalc after
    } else {
      const wind = {
        speed_ms: inputs.wind_speed_kmh / 3.6,
        dir_deg: inputs.wind_dir_deg,
      };
      onCalculate(wind, params, filters);
    }
  };

  const handleEstimateWind = () => {
    const params = {
      mass_kg: inputs.mass_kg,
      Crr: inputs.Crr,
      Cm: inputs.Cm,
      eta: inputs.eta,
      temp_C: inputs.temp_C,
    };
    const filters = {
      minDuration: inputs.minDuration,
      maxPowerCV: inputs.maxPowerCV,
      maxSpeedCV: inputs.maxSpeedCV,
      maxGradientDeg: inputs.maxGradientDeg,
      minSpeed_ms: inputs.minSpeed_ms,
    };
    onEstimateWind(params, filters, false);
  };

  const acceptWind = () => {
    if (windResult?.feasible) {
      setInputs((prev) => ({
        ...prev,
        wind_speed_kmh: windResult.wind_speed_kmh,
        wind_dir_deg: windResult.wind_dir_deg,
        windMode: 'manual',
      }));
    }
  };

  return (
    <aside className="sidebar">
      {/* Rider & Bike */}
      <div className="sidebar-section">
        <div className="section-tag">Rider &amp; Bike</div>
        <div className="sidebar-field">
          <label>Total mass (kg)</label>
          <input
            type="number"
            value={inputs.mass_kg}
            onChange={(e) => set('mass_kg', parseFloat(e.target.value) || 0)}
            min={40}
            max={150}
            step={0.5}
          />
        </div>
        <div className="sidebar-field">
          <label>Crr</label>
          <input
            type="number"
            value={inputs.Crr}
            onChange={(e) => set('Crr', parseFloat(e.target.value) || 0)}
            min={0.001}
            max={0.015}
            step={0.0001}
          />
        </div>
        <div className="sidebar-field">
          <label>Cm (W/(m/s))</label>
          <input
            type="number"
            value={inputs.Cm}
            onChange={(e) => set('Cm', parseFloat(e.target.value) || 0)}
            min={0}
            max={10}
            step={0.1}
          />
        </div>
        <div className="sidebar-field">
          <label>Drivetrain η</label>
          <input
            type="number"
            value={inputs.eta}
            onChange={(e) => set('eta', parseFloat(e.target.value) || 0)}
            min={0.9}
            max={1.0}
            step={0.001}
          />
        </div>
        <div className="sidebar-field">
          <label>Temperature (°C){rideData?.hasTemp ? ' — from GPX' : ''}</label>
          <input
            type="number"
            value={inputs.temp_C}
            onChange={(e) => set('temp_C', parseFloat(e.target.value) || 0)}
            min={-10}
            max={45}
            step={1}
          />
        </div>
      </div>

      {/* Wind */}
      <div className="sidebar-section">
        <div className="section-tag">Wind</div>
        <div className="wind-toggle">
          <button
            className={`toggle-btn ${inputs.windMode === 'manual' ? 'active' : ''}`}
            onClick={() => set('windMode', 'manual')}
          >
            Manual
          </button>
          <button
            className={`toggle-btn ${inputs.windMode === 'auto' ? 'active' : ''}`}
            onClick={() => set('windMode', 'auto')}
          >
            Auto-estimate
          </button>
        </div>

        {inputs.windMode === 'manual' ? (
          <>
            <div className="sidebar-field">
              <label>Wind speed (km/h)</label>
              <input
                type="number"
                value={inputs.wind_speed_kmh}
                onChange={(e) => set('wind_speed_kmh', parseFloat(e.target.value) || 0)}
                min={0}
                max={80}
                step={0.5}
              />
            </div>
            <div className="sidebar-field">
              <label>Wind direction (°)</label>
              <input
                type="number"
                value={inputs.wind_dir_deg}
                onChange={(e) => set('wind_dir_deg', parseFloat(e.target.value) || 0)}
                min={0}
                max={359}
                step={1}
              />
            </div>
            <div className="compass-container">
              <WindCompass direction={inputs.wind_dir_deg} size={72} />
            </div>
          </>
        ) : (
          <div className="wind-auto">
            <button
              className="btn btn-secondary"
              onClick={handleEstimateWind}
              disabled={!hasSegments || calculating}
            >
              Estimate Wind
            </button>
            {windResult && windResult.feasible && (
              <div className="wind-result-card">
                <div className="wind-result-value">
                  {windResult.wind_speed_kmh} km/h from {windResult.wind_dir_deg}° ({windResult.wind_dir_cardinal})
                </div>
                <div className="wind-result-meta">
                  <span className={`confidence-badge confidence-${windResult.confidence_label.toLowerCase()}`}>
                    {windResult.confidence_label} ({windResult.confidence_pct}%)
                  </span>
                </div>
                <button className="btn btn-accent btn-sm" onClick={acceptWind}>
                  Accept
                </button>
              </div>
            )}
            {windResult && !windResult.feasible && (
              <div className="error-card">{windResult.reason}</div>
            )}
          </div>
        )}
      </div>

      {/* Segment Filters */}
      <div className="sidebar-section">
        <button className="section-tag clickable" onClick={() => setFiltersOpen(!filtersOpen)}>
          Segment Filters {filtersOpen ? '▾' : '▸'}
        </button>
        {filtersOpen && (
          <div className="filter-fields">
            <div className="sidebar-field">
              <label>Min duration (s)</label>
              <input
                type="number"
                value={inputs.minDuration}
                onChange={(e) => set('minDuration', parseInt(e.target.value) || 0)}
              />
            </div>
            <div className="sidebar-field">
              <label>Max power CV (%)</label>
              <input
                type="number"
                value={inputs.maxPowerCV}
                onChange={(e) => set('maxPowerCV', parseFloat(e.target.value) || 0)}
              />
            </div>
            <div className="sidebar-field">
              <label>Max speed CV (%)</label>
              <input
                type="number"
                value={inputs.maxSpeedCV}
                onChange={(e) => set('maxSpeedCV', parseFloat(e.target.value) || 0)}
              />
            </div>
            <div className="sidebar-field">
              <label>Max gradient (°)</label>
              <input
                type="number"
                value={inputs.maxGradientDeg}
                onChange={(e) => set('maxGradientDeg', parseFloat(e.target.value) || 0)}
              />
            </div>
            <div className="sidebar-field">
              <label>Min speed (m/s)</label>
              <input
                type="number"
                value={inputs.minSpeed_ms}
                onChange={(e) => set('minSpeed_ms', parseFloat(e.target.value) || 0)}
              />
            </div>
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="sidebar-actions">
        <button
          className="btn btn-primary btn-full"
          onClick={handleCalculate}
          disabled={!hasRide || calculating}
        >
          {calculating ? 'Calculating...' : 'Calculate CdA'}
        </button>
        <button className="btn-link" onClick={clearCachedRides}>
          Clear cached rides
        </button>
      </div>
    </aside>
  );
}
