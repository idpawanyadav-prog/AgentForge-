import React from 'react';
import { MarkdownPreview } from '../MarkdownText';
import { get, post, patch, del, fmtRel } from '../api';
import { Badge, Collapse, ErrorNote, Field, Modal, useAsyncData } from '../components';
import { AGENT_STATE_CLASS } from '../api';

function iconFor(name: string): string {
  if (/develop|coder|senior/i.test(name)) return '</>';
  if (/architect/i.test(name)) return '⬢';
  if (/qa|quality|test/i.test(name)) return '◎';
  if (/ops|devops|platform/i.test(name)) return '⚙';
  if (/analy/i.test(name)) return '📊';
  if (/manager|scrum|owner/i.test(name)) return '◔';
  if (/design/i.test(name)) return '🎨';
  return '⬡';
}

function CodeEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const gutterRef = React.useRef<HTMLDivElement>(null);
  const taRef = React.useRef<HTMLTextAreaElement>(null);
  const lines = value.split('\n').length;
  const onScroll = () => {
    if (gutterRef.current && taRef.current) gutterRef.current.scrollTop = taRef.current.scrollTop;
  };
  return (
    <div className="editor-wrap">
      <div className="editor-gutter" ref={gutterRef}>
        {Array.from({ length: lines }, (_, i) => <div key={i}>{i + 1}</div>)}
      </div>
      <textarea ref={taRef} className="editor-area" value={value} spellCheck={false}
        onChange={(e) => onChange(e.target.value)} onScroll={onScroll} />
    </div>
  );
}

function LintWarnings({ list }: { list?: string[] }) {
  if (!list?.length) return null;
  return (
    <div style={{ margin: '8px 0' }}>
      {list.map((w, i) => (
        <div key={i} className="badge warn" style={{ display: 'block', margin: '4px 0', whiteSpace: 'normal' }}>
          ⚠ {w}
        </div>
      ))}
    </div>
  );
}

function HistoryPanel({ kind, id, onChanged, onError }:
  { kind: 'instructions' | 'personas'; id: string; onChanged: () => void; onError: (e: any) => void }) {
  const { data: versions, reload } = useAsyncData<any[]>(
    () => get(`/api/v1/${kind}/${id}/versions`), [id]);
  return (
    <Collapse header={<>Version history</>} sub="Every content change is snapshotted; restore is append-only.">
      {(versions ?? []).map((v) => (
        <div key={v.id} className="spread" style={{ padding: '4px 0' }}>
          <span className="kv mono">v{v.version} · {fmtRel(v.created_at)} · {v.content_chars ?? v.instructions_chars ?? 0} chars</span>
          <button className="btn small" onClick={async () => {
            if (!confirm(`Restore version ${v.version}? Its content becomes the new current version.`)) return;
            try { await post(`/api/v1/${kind}/${id}/restore`, { version: v.version }); onChanged(); reload(); }
            catch (e: any) { onError(e); }
          }}>Restore</button>
        </div>
      ))}
      {!versions?.length && <div className="kv">No stored versions yet.</div>}
    </Collapse>
  );
}

