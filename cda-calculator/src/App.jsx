import React, { useState, useCallback, useRef } from 'react';
import { useRideData } from './hooks/useRideData.js';
import { useSegments } from './hooks/useSegments.js';
import { useResults } from './hooks/useResults.js';
import FileUpload from './components/FileUpload.jsx';
import Sidebar from './components/Sidebar.jsx';
import ResultsPanel from './components/ResultsPanel.jsx';
import MapView from './components/MapView.jsx';
import Timeline from './components/Timeline.jsx';
import Histogram from './components/Histogram.jsx';
import SegmentTable from './components/SegmentTable.jsx';
import SensitivityTable from './components/SensitivityTable.jsx';
import './App.css';

// State machine: IDLE → FILE_LOADING → PARSED/CACHE_HIT → READY → CALCULATING → RESULTS
// ERROR can occur from any state

function formatDuration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h > 0 ? `${h}h ${m}m ${s}s` : `${m}m ${s}s`;
}

function formatDistance(metres) {
  return (metres / 1000).toFixed(1) + ' km';
}

export default function App() {
  const { rideData, loading: rideLoading, error: rideError, fromCache, loadFile, clearRide } = useRideData();
  const { segments, segmenting, runDetection, clearSegments } = useSegments();
  const { results, windResult, calculating, error: calcError, calculate, runWindEstimation, clearResults, setWindResult } = useResults();

  const [wind, setWind] = useState(null);
  const [params, setParams] = useState(null);
  const paramsRef = useRef(null);

  const handleParamsChange = useCallback((inputs) => {
    paramsRef.current = inputs;
  }, []);

  // When ride is loaded, auto-detect segments
  const handleFileLoad = useCallback(
    async (file) => {
      clearResults();
      clearSegments();
      await loadFile(file);
    },
    [loadFile, clearResults, clearSegments]
  );

  // After rideData changes, auto-run segment detection
  React.useEffect(() => {
    if (rideData && rideData.trackpoints.length > 0 && paramsRef.current) {
      const inputs = paramsRef.current;
      runDetection(rideData.trackpoints, {
        minDuration: inputs.minDuration,
        maxPowerCV: inputs.maxPowerCV,
        maxSpeedCV: inputs.maxSpeedCV,
        maxGradientDeg: inputs.maxGradientDeg,
        minSpeed_ms: inputs.minSpeed_ms,
      });
    }
  }, [rideData, runDetection]);

  const pendingCalcRef = useRef(null);

  const handleCalculateClick = useCallback(
    (windVec, paramSet, filters) => {
      if (!rideData) return;
      setWind(windVec);
      setParams(paramSet);
      pendingCalcRef.current = { windVec, paramSet };
      runDetection(rideData.trackpoints, filters);
    },
    [rideData, runDetection]
  );

  React.useEffect(() => {
    if (segments.length > 0 && pendingCalcRef.current) {
      const { windVec, paramSet } = pendingCalcRef.current;
      pendingCalcRef.current = null;
      calculate(segments, windVec, paramSet);
    }
  }, [segments, calculate]);

  const handleEstimateWind = useCallback(
    (paramSet, filters, autoCalc = false) => {
      if (!rideData) return;
      setParams(paramSet);
      pendingWindRef.current = { paramSet, autoCalc };
      if (segments.length > 0) {
        runWindEstimation(segments, paramSet);
      } else {
        runDetection(rideData.trackpoints, filters);
      }
    },
    [rideData, segments, runDetection, runWindEstimation]
  );

  const pendingWindRef = useRef(null);
  React.useEffect(() => {
    if (segments.length > 0 && pendingWindRef.current && !pendingCalcRef.current) {
      const { paramSet } = pendingWindRef.current;
      runWindEstimation(segments, paramSet);
    }
  }, [segments, runWindEstimation]);

  // When wind estimation completes and autoCalc was requested, trigger calculation
  React.useEffect(() => {
    if (windResult?.feasible && pendingWindRef.current?.autoCalc && segments.length > 0) {
      const { paramSet } = pendingWindRef.current;
      pendingWindRef.current = null;
      const windVec = {
        speed_ms: windResult.wind_speed_ms,
        dir_deg: windResult.wind_dir_deg,
      };
      setWind(windVec);
      calculate(segments, windVec, paramSet);
    } else if (windResult && !windResult.feasible && pendingWindRef.current?.autoCalc) {
      pendingWindRef.current = null;
    }
  }, [windResult, segments, calculate]);

  // Determine app state
  let appState = 'IDLE';
  if (rideLoading) appState = 'FILE_LOADING';
  else if (rideError) appState = 'ERROR';
  else if (rideData && fromCache) appState = segments.length > 0 ? 'READY' : 'CACHE_HIT';
  else if (rideData && segmenting) appState = 'SEGMENTING';
  else if (rideData && segments.length > 0 && !results) appState = 'READY';
  else if (rideData && !results) appState = 'PARSED';
  else if (calculating) appState = 'CALCULATING';
  else if (results) appState = 'RESULTS';

  const error = rideError || calcError;

  return (
    <div className="app">
      <header className="app-header">
        <a href="/" className="back">← diamondpeak.uk</a>
        <h1 className="app-title">CdA Calculator</h1>
        <p className="app-subtitle">Aerodynamic drag analysis from ride data</p>
      </header>

      <div className="app-layout">
        {/* Sidebar — only show after a file is loaded */}
        {rideData && (
          <Sidebar
            onCalculate={handleCalculateClick}
            onEstimateWind={handleEstimateWind}
            windResult={windResult}
            hasSegments={segments.length > 0}
            hasRide={!!rideData}
            calculating={calculating || segmenting}
            onParamsChange={handleParamsChange}
            rideData={rideData}
          />
        )}

        <main className="main-content">
          {/* IDLE / Upload */}
          {appState === 'IDLE' && (
            <div className="upload-section">
              <FileUpload onFile={handleFileLoad} disabled={rideLoading} />
              <div className="upload-info">
                <p>Upload a GPX file with power data from your ride.</p>
                <p className="upload-info-sub">All processing happens in your browser — no data leaves your device.</p>
              </div>
            </div>
          )}

          {/* Loading */}
          {appState === 'FILE_LOADING' && (
            <div className="status-card">
              <div className="spinner" />
              <span>Parsing GPX file...</span>
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="error-card">
              {error}
            </div>
          )}

          {/* Ride summary */}
          {rideData && (
            <div className="ride-summary">
              <div className="ride-summary-header">
                <span className="ride-filename">{rideData.filename}</span>
                {fromCache && <span className="cache-badge">Loaded from cache</span>}
                <button className="btn-link" onClick={() => { clearRide(); clearSegments(); clearResults(); }}>
                  New file
                </button>
              </div>
              <div className="ride-stats">
                <div className="ride-stat">
                  <span className="ride-stat-label">Distance</span>
                  <span className="ride-stat-value">{formatDistance(rideData.distanceM)}</span>
                </div>
                <div className="ride-stat">
                  <span className="ride-stat-label">Duration</span>
                  <span className="ride-stat-value">{formatDuration(rideData.durationS)}</span>
                </div>
                <div className="ride-stat">
                  <span className="ride-stat-label">Points</span>
                  <span className="ride-stat-value">{rideData.pointCount.toLocaleString()}</span>
                </div>
                <div className="ride-stat">
                  <span className="ride-stat-label">Elevation</span>
                  <span className="ride-stat-value">{Math.round(rideData.elevationGainM)} m</span>
                </div>
                {rideData.hasTemp && (
                  <div className="ride-stat">
                    <span className="ride-stat-label">Avg Temp</span>
                    <span className="ride-stat-value">{Math.round(rideData.meanTemp_C)}°C</span>
                  </div>
                )}
                {segments.length > 0 && (
                  <div className="ride-stat">
                    <span className="ride-stat-label">Segments</span>
                    <span className="ride-stat-value">{segments.length}</span>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Segmenting */}
          {segmenting && (
            <div className="status-card">
              <div className="spinner" />
              <span>Detecting segments...</span>
            </div>
          )}

          {/* Calculating */}
          {calculating && (
            <div className="status-card">
              <div className="spinner" />
              <span>Calculating CdA...</span>
            </div>
          )}

          {/* Segment warning */}
          {rideData && !segmenting && segments.length > 0 && segments.length < 8 && !results && (
            <div className="warn-card">
              Only {segments.length} valid segments found (minimum 8 recommended for wind estimation) — try relaxing the filter thresholds.
            </div>
          )}

          {/* Results */}
          {results && wind && (
            <>
              <ResultsPanel results={results} wind={wind} />

              <div className="results-grid">
                <MapView trackpoints={rideData.trackpoints} segments={segments} results={results} />
                <Timeline trackpoints={rideData.trackpoints} segments={segments} results={results} />
              </div>

              <div className="results-grid">
                <Histogram results={results} />
                <SensitivityTable
                  segments={segments}
                  wind={wind}
                  params={params}
                  baseCdA={results.median}
                />
              </div>

              <SegmentTable results={results} />
            </>
          )}
        </main>
      </div>
    </div>
  );
}
