import { useState, useCallback } from 'react';
import { detectSegments } from '../lib/segmentDetection.js';

export function useSegments() {
  const [segments, setSegments] = useState([]);
  const [segmenting, setSegmenting] = useState(false);

  const runDetection = useCallback((rides, filters, weatherMap = {}) => {
    setSegmenting(true);
    setTimeout(() => {
      let globalId = 0;
      const allSegments = [];

      for (const ride of rides) {
        const rideSegments = detectSegments(ride.trackpoints, filters);
        const weather = weatherMap[ride.id];
        const wind = weather
          ? { speed_ms: weather.wind_speed_kmh / 3.6, dir_deg: weather.wind_dir_deg }
          : null;

        for (const seg of rideSegments) {
          allSegments.push({
            ...seg,
            id: globalId++,
            rideId: ride.id,
            rideFilename: ride.filename,
            wind,
          });
        }
      }

      setSegments(allSegments);
      setSegmenting(false);
    }, 0);
  }, []);

  const clearSegments = useCallback(() => {
    setSegments([]);
  }, []);

  return { segments, segmenting, runDetection, clearSegments };
}
