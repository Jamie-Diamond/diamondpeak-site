import React, { useMemo, useState, useCallback } from 'react';

function formatTime(date) {
  if (!date) return '-';
  const d = new Date(date);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export default function SegmentTable({ results }) {
  const [sortCol, setSortCol] = useState('id');
  const [sortAsc, setSortAsc] = useState(true);

  const rows = useMemo(() => {
    if (!results) return [];
    return results.pairs.map((p) => ({
      id: p.segment.id + 1,
      time: p.segment.startTime,
      duration: Math.round(p.segment.duration_s),
      bearing: Math.round(p.segment.mean_bearing),
      speed: p.segment.mean_speed_kmh.toFixed(1),
      power: Math.round(p.segment.mean_power),
      gradient: p.segment.mean_gradient_pct.toFixed(1),
      cda: p.cda.toFixed(4),
      cdaNum: p.cda,
      isOutlier: p.isOutlier,
    }));
  }, [results]);

  const sorted = useMemo(() => {
    const arr = [...rows];
    arr.sort((a, b) => {
      let va = a[sortCol];
      let vb = b[sortCol];
      if (typeof va === 'string') va = parseFloat(va) || va;
      if (typeof vb === 'string') vb = parseFloat(vb) || vb;
      if (va < vb) return sortAsc ? -1 : 1;
      if (va > vb) return sortAsc ? 1 : -1;
      return 0;
    });
    return arr;
  }, [rows, sortCol, sortAsc]);

  const toggleSort = useCallback(
    (col) => {
      if (sortCol === col) setSortAsc(!sortAsc);
      else {
        setSortCol(col);
        setSortAsc(true);
      }
    },
    [sortCol, sortAsc]
  );

  const exportCSV = useCallback(() => {
    const headers = ['#', 'Time', 'Duration(s)', 'Bearing(°)', 'Speed(km/h)', 'Power(W)', 'Gradient(%)', 'CdA(m²)', 'Status'];
    const csvRows = [headers.join(',')];
    for (const r of sorted) {
      csvRows.push(
        [r.id, formatTime(r.time), r.duration, r.bearing, r.speed, r.power, r.gradient, r.cda, r.isOutlier ? 'outlier' : 'ok'].join(',')
      );
    }
    const blob = new Blob([csvRows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'cda_segments.csv';
    a.click();
    URL.revokeObjectURL(url);
  }, [sorted]);

  const columns = [
    { key: 'id', label: '#' },
    { key: 'time', label: 'Time' },
    { key: 'duration', label: 'Dur (s)' },
    { key: 'bearing', label: 'Bearing' },
    { key: 'speed', label: 'Speed' },
    { key: 'power', label: 'Power' },
    { key: 'gradient', label: 'Grad %' },
    { key: 'cda', label: 'CdA' },
  ];

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="section-tag">Segments</span>
        <button className="btn btn-sm btn-secondary" onClick={exportCSV}>
          Export CSV
        </button>
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              {columns.map((col) => (
                <th
                  key={col.key}
                  onClick={() => toggleSort(col.key)}
                  className="sortable"
                >
                  {col.label}
                  {sortCol === col.key && (sortAsc ? ' ↑' : ' ↓')}
                </th>
              ))}
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr key={r.id} className={r.isOutlier ? 'row-outlier' : ''}>
                <td>{r.id}</td>
                <td>{formatTime(r.time)}</td>
                <td>{r.duration}</td>
                <td>{r.bearing}°</td>
                <td>{r.speed}</td>
                <td>{r.power}</td>
                <td>{r.gradient}</td>
                <td className="cda-cell">{r.cda}</td>
                <td>{r.isOutlier ? '⚠' : '✓'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
