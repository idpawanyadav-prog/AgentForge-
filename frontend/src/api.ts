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
  return res.json();
}

export const get = <T = any>(path: string) => api<T>(path);
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
};
