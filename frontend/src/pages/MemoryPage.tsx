import React from 'react';
import { get, post, patch, del } from '../api';
import { Badge, Field, Modal, useAsyncData, ErrorNote } from '../components';

export default function MemoryPage() {
  const [tab, setTab] = React.useState<'roles' | 'skills'>('roles');
  const { data: roles, reload: reloadRoles } = useAsyncData<any[]>(() => get('/api/v1/roles'), []);
  const { data: skills, reload: reloadSkills } = useAsyncData<any[]>(() => get('/api/v1/skills'), []);
  const [modal, setModal] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const fail = (e: any) => setError(e.message || String(e));

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Agent Memory</h1>
          <div className="sub">Reusable roles, versioned personas and skills. Instructions are immutable per version.</div>
        </div>
        <div className="btn-row">
          {tab === 'roles' && <button className="btn primary" onClick={() => setModal('newRole')}>+ New Role</button>}
          {tab === 'skills' && <button className="btn primary" onClick={() => setModal('newSkill')}>+ New Skill</button>}
        </div>
      </div>
      <ErrorNote error={error} />
      <div className="row" style={{ marginBottom: 14 }}>
        <button className={`btn small ${tab === 'roles' ? 'primary' : ''}`} onClick={() => setTab('roles')}>Roles & Personas</button>
        <button className={`btn small ${tab === 'skills' ? 'primary' : ''}`} onClick={() => setTab('skills')}>Skills</button>
      </div>

      {tab === 'roles' && (roles ?? []).map((r) => (
        <RoleCard key={r.id} role={r} reload={reloadRoles} onError={fail} />
      ))}
      {tab === 'roles' && !roles?.length && <div className="empty">No roles defined.</div>}

      {tab === 'skills' && (skills ?? []).map((s) => (
        <div key={s.id} className="card">
          <div className="spread">
            <div>
              <h3 style={{ margin: 0 }}>{s.name} <Badge kind="dim">v{s.version}</Badge></h3>
              <div className="muted small">{s.description}</div>
            </div>
            <div className="btn-row">
              <button className="btn small" onClick={() => setModal('editSkill-' + s.id)}>Edit source</button>
              <button className="btn small danger" onClick={async () => {
                try { await del(`/api/v1/skills/${s.id}`); reloadSkills(); } catch (e: any) { fail(e); }
              }}>Delete</button>
            </div>
          </div>
        </div>
      ))}
      {tab === 'skills' && !skills?.length && <div className="empty">No skills defined.</div>}

      {modal === 'newRole' && (
        <RoleForm onClose={() => setModal(null)} onSaved={() => { setModal(null); reloadRoles(); }} />
      )}
      {modal === 'newSkill' && (
        <SkillForm onClose={() => setModal(null)} onSaved={() => { setModal(null); reloadSkills(); }} />
      )}
      {modal?.startsWith('editSkill-') && (
        <SkillForm skill={skills?.find((s) => s.id === modal.slice(10))}
          onClose={() => setModal(null)} onSaved={() => { setModal(null); reloadSkills(); }} />
      )}
    </div>
  );
}

