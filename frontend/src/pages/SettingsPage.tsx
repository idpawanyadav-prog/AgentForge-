import React from 'react';
import { get, post, put, patch, del, fmtDateTime, md } from '../api';
import { Badge, Field, Modal, useAsyncData, ErrorNote } from '../components';

export default function SettingsPage() {
  const { data: gateways, reload } = useAsyncData<any[]>(() => get('/api/v1/gateways'), []);
  const { data: audit, reload: reloadAudit } = useAsyncData<any[]>(() => get('/api/v1/audit?limit=40'), []);
  const [modal, setModal] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const fail = (e: any) => setError(e.message || String(e));

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <div className="sub">Gateway connections, model catalogs and the audit trail. Credentials are stored as masked references only.</div>
        </div>
        <button className="btn primary" onClick={() => setModal('newGateway')}>+ Add Gateway</button>
      </div>
      <ErrorNote error={error} />

      <div className="settings-grid">
        <ControlBotCard gateways={gateways ?? []} onError={fail} />
        <PoBotCard gateways={gateways ?? []} onError={fail} />
      </div>

      <div className="section-title">Gateways</div>
      {(gateways ?? []).map((g) => (
        <GatewayCard key={g.id} gw={g} reload={reload} onError={fail} onEdit={() => setModal('editGw-' + g.id)} />
      ))}
      {!gateways?.length && <div className="empty">No gateways configured.</div>}

      <ModelPlayground gateways={gateways ?? []} onError={fail} />

      <AuditTrail audit={audit ?? []} />

      {modal === 'newGateway' && (
        <GatewayForm onClose={() => setModal(null)} onSaved={() => { setModal(null); reload(); }} />
      )}
      {modal?.startsWith('editGw-') && (
        <GatewayForm gateway={gateways?.find((g) => g.id === modal.slice(7))}
          onClose={() => setModal(null)} onSaved={() => { setModal(null); reload(); }} />
      )}
    </div>
  );
}

function AuditTrail({ audit }: { audit: any[] }) {
  const [open, setOpen] = React.useState(false);
  return (
    <>
      <div className="spread" style={{ margin: '18px 0 8px' }}>
        <button
          className="section-title"
          style={{ margin: 0, cursor: 'pointer', background: 'none', border: 'none', padding: 0, color: 'inherit', font: 'inherit' }}
          onClick={() => setOpen((o) => !o)}
          title={open ? 'Hide audit trail' : 'Show audit trail'}
        >
          {open ? '▾' : '▸'} Audit trail (latest {Math.min(40, audit.length)})
        </button>
      </div>
      {open && (
        <div className="card">
          {audit.map((a) => (
            <div key={a.audit_seq} className="event-item">
              <span className={`event-dot ${/delete|cancel|fail/.test(a.action) ? 'dot-err' : /create|start/.test(a.action) ? 'dot-ok' : 'dot-info'}`} />
              <div>
                <div><span className="mono">{a.action}</span> — {a.summary}</div>
                <div className="kv">{a.resource_type}{a.resource_id ? ` · ${a.resource_id.slice(0, 8)}` : ''} · by {a.actor_id}</div>
              </div>
              <span style={{ flex: 1 }} />
              <span className="event-time">{fmtDateTime(a.created_at)}</span>
            </div>
          ))}
          {!audit.length && <div className="empty">No audit events yet.</div>}
        </div>
      )}
    </>
  );
}