export default function MemoryPage() {
  const { data: roles, reload: reloadRoles } = useAsyncData<any[]>(() => get('/api/v1/roles/memory'), []);
  const { data: agents, reload: reloadAgents } = useAsyncData<any[]>(() => get('/api/v1/agents'), []);
  const { data: allSkills } = useAsyncData<any[]>(() => get('/api/v1/skills'), []);
  const [search, setSearch] = React.useState('');
  const [selectedRole, setSelectedRole] = React.useState<string | null>(null);
  const [tab, setTab] = React.useState<'instructions' | 'skills' | 'persona'>('instructions');
  const [modal, setModal] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const fail = (e: any) => setError(e.message || String(e));

  React.useEffect(() => {
    if (!selectedRole && roles?.length) setSelectedRole(roles[0].id);
    if (selectedRole && roles && !roles.find((r) => r.id === selectedRole)) setSelectedRole(roles[0]?.id ?? null);
  }, [roles, selectedRole]);

  const role = roles?.find((r) => r.id === selectedRole);
  const roleAgents = (agents ?? []).filter((a) => a.role_id === selectedRole);
  const filtered = (roles ?? []).filter((r) => r.name.toLowerCase().includes(search.toLowerCase()));
  const [selectedInstr, setSelectedInstr] = React.useState<string | null>(null);
  React.useEffect(() => { setSelectedInstr(null); }, [selectedRole]);
  const [selectedPersona, setSelectedPersona] = React.useState<string | null>(null);
  React.useEffect(() => { setSelectedPersona(null); }, [selectedRole]);
  // Bumped after any instruction create/save/delete/toggle so the list and
  // editor panels refetch (their loaders otherwise only track roleId).
  const [instrRefresh, setInstrRefresh] = React.useState(0);
  const refreshInstr = () => setInstrRefresh((k) => k + 1);

  return (
    <div className="page">
      <div className="page-header">
        <div className="row" style={{ gap: 14 }}>
          <div className="brain-icon">🧠</div>
          <div>
            <h1 style={{ margin: 0 }}>Agent Memory</h1>
            <div className="sub">Define role-based instructions and skills to guide your AI agents.</div>
          </div>
        </div>
        <button className="btn primary" onClick={() => setModal('newRole')}>+ New Group</button>
      </div>
      <ErrorNote error={error} />

      <div className="memory-layout">
        {/* ---------------- left: role groups ---------------- */}
        <div className="mem-card col-groups">
          <div className="mem-card-title">Role Groups</div>
          <div className="search-wrap">
            <input placeholder="Search groups…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <div className="group-list">
            {filtered.map((r) => (
              <div key={r.id} className={`group-item ${selectedRole === r.id ? 'active' : ''}`}
                onClick={() => { setSelectedRole(r.id); setTab('instructions'); }}>
                <div className="group-icon">{iconFor(r.name)}</div>
                <div style={{ minWidth: 0 }}>
                  <div className="group-name">{r.name}</div>
                  <div className="kv">{r.instruction_count} instr · {r.skill_count} skills · {r.persona_count ?? 0} personas</div>
                </div>
                {!r.active && <span style={{ marginLeft: 'auto' }}><Badge kind="dim">off</Badge></span>}
              </div>
            ))}
            {!filtered.length && <div className="empty" style={{ padding: 18 }}>No groups match.</div>}
          </div>
          <button className="btn add-group" onClick={() => setModal('newRole')}>+ Add Role Group</button>
        </div>

        {/* ---------------- middle: group detail ---------------- */}
        {role ? (
          <div className="mem-card col-main">
            <div className="spread">
              <div>
                <div className="mem-group-title">{role.name}</div>
                <div className="muted small">{role.description || 'No description'}</div>
              </div>
              <div className="btn-row">
                <button className="btn small" onClick={() => setModal('editRole')}>✎ Edit Group</button>
                <button className="btn small danger" onClick={async () => {
                  if (!confirm(`Delete role group "${role.name}"?`)) return;
                  try { await del(`/api/v1/roles/${role.id}`); reloadRoles(); } catch (e: any) { fail(e); }
                }}>🗑 Delete</button>
              </div>
            </div>

            <div className="subtabs">
              <button className={`subtab ${tab === 'instructions' ? 'active' : ''}`} onClick={() => setTab('instructions')}>📋 Instructions</button>
              <button className={`subtab ${tab === 'skills' ? 'active' : ''}`} onClick={() => setTab('skills')}>⛁ Skills</button>
              <button className={`subtab ${tab === 'persona' ? 'active' : ''}`} onClick={() => setTab('persona')}>🎭 Persona</button>
            </div>

            {tab === 'instructions' ? (
              <InstructionsPanel roleId={role.id} selected={selectedInstr} onSelect={setSelectedInstr}
                onError={fail} onOpenModal={setModal} refreshKey={instrRefresh} onChanged={refreshInstr} />
            ) : tab === 'skills' ? (
              <SkillsPanel roleId={role.id} allSkills={allSkills ?? []} onError={fail} onChanged={reloadRoles} />
            ) : (
              <PersonaPanel roleId={role.id} selected={selectedPersona} onSelect={setSelectedPersona}
                onError={fail} onOpenModal={setModal} />
            )}

            <div className="assign-section">
              <div className="mem-card-title">Assign to Agents</div>
              <div className="muted small" style={{ marginBottom: 10 }}>
                Agents with this role group will use these instructions and skills in their context.
              </div>
              <div className="row">
                {roleAgents.map((a) => (
                  <div key={a.id} className="agent-chip">
                    <span className={`dot ${a.lifecycle_state === 'Working' ? 'dot-ok' : AGENT_STATE_CLASS[a.lifecycle_state] === 'err' ? 'dot-err' : 'dot-dim'}`} />
                    <div>
                      <div className="small" style={{ fontWeight: 600 }}>{a.name}</div>
                      <div className="kv">{role.name}</div>
                    </div>
                  </div>
                ))}
                <button className="btn add-group" style={{ width: 'auto', padding: '8px 16px' }}
                  onClick={() => setModal('assignAgent')}>+ Assign Agent</button>
              </div>
              {!roleAgents.length && <div className="kv">No agents use this group yet.</div>}
            </div>
          </div>
        ) : (
          <div className="mem-card col-main"><div className="empty">Select or create a role group.</div></div>
        )}

        {/* ---------------- right: instruction / persona editor ---------------- */}
        {tab === 'persona' ? (
          <PersonaEditor roleId={selectedRole} personaId={selectedPersona} onSelect={setSelectedPersona}
            onError={fail} reloadRoles={reloadRoles} />
        ) : (
          <EditorPanel roleId={selectedRole} selectedId={selectedInstr} onSelect={setSelectedInstr}
            onError={fail} refreshKey={instrRefresh} onChanged={refreshInstr} />
        )}
      </div>

      {modal === 'newRole' && (
        <RoleGroupForm onClose={() => setModal(null)} onSaved={async () => { setModal(null); reloadRoles(); }} />
      )}
      {modal === 'editRole' && role && (
        <RoleGroupForm role={role} onClose={() => setModal(null)} onSaved={async () => { setModal(null); reloadRoles(); }} />
      )}
      {modal === 'newInstruction' && role && (
        <InstructionForm roleId={role.id} onClose={() => setModal(null)}
          onSaved={async (id: string) => { setModal(null); setSelectedInstr(id); refreshInstr(); }} />
      )}
      {modal === 'newPersona' && role && (
        <NewPersonaModal roleId={role.id} onClose={() => setModal(null)}
          onSaved={async (id: string) => { setModal(null); setSelectedPersona(id); reloadRoles(); }} onError={fail} />
      )}
      {modal === 'assignAgent' && role && (
        <AssignAgentModal role={role} agents={agents ?? []} onClose={() => setModal(null)}
          onSaved={async () => { setModal(null); reloadAgents(); }} onError={fail} />
      )}
    </div>
  );
}

