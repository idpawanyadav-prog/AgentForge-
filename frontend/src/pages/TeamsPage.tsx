import React from 'react';
import { get, post, patch, del } from '../api';
import { Badge, Field, Modal, useAsyncData, ErrorNote } from '../components';
import { AGENT_STATE_CLASS } from '../api';

export default function TeamsPage() {
  const { data: teams, reload } = useAsyncData<any[]>(() => get('/api/v1/teams'), []);
  const { data: agents, reload: reloadAgents } = useAsyncData<any[]>(() => get('/api/v1/agents'), []);
  const [modal, setModal] = React.useState<'agent' | 'team' | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const fail = (e: any) => setError(e.message || String(e));

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Teams</h1>
          <div className="sub">Concrete agent instances (Role + Persona + Model) assembled into teams.</div>
        </div>
        <div className="btn-row">
          <button className="btn" onClick={() => setModal('agent')}>+ New Agent</button>
          <button className="btn primary" onClick={() => setModal('team')}>+ New Team</button>
        </div>
      </div>
      <ErrorNote error={error} />

      <div className="section-title">Agents</div>
      <div className="grid cols-3">
        {(agents ?? []).map((a) => (
          <div key={a.id} className="card">
            <div className="spread">
              <b>{a.name}</b>
              <Badge kind={AGENT_STATE_CLASS[a.lifecycle_state] ?? 'dim'}>{a.lifecycle_state}</Badge>
            </div>
            <div className="kv" style={{ marginTop: 6 }}>
              Role: {a.role_name ?? '—'}<br />
              Persona: {a.persona_name ?? '—'} (v{a.persona_version ?? '?'})<br />
              Model: {a.provider_model_id ?? 'not bound'}
            </div>
            {a.current_activity && <div className="agent-activity">{a.current_activity}</div>}
            <div className="btn-row" style={{ marginTop: 10 }}>
              <button className="btn small danger" onClick={async () => {
                try { await del(`/api/v1/agents/${a.id}`); reloadAgents(); } catch (e: any) { fail(e); }
              }}>Delete</button>
            </div>
          </div>
        ))}
      </div>
      {!agents?.length && <div className="empty">No agents. Create one from a role + persona.</div>}

      <div className="section-title">Teams</div>
      {(teams ?? []).map((t) => (
        <div key={t.id} className="card">
          <div className="spread">
            <div>
              <h3 style={{ margin: 0 }}>{t.name}</h3>
              <div className="muted small">{t.description}</div>
            </div>
            <Badge kind={t.status === 'Active' ? 'ok' : 'dim'}>{t.status}</Badge>
          </div>
          <div className="row" style={{ marginTop: 10 }}>
            {(t.agents ?? []).map((a: any) => (
              <span key={a.id} className="stat-chip">
                <b>{a.name}</b> <span className="l">{a.role_name}{a.role_in_team !== 'Member' ? ` · ${a.role_in_team}` : ''}</span>
              </span>
            ))}
          </div>
          <div className="btn-row" style={{ marginTop: 10 }}>
            <button className="btn small" onClick={() => setModal('team-edit-' + t.id)}>Edit members</button>
            <button className="btn small danger" onClick={async () => {
              try { await del(`/api/v1/teams/${t.id}`); reload(); } catch (e: any) { fail(e); }
            }}>Delete</button>
          </div>
        </div>
      ))}
      {!teams?.length && <div className="empty">No teams yet.</div>}

      {modal === 'agent' && (
        <AgentForm onClose={() => setModal(null)} onSaved={() => { setModal(null); reloadAgents(); }} />
      )}
      {modal === 'team' && (
        <TeamForm agents={agents ?? []} onClose={() => setModal(null)} onSaved={() => { setModal(null); reload(); }} />
      )}
      {modal?.startsWith('team-edit-') && (
        <TeamForm agents={agents ?? []} team={teams?.find((t) => t.id === modal.slice(10))}
          onClose={() => setModal(null)} onSaved={() => { setModal(null); reload(); }} />
      )}
    </div>
  );
}

function AgentForm({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const { data: roles } = useAsyncData<any[]>(() => get('/api/v1/roles'), []);
  const { data: personas } = useAsyncData<any[]>(() => get('/api/v1/personas'), []);
  const { data: bindings } = useAsyncData<any[]>(() => get('/api/v1/model-bindings'), []);
  const [f, setF] = React.useState({ name: '', role_id: '', persona_id: '', model_binding_id: '' });
  const upd = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const rolePersonas = (personas ?? []).filter((p) => p.role_id === f.role_id);
  const roleBindings = (bindings ?? []).filter((b) => b.role_id === f.role_id);
  return (
    <Modal title="Create Agent" onClose={onClose}>
      <Field label="Agent name"><input value={f.name} onChange={upd('name')} /></Field>
      <Field label="Role">
        <select value={f.role_id} onChange={(e) => setF({ ...f, role_id: e.target.value, persona_id: '', model_binding_id: '' })}>
          <option value="">— select role —</option>
          {(roles ?? []).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
        </select>
      </Field>
      <Field label="Persona">
        <select value={f.persona_id} onChange={upd('persona_id')} disabled={!f.role_id}>
          <option value="">— default persona —</option>
          {rolePersonas.map((p) => <option key={p.id} value={p.id}>{p.name} (v{p.version})</option>)}
        </select>
      </Field>
      <Field label="Model binding (inherits role default)">
        <select value={f.model_binding_id} onChange={upd('model_binding_id')} disabled={!f.role_id}>
          <option value="">— role default —</option>
          {roleBindings.map((b) => <option key={b.id} value={b.id}>{b.provider_model_id} @ {b.gateway_name}</option>)}
        </select>
      </Field>
      <button className="btn primary" disabled={!f.name || !f.role_id} onClick={async () => {
        try {
          await post('/api/v1/agents', { ...f, persona_id: f.persona_id || (rolePersonas[0]?.id ?? ''), model_binding_id: f.model_binding_id || roleBindings[0]?.id || null });
          onSaved();
        } catch (e: any) { alert(e.message); }
      }}>Create Agent</button>
    </Modal>
  );
}

function TeamForm({ agents, team, onClose, onSaved }: { agents: any[]; team?: any; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = React.useState({
    name: team?.name ?? '', description: team?.description ?? '',
    agent_ids: team?.agents?.map((a: any) => a.id) ?? [],
  });
  return (
    <Modal title={team ? `Edit Team: ${team.name}` : 'Create Team'} onClose={onClose}>
      <Field label="Team name"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
      <Field label="Description"><input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <Field label="Members (ctrl/cmd-click for multiple)">
        <select multiple value={f.agent_ids} style={{ height: 160 }}
          onChange={(e) => setF({ ...f, agent_ids: Array.from(e.target.selectedOptions).map((o) => o.value) })}>
          {agents.map((a) => <option key={a.id} value={a.id}>{a.name} — {a.role_name}</option>)}
        </select>
      </Field>
      <button className="btn primary" disabled={!f.name || f.agent_ids.length === 0} onClick={async () => {
        try {
          if (team) await patch(`/api/v1/teams/${team.id}`, f);
          else await post('/api/v1/teams', f);
          onSaved();
        } catch (e: any) { alert(e.message); }
      }}>{team ? 'Save Team' : 'Create Team'}</button>
    </Modal>
  );
}
