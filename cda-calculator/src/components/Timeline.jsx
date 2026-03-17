import React, { useMemo } from 'react';
import {
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceArea,
  ResponsiveContainer,
} from 'recharts';

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export default function Timeline({ trackpoints, segments, results }) {
  const data = useMemo(() => {
    // Downsample for performance
    const step = Math.max(1, Math.floor(trackpoints.length / 800));
    return trackpoints
      .filter((_, i) => i % step === 0)
      .map((p) => ({
        time: p.elapsed_s,
        power: p.power != null ? Math.round(p.power) : null,
        speed: p.v_ground * 3.6,
      }));
  }, [trackpoints]);

  const segmentAreas = useMemo(() => {
    if (!results) return [];
    return results.pairs.map((pair) => {
      const seg = pair.segment;
      const start = trackpoints[seg.startIdx]?.elapsed_s || 0;
      const end = trackpoints[seg.endIdx]?.elapsed_s || 0;
      return { x1: start, x2: end, cda: pair.cda };
    });
  }, [results, trackpoints]);

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="section-tag">Timeline</span>
      </div>
      <div className="chart-container">
        <ResponsiveContainer width="100%" height={220}>
          <ComposedChart data={data} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
            <XAxis
              dataKey="time"
              tickFormatter={formatTime}
              stroke="var(--muted)"
              fontSize={9}
              fontFamily="'DM Mono', monospace"
            />
            <YAxis
              yAxisId="power"
              orientation="left"
              stroke="var(--muted)"
              fontSize={9}
              fontFamily="'DM Mono', monospace"
              label={{ value: 'W', position: 'insideTopLeft', fontSize: 9, fill: 'var(--muted)' }}
            />
            <YAxis
              yAxisId="speed"
              orientation="right"
              stroke="var(--muted)"
              fontSize={9}
              fontFamily="'DM Mono', monospace"
              label={{ value: 'km/h', position: 'insideTopRight', fontSize: 9, fill: 'var(--muted)' }}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--ink)',
                border: 'none',
                borderRadius: '6px',
                color: '#fff',
                fontFamily: "'DM Mono', monospace",
                fontSize: '10px',
              }}
              labelFormatter={formatTime}
            />
            {segmentAreas.map((sa, i) => (
              <ReferenceArea
                key={i}
                yAxisId="power"
                x1={sa.x1}
                x2={sa.x2}
                fill="var(--green)"
                fillOpacity={0.12}
                stroke="var(--green)"
                strokeOpacity={0.3}
              />
            ))}
            <Area
              yAxisId="power"
              type="monotone"
              dataKey="power"
              stroke="none"
              fill="var(--muted)"
              fillOpacity={0.2}
            />
            <Line
              yAxisId="speed"
              type="monotone"
              dataKey="speed"
              stroke="var(--green)"
              strokeWidth={1.5}
              dot={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