function InstructionsPanel({ roleId, selected, onSelect, onError, onOpenModal, refreshKey, onChanged }:
  { roleId: string; selected: string | null; onSelect: (id: string) => void; onError: (e: any) => void;
    onOpenModal: (m: string) => void; refreshKey: number; onChanged: () => void }) {
  const { data: instructions, reload } = useAsyncData<any[]>(
    () => get(`/api/v1/roles/${roleId}/instructions`), [roleId, refreshKey]);
  React.useEffect(() => {
    if (!selected && instructions?.length) onSelect(instructions[0].id);
  }, [instructions, selected]);
  const toggleActive = async (f: any) => {
    try {
      await patch(`/api/v1/instructions/${f.id}`, { active: f.active === 0 });
      reload();
      onChanged();
    } catch (e: any) { onError(e); }
  };
  return (
    <>
      <div className="spread" style={{ marginBottom: 10 }}>
        <div>
          <div className="mem-card-title" style={{ margin: 0 }}>Instruction Files</div>
          <div className="muted small">These files define how agents in this group should behave.</div>
        </div>
        <button className="btn small primary" onClick={() => onOpenModal('newInstruction')}>+ New Instruction</button>
      </div>
      <div className="instr-list">
        {(instructions ?? []).map((f) => (
          <div key={f.id} className={`instr-item ${selected === f.id ? 'active' : ''}`}
            style={f.active === 0 ? { opacity: 0.5 } : undefined}
            onClick={() => onSelect(f.id)}>
            <div className="group-icon">📄</div>
            <div className="grow" style={{ minWidth: 0 }}>
              <div className="group-name mono">{f.filename}{f.active === 0 ? ' (disabled)' : ''}</div>
              <div className="kv">{f.description}</div>
            </div>
            <span className="kv" style={{ whiteSpace: 'nowrap' }}>Updated {fmtRel(f.updated_at)}</span>
            <button className={`btn small ${f.active === 0 ? '' : 'primary'}`} title={f.active === 0 ? 'Enable' : 'Disable'}
              onClick={(e) => { e.stopPropagation(); toggleActive(f); }}>
              {f.active === 0 ? '⏻ Enable' : '⏻ Disable'}
            </button>
            <button className="btn small danger" onClick={async (e) => {
              e.stopPropagation();
              if (!confirm(`Delete ${f.filename}?`)) return;
              try { await del(`/api/v1/instructions/${f.id}`); onChanged(); } catch (err: any) { onError(err); }
            }}>⋮</button>
          </div>
        ))}
        {!instructions?.length && <div className="empty">No instruction files yet. Create the first one.</div>}
      </div>
    </>
  );
}

