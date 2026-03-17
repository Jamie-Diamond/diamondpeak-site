import React, { useMemo } from 'react';
import { MapContainer, TileLayer, Polyline, Popup, useMap } from 'react-leaflet';

function FitBounds({ bounds }) {
  const map = useMap();
  React.useEffect(() => {
    if (bounds && bounds.length > 0) {
      map.fitBounds(bounds, { padding: [30, 30] });
    }
  }, [map, bounds]);
  return null;
}

export default function MapView({ trackpoints, segments, results }) {
  const trackCoords = useMemo(
    () => trackpoints.map((p) => [p.lat, p.lon]),
    [trackpoints]
  );

  const bounds = useMemo(() => {
    if (trackCoords.length === 0) return null;
    const lats = trackCoords.map((c) => c[0]);
    const lons = trackCoords.map((c) => c[1]);
    return [
      [Math.min(...lats), Math.min(...lons)],
      [Math.max(...lats), Math.max(...lons)],
    ];
  }, [trackCoords]);

  const segmentLines = useMemo(() => {
    if (!segments || !results) return [];
    return results.pairs.map((pair) => {
      const seg = pair.segment;
      const coords = trackpoints
        .slice(seg.startIdx, seg.endIdx + 1)
        .map((p) => [p.lat, p.lon]);
      return { coords, pair };
    });
  }, [segments, results, trackpoints]);

  if (trackCoords.length === 0) return null;

  return (
    <div className="panel map-panel">
      <div className="panel-header">
        <span className="section-tag">Map</span>
      </div>
      <div className="map-container">
        <MapContainer
          center={trackCoords[0]}
          zoom={13}
          style={{ height: '100%', width: '100%', borderRadius: '6px' }}
          scrollWheelZoom={true}
        >
          <TileLayer
            attribution='&copy; <a href="https://carto.com/">CARTO</a>'
            url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
          />
          <FitBounds bounds={bounds} />
          {/* Full track */}
          <Polyline positions={trackCoords} color="#666" weight={1.5} opacity={0.5} />
          {/* Segments */}
          {segmentLines.map(({ coords, pair }, i) => (
            <Polyline
              key={i}
              positions={coords}
              color={pair.isOutlier ? '#b87c20' : '#1d6840'}
              weight={3}
              opacity={0.85}
            >
              <Popup>
                <div style={{ fontFamily: "'DM Mono', monospace", fontSize: '11px' }}>
                  <strong>Segment #{pair.segment.id + 1}</strong>
                  <br />
                  CdA: {pair.cda.toFixed(4)} m²
                  <br />
                  Power: {Math.round(pair.segment.mean_power)} W
                  <br />
                  Speed: {pair.segment.mean_speed_kmh.toFixed(1)} km/h
                  <br />
                  Bearing: {Math.round(pair.segment.mean_bearing)}°
                </div>
              </Popup>
            </Polyline>
          ))}
        </MapContainer>
      </div>
    </div>
  );
}
