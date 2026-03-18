import React, { useState, useCallback, useRef } from 'react';
import { useRideData } from './hooks/useRideData.js';
import { useSegments } from './hooks/useSegments.js';
import { useResults } from './hooks/useResults.js';
import { fetchRideWeather } from './lib/weatherFetch.js';
import FileUpload from './components/FileUpload.jsx';
import Sidebar from './components/Sidebar.jsx';
import ResultsPanel from './components/ResultsPanel.jsx';
import MapView from './components/MapView.jsx';
import Timeline from './components/Timeline.jsx';
import Histogram from './components/Histogram.jsx';
import SegmentTable from './components/SegmentTable.jsx';
import SensitivityTable from './components/SensitivityTable.jsx';
import './App.css';

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
  const { rides, loading: rideLoading, error: rideError, loadFiles, removeRide, clearAllRides } = useRideData();
  const { segments, segmenting, runDetection, clearSegments } = useSegments();
  const { results, windResult, calculating, error: calcError, calculate, runWindEstimation, clearResults } = useResults();

  const [wind, setWind] = useState(null);
  const [params, setParams] = useState(null);
  const [weatherMap, setWeatherMap] = useState({});
  const [weatherLoading, setWeatherLoading] = useState(false);
  const paramsRef = useRef(null);

  const handleParamsChange = useCallback((inputs) => {
    paramsRef.current = inputs;
  }, []);

  // Load files
  const handleFileLoad = useCallback(
    async (files) => {
      clearResults();
      clearSegments();
      await loadFiles(files);
    },
    [loadFiles, clearResults, clearSegments]
  );

  // Add more files without clearing existing
  const handleAddFiles = useCallback(
    async (files) => {
      clearResults();
      clearSegments();
      await loadFiles(files);
    },
    [loadFiles, clearResults, clearSegments]
  );

  // Fetch weather for all rides that don't have it yet
  React.useEffect(() => {
    if (rides.length === 0) return;
    const missing = rides.filter((r) => !weatherMap[r.id]);
    if (missing.length === 0) return;

    setWeatherLoading(true);
    Promise.all(
      missing.map(async (ride) => {
        const w = await fetchRideWeather(ride);
        return { id: ride.id, weather: w };
      })
    ).then((results) => {
      setWeatherMap((prev) => {
        const next = { ...prev };
        for (const { id, weather } of results) {
          if (weather) next[id] = weather;
        }
        return next;
      });
      setWeatherLoading(false);
    });
  }, [rides]);

  // Auto-detect segments when rides or weather change
  React.useEffect(() => {
    if (rides.length > 0 && paramsRef.current) {
      const inputs = paramsRef.current;
      runDetection(rides, {
        minDuration: inputs.minDuration,
        maxPowerCV: inputs.maxPowerCV,
        maxSpeedCV: inputs.maxSpeedCV,
        maxGradientDeg: inputs.maxGradientDeg,
        minSpeed_ms: inputs.minSpeed_ms,
      }, weatherMap);
    }
  }, [rides, weatherMap, runDetection]);

  const pendingCalcRef = useRef(null);

  const handleCalculateClick = useCallback(
    (windVec, paramSet, filters) => {
      if (rides.length === 0) return;
      setWind(windVec);
      setParams(paramSet);
      pendingCalcRef.current = { windVec, paramSet };
      runDetection(rides, filters, weatherMap);
    },
    [rides, weatherMap, runDetection]
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
      if (rides.length === 0) return;
      setParams(paramSet);
      pendingWindRef.current = { paramSet, autoCalc };
      if (segments.length > 0) {
        runWindEstimation(segments, paramSet);
      } else {
        runDetection(rides, filters, weatherMap);
      }
    },
    [rides, segments, weatherMap, runDetection, runWindEstimation]
  );

  const pendingWindRef = useRef(null);
  React.useEffect(() => {
    if (segments.length > 0 && pendingWindRef.current && !pendingCalcRef.current) {
      const { paramSet } = pendingWindRef.current;
      runWindEstimation(segments, paramSet);
    }
  }, [segments, runWindEstimation]);

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

  // For map/timeline, use first ride's trackpoints
  const primaryRide = rides[0] || null;
  const allTrackpoints = primaryRide ? primaryRide.trackpoints : [];

  // Determine app state
  let appState = 'IDLE';
  if (rideLoading) appState = 'FILE_LOADING';
  else if (rideError) appState = 'ERROR';
  else if (rides.length > 0 && segmenting) appState = 'SEGMENTING';
  else if (rides.length > 0 && segments.length > 0 && !results) appState = 'READY';
  else if (rides.length > 0 && !results) appState = 'PARSED';
  else if (calculating) appState = 'CALCULATING';
  else if (results) appState = 'RESULTS';

  const error = rideError || calcError;

  const totalSegments = segments.length;
  const totalDistance = rides.reduce((s, r) => s + r.distanceM, 0);
  const totalDuration = rides.reduce((s, r) => s + r.durationS, 0);

  return (
    <div className="app">
      <header className="app-header">
        <a href="/" className="back">← diamondpeak.uk</a>
        <h1 className="app-title">CdA Calculator</h1>
        <p className="app-subtitle">Aerodynamic drag analysis from ride data</p>
      </header>

      <div className="app-layout">
        {rides.length > 0 && (
          <Sidebar
            onCalculate={handleCalculateClick}
            onEstimateWind={handleEstimateWind}
            windResult={windResult}
            hasSegments={segments.length > 0}
            hasRides={rides.length > 0}
            calculating={calculating || segmenting}
            onParamsChange={handleParamsChange}
            rides={rides}
            weatherMap={weatherMap}
            weatherLoading={weatherLoading}
          />
        )}

        <main className="main-content">
          {/* Upload — always show when no rides or as add-more */}
          {rides.length === 0 && (
            <div className="upload-section">
              <FileUpload onFiles={handleFileLoad} disabled={rideLoading} />
              <div className="upload-info">
                <p>Upload GPX files with power data from your rides.</p>
                <p className="upload-info-sub">Multiple files supported — each ride uses its own weather data. All processing happens in your browser.</p>
              </div>
            </div>
          )}

          {/* Loading */}
          {appState === 'FILE_LOADING' && (
            <div className="status-card">
              <div className="spinner" />
              <span>Parsing GPX files...</span>
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="error-card">
              {error}
            </div>
          )}

          {/* Rides summary */}
          {rides.length > 0 && (
            <div className="ride-summary">
              <div className="ride-summary-header">
                <span className="ride-filename">
                  {rides.length} ride{rides.length > 1 ? 's' : ''} loaded
                </span>
                <button className="btn-link" onClick={() => { clearAllRides(); clearSegments(); clearResults(); setWeatherMap({}); }}>
                  Clear all
                </button>
              </div>

              {/* Per-ride cards */}
              {rides.map((ride) => (
                <div key={ride.id} className="ride-card">
                  <div className="ride-card-header">
                    <span className="ride-card-name">{ride.filename}</span>
                    {rides.length > 1 && (
                      <button className="btn-link btn-sm" onClick={() => { removeRide(ride.id); clearResults(); clearSegments(); }}>
                        remove
                      </button>
                    )}
                  </div>
                  <div className="ride-stats ride-stats-compact">
                    <div className="ride-stat">
                      <span className="ride-stat-label">Dist</span>
                      <span className="ride-stat-value">{formatDistance(ride.distanceM)}</span>
                    </div>
                    <div className="ride-stat">
                      <span className="ride-stat-label">Dur</span>
                      <span className="ride-stat-value">{formatDuration(ride.durationS)}</span>
                    </div>
                    <div className="ride-stat">
                      <span className="ride-stat-label">Pts</span>
                      <span className="ride-stat-value">{ride.pointCount.toLocaleString()}</span>
                    </div>
                    {ride.hasTemp && (
                      <div className="ride-stat">
                        <span className="ride-stat-label">Temp</span>
                        <span className="ride-stat-value">{Math.round(ride.meanTemp_C)}°C</span>
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {/* Add more files */}
              <div className="add-rides">
                <label className="btn-link add-rides-btn">
                  + Add more rides
                  <input
                    type="file"
                    accept=".gpx"
                    multiple
                    onChange={(e) => handleAddFiles([...e.target.files])}
                    style={{ display: 'none' }}
                  />
                </label>
              </div>

              {/* Totals */}
              {rides.length > 1 && (
                <div className="ride-stats">
                  <div className="ride-stat">
                    <span className="ride-stat-label">Total dist</span>
                    <span className="ride-stat-value">{formatDistance(totalDistance)}</span>
                  </div>
                  <div className="ride-stat">
                    <span className="ride-stat-label">Total dur</span>
                    <span className="ride-stat-value">{formatDuration(totalDuration)}</span>
                  </div>
                  {totalSegments > 0 && (
                    <div className="ride-stat">
                      <span className="ride-stat-label">Segments</span>
                      <span className="ride-stat-value">{totalSegments}</span>
                    </div>
                  )}
                </div>
              )}
              {rides.length === 1 && totalSegments > 0 && (
                <div className="ride-stats">
                  <div className="ride-stat">
                    <span className="ride-stat-label">Segments</span>
                    <span className="ride-stat-value">{totalSegments}</span>
                  </div>
                </div>
              )}
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
          {rides.length > 0 && !segmenting && segments.length > 0 && segments.length < 8 && !results && (
            <div className="warn-card">
              Only {segments.length} valid segments found (minimum 8 recommended) — try adding more rides or relaxing filter thresholds.
            </div>
          )}

          {/* Results */}
          {results && (
            <>
              <ResultsPanel results={results} wind={wind || { speed_ms: 0, dir_deg: 0 }} />

              {allTrackpoints.length > 0 && (
                <div className="results-grid">
                  <MapView trackpoints={allTrackpoints} segments={segments} results={results} />
                  <Timeline trackpoints={allTrackpoints} segments={segments} results={results} />
                </div>
              )}

              <div className="results-grid">
                <Histogram results={results} />
                <SensitivityTable
                  segments={segments}
                  wind={wind || { speed_ms: 0, dir_deg: 0 }}
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
