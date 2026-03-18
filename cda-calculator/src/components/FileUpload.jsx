import React, { useCallback, useState } from 'react';

export default function FileUpload({ onFiles, disabled }) {
  const [dragOver, setDragOver] = useState(false);

  const handleDrop = useCallback(
    (e) => {
      e.preventDefault();
      setDragOver(false);
      const files = [...(e.dataTransfer?.files || [])].filter((f) =>
        f.name.toLowerCase().endsWith('.gpx')
      );
      if (files.length > 0) onFiles(files);
    },
    [onFiles]
  );

  const handleChange = useCallback(
    (e) => {
      const files = [...(e.target.files || [])];
      if (files.length > 0) onFiles(files);
    },
    [onFiles]
  );

  return (
    <div
      className={`file-upload ${dragOver ? 'drag-over' : ''}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
    >
      <div className="file-upload-icon">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--muted)" strokeWidth="1.5">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="17 8 12 3 7 8" />
          <line x1="12" y1="3" x2="12" y2="15" />
        </svg>
      </div>
      <p className="file-upload-label">Drop GPX files here</p>
      <p className="file-upload-sub">or click to browse — multiple files supported</p>
      <input
        type="file"
        accept=".gpx"
        multiple
        onChange={handleChange}
        disabled={disabled}
        className="file-upload-input"
      />
    </div>
  );
}
