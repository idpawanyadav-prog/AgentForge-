import React from 'react';

/**
 * Offline/degraded-mode helpers.
 *
 * - `CACHE_KEY`: localStorage key for the last-known-good summary payload.
 * - `cacheResponse`: writes a JSON payload with a timestamp.
 * - `getCachedResponse`: returns the cached payload (and its age in ms) or null.
 * - `isApiReachable`: lightweight probe (GET /api/v1/health or /api/v1/settings).
 * - `DisconnectedBanner`: renders when the API is unreachable.
 */

const CACHE_KEY = 'af.api_cache';

export function cacheResponse(data: unknown): void {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ data, ts: Date.now() }));
  } catch { /* private mode */ }
}

export interface CachedResponse {
  data: unknown;
  ts: number;
}

export function getCachedResponse(): CachedResponse | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as CachedResponse;
  } catch { return null; }
}

export function isApiReachable(): Promise<boolean> {
  return fetch('/api/v1/health', { method: 'GET', signal: AbortSignal.timeout?.(3000) as any })
    .then((r) => r.ok)
    .catch(() => fetch('/api/v1/settings', { method: 'GET', signal: AbortSignal.timeout?.(3000) as any })
      .then((r) => r.ok)
      .catch(() => false));
}

export function DisconnectedBanner({ onRetry }: { onRetry: () => void }) {
  const cached = getCachedResponse();
  const age = cached ? Math.round((Date.now() - cached.ts) / 1000) : 0;
  return (
    <div style={{
      background: 'var(--err, #ff7b72)', color: '#fff', padding: '8px 16px',
      fontSize: 13, display: 'flex', alignItems: 'center', gap: 12,
    }}>
      <span>⚠ API unreachable — showing stale cached data ({age}s old)</span>
      <button className="btn small" onClick={onRetry} style={{ background: 'rgba(255,255,255,0.2)', borderColor: 'rgba(255,255,255,0.4)' }}>
        Retry
      </button>
    </div>
  );
}
