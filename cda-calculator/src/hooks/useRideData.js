import { useState, useCallback } from 'react';
import { parseGPX } from '../lib/gpxParser.js';
import { rideKey, getCachedRide, cacheRide } from '../lib/storage.js';

export function useRideData() {
  const [rides, setRides] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const loadFiles = useCallback(async (files) => {
    setLoading(true);
    setError(null);

    const newRides = [];

    for (const file of files) {
      try {
        const key = rideKey(file.name, file.size);

        // Check cache first
        const cached = await getCachedRide(key);
        if (cached && cached.hasTemp !== undefined && cached.trackpoints?.[0]?.power_smooth !== undefined) {
          const trackpoints = cached.trackpoints.map((p) => ({
            ...p,
            time: new Date(p.time),
          }));
          newRides.push({ ...cached, trackpoints, filename: file.name, id: key, fromCache: true });
          continue;
        }

        // Parse from file
        const text = await file.text();
        const data = parseGPX(text, file.name);

        if (!data.hasPower) {
          setError(`${file.name}: No power data — a power meter is required`);
          continue;
        }

        const rideObj = {
          id: key,
          filename: data.filename,
          hasPower: data.hasPower,
          hasTemp: data.hasTemp,
          meanTemp_C: data.meanTemp_C,
          pointCount: data.pointCount,
          durationS: data.durationS,
          distanceM: data.distanceM,
          elevationGainM: data.elevationGainM,
          trackpoints: data.trackpoints,
          fromCache: false,
        };

        // Cache for future use
        await cacheRide(key, {
          filename: data.filename,
          hasPower: data.hasPower,
          hasTemp: data.hasTemp,
          meanTemp_C: data.meanTemp_C,
          pointCount: data.pointCount,
          durationS: data.durationS,
          distanceM: data.distanceM,
          elevationGainM: data.elevationGainM,
          trackpoints: data.trackpoints,
        });

        newRides.push(rideObj);
      } catch (err) {
        setError(`${file.name}: ${err.message}`);
      }
    }

    // Add new rides, dedup by id
    setRides((prev) => {
      const existing = new Set(prev.map((r) => r.id));
      const unique = newRides.filter((r) => !existing.has(r.id));
      return [...prev, ...unique];
    });

    setLoading(false);
  }, []);

  const removeRide = useCallback((id) => {
    setRides((prev) => prev.filter((r) => r.id !== id));
  }, []);

  const clearAllRides = useCallback(() => {
    setRides([]);
    setError(null);
  }, []);

  return { rides, loading, error, loadFiles, removeRide, clearAllRides };
}
