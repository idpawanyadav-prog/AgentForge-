import React from 'react';
import { get, post, patch, del } from '../api';
import { Badge, Field, Modal, useAsyncData, ErrorNote } from '../components';

type Member = { gateway_id: string; model_id: string };

export default function ModelsPage() {
  const { data: models, reload } = useAsyncData<any[]>(() => get('/api/v1/models'), []);
  const [modal, setModal] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const list = models ?? [];

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Models</h1>
          <div className="sub">
            Named failover chains of gateway + model presets. An agent picks one
            model; at runtime it tries that model's members in order and falls
            through to the next when one fails. Editing a model here updates every
            agent that uses it.
          </div>
        </div>
        <div className="btn-row">
          <button className="btn primary" onClick={() => setModal('model')}>+ New Model</button>
        </div>
      </div>
      <ErrorNote error={error} />

      <div className="grid cols-3">
        {list.map((m) => {
          const members: any[] = m.members ?? [];
          const primary = members[0];
          const extra = Math.max(0, members.length - 1);
          return (
            <div key={m.id} className="card">
              <div className="spread">
                <b>{m.name}</b>
                <Badge kind={m.active ? 'ok' : 'dim'}>{m.active ? 'Active' : 'Inactive'}</Badge>
              </div>
              <div className="kv" style={{ marginTop: 6 }}>
                {primary ? (
                  <>
                    Primary: {primary.gateway_name} · {primary.name}
                    {extra > 0 && (
                      <span className="muted"> +{extra} fallback{extra > 1 ? 's' : ''}</span>
                    )}
                    {members.length > 1 && (
                      <div style={{ marginTop: 4 }}>
                        {members.map((mm, i) => (
                          <div key={i} className="muted">
                            {i + 1}. {mm.gateway_name} · {mm.name}
                          </div>
                        ))}
                      </div>
                    )}
                  </>
                ) : (
                  <>Gateway: {m.gateway_name ?? '—'} <span className="muted">({m.gateway_provider})</span><br />
                    Model: {m.provider_model_id ?? '—'}</>
                )}
              </div>
              <div className="btn-row" style={{ marginTop: 10 }}>
                <button className="btn small" onClick={() => setModal('model-edit-' + m.id)}>Edit</button>
                <button className="btn small danger" onClick={async () => {
                  try {
                    const res = await del(`/api/v1/models/${m.id}`);
                    setError(res?.deactivated
                      ? `"${m.name}" is in use by an agent and was deactivated instead of deleted.`
                      : null);
                    reload();
                  } catch (e: any) { setError(e.message || String(e)); }
                }}>Delete</button>
              </div>
            </div>
          );
        })}
      </div>
      {!list.length && (
        <div className="empty">No models yet. Create one from one or more gateway + model pairs.</div>
      )}

      {modal === 'model' && (
        <ModelForm onClose={() => setModal(null)} onSaved={() => { setModal(null); reload(); }} />
      )}
      {modal?.startsWith('model-edit-') && (
        <ModelForm model={list.find((m) => m.id === modal.slice(11))}
          onClose={() => setModal(null)} onSaved={() => { setModal(null); reload(); }} />
      )}
    </div>
  );
}

function initialMembers(model: any): Member[] {
  const ms: any[] = model?.members ?? [];
  if (ms.length) {
    return ms.map((m) => ({ gateway_id: m.gateway_id, model_id: m.model_id }));
  }
  if (model?.gateway_id && model?.model_id) {
    return [{ gateway_id: model.gateway_id, model_id: model.model_id }];
  }
  return [{ gateway_id: '', model_id: '' }];
}

