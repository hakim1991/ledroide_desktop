import React, { useState } from 'react'

const BACKEND_PORT = window.ledroide?.backendPort ?? 8765

export default function ModuleIcon({ id, fallback, size = 20, className = '' }) {
  const [failed, setFailed] = useState(false)
  if (failed) {
    return (
      <span className={`module-icon-emoji ${className}`} style={{ fontSize: size * 0.85 }}>
        {fallback ?? '⬡'}
      </span>
    )
  }
  return (
    <img
      className={`module-icon-img ${className}`}
      src={`http://127.0.0.1:${BACKEND_PORT}/modules/${id}/icon`}
      alt=""
      width={size}
      height={size}
      onError={() => setFailed(true)}
      style={{ width: size, height: size }}
    />
  )
}
