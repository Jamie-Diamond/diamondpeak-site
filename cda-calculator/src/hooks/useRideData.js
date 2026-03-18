import { useState, useCallback } from 'react';
import { parseGPX } from '../lib/gpxParser.js';
import { rideKey, getCachedRide, cacheRide } from '../lib/storage.js';

export function useRideData() {
  const [rideData, setRideData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [fromCache, setFromCache] = useState(false);

  const loadFile = useCallback(async (file) => {
    setLoading(true);
    setError(null);
    setFromCache(false);

    try {
      const key = rideKey(file.name, file.size);

      // Check cache first
      const cached = await getCachedRide(key);
      if (cached && cached.hasTemp !== undefined && cached.trackpoints?.[0]?.power_smooth !== undefined) {
        // Restore Date objects
        const trackpoints = cached.trackpoints.map((p) => ({
          ...p,
          time: new Date(p.time),
        }));
        setRideData({ ...cached, trackpoints, filename: file.name });
        setFromCache(true);
        setLoading(false);
        return;
      }

      // Parse from file
      const text = await file.text();
      const data = parseGPX(text, file.name);

      if (!data.hasPower) {
        throw new Error('No power data in GPX — a power meter is required for CdA calculation');
      }

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

      setRideData(data);
    } catch (err) {
      setError(err.message);
      setRideData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const clearRide = useCallback(() => {
    setRideData(null);
    setError(null);
    setFromCache(false);
  }, []);

  return { rideData, loading, error, fromCache, loadFile, clearRide };
}