function EditorPanel({ roleId, selectedId, onSelect, onError, refreshKey, onChanged }:
  { roleId: string | null; selectedId: string | null; onSelect: (id: string | null) => void; onError: (e: any) => void;
    refreshKey: number; onChanged: () => void }) {
  const { data: instructions, reload } = useAsyncData<any[]>(
    () => (roleId ? get(`/api/v1/roles/${roleId}/instructions`) : Promise.resolve([])), [roleId, refreshKey]);
  const file = instructions?.find((f) => f.id === selectedId) ?? null;
  const [draft, setDraft] = React.useState('');
  const [mode, setMode] = React.useState<'edit' | 'preview'>('edit');
  const [saving, setSaving] = React.useState(false);
  const [savedTick, setSavedTick] = React.useState(0);
  const [warnings, setWarnings] = React.useState<string[]>([]);

  React.useEffect(() => { setDraft(file?.content ?? ''); setMode('edit'); setWarnings([]); }, [file?.id, file?.version]);

  // Keep selection valid when the refreshed list changes (e.g. after a
  // delete elsewhere) so the editor never sits on a ghost id.
  React.useEffect(() => {
    if (selectedId && instructions && !instructions.some((f) => f.id === selectedId)) {
      onSelect(instructions[0]?.id ?? null);
    }
  }, [instructions, selectedId]);

  if (!roleId) return null;
  if (!file) {
    return (
      <div className="mem-card col-editor"><div className="empty">Select an instruction file to view or edit it.</div></div>
    );
  }
  const dirty = draft !== file.content;
  const toggleActive = async () => {
    try {
      await patch(`/api/v1/instructions/${file.id}`, { active: file.active === 0 });
      onChanged();
    } catch (e: any) { onError(e); }
  };
  return (
    <div className="mem-card col-editor">
      <div className="spread">
        <div className="row" style={{ gap: 8 }}>
          <span className="group-icon">📄</span>
          <b className="mono">{file.filename}</b>
          <Badge kind="dim">v{file.version}</Badge>
          {file.active === 0 && <Badge kind="err">Disabled</Badge>}
        </div>
        <div className="btn-row">
          <button className={`btn small ${file.active === 0 ? '' : 'primary'}`} onClick={toggleActive}>
            {file.active === 0 ? '⏻ Enable' : '⏻ Disable'}
          </button>
          <button className="btn small danger" onClick={async () => {
            if (!confirm(`Delete ${file.filename}?`)) return;
            try { await del(`/api/v1/instructions/${file.id}`); onSelect(null); onChanged(); } catch (e: any) { onError(e); }
          }}>🗑</button>
        </div>
      </div>
      <div className="subtabs">
        <button className={`subtab ${mode === 'edit' ? 'active' : ''}`} onClick={() => setMode('edit')}>✎ Edit</button>
        <button className={`subtab ${mode === 'preview' ? 'active' : ''}`} onClick={() => setMode('preview')}>👁 Preview</button>
      </div>
      <div className="editor-holder">
        {mode === 'edit'
          ? <CodeEditor value={draft} onChange={setDraft} />
          : <div className="md-preview"><MarkdownPreview text={draft} /></div>}
      </div>
      <LintWarnings list={warnings} />
      <div className="spread" style={{ marginTop: 10 }}>
        <span className="kv">Last saved: {fmtRel(file.updated_at)}{savedTick > 0 && !dirty ? ' ✓' : ''}</span>
        <div className="btn-row">
          <button className="btn small" disabled={!dirty} onClick={() => setDraft(file.content)}>Cancel</button>
          <button className="btn small primary" disabled={!dirty || saving} onClick={async () => {
            setSaving(true);
            try {
              const updated = await patch(`/api/v1/instructions/${file.id}`, { content: draft });
              setWarnings(updated.lint_warnings ?? []);
              setSavedTick((t) => t + 1);
              onChanged();
            } catch (e: any) { onError(e); } finally { setSaving(false); }
          }}>Save Changes</button>
        </div>
      </div>
      <HistoryPanel kind="instructions" id={file.id} onChanged={onChanged} onError={onError} />
    </div>
  );
}