function RoleCard({ role, reload, onError }: { role: any; reload: () => void; onError: (e: any) => void }) {
  const { data: personas, reload: reloadPersonas } = useAsyncData<any[]>(() => get(`/api/v1/personas?role_id=${role.id}`), [role.id]);
  const { data: bindings, reload: reloadBindings } = useAsyncData<any[]>(() => get(`/api/v1/model-bindings?role_id=${role.id}`), [role.id]);
  const { data: gateways } = useAsyncData<any[]>(() => get('/api/v1/gateways'), []);
  const [modal, setModal] = React.useState<string | null>(null);
  const [expanded, setExpanded] = React.useState<string | null>(null);
  const { data: allSkills } = useAsyncData<any[]>(() => get('/api/v1/skills'), []);

  return (
    <div className="card" style={{ opacity: role.active ? 1 : 0.55 }}>
      <div className="spread">
        <div>
          <h3 style={{ margin: 0 }}>{role.name} {!role.active && <Badge kind="dim">inactive</Badge>}</h3>
          <div className="muted small">{role.description}</div>
        </div>
        <div className="btn-row">
          <button className="btn small" onClick={() => setModal('persona')}>+ Persona</button>
          <button className="btn small" onClick={() => setModal('binding')}>+ Model binding</button>
          <button className="btn small" onClick={async () => {
            try { await patch(`/api/v1/roles/${role.id}`, { active: role.active ? 0 : 1 }); reload(); }
            catch (e: any) { onError(e); }
          }}>{role.active ? 'Deactivate' : 'Activate'}</button>
        </div>
      </div>

      <div className="section-title">Personas</div>
      {(personas ?? []).map((p) => (
        <div key={p.id} className="list-row">
          <div className="grow">
            <b>{p.name}</b> <Badge kind="dim">v{p.version}</Badge> {!p.active && <Badge kind="dim">inactive</Badge>}
            <div className="kv">{p.description}</div>
          </div>
          <div className="btn-row">
            <button className="btn small" onClick={() => setExpanded(expanded === p.id ? null : p.id)}>
              {expanded === p.id ? 'Hide' : 'Instructions'}
            </button>
            <button className="btn small danger" onClick={async () => {
              try { await del(`/api/v1/personas/${p.id}`); reloadPersonas(); } catch (e: any) { onError(e); }
            }}>Delete</button>
          </div>
          {expanded === p.id && (
            <PersonaDetail persona={p} onError={onError} allSkills={allSkills ?? []} reload={reloadPersonas} />
          )}
        </div>
      ))}
      {!personas?.length && <div className="kv" style={{ padding: '4px 0 10px' }}>No personas for this role.</div>}

      <div className="section-title">Model bindings</div>
      {(bindings ?? []).map((b) => (
        <div key={b.id} className="list-row">
          <div className="grow">
            <span className="mono">{b.provider_model_id}</span> <span className="kv">@ {b.gateway_name}</span>
          </div>
          <button className="btn small danger" onClick={async () => {
            try { await del(`/api/v1/model-bindings/${b.id}`); reloadBindings(); } catch (e: any) { onError(e); }
          }}>Remove</button>
        </div>
      ))}
      {!bindings?.length && <div className="kv" style={{ padding: '4px 0 10px' }}>No model binding — agents need one to run.</div>}

      {modal === 'persona' && (
        <PersonaForm roleId={role.id} onClose={() => setModal(null)}
          onSaved={() => { setModal(null); reloadPersonas(); }} />
      )}
      {modal === 'binding' && (
        <BindingForm roleId={role.id} gateways={gateways ?? []} onClose={() => setModal(null)}
          onSaved={() => { setModal(null); reloadBindings(); }} />
      )}
    </div>
  );
}

function PersonaDetail({ persona, allSkills, onError, reload }: any) {
  const { data: attached, reload: reloadAttached } = useAsyncData<any[]>(() => get(`/api/v1/personas/${persona.id}/skills`), [persona.id]);
  const [instr, setInstr] = React.useState(persona.instructions);
  const [cons, setCons] = React.useState(persona.constraints_text);
  const [dirty, setDirty] = React.useState(false);
  React.useEffect(() => { setInstr(persona.instructions); setCons(persona.constraints_text); setDirty(false); }, [persona]);

  return (
    <div style={{ width: '100%', marginTop: 8 }}>
      <Field label={`Instructions (immutable versions — saving creates v${persona.version + 1})`}>
        <textarea value={instr} rows={5} onChange={(e) => { setInstr(e.target.value); setDirty(true); }} />
      </Field>
      <Field label="Constraints">
        <textarea value={cons} rows={2} onChange={(e) => { setCons(e.target.value); setDirty(true); }} />
      </Field>
      <div className="btn-row" style={{ marginBottom: 10 }}>
        <button className="btn small primary" disabled={!dirty} onClick={async () => {
          try { await patch(`/api/v1/personas/${persona.id}`, { instructions: instr, constraints_text: cons }); reload(); }
          catch (e: any) { onError(e); }
        }}>Save as new version</button>
      </div>
      <div className="kv" style={{ marginBottom: 6 }}>Skills attached:</div>
      <div className="row">
        {(allSkills ?? []).map((s) => {
          const on = (attached ?? []).some((a: any) => a.id === s.id);
          return (
            <button key={s.id} className={`btn small ${on ? 'primary' : ''}`} onClick={async () => {
              try {
                if (on) await del(`/api/v1/personas/${persona.id}/skills/${s.id}`);
                else await post(`/api/v1/personas/${persona.id}/skills/${s.id}`);
                reloadAttached();
              } catch (e: any) { onError(e); }
            }}>{s.name}{on ? ' ✓' : ''}</button>
          );
        })}
      </div>
    </div>
  );
}

