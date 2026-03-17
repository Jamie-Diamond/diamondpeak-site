import { useState, useCallback } from 'react';
import { detectSegments } from '../lib/segmentDetection.js';

export function useSegments() {
  const [segments, setSegments] = useState([]);
  const [segmenting, setSegmenting] = useState(false);

  const runDetection = useCallback((trackpoints, filters) => {
    setSegmenting(true);
    // Use setTimeout to not block the UI
    setTimeout(() => {
      const result = detectSegments(trackpoints, filters);
      setSegments(result);
      setSegmenting(false);
    }, 0);
  }, []);

  const clearSegments = useCallback(() => {
    setSegments([]);
  }, []);

  return { segments, segmenting, runDetection, clearSegments };
}