function SkillsPanel({ roleId, allSkills, onError, onChanged }:
  { roleId: string; allSkills: any[]; onError: (e: any) => void; onChanged: () => void }) {
  const { data: attached, reload } = useAsyncData<any[]>(() => get(`/api/v1/roles/${roleId}/skills`), [roleId]);
  const available = allSkills.filter((s) => !(attached ?? []).some((a) => a.id === s.id));
  const toggle = async (sid: string, on: boolean) => {
    try {
      if (on) await del(`/api/v1/roles/${roleId}/skills/${sid}`);
      else await post(`/api/v1/roles/${roleId}/skills/${sid}`);
      reload(); onChanged();
    } catch (e: any) { onError(e); }
  };
  return (
    <>
      <div className="mem-card-title">Attached Skills</div>
      <div className="row" style={{ marginBottom: 14 }}>
        {(attached ?? []).map((s) => (
          <span key={s.id} className="skill-chip on" onClick={() => toggle(s.id, true)}>
            {s.name} <b>✕</b>
          </span>
        ))}
        {!attached?.length && <span className="kv">No skills attached yet.</span>}
      </div>
      <div className="mem-card-title">Available Skills</div>
      <div className="row">
        {available.map((s) => (
          <span key={s.id} className="skill-chip" onClick={() => toggle(s.id, false)} title={s.description}>
            + {s.name}
          </span>
        ))}
        {!available.length && <span className="kv">All skills attached.</span>}
      </div>
    </>
  );
}

function InstructionForm({ roleId, onClose, onSaved }: any) {
  const [f, setF] = React.useState({ filename: '', description: '', content: '# New Instruction\n\n- Point one\n- Point two' });
  return (
    <Modal title="New Instruction File" onClose={onClose}>
      <Field label="File name"><input value={f.filename} onChange={(e) => setF({ ...f, filename: e.target.value })} placeholder="coding-standards" /></Field>
      <Field label="Description"><input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <Field label="Markdown content"><textarea value={f.content} rows={8} onChange={(e) => setF({ ...f, content: e.target.value })} /></Field>
      <button className="btn primary" disabled={!f.filename} onClick={async () => {
        try {
          const instr = await post(`/api/v1/roles/${roleId}/instructions`, f);
          onSaved(instr.id);
        } catch (e: any) { alert(e.message); }
      }}>Create Instruction</button>
    </Modal>
  );
}

