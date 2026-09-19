import React from 'react';

/**
 * ErrorBoundary + global crash overlay.
 *
 * Any render crash inside the app shows the exact error (message, stack,
 * component stack) in a readable panel instead of a black page. Global
 * window handlers catch errors React boundaries miss (event handlers,
 * unhandled promise rejections, module-level crashes).
 */

interface State {
  error: Error | null;
  info: string;
}

const OVERLAY_ID = 'ao-crash-overlay';

function esc(s: string) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export function showCrashOverlay(title: string, detail: string) {
  let el = document.getElementById(OVERLAY_ID);
  if (!el) {
    el = document.createElement('div');
    el.id = OVERLAY_ID;
    el.style.cssText =
      'position:fixed;inset:0;z-index:99999;background:rgba(8,10,16,0.94);color:#e6e9f0;' +
      'font:13px/1.5 Consolas,Menlo,monospace;padding:24px;overflow:auto;';
    document.body.appendChild(el);
  }
  el.innerHTML =
    `<div style="max-width:900px;margin:0 auto;">` +
    `<div style="font:600 16px/1.4 system-ui,sans-serif;color:#ff7b72;margin-bottom:10px;">${esc(title)}</div>` +
    `<pre style="white-space:pre-wrap;word-break:break-word;background:#141824;border:1px solid #2a3040;` +
    `border-radius:8px;padding:14px;margin:0 0 12px;">${esc(detail)}</pre>` +
    `<button onclick="this.closest('#${OVERLAY_ID}').remove()" ` +
    `style="background:#2a3040;border:1px solid #3a4152;color:#e6e9f0;border-radius:6px;` +
    `padding:6px 14px;cursor:pointer;font:13px system-ui;">Dismiss</button> ` +
    `<button onclick="location.reload()" ` +
    `style="background:#2a3040;border:1px solid #3a4152;color:#e6e9f0;border-radius:6px;` +
    `padding:6px 14px;cursor:pointer;font:13px system-ui;">Reload app</button>` +
    `</div>`;
}

export function installGlobalErrorHandlers() {
  window.addEventListener('error', (ev) => {
    const e = ev.error || (ev as any);
    const detail = `${ev.message}\n\nat ${ev.filename}:${ev.lineno}:${ev.colno}` +
      (e?.stack ? `\n\n${e.stack}` : '');
    showCrashOverlay('Unhandled error', detail);
  });
  window.addEventListener('unhandledrejection', (ev) => {
    const reason: any = ev.reason;
    const detail = String(reason?.message || reason) +
      (reason?.stack ? `\n\n${reason.stack}` : '');
    showCrashOverlay('Unhandled promise rejection', detail);
  });
}

export default class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  State
> {
  state: State = { error: null, info: '' };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Surface it in the console too for devtools users.
    console.error('Render crash caught by ErrorBoundary:', error, info);
    this.setState({ info: info.componentStack || '' });
  }

  render() {
    const { error, info } = this.state;
    if (error) {
      return (
        <div style={{
          margin: '40px auto', maxWidth: 900, padding: 20,
          fontFamily: 'Consolas, Menlo, monospace', fontSize: 13, lineHeight: 1.5,
        }}>
          <h2 style={{ color: 'var(--err, #ff7b72)', fontSize: 17, margin: '0 0 10px' }}>
            The page crashed while rendering
          </h2>
          <pre style={{
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            background: 'var(--bg3, #141824)', border: '1px solid var(--border, #2a3040)',
            borderRadius: 8, padding: 14, margin: '0 0 12px',
          }}>
            {error.message}

{error.stack || ''}
          </pre>
          {info && (
            <pre style={{
              whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--text-dim, #8b93a5)',
              background: 'var(--bg3, #141824)', border: '1px solid var(--border, #2a3040)',
              borderRadius: 8, padding: 14, margin: '0 0 12px', maxHeight: 220, overflow: 'auto',
            }}>{info}</pre>
          )}
          <button className="btn small" onClick={() => this.setState({ error: null, info: '' })}>
            Try to recover
          </button>{' '}
          <button className="btn small" onClick={() => location.reload()}>
            Reload app
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