function ControlBotCard({ gateways, onError }: { gateways: any[]; onError: (e: any) => void }) {
  const { data: cfg, reload } = useAsyncData<any>(() => get('/api/v1/settings'), []);
  const bot = cfg?.control_bot ?? {};
  const [gid, setGid] = React.useState('');
  const [modelId, setModelId] = React.useState('');
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const { data: models } = useAsyncData<any[]>(
    () => (gid ? get(`/api/v1/gateways/${gid}/models`) : Promise.resolve([])), [gid]);

  React.useEffect(() => {
    if (bot.gateway_id) { setGid(bot.gateway_id); setModelId(bot.model_id ?? ''); }
  }, [bot.gateway_id, bot.model_id]);

  const save = async () => {
    setSaving(true); setSaved(false);
    try {
      await put('/api/v1/settings/control-bot', { gateway_id: gid || null, model_id: modelId || null });
      reload(); setSaved(true);
    } catch (e: any) { onError(e); }
    finally { setSaving(false); }
  };

  const configured = bot.gateway_id && bot.model_id;
  return (
    <div className="card playground-card">
      <div className="spread">
        <div>
          <h3 style={{ margin: 0 }}>🤖 Project Control AI</h3>
          <div className="muted small">
            The Project Control chatbot uses this gateway + model as its brain to understand natural language
            (create/start/pause sprints, build teams, assign tasks, agent status…). Without it, only typed commands work.
            {configured
              ? <> Current: <b>{bot.gateway_name}</b> → <b>{bot.model_name}</b>.</>
              : <> <b>Not configured</b> — pick a gateway and model below.</>}
          </div>
        </div>
      </div>
      <div className="row" style={{ marginTop: 10, gap: 8 }}>
        <select style={{ width: 220 }} value={gid} onChange={(e) => { setGid(e.target.value); setModelId(''); }}>
          <option value="">— select gateway —</option>
          {gateways.map((g) => <option key={g.id} value={g.id}>{g.name} ({g.provider})</option>)}
        </select>
        <select style={{ width: 260 }} value={modelId} onChange={(e) => setModelId(e.target.value)} disabled={!gid}>
          <option value="">— select model —</option>
          {(models ?? []).map((m) => <option key={m.id} value={m.id}>{m.display_name} ({m.provider_model_id})</option>)}
        </select>
        <button className="btn primary" disabled={saving || !gid || !modelId} onClick={save}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        {saved && <Badge kind="ok">Saved</Badge>}
      </div>
    </div>
  );
}

