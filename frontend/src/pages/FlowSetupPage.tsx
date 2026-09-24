import React from 'react';
import { del, get, post, put } from '../api';
import { Badge, ErrorNote, Field, Modal } from '../components';

interface FlowTeam { role: string; count: number }
interface DesignStep {
  seq?: number; role: string; processes: string[]; approval_required: boolean;
}
interface Flow {
  id: string; name: string; description: string; stages: string[]; team: FlowTeam[];
  design: DesignStep[];
  docs_gate: number; po_enabled: number; is_default: number; is_builtin: number;
  projects_using: number;
}
interface StageKind { key: string; kind: string; role: string; label: string }
interface ProcessItem { process: string; kind: string }

const KIND_ICON: Record<string, string> = {
  implement: '💻', review: '🔍', test: '🧪', approve: '🛑',
};

export default function FlowSetupPage() {
  const [flows, setFlows] = React.useState<Flow[] | null>(null);
  const [kinds, setKinds] = React.useState<StageKind[]>([]);
  const [processes, setProcesses] = React.useState<ProcessItem[]>([]);
  const [roles, setRoles] = React.useState<{ id: string; name: string }[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [editing, setEditing] = React.useState<Flow | 'new' | null>(null);

  const load = React.useCallback(() => {
    get<Flow[]>('/api/v1/flows').then(setFlows).catch((e) => setError(e?.message || 'Failed to load flows'));
  }, []);
  React.useEffect(() => {
    load();
    get<StageKind[]>('/api/v1/flows/stage_kinds').then(setKinds).catch(() => undefined);
    get<ProcessItem[]>('/api/v1/flows/process_catalog').then(setProcesses).catch(() => undefined);
    get<any>('/api/v1/roles').then((r) => setRoles(Array.isArray(r) ? r : r?.items ?? []))
      .catch(() => undefined);
  }, [load]);

  const kindOf = (key: string) => kinds.find((k) => k.key === key);

  async function remove(flow: Flow) {
    if (!window.confirm(`Delete flow "${flow.name}"?`)) return;
    try {
      await del(`/api/v1/flows/${flow.id}`);
      load();
    } catch (e: any) { setError(e?.message || 'Delete failed'); }
  }
  async function setDefault(flow: Flow) {
    try {
      await put(`/api/v1/flows/${flow.id}`, { ...flow, is_default: 1 });
      load();
    } catch (e: any) { setError(e?.message || 'Update failed'); }
  }

  return (
    <div style={{ padding: 18, maxWidth: 1000 }}>
      <div className="spread" style={{ marginBottom: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>Flow Setup</h2>
          <div className="muted small">
            Reusable SDLC templates: the design phase that precedes delivery, the per-task
            stage chain, and the team that runs it. New projects pick one flow at creation.
          </div>
        </div>
        <button className="btn primary" onClick={() => setEditing('new')}>+ New Flow</button>
      </div>
      {error && <ErrorNote error={error} />}
      {!flows ? <div className="card muted">Loading flows…</div> : flows.length === 0 ? (
        <div className="card muted">No flows yet — create one to get started.</div>
      ) : flows.map((f) => (
        <div className="card" key={f.id}>
          <div className="spread">
            <div className="row">
              <b>{f.name}</b>
              {f.is_default === 1 && <Badge kind="info">default</Badge>}
              {f.is_builtin === 1 && <Badge kind="dim">built-in</Badge>}
              {!!f.design?.length && <Badge kind="warn">design phase</Badge>}
              {f.po_enabled === 1 && <Badge kind="warn">PO autonomous</Badge>}
              <span className="muted small">{f.projects_using} project(s)</span>
            </div>
            <div className="btn-row">
              <button className="btn small" onClick={() => setEditing(f)}>Edit</button>
              {f.is_default !== 1 && (
                <button className="btn small" onClick={() => setDefault(f)}>Make default</button>)}
              <button className="btn small danger" onClick={() => remove(f)}>Delete</button>
            </div>
          </div>
          <div className="row small muted" style={{ marginTop: 10 }}>
            <b style={{ minWidth: 96 }}>Design phase:</b>
            {(f.design?.length ?? 0) === 0 ? <span>none — goes straight to the stage chain</span>
              : f.design.map((s, i) => (
                <React.Fragment key={s.seq ?? i}>
                  {i > 0 && <span className="muted">→</span>}
                  <span className="chip" title={s.processes.join(', ')}>
                    {i + 1}. {s.role}{s.approval_required ? ' 🛑' : ''}
                  </span>
                </React.Fragment>
              ))}
          </div>
          <div className="row small muted" style={{ marginTop: 6 }}>
            <b style={{ minWidth: 96 }}>Stage chain:</b>
            {f.stages.map((s, i) => {
              const k = kindOf(s);
              return (
                <React.Fragment key={s}>
                  {i > 0 && <span className="muted">→</span>}
                  <span className="chip" title={`${k?.kind ?? ''} · ${k?.role ?? ''}`}>
                    {KIND_ICON[k?.kind ?? ''] ?? '•'} {k?.label ?? s}
                  </span>
                </React.Fragment>
              );
            })}
          </div>
          <div className="row small muted" style={{ marginTop: 8 }}>
            <b style={{ minWidth: 96 }}>Team:</b>
            {f.team.length === 0 ? <span>no team spec — assign a team manually</span>
              : f.team.map((t) => <span className="chip" key={t.role}>{t.count}× {t.role}</span>)}
          </div>
          {f.description && <div className="small muted" style={{ marginTop: 8 }}>{f.description}</div>}
        </div>
      ))}
      {editing && (
        <FlowForm flow={editing === 'new' ? null : editing} kinds={kinds} processes={processes}
          roles={roles} onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); load(); }}
          onError={setError} />
      )}
    </div>
  );
}

