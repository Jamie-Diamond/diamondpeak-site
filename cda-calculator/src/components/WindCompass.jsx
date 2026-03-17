import React from 'react';

export default function WindCompass({ direction = 0, size = 80 }) {
  const r = size / 2;
  const arrowLen = r * 0.6;
  // Arrow points in the direction the wind is coming FROM
  const rad = (direction * Math.PI) / 180;
  const tipX = r + arrowLen * Math.sin(rad);
  const tipY = r - arrowLen * Math.cos(rad);
  const tailX = r - arrowLen * 0.3 * Math.sin(rad);
  const tailY = r + arrowLen * 0.3 * Math.cos(rad);

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ display: 'block' }}>
      {/* Outer circle */}
      <circle cx={r} cy={r} r={r - 2} fill="none" stroke="var(--border)" strokeWidth="1.5" />
      {/* Cardinal labels */}
      {[
        ['N', r, 12],
        ['E', size - 8, r + 4],
        ['S', r, size - 6],
        ['W', 8, r + 4],
      ].map(([label, x, y]) => (
        <text
          key={label}
          x={x}
          y={y}
          textAnchor="middle"
          fill="var(--muted)"
          fontSize="9"
          fontFamily="'DM Mono', monospace"
        >
          {label}
        </text>
      ))}
      {/* Arrow */}
      <line
        x1={tailX}
        y1={tailY}
        x2={tipX}
        y2={tipY}
        stroke="var(--green)"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      {/* Arrowhead */}
      <circle cx={tipX} cy={tipY} r="3" fill="var(--green)" />
      {/* Centre dot */}
      <circle cx={r} cy={r} r="2" fill="var(--muted)" />
    </svg>
  );
}
