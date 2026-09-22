import React from 'react';

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal">
        <div className="spread" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>{title}</h2>
          <button className="btn small" onClick={onClose}>Close</button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
    </div>
  );
}

export function Badge({ kind, children }: { kind: string; children: React.ReactNode }) {
  return <span className={`badge ${kind}`}>{children}</span>;
}

export function useAsyncData<T>(loader: () => Promise<T>, deps: any[] = []) {
  const [data, setData] = React.useState<T | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const reload = React.useCallback(() => {
    setLoading(true);
    loader()
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  React.useEffect(() => { reload(); }, [reload]);
  return { data, error, loading, reload, setData };
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="badge err" style={{ marginBottom: 10 }}>{error}</div>;
}

export function Pagination({ total, limit, offset, onChange }: {
  total: number; limit: number; offset: number; onChange: (offset: number) => void;
}) {
  if (total <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="row" style={{ justifyContent: 'space-between', marginTop: 10 }}>
      <span className="kv">Page {page} of {pages} · {total} total</span>
      <div className="btn-row">
        <button className="btn small" disabled={offset <= 0}
          onClick={() => onChange(Math.max(0, offset - limit))}>Previous</button>
        <button className="btn small" disabled={offset + limit >= total}
          onClick={() => onChange(offset + limit)}>Next</button>
      </div>
    </div>
  );
}

/**
 * Modern collapsible card: clickable header row (title/status/actions on the
 * left, animated chevron on the right) with a smooth height transition on the
 * body. Body is unmounted while collapsed (async content loads on expand).
 */
export function Collapse({
  header, sub, right, defaultOpen = false, children,
}: {
  header: React.ReactNode;
  sub?: React.ReactNode;
  right?: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(defaultOpen);
  return (
    <div className={`card collapse-card ${open ? 'open' : ''}`}>
      <button className="collapse-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className={`collapse-chevron ${open ? 'open' : ''}`} aria-hidden>▸</span>
        <span className="collapse-title">
          <span className="collapse-header">{header}</span>
          {sub && <span className="collapse-sub">{sub}</span>}
        </span>
        <span className="collapse-right" onClick={(e) => e.stopPropagation()}>{right}</span>
      </button>
      {open && <div className="collapse-body">{children}</div>}
    </div>
  );
}
