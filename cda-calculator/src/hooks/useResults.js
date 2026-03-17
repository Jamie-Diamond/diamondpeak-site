import { useState, useCallback } from 'react';
import { computeAllSegments } from '../lib/physics.js';
import { estimateWind } from '../lib/windEstimator.js';

function median(arr) {
  const sorted = [...arr].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function stdDev(arr) {
  if (arr.length < 2) return 0;
  const m = arr.reduce((s, v) => s + v, 0) / arr.length;
  return Math.sqrt(arr.reduce((s, v) => s + (v - m) ** 2, 0) / (arr.length - 1));
}

export function useResults() {
  const [results, setResults] = useState(null);
  const [windResult, setWindResult] = useState(null);
  const [calculating, setCalculating] = useState(false);
  const [error, setError] = useState(null);

  const calculate = useCallback((segments, wind, params) => {
    setCalculating(true);
    setError(null);

    setTimeout(() => {
      try {
        const pairs = computeAllSegments(segments, wind, params);

        if (pairs.length === 0) {
          setError('All segments filtered out — check gradient and speed thresholds');
          setCalculating(false);
          return;
        }

        const cdaValues = pairs.map((p) => p.cda);
        const med = median(cdaValues);
        const sd = stdDev(cdaValues);

        // Mark outliers (>2σ from median)
        const enriched = pairs.map((p) => ({
          ...p,
          isOutlier: Math.abs(p.cda - med) > 2 * sd,
        }));

        setResults({
          pairs: enriched,
          median: med,
          stdDev: sd,
          count: pairs.length,
        });
      } catch (err) {
        setError(err.message);
      } finally {
        setCalculating(false);
      }
    }, 0);
  }, []);

  const runWindEstimation = useCallback((segments, params) => {
    setCalculating(true);
    setError(null);

    setTimeout(() => {
      try {
        const result = estimateWind(segments, params);
        setWindResult(result);
        if (!result.feasible) {
          setError(result.reason);
        }
      } catch (err) {
        setError('Wind estimation failed to converge — try manual wind, or check ride data');
      } finally {
        setCalculating(false);
      }
    }, 0);
  }, []);

  const clearResults = useCallback(() => {
    setResults(null);
    setWindResult(null);
    setError(null);
  }, []);

  return { results, windResult, calculating, error, calculate, runWindEstimation, clearResults, setWindResult };
}
