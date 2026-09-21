import React from 'react';
import { get, post, patch, del } from '../api';
import { Badge, Field, Modal, useAsyncData, ErrorNote } from '../components';

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
            Named gateway + model presets. Agents pick one of these; editing a model
            here updates every agent that uses it.
          </div>
        </div>
        <div className="btn-row">
          <button className="btn primary" onClick={() => setModal('model')}>+ New Model</button>
        </div>
      </div>
      <ErrorNote error={error} />

      <div className="grid cols-3">
        {list.map((m) => (
          <div key={m.id} className="card">
            <div className="spread">
              <b>{m.name}</b>
              <Badge kind={m.active ? 'ok' : 'dim'}>{m.active ? 'Active' : 'Inactive'}</Badge>
            </div>
            <div className="kv" style={{ marginTop: 6 }}>
              Gateway: {m.gateway_name ?? '—'} <span className="muted">({m.gateway_provider})</span><br />
              Model: {m.provider_model_id ?? '—'}
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
        ))}
      </div>
      {!list.length && (
        <div className="empty">No models yet. Create one from a gateway + model.</div>
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

function ModelForm({ model, onClose, onSaved }: {
  model?: any; onClose: () => void; onSaved: () => void;
}) {
  const { data: gateways } = useAsyncData<any[]>(() => get('/api/v1/gateways'), []);
  const [gwModels, setGwModels] = React.useState<any[]>([]);
  const [f, setF] = React.useState({
    name: model?.name ?? '',
    gateway_id: model?.gateway_id ?? '',
    model_id: model?.model_id ?? '',
  });

  React.useEffect(() => {
    if (!f.gateway_id) { setGwModels([]); return; }
    get(`/api/v1/gateways/${f.gateway_id}/models`).then(setGwModels).catch(() => setGwModels([]));
  }, [f.gateway_id]);

  const save = async () => {
    try {
      if (model) await patch(`/api/v1/models/${model.id}`, f);
      else await post('/api/v1/models', f);
      onSaved();
    } catch (e: any) { alert(e.message || String(e)); }
  };

  return (
    <Modal title={model ? `Edit Model: ${model.name}` : 'Create Model'} onClose={onClose}>
      <Field label="Model name">
        <input value={f.name} placeholder="e.g. GPT-4o Dev"
          onChange={(e) => setF({ ...f, name: e.target.value })} />
      </Field>
      <Field label="Gateway">
        <select value={f.gateway_id}
          onChange={(e) => setF({ ...f, gateway_id: e.target.value, model_id: '' })}>
          <option value="">— select gateway —</option>
          {(gateways ?? []).map((g) => <option key={g.id} value={g.id}>{g.name} ({g.provider})</option>)}
        </select>
      </Field>
      <Field label="Model">
        <select value={f.model_id} onChange={(e) => setF({ ...f, model_id: e.target.value })}
          disabled={!f.gateway_id}>
          <option value="">{f.gateway_id ? '— select model —' : '— select gateway first —'}</option>
          {gwModels.map((m) => <option key={m.id} value={m.id}>{m.provider_model_id}</option>)}
        </select>
      </Field>
      <button className="btn primary"
        disabled={!f.name || !f.gateway_id || !f.model_id} onClick={save}>
        {model ? 'Save Model' : 'Create Model'}
      </button>
    </Modal>
  );
}