function PersonaForm({ roleId, onClose, onSaved }: any) {
  const [f, setF] = React.useState({ name: '', description: '', instructions: '', constraints_text: '' });
  const upd = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return (
    <Modal title="New Persona" onClose={onClose}>
      <Field label="Name"><input value={f.name} onChange={upd('name')} /></Field>
      <Field label="Description"><input value={f.description} onChange={upd('description')} /></Field>
      <Field label="Instructions"><textarea value={f.instructions} rows={6} onChange={upd('instructions')} /></Field>
      <Field label="Constraints"><textarea value={f.constraints_text} rows={2} onChange={upd('constraints_text')} /></Field>
      <button className="btn primary" disabled={!f.name} onClick={async () => {
        try { await post(`/api/v1/roles/${roleId}/personas`, f); onSaved(); }
        catch (e: any) { alert(e.message); }
      }}>Create Persona</button>
    </Modal>
  );
}

function BindingForm({ roleId, gateways, onClose, onSaved }: any) {
  const [gw, setGw] = React.useState('');
  const [modelId, setModelId] = React.useState('');
  const { data: models } = useAsyncData<any[]>(() => (gw ? get(`/api/v1/gateways/${gw}/models`) : Promise.resolve([])), [gw]);
  return (
    <Modal title="Bind Role to Model" onClose={onClose}>
      <Field label="Gateway">
        <select value={gw} onChange={(e) => { setGw(e.target.value); setModelId(''); }}>
          <option value="">— select gateway —</option>
          {(gateways ?? []).map((g: any) => <option key={g.id} value={g.id}>{g.name} ({g.provider})</option>)}
        </select>
      </Field>
      <Field label="Model">
        <select value={modelId} onChange={(e) => setModelId(e.target.value)} disabled={!gw}>
          <option value="">— select model —</option>
          {(models ?? []).map((m: any) => <option key={m.id} value={m.id}>{m.display_name} ({m.provider_model_id})</option>)}
        </select>
      </Field>
      <button className="btn primary" disabled={!gw || !modelId} onClick={async () => {
        try { await post('/api/v1/model-bindings', { role_id: roleId, gateway_id: gw, model_id: modelId }); onSaved(); }
        catch (e: any) { alert(e.message); }
      }}>Create Binding</button>
    </Modal>
  );
}

function RoleForm({ onClose, onSaved }: any) {
  const [f, setF] = React.useState({ name: '', description: '' });
  return (
    <Modal title="New Role" onClose={onClose}>
      <Field label="Role name"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
      <Field label="Description"><input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <button className="btn primary" disabled={!f.name} onClick={async () => {
        try { await post('/api/v1/roles', f); onSaved(); } catch (e: any) { alert(e.message); }
      }}>Create Role</button>
    </Modal>
  );
}

function SkillForm({ skill, onClose, onSaved }: any) {
  const [f, setF] = React.useState({
    name: skill?.name ?? '', description: skill?.description ?? '',
    content: skill?.content ?? '# Skill\nDescribe the procedure…',
  });
  return (
    <Modal title={skill ? `Edit Skill: ${skill.name}` : 'New Skill'} onClose={onClose}>
      {!skill && <Field label="Name"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>}
      <Field label="Description"><input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <Field label="Markdown source"><textarea value={f.content} rows={10} onChange={(e) => setF({ ...f, content: e.target.value })} /></Field>
      <button className="btn primary" disabled={!f.name} onClick={async () => {
        try {
          if (skill) await patch(`/api/v1/skills/${skill.id}`, { description: f.description, content: f.content });
          else await post('/api/v1/skills', f);
          onSaved();
        } catch (e: any) { alert(e.message); }
      }}>{skill ? 'Save (bumps version)' : 'Create Skill'}</button>
    </Modal>
  );
}