function PoBotCard({ gateways, onError }: { gateways: any[]; onError: (e: any) => void }) {
  const { data: cfg, reload } = useAsyncData<any>(() => get('/api/v1/settings'), []);
  const bot = cfg?.po_bot ?? {};
  const [gid, setGid] = React.useState('');
  const [modelId, setModelId] = React.useState('');
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const { data: models } = useAsyncData<any[]>(
    () => (gid ? get(`/api/v1/gateways/${gid}/models`) : Promise.resolve([])), [gid]);

  React.useEffect(() => {
    if (bot.gateway_id) { setGid(bot.gateway_id); setModelId(bot.model_id ?? ''); }
  }, [bot.gateway_id, bot.model_id]);

  const save = async () => {
    setSaving(true); setSaved(false);
    try {
      await put('/api/v1/settings/po-bot', { gateway_id: gid || null, model_id: modelId || null });
      reload(); setSaved(true);
    } catch (e: any) { onError(e); }
    finally { setSaving(false); }
  };

  const configured = bot.gateway_id && bot.model_id;
  return (
    <div className="card playground-card">
      <div className="spread">
        <div>
          <h3 style={{ margin: 0 }}>👑 Product Owner AI</h3>
          <div className="muted small">
            The Product Owner agent uses this gateway + model as its brain for requirement reviews,
            sprint planning, task rewrites, assignments and unblocking — in chat and in autonomous mode.
            {configured
              ? <> Current: <b>{bot.gateway_name}</b> → <b>{bot.model_name}</b>.</>
              : <> <b>Not configured</b> — falls back to the project's default model.</>}
          </div>
        </div>
      </div>
      <div className="row" style={{ marginTop: 10, gap: 8 }}>
        <select style={{ width: 220 }} value={gid} onChange={(e) => { setGid(e.target.value); setModelId(''); }}>
          <option value="">— select gateway —</option>
          {gateways.map((g) => <option key={g.id} value={g.id}>{g.name} ({g.provider})</option>)}
        </select>
        <select style={{ width: 260 }} value={modelId} onChange={(e) => setModelId(e.target.value)} disabled={!gid}>
          <option value="">— select model —</option>
          {(models ?? []).map((m) => <option key={m.id} value={m.id}>{m.display_name} ({m.provider_model_id})</option>)}
        </select>
        <button className="btn primary" disabled={saving || !gid || !modelId} onClick={save}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        {saved && <Badge kind="ok">Saved</Badge>}
      </div>
    </div>
  );
}

function ModelPlayground({ gateways, onError }: { gateways: any[]; onError: (e: any) => void }) {
  const [gid, setGid] = React.useState('');
  const [modelId, setModelId] = React.useState('');
  const [messages, setMessages] = React.useState<{ role: string; content: string; meta?: string }[]>([
    { role: 'assistant', content: 'Pick a gateway and a model, then send a message to verify the connection works end to end.' },
  ]);
  const [input, setInput] = React.useState('');
  const [sending, setSending] = React.useState(false);
  const { data: models } = useAsyncData<any[]>(
    () => (gid ? get(`/api/v1/gateways/${gid}/models`) : Promise.resolve([])), [gid]);
  const endRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => { if (!gid && gateways.length) setGid(gateways[0].id); }, [gateways, gid]);
  React.useEffect(() => { setModelId(''); }, [gid]);
  React.useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  const gw = gateways.find((g) => g.id === gid);

  const send = async () => {
    const text = input.trim();
    if (!text || !gid || !modelId || sending) return;
    setSending(true); setInput('');
    setMessages((m) => [...m, { role: 'user', content: text }]);
    try {
      const res = await post(`/api/v1/gateways/${gid}/test-chat`, { model_id: modelId, message: text });
      setMessages((m) => [...m, {
        role: 'assistant', content: res.reply,
        meta: `${res.model} · ${res.latency_ms} ms · ${res.input_tokens}+${res.output_tokens} tokens`,
      }]);
    } catch (e: any) {
      setMessages((m) => [...m, { role: 'assistant', content: `⚠ Test failed: ${e.message}` }]);
    } finally { setSending(false); }
  };

  return (
    <div className="card playground-card">
      <div className="spread">
        <div>
          <h3 style={{ margin: 0 }}>🧪 Model Playground</h3>
          <div className="muted small">Live test: messages are sent through the gateway to the selected model. Requires an API key stored on the gateway (Edit → paste key).</div>
        </div>
        <div className="row">
          <select style={{ width: 190 }} value={gid} onChange={(e) => setGid(e.target.value)}>
            {gateways.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
          <select style={{ width: 220 }} value={modelId} onChange={(e) => setModelId(e.target.value)} disabled={!gid}>
            <option value="">— select model —</option>
            {(models ?? []).map((m) => <option key={m.id} value={m.id}>{m.display_name} ({m.provider_model_id})</option>)}
          </select>
        </div>
      </div>
      <div className="playground-msgs">
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div dangerouslySetInnerHTML={{ __html: md(m.content) }} />
            {m.meta && <div className="kv" style={{ marginTop: 6 }}>{m.meta}</div>}
          </div>
        ))}
        {sending && <div className="msg assistant muted small">awaiting {gw?.name ?? 'gateway'} response…</div>}
        <div ref={endRef} />
      </div>
      <div className="chat-input" style={{ borderTop: '1px solid var(--border)' }}>
        <textarea value={input} placeholder={modelId ? 'Type a test message…' : 'Select a model first…'}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <button className="btn primary" disabled={!modelId || sending || !input.trim()} onClick={send}>
          {sending ? '…' : 'Send'}
        </button>
      </div>
    </div>
  );
}

