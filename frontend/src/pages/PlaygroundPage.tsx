import React from 'react';
import { get, post } from '../api';
import { Badge, ErrorNote, Field, useAsyncData } from '../components';

/**
 * Role playground (AGENT_DEV_SPEEDUP 4.2): iterate on a role/persona's system
 * prompt against a real model in seconds — no project, sprint, or workflow
 * run needed. With the server in DRY_RUN=1 mode the loop is instant and free.
 */
export default function PlaygroundPage() {
  const { data: agents } = useAsyncData<any[]>(() => get('/api/v1/agents'), []);
  const [agentId, setAgentId] = React.useState('');
  const [stack, setStack] = React.useState('python');
  const [system, setSystem] = React.useState('');
  const [user, setUser] = React.useState('');
  const [warnings, setWarnings] = React.useState<string[]>([]);
  const [result, setResult] = React.useState<any | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const preview = async () => {
    setBusy(true); setError(null);
    try {
      const r = await post('/api/v1/playground/preview',
        { agent_id: agentId || null, stack, prompt: user });
      setSystem(r.system_prompt);
      setWarnings(r.lint_warnings ?? []);
    } catch (e: any) { setError(e.message || String(e)); }
    finally { setBusy(false); }
  };

  const run = async () => {
    setBusy(true); setError(null); setResult(null);
    const t0 = Date.now();
    try {
      const r = await post('/api/v1/playground/run',
        { agent_id: agentId || null, stack, system_prompt: system, prompt: user });
      setResult({ ...r, wall_ms: Date.now() - t0 });
    } catch (e: any) { setError(e.message || String(e)); }
    finally { setBusy(false); }
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Playground</h1>
          <div className="sub">Preview and run one agent's composed prompt directly — no sprint required.</div>
        </div>
        <div className="btn-row">
          <button className="btn" disabled={busy} onClick={preview}>👁 Preview composition</button>
          <button className="btn primary" disabled={busy} onClick={run}>{busy ? 'Running…' : '▶ Run'}</button>
        </div>
      </div>
      <ErrorNote error={error} />

      <div className="form-grid" style={{ marginBottom: 10 }}>
        <Field label="Agent (role + persona source)">
          <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
            <option value="">— base stack prompt, no agent —</option>
            {(agents ?? []).map((a) => (
              <option key={a.id} value={a.id}>{a.name} ({a.role_name})</option>
            ))}
          </select>
        </Field>
        <Field label="Stack">
          <select value={stack} onChange={(e) => setStack(e.target.value)}>
            {['python', 'dotnet', 'go', 'node'].map((s) => <option key={s}>{s}</option>)}
          </select>
        </Field>
      </div>

      <Field label={`System prompt (editable — the Run uses this text) · ${system.length} chars`}>
        <textarea style={{ minHeight: 200, fontFamily: 'var(--mono, monospace)' }}
          value={system} onChange={(e) => setSystem(e.target.value)}
          placeholder="Click Preview composition to load the exact prompt the runtime would send…" />
      </Field>
      {warnings.map((w, i) => (
        <div key={i} className="badge warn" style={{ display: 'block', margin: '4px 0', whiteSpace: 'normal' }}>⚠ {w}</div>
      ))}
      <Field label="User prompt">
        <textarea style={{ minHeight: 70 }} value={user}
          onChange={(e) => setUser(e.target.value)}
          placeholder="Optional task text; empty uses a short acknowledgement probe." />
      </Field>

      {result && (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="spread">
            <div className="row" style={{ gap: 8 }}>
              <b>Result</b>
              {result.dry_run && <Badge kind="info">dry run</Badge>}
              {result.model && <Badge kind="dim">{result.gateway} · {result.model}</Badge>}
            </div>
            <span className="kv">
              {result.input_tokens} in / {result.output_tokens} out · {result.latency_ms ?? result.wall_ms} ms
            </span>
          </div>
          <pre style={{ whiteSpace: 'pre-wrap', marginTop: 8 }}>{result.text}</pre>
        </div>
      )}
    </div>
  );
}