function ModelForm({ model, onClose, onSaved }: {
  model?: any; onClose: () => void; onSaved: () => void;
}) {
  const { data: gateways } = useAsyncData<any[]>(() => get('/api/v1/gateways'), []);
  const gwList = gateways ?? [];
  const [name, setName] = React.useState<string>(model?.name ?? '');
  const [members, setMembers] = React.useState<Member[]>(() => initialMembers(model));
  // per-gateway model list cache: gateway_id -> models
  const [gwModels, setGwModels] = React.useState<Record<string, any[]>>({});

  const loadModels = React.useCallback((gid: string) => {
    if (!gid || gwModels[gid] !== undefined) return;
    get(`/api/v1/gateways/${gid}/models`)
      .then((ms) => setGwModels((prev) => ({ ...prev, [gid]: ms ?? [] })))
      .catch(() => setGwModels((prev) => ({ ...prev, [gid]: [] })));
  }, [gwModels]);

  React.useEffect(() => { members.forEach((m) => loadModels(m.gateway_id)); }, [members, loadModels]);

  const setMember = (i: number, patchObj: Partial<Member>) => {
    setMembers((prev) => prev.map((m, j) => (j === i ? { ...m, ...patchObj } : m)));
  };
  const addMember = () => setMembers((prev) => [...prev, { gateway_id: '', model_id: '' }]);
  const removeMember = (i: number) => setMembers((prev) => prev.filter((_, j) => j !== i));
  const move = (i: number, dir: -1 | 1) => setMembers((prev) => {
    const j = i + dir;
    if (j < 0 || j >= prev.length) return prev;
    const next = [...prev];
    [next[i], next[j]] = [next[j], next[i]];
    return next;
  });

  const cleaned = members.filter((m) => m.gateway_id && m.model_id);
  const valid = name.trim() && cleaned.length >= 1;

  const save = async () => {
    const payload = { name: name.trim(), members: cleaned };
    try {
      if (model) await patch(`/api/v1/models/${model.id}`, payload);
      else await post('/api/v1/models', payload);
      onSaved();
    } catch (e: any) { alert(e.message || String(e)); }
  };

  return (
    <Modal title={model ? `Edit Model: ${model.name}` : 'Create Model'} onClose={onClose}>
      <Field label="Model name">
        <input value={name} placeholder="e.g. Resilient Dev"
          onChange={(e) => setName(e.target.value)} />
      </Field>

      <div className="field">
        <label>Failover chain <span className="muted">(tried top to bottom; first failure falls through to the next)</span></label>
        {members.map((m, i) => {
          const opts = gwModels[m.gateway_id] ?? [];
          return (
            <div key={i} className="row" style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
              <Badge kind={i === 0 ? 'ok' : 'dim'}>{i === 0 ? '1° primary' : `#${i + 1}`}</Badge>
              <select value={m.gateway_id} style={{ flex: '0 0 40%' }}
                onChange={(e) => {
                  const gid = e.target.value;
                  setMember(i, { gateway_id: gid, model_id: '' });
                  loadModels(gid);
                }}>
                <option value="">— gateway —</option>
                {gwList.map((g) => <option key={g.id} value={g.id}>{g.name} ({g.provider})</option>)}
              </select>
              <select value={m.model_id} style={{ flex: '1 1 auto' }} disabled={!m.gateway_id}
                onChange={(e) => setMember(i, { model_id: e.target.value })}>
                <option value="">{m.gateway_id ? '— model —' : '— gateway first —'}</option>
                {opts.map((mm) => <option key={mm.id} value={mm.id}>{mm.provider_model_id}</option>)}
              </select>
              <button className="btn small" title="Move up" disabled={i === 0}
                onClick={() => move(i, -1)}>↑</button>
              <button className="btn small" title="Move down" disabled={i === members.length - 1}
                onClick={() => move(i, 1)}>↓</button>
              <button className="btn small danger" title="Remove" disabled={members.length <= 1}
                onClick={() => removeMember(i)}>×</button>
            </div>
          );
        })}
        <button className="btn small" style={{ marginTop: 2 }} onClick={addMember}>+ Add model</button>
      </div>

      <button className="btn primary" style={{ marginTop: 12 }}
        disabled={!valid} onClick={save}>
        {model ? 'Save Model' : 'Create Model'}
      </button>
    </Modal>
  );
}
