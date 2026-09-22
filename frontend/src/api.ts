/**
 * List endpoints return a paginated envelope `{ total, items, limit, offset }`
 * while the rest of the app consumes plain arrays. Unwrap that envelope here
 * so every consumer keeps working without touching each call site. Anything
 * that is not an envelope (plain arrays, single objects, summaries) passes
 * through unchanged.
 */
function unwrapEnvelope<T>(data: unknown): unknown {
  if (
    data &&
    typeof data === 'object' &&
    !Array.isArray(data) &&
    Array.isArray((data as any).items)
  ) {
    return (data as any).items as T;
  }
  return data as T;
}

export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
      if (Array.isArray(detail)) {
        detail = detail.map((d: any) => d.msg || JSON.stringify(d)).join('; ');
      }
    } catch { /* ignore */ }
    throw new Error(String(detail));
  }
  return unwrapEnvelope(await res.json()) as T;
}

export interface Page<T> {
  total: number;
  items: T[];
  limit: number;
  offset: number;
}

export const get = <T = any>(path: string) => api<T>(path);
export const getPage = <T = any>(path: string) =>
  fetch(path, { headers: { 'Content-Type': 'application/json' } }).then(async (res) => {
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail ?? JSON.stringify(body);
      } catch { /* ignore */ }
      throw new Error(String(detail));
    }
    return await res.json() as Page<T>;
  });
export const post = <T = any>(path: string, body?: any, headers?: Record<string, string>) =>
  api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body), headers });
export const patch = <T = any>(path: string, body: any) =>
  api<T>(path, { method: 'PATCH', body: JSON.stringify(body) });
export const put = <T = any>(path: string, body: any) =>
  api<T>(path, { method: 'PUT', body: JSON.stringify(body) });
export const del = <T = any>(path: string) => api<T>(path, { method: 'DELETE' });

export function fmtTime(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function fmtDateTime(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function fmtRel(iso: string): string {
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.floor(ms / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min}m ago`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d} day${d > 1 ? 's' : ''} ago`;
  const w = Math.floor(d / 7);
  return `${w} week${w > 1 ? 's' : ''} ago`;
}

// Minimal markdown renderer: bold + inline code + newlines (escaped).
export function md(text: string): string {
  const esc = text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

export const AGENT_STATE_CLASS: Record<string, string> = {
  Idle: 'dim', Working: 'info', Waiting: 'warn', Blocked: 'err',
  Failed: 'err', Paused: 'warn', Completed: 'ok',
};

export const TASK_STATE_CLASS: Record<string, string> = {
  Todo: 'dim', Ready: 'info', 'In Progress': 'info', Blocked: 'err',
  Review: 'warn', Testing: 'warn', Done: 'ok', Cancelled: 'dim',
  'Waiting QA': 'warn', Rework: 'err',
  'SA Review': 'warn', 'BA Review': 'warn',
};