function FlowForm({ flow, kinds, processes, roles, onClose, onSaved, onError }: {
  flow: Flow | null; kinds: StageKind[]; processes: ProcessItem[];
  roles: { id: string; name: string }[];
  onClose: () => void; onSaved: () => void; onError: (m: string) => void;
}) {
  const [name, setName] = React.useState(flow?.name ?? '');
  const [description, setDescription] = React.useState(flow?.description ?? '');
  const [design, setDesign] = React.useState<DesignStep[]>(
    (flow?.design ?? []).map((s) => ({ role: s.role, processes: [...(s.processes ?? [])],
      approval_required: !!s.approval_required })));
  const [stages, setStages] = React.useState<string[]>(flow?.stages ?? ['dev', 'sa', 'ba', 'qa']);
  const [team, setTeam] = React.useState<FlowTeam[]>(flow?.team ?? []);
  const [poEnabled, setPoEnabled] = React.useState<number>(flow?.po_enabled ?? 0);
  const [isDefault, setIsDefault] = React.useState<number>(flow?.is_default ?? 0);
  const [busy, setBusy] = React.useState(false);
  const [newStage, setNewStage] = React.useState('approve');
  const [newRole, setNewRole] = React.useState('');

  const roleNames = roles.map((r) => r.name);
  const usable = kinds.filter((k) => !stages.includes(k.key));

  function moveStage(i: number, dir: -1 | 1) {
    const j = i + dir;
    if (j < 0 || j >= stages.length) return;
    const next = [...stages];
    [next[i], next[j]] = [next[j], next[i]];
    setStages(next);
  }

  function addStep() {
    const role = roleNames.find((r) => !design.some((s) => s.role === r)) ?? roleNames[0] ?? '';
    setDesign([...design, { role, processes: [], approval_required: true }]);
  }
  function moveStep(i: number, dir: -1 | 1) {
    const j = i + dir;
    if (j < 0 || j >= design.length) return;
    const next = [...design];
    [next[i], next[j]] = [next[j], next[i]];
    setDesign(next);
  }
  function patchStep(i: number, patch: Partial<DesignStep>) {
    const next = [...design];
    next[i] = { ...next[i], ...patch };
    setDesign(next);
  }
  function toggleProcess(i: number, proc: string) {
    const cur = design[i].processes;
    patchStep(i, { processes: cur.includes(proc) ? cur.filter((p) => p !== proc)
      : [...cur, proc] });
  }

  async function save() {
    setBusy(true);
    const body = {
      name, description, stages, team,
      design: design.map((s, i) => ({ seq: i + 1, role: s.role,
        processes: s.processes, approval_required: s.approval_required })),
      po_enabled: poEnabled, is_default: isDefault,
    };
    try {
      if (flow) await put(`/api/v1/flows/${flow.id}`, body);
      else await post('/api/v1/flows', body);
      onSaved();
    } catch (e: any) {
      onError(e?.message || 'Save failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={flow ? `Edit flow — ${flow.name}` : 'New Flow'} onClose={onClose}>
      <Field label="Name"><input className="input" value={name}
        onChange={(e) => setName(e.target.value)} placeholder="e.g. Fast Track (no BA review)" /></Field>
      <Field label="Description"><input className="input" value={description}
        onChange={(e) => setDescription(e.target.value)} placeholder="What this flow is for" /></Field>

      <Field label="Design phase (before the stage chain)">
        <div className="card" style={{ padding: 10 }}>
          <div className="small muted" style={{ marginBottom: 8 }}>
            A strictly sequential pre-delivery workflow. Each step's role authors its
            documents; steps with an approval gate pause until approved — a rejection
            returns the step to its role for revision.
          </div>
          {design.length === 0 && <div className="small muted">No steps — the project goes
            straight from the requirement to the stage chain.</div>}
          {design.length > 0 && (
            <div className="row small muted" style={{ marginBottom: 4, fontWeight: 600 }}>
              <span style={{ width: 26 }}>Seq</span>
              <span style={{ minWidth: 150 }}>Role</span>
              <span style={{ flex: 1 }}>Processes</span>
              <span style={{ width: 70 }}>Approve?</span>
              <span style={{ width: 78 }} />
            </div>
          )}
          {design.map((s, i) => (
            <div key={i} style={{ marginBottom: 10, paddingBottom: 10,
              borderBottom: i < design.length - 1 ? '1px solid var(--border, #333)' : 'none' }}>
              <div className="row">
                <span className="muted small" style={{ width: 26 }}>{i + 1}.</span>
                <select className="input" style={{ minWidth: 150 }} value={s.role}
                  onChange={(e) => patchStep(i, { role: e.target.value })}>
                  {!roleNames.includes(s.role) && <option value={s.role}>{s.role || 'select role…'}</option>}
                  {roleNames.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
                <label className="row small" style={{ width: 70 }}>
                  <input type="checkbox" checked={s.approval_required}
                    onChange={(e) => patchStep(i, { approval_required: e.target.checked })} />
                  needed</label>
                <span style={{ flex: 1 }} />
                <button className="btn small" disabled={i === 0} onClick={() => moveStep(i, -1)}>↑</button>
                <button className="btn small" disabled={i === design.length - 1}
                  onClick={() => moveStep(i, 1)}>↓</button>
                <button className="btn small danger"
                  onClick={() => setDesign(design.filter((_, x) => x !== i))}>×</button>
              </div>
              <div className="row" style={{ marginTop: 6, flexWrap: 'wrap', gap: 6, paddingLeft: 32 }}>
                {processes.map((p) => (
                  <label key={p.process} className="chip small"
                    style={{ cursor: 'pointer', opacity: s.processes.includes(p.process) ? 1 : 0.55,
                             outline: s.processes.includes(p.process) ? '1px solid var(--accent, #888)' : 'none' }}>
                    <input type="checkbox" style={{ marginRight: 4 }}
                      checked={s.processes.includes(p.process)}
                      onChange={() => toggleProcess(i, p.process)} />
                    {p.process} <span className="muted">({p.kind})</span>
                  </label>
                ))}
              </div>
            </div>
          ))}
          <button className="btn small" style={{ marginTop: 6 }} onClick={addStep}
            disabled={roleNames.length === 0}>+ Add step</button>
        </div>
      </Field>

      <Field label="Stage chain (per task)">
        <div className="card" style={{ padding: 10 }}>
          {stages.map((s, i) => {
            const k = kinds.find((x) => x.key === s);
            return (
              <div className="row" key={s} style={{ marginBottom: 6 }}>
                <span className="muted small" style={{ width: 18 }}>{i + 1}.</span>
                <span>{KIND_ICON[k?.kind ?? ''] ?? '•'}</span>
                <b style={{ minWidth: 130 }}>{k?.label ?? s}</b>
                <span className="muted small">{k?.kind} · {k?.role}</span>
                <span style={{ flex: 1 }} />
                <button className="btn small" disabled={i === 0} onClick={() => moveStage(i, -1)}>↑</button>
                <button className="btn small" disabled={i === stages.length - 1}
                  onClick={() => moveStage(i, 1)}>↓</button>
                <button className="btn small danger" disabled={s === 'dev'}
                  onClick={() => setStages(stages.filter((x) => x !== s))}>×</button>
              </div>
            );
          })}
          <div className="row" style={{ marginTop: 8 }}>
            <select className="input" value={newStage} onChange={(e) => setNewStage(e.target.value)}>
              {usable.map((k) => <option key={k.key} value={k.key}>{k.label} ({k.kind})</option>)}
            </select>
            <button className="btn small" disabled={!usable.some((k) => k.key === newStage)}
              onClick={() => setStages([...stages, newStage])}>+ Add stage</button>
          </div>
        </div>
      </Field>

      <Field label="Team (provisioned when a new project picks this flow)">
        <div className="card" style={{ padding: 10 }}>
          {team.map((t, i) => (
            <div className="row" key={t.role} style={{ marginBottom: 6 }}>
              <b style={{ minWidth: 160 }}>{t.role}</b>
              <input className="input" type="number" min={1} max={9} style={{ width: 64 }}
                value={t.count}
                onChange={(e) => {
                  const next = [...team];
                  next[i] = { ...t, count: Math.max(1, Number(e.target.value) || 1) };
                  setTeam(next);
                }} />
              <span className="muted small">member(s)</span>
              <span style={{ flex: 1 }} />
              <button className="btn small danger"
                onClick={() => setTeam(team.filter((x) => x.role !== t.role))}>×</button>
            </div>
          ))}
          <div className="row" style={{ marginTop: 8 }}>
            <select className="input" value={newRole} onChange={(e) => setNewRole(e.target.value)}>
              <option value="">add role…</option>
              {roleNames.filter((r) => !team.some((t) => t.role === r))
                .map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
            <button className="btn small" disabled={!newRole}
              onClick={() => { setTeam([...team, { role: newRole, count: 1 }]); setNewRole(''); }}>
              + Add role</button>
          </div>
        </div>
      </Field>

      <div className="row" style={{ marginTop: 4 }}>
        <label className="row small"><input type="checkbox" checked={poEnabled === 1}
          onChange={(e) => setPoEnabled(e.target.checked ? 1 : 0)} />
          PO autonomous (auto-reviews design-phase approval gates)</label>
        <label className="row small"><input type="checkbox" checked={isDefault === 1}
          onChange={(e) => setIsDefault(e.target.checked ? 1 : 0)} />
          Default for new projects</label>
      </div>

      <div className="btn-row" style={{ marginTop: 14 }}>
        <button className="btn primary" disabled={busy || !name.trim()} onClick={save}>
          {flow ? 'Save flow' : 'Create flow'}</button>
        <button className="btn" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}
