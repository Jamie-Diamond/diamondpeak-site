import React, { useMemo } from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
  Cell,
} from 'recharts';

export default function Histogram({ results }) {
  const { bins, median } = useMemo(() => {
    if (!results) return { bins: [], median: 0 };
    const cdas = results.pairs.map((p) => p.cda);
    const med = results.median;
    const min = Math.min(...cdas);
    const max = Math.max(...cdas);
    const binWidth = 0.005;
    const binStart = Math.floor(min / binWidth) * binWidth;
    const binEnd = Math.ceil(max / binWidth) * binWidth;
    const numBins = Math.max(1, Math.round((binEnd - binStart) / binWidth));

    const bins = [];
    for (let i = 0; i < numBins; i++) {
      const lo = binStart + i * binWidth;
      const hi = lo + binWidth;
      const count = cdas.filter((v) => v >= lo && v < hi).length;
      const center = lo + binWidth / 2;
      const distFromMedian = Math.abs(center - med);
      bins.push({
        label: center.toFixed(3),
        count,
        distFromMedian,
      });
    }
    return { bins, median: med };
  }, [results]);

  if (bins.length === 0) return null;

  const maxDist = Math.max(...bins.map((b) => b.distFromMedian), 0.001);

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="section-tag">CdA Distribution</span>
      </div>
      <div className="chart-container">
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={bins} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
            <XAxis
              dataKey="label"
              stroke="var(--muted)"
              fontSize={9}
              fontFamily="'DM Mono', monospace"
            />
            <YAxis
              stroke="var(--muted)"
              fontSize={9}
              fontFamily="'DM Mono', monospace"
              allowDecimals={false}
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
            />
            <ReferenceLine
              x={median.toFixed(3)}
              stroke="var(--green)"
              strokeWidth={2}
              strokeDasharray="4 2"
              label={{
                value: `median ${median.toFixed(3)}`,
                position: 'top',
                fill: 'var(--green)',
                fontSize: 9,
                fontFamily: "'DM Mono', monospace",
              }}
            />
            <Bar dataKey="count" radius={[3, 3, 0, 0]}>
              {bins.map((b, i) => {
                const ratio = 1 - b.distFromMedian / maxDist;
                const r = Math.round(29 + (154 - 29) * (1 - ratio));
                const g = Math.round(104 + (144 - 104) * (1 - ratio));
                const bl = Math.round(64 + (128 - 64) * (1 - ratio));
                return <Cell key={i} fill={`rgb(${r},${g},${bl})`} />;
              })}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