function RoleGroupForm({ role, onClose, onSaved }: { role?: any; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = React.useState({ name: role?.name ?? '', description: role?.description ?? '' });
  return (
    <Modal title={role ? `Edit Group: ${role.name}` : 'New Role Group'} onClose={onClose}>
      <Field label="Group name"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
      <Field label="Description"><input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <button className="btn primary" disabled={!f.name} onClick={async () => {
        try {
          if (role) await patch(`/api/v1/roles/${role.id}`, f);
          else await post('/api/v1/roles', f);
          onSaved();
        } catch (e: any) { alert(e.message); }
      }}>{role ? 'Save Group' : 'Create Group'}</button>
    </Modal>
  );
}

function AssignAgentModal({ role, agents, onClose, onSaved, onError }: any) {
  const candidates = agents.filter((a: any) => a.role_id !== role.id);
  const [agentId, setAgentId] = React.useState('');
  const agent = agents.find((a: any) => a.id === agentId);
  const { data: personas } = useAsyncData<any[]>(
    () => (agentId ? get(`/api/v1/personas?role_id=${role.id}`) : Promise.resolve([])), [agentId, role.id]);
  return (
    <Modal title={`Assign Agent to ${role.name}`} onClose={onClose}>
      <Field label="Agent">
        <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          <option value="">— select agent —</option>
          {candidates.map((a: any) => <option key={a.id} value={a.id}>{a.name} (currently {a.role_name})</option>)}
        </select>
      </Field>
      {agentId && (
        <Field label="Persona for this role">
          <select defaultValue="">
            <option value="">— first persona of role —</option>
            {(personas ?? []).map((p: any) => <option key={p.id} value={p.id}>{p.name} (v{p.version})</option>)}
          </select>
        </Field>
      )}
      <button className="btn primary" disabled={!agentId} onClick={async () => {
        try {
          const persona = (personas ?? [])[0];
          await patch(`/api/v1/agents/${agentId}`, { role_id: role.id, persona_id: persona?.id ?? agent?.persona_id });
          onSaved();
        } catch (e: any) { onError(e); }
      }}>Assign Agent</button>
      {!candidates.length && <div className="kv" style={{ marginTop: 10 }}>All agents already use this role.</div>}
    </Modal>
  );
}

function PersonaPanel({ roleId, selected, onSelect, onError, onOpenModal }:
  { roleId: string; selected: string | null; onSelect: (id: string) => void; onError: (e: any) => void; onOpenModal: (m: string) => void }) {
  const { data: personas, reload } = useAsyncData<any[]>(() => get(`/api/v1/personas?role_id=${roleId}`), [roleId]);
  React.useEffect(() => {
    if (!selected && personas?.length) onSelect(personas[0].id);
  }, [personas, selected]);
  return (
    <>
      <div className="spread" style={{ marginBottom: 10 }}>
        <div>
          <div className="mem-card-title" style={{ margin: 0 }}>Personas</div>
          <div className="muted small">System prompt + constraints for agents with this role. Editing bumps the version.</div>
        </div>
        <button className="btn small primary" onClick={() => onOpenModal('newPersona')}>+ New Persona</button>
      </div>
      <div className="instr-list">
        {(personas ?? []).map((p) => (
          <div key={p.id} className={`instr-item ${selected === p.id ? 'active' : ''}`} onClick={() => onSelect(p.id)}>
            <div className="group-icon">🎭</div>
            <div className="grow" style={{ minWidth: 0 }}>
              <div className="group-name">{p.name}</div>
              <div className="kv">{p.description || 'No description'}</div>
            </div>
            <Badge kind="dim">v{p.version}</Badge>
            <button className="btn small danger" onClick={async (e) => {
              e.stopPropagation();
              if (!confirm(`Delete persona "${p.name}"?`)) return;
              try { await del(`/api/v1/personas/${p.id}`); if (selected === p.id) onSelect(''); reload(); } catch (err: any) { onError(err); }
            }}>⋮</button>
          </div>
        ))}
        {!personas?.length && <div className="empty">No personas yet. Create the first one.</div>}
      </div>
    </>
  );
}

function PersonaEditor({ roleId, personaId, onSelect, onError, reloadRoles }:
  { roleId: string | null; personaId: string | null; onSelect: (id: string) => void; onError: (e: any) => void; reloadRoles: () => void }) {
  const { data: personas, reload: reloadPersonas } = useAsyncData<any[]>(
    () => (roleId ? get(`/api/v1/personas?role_id=${roleId}`) : Promise.resolve([])), [roleId, personaId]);
  const persona = personas?.find((p) => p.id === personaId) ?? null;
  const [f, setF] = React.useState({ name: '', description: '', instructions: '', constraints_text: '' });
  const [saving, setSaving] = React.useState(false);
  const [savedTick, setSavedTick] = React.useState(0);
  const [warnings, setWarnings] = React.useState<string[]>([]);

  React.useEffect(() => {
    if (persona) setF({ name: persona.name, description: persona.description ?? '',
      instructions: persona.instructions ?? '', constraints_text: persona.constraints_text ?? '' });
  }, [persona?.id, persona?.version]);
  React.useEffect(() => { setWarnings([]); }, [persona?.id]);

  if (!roleId) return null;
  if (!persona) {
    return <div className="mem-card col-editor"><div className="empty">Select a persona to view or edit it.</div></div>;
  }
  const dirty = f.name !== persona.name || f.description !== persona.description ||
    f.instructions !== persona.instructions || f.constraints_text !== persona.constraints_text;
  return (
    <div className="mem-card col-editor">
      <div className="spread">
        <div className="row" style={{ gap: 8 }}>
          <span className="group-icon">🎭</span>
          <b>{persona.name}</b>
          <Badge kind="dim">v{persona.version}</Badge>
          {savedTick > 0 && <Badge kind="ok">Saved v{savedTick}</Badge>}
        </div>
      </div>
      <div className="persona-editor-fields">
        <Field label="Persona name"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label="Description"><input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
        <Field label="Instructions (system prompt)">
          <CodeEditor value={f.instructions} onChange={(v) => setF({ ...f, instructions: v })} />
        </Field>
        <Field label="Constraints (one per line)">
          <textarea style={{ minHeight: 80 }} value={f.constraints_text}
            onChange={(e) => setF({ ...f, constraints_text: e.target.value })} />
        </Field>
      </div>
      <LintWarnings list={warnings} />
      <div className="btn-row" style={{ marginTop: 10 }}>
        <button className="btn primary" disabled={!dirty || saving || !f.name} onClick={async () => {
          setSaving(true);
          try {
            const updated = await patch(`/api/v1/personas/${persona.id}`, f);
            setWarnings(updated.lint_warnings ?? []);
            setSavedTick(updated.version);
            reloadRoles();
          } catch (e: any) { onError(e); }
          finally { setSaving(false); }
        }}>{saving ? 'Saving…' : dirty ? 'Save (bumps version)' : 'Saved'}</button>
        <button className="btn danger" onClick={async () => {
          if (!confirm(`Delete persona "${persona.name}"?`)) return;
          try { await del(`/api/v1/personas/${persona.id}`); onSelect(''); reloadRoles(); } catch (e: any) { onError(e); }
        }}>Delete</button>
      </div>
      <HistoryPanel kind="personas" id={persona.id} onChanged={() => { reloadPersonas(); reloadRoles(); }} onError={onError} />
    </div>
  );
}

function NewPersonaModal({ roleId, onClose, onSaved, onError }: any) {
  const [f, setF] = React.useState({ name: '', description: '', instructions: '', constraints_text: '' });
  return (
    <Modal title="New Persona" onClose={onClose}>
      <Field label="Persona name"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
      <Field label="Description"><input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <Field label="Instructions (system prompt)">
        <textarea style={{ minHeight: 140 }} value={f.instructions}
          onChange={(e) => setF({ ...f, instructions: e.target.value })}
          placeholder="You are a …" />
      </Field>
      <Field label="Constraints (one per line)">
        <textarea style={{ minHeight: 70 }} value={f.constraints_text}
          onChange={(e) => setF({ ...f, constraints_text: e.target.value })} />
      </Field>
      <button className="btn primary" disabled={!f.name} onClick={async () => {
        try {
          const created = await post(`/api/v1/roles/${roleId}/personas`, f);
          onSaved(created.id);
        } catch (e: any) { onError(e); }
      }}>Create Persona</button>
    </Modal>
  );
}