function GatewayCard({ gw, reload, onError, onEdit }: { gw: any; reload: () => void; onError: (e: any) => void; onEdit: () => void }) {
  const { data: models, reload: reloadModels } = useAsyncData<any[]>(() => get(`/api/v1/gateways/${gw.id}/models`), [gw.id]);
  const [newModel, setNewModel] = React.useState('');
  const hasKey = !!gw.key_mask;
  return (
    <div className="card">
      <div className="spread">
        <div>
          <h3 style={{ margin: 0 }}>{gw.name} <Badge kind={gw.status === 'Active' ? 'ok' : 'dim'}>{gw.status}</Badge></h3>
          <div className="kv mono" style={{ marginTop: 4 }}>{gw.provider} · {gw.base_url} · {gw.api_type}</div>
          <div className="kv">API key: <span className="mono">{gw.key_mask ?? 'not set'}</span> {hasKey ? '(stored encrypted)' : ''} · last test: {gw.test_status ?? 'never'} {gw.last_tested_at ? `at ${fmtDateTime(gw.last_tested_at)}` : ''}</div>
          {gw.test_diagnostic && <div className="kv">{gw.test_diagnostic}</div>}
        </div>
        <div className="btn-row">
          <button className="btn small" onClick={onEdit}>Edit</button>
          <button className="btn small" onClick={async () => {
            try { await post(`/api/v1/gateways/${gw.id}/test`); reload(); reloadModels(); } catch (e: any) { onError(e); }
          }}>Test connection</button>
          <button className="btn small" onClick={async () => {
            try { await post(`/api/v1/gateways/${gw.id}/discover`); reloadModels(); } catch (e: any) { onError(e); }
          }}>Discover models</button>
          <button className="btn small" onClick={() => reload()}>Refresh</button>
          <button className="btn small danger" onClick={async () => {
            if (!confirm(`Delete gateway "${gw.name}"?`)) return;
            try { await del(`/api/v1/gateways/${gw.id}`); reload(); } catch (e: any) { onError(e); }
          }}>Delete</button>
        </div>
      </div>

      <div className="section-title">Model catalog</div>
      <div className="row">
        {(models ?? []).map((m) => (
          <span key={m.id} className="stat-chip" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span><b>{m.display_name}</b> <span className="l mono">{m.provider_model_id}</span></span>
            {m.capabilities && <Badge kind="info">{m.capabilities}</Badge>}
            <button className="btn small danger" onClick={async () => {
              try { await del(`/api/v1/gateways/${gw.id}/models/${m.id}`); reloadModels(); } catch (e: any) { onError(e); }
            }}>✕</button>
          </span>
        ))}
      </div>
      {!models?.length && <div className="kv">No models registered. Use "Discover models" or add manually below.</div>}
      <div className="row" style={{ marginTop: 10 }}>
        <input style={{ width: 260 }} placeholder="provider-model-id" value={newModel}
          onChange={(e) => setNewModel(e.target.value)} />
        <button className="btn small" disabled={!newModel} onClick={async () => {
          try {
            await post(`/api/v1/gateways/${gw.id}/models`, { provider_model_id: newModel, display_name: newModel });
            setNewModel(''); reloadModels();
          } catch (e: any) { onError(e); }
        }}>+ Register model</button>
      </div>
    </div>
  );
}

function GatewayForm({ gateway, onClose, onSaved }: { gateway?: any; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = React.useState({
    name: gateway?.name ?? '', provider: gateway?.provider ?? 'openai',
    base_url: gateway?.base_url ?? 'https://api.openai.com/v1',
    api_type: gateway?.api_type ?? 'openai-chat', api_key: '',
  });
  const upd = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return (
    <Modal title={gateway ? `Edit Gateway: ${gateway.name}` : 'Add Gateway'} onClose={onClose}>
      <div className="form-grid">
        <Field label="Name"><input value={f.name} onChange={upd('name')} /></Field>
        <Field label="Provider">
          <select value={f.provider} onChange={upd('provider')}>
            {['openai', 'azure-openai', 'anthropic', 'ollama', 'custom'].map((p) => <option key={p}>{p}</option>)}
          </select>
        </Field>
      </div>
      <Field label="API protocol">
        <select value={f.api_type} onChange={upd('api_type')}>
          <option value="openai-chat">OpenAI-compatible (/v1/chat/completions)</option>
          <option value="anthropic-messages">Anthropic native (/v1/messages)</option>
        </select>
      </Field>
      <Field label="Base URL"><input value={f.base_url} onChange={upd('base_url')} /></Field>
      <Field label="API key (stored encrypted; required for live playground testing)">
        <input type="password" value={f.api_key} onChange={upd('api_key')}
          placeholder={gateway?.key_mask ?? 'sk-…'} />
      </Field>
      <button className="btn primary" disabled={!f.name || !f.base_url} onClick={async () => {
        try {
          if (gateway) {
            const body: any = { name: f.name, provider: f.provider, base_url: f.base_url, api_type: f.api_type };
            if (f.api_key) body.api_key = f.api_key;
            await patch(`/api/v1/gateways/${gateway.id}`, body);
          } else {
            const g = await post('/api/v1/gateways', f);
            await post(`/api/v1/gateways/${g.id}/test`).catch(() => undefined);
          }
          onSaved();
        } catch (e: any) { alert(e.message); }
      }}>{gateway ? 'Save Gateway' : 'Create & Test Gateway'}</button>
    </Modal>
  );
}
