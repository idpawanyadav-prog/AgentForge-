import React from 'react';
import { del, get, post, put } from '../api';
import { Badge, ErrorNote, Field } from '../components';

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

const ROLE_ABBR: Record<string, string> = {
  'Business Analyst': 'BA', 'Solution Architect': 'SA', 'Senior Developer': 'Sr Dev',
  'Junior Developer': 'Jr Dev', 'QA Engineer': 'QA', 'Product Owner': 'PO',
  'UI/UX Designer': 'Designer', 'DevOps Engineer': 'DevOps',
};
function abbr(name: string): string {
  return ROLE_ABBR[name]
    || name.split(/[\s/]+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 3).toUpperCase();
}

// Low-alpha hues so the tint reads as pastel on the bright theme and muted on
// dark, while the label text stays theme-driven (readable in both).
const HUES: [number, number, number][] = [
  [79, 142, 247], [155, 108, 222], [63, 185, 80], [210, 153, 34],
  [248, 81, 120], [47, 169, 184], [224, 108, 63],
];
function hueOf(name: string): [number, number, number] {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return HUES[h % HUES.length];
}
function roleStyle(name: string): React.CSSProperties {
  const [r, g, b] = hueOf(name);
  return {
    background: `rgba(${r},${g},${b},0.12)`,
    borderColor: `rgba(${r},${g},${b},0.4)`,
    ['--hue' as any]: `rgb(${r},${g},${b})`,
  } as React.CSSProperties;
}

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

  if (editing) {
    return (
      <div className="page">
        <FlowEditor flow={editing === 'new' ? null : editing} kinds={kinds} processes={processes}
          roles={roles} onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); load(); }} onError={setError} />
      </div>
    );
  }

  return (
    <div className="page">
      <div style={{ maxWidth: 1000, margin: '0 auto' }}>
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
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ editor

// A "cr" (code review) stage is not shown as its own row: it is folded into
// the row for the stage it reviews, surfaced as that row's Code Review
// checkbox. These helpers convert between the stored `stages` key list (which
// does contain "cr") and the editor's visible rows.
interface DevRow { key: string; cr: boolean }
function stagesToRows(stages: string[]): DevRow[] {
  const rows: DevRow[] = [];
  for (const s of stages) {
    if (s === 'cr' && rows.length) rows[rows.length - 1].cr = true;
    else rows.push({ key: s, cr: false });
  }
  return rows;
}
function rowsToStages(rows: DevRow[]): string[] {
  return rows.flatMap((r) => (r.cr ? [r.key, 'cr'] : [r.key]));
}

function FlowEditor({ flow, kinds, processes, roles, onClose, onSaved, onError }: {
  flow: Flow | null; kinds: StageKind[]; processes: ProcessItem[];
  roles: { id: string; name: string }[];
  onClose: () => void; onSaved: () => void; onError: (m: string) => void;
}) {
  const [name, setName] = React.useState(flow?.name ?? '');
  const [description, setDescription] = React.useState(flow?.description ?? '');
  const [design, setDesign] = React.useState<DesignStep[]>(
    (flow?.design ?? []).map((s) => ({ role: s.role, processes: [...(s.processes ?? [])],
      approval_required: !!s.approval_required })));
  const [devRows, setDevRows] = React.useState<DevRow[]>(
    stagesToRows(flow?.stages ?? ['dev', 'sa', 'ba', 'qa']));
  const [team, setTeam] = React.useState<FlowTeam[]>(flow?.team ?? []);
  const [poEnabled, setPoEnabled] = React.useState<number>(flow?.po_enabled ?? 0);
  const [isDefault, setIsDefault] = React.useState<number>(flow?.is_default ?? 0);
  const [busy, setBusy] = React.useState(false);

  const roleNames = roles.map((r) => r.name);
  const kindOf = (key: string) => kinds.find((k) => k.key === key);
  const roleLabel = (role: string) => `${abbr(role)} (${role})`;

  // ---- team member selection (backed by the existing team spec) ----
  const teamByRole = new Map(team.map((t) => [t.role, t]));
  function toggleRole(role: string) {
    if (teamByRole.has(role)) setTeam(team.filter((t) => t.role !== role));
    else setTeam([...team, { role, count: 1 }]);
  }
  function setCount(role: string, n: number) {
    setTeam(team.map((t) => t.role === role ? { ...t, count: Math.max(1, n) } : t));
  }

  // ---- design phase steps ----
  function addDesignStep() {
    const role = roleNames.find((r) => !design.some((s) => s.role === r)) ?? roleNames[0] ?? '';
    setDesign([...design, { role, processes: [], approval_required: true }]);
  }
  function patchStep(i: number, patch: Partial<DesignStep>) {
    const next = [...design]; next[i] = { ...next[i], ...patch }; setDesign(next);
  }
  function removeDesignStep(i: number) { setDesign(design.filter((_, x) => x !== i)); }
  function moveDesign(i: number, to: number) {
    const next = [...design];
    const [row] = next.splice(i, 1);
    next.splice(Math.max(0, Math.min(next.length, to)), 0, row);
    setDesign(next);
  }

  // ---- development flow (fixed stage-kind chain; "cr" = per-row code review) ----
  const addableKinds = kinds.filter((k) => k.key !== 'cr');
  function addDevStep() {
    const next = addableKinds.find((k) => !devRows.some((r) => r.key === k.key));
    if (next) setDevRows([...devRows, { key: next.key, cr: false }]);
  }
  function setDevStep(i: number, key: string) {
    const next = devRows.map((r, x) => (x === i ? { ...r, key } : r));
    setDevRows(next);
  }
  function toggleDevCR(i: number, on: boolean) {
    setDevRows(devRows.map((r, x) => (x === i ? { ...r, cr: on } : r)));
  }
  function removeDevStep(i: number) { setDevRows(devRows.filter((_, x) => x !== i)); }
  function moveDev(i: number, to: number) {
    const next = [...devRows];
    const [row] = next.splice(i, 1);
    // The Developer stage must stay first.
    const idx = (row.key === 'dev') ? 0 : Math.max(next[0]?.key === 'dev' ? 1 : 0,
      Math.min(next.length, to));
    next.splice(idx, 0, row);
    setDevRows(next);
  }

  async function save() {
    setBusy(true);
    const body = {
      name, description, stages: rowsToStages(devRows), team,
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
    <div className="flow-editor">
      <div className="page-header">
        <div className="row" style={{ gap: 12 }}>
          <button className="btn small back-btn" onClick={onClose} title="Back to flows">←</button>
          <h1 style={{ margin: 0 }}>{flow ? `Edit Flow — ${flow.name}` : 'Create Flow'}</h1>
        </div>
      </div>

      {/* 1 — Flow Details & Team Members */}
      <section className="flowsec flowsec-1">
        <SectionHead n={1} title="Flow Details & Team Members" />
        <div className="flowsec-body two-col">
          <div>
            <Field label="Flow Name *">
              <input className="input" value={name} onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Web Application Development" />
            </Field>
            <Field label="Description">
              <textarea className="input" value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="What this flow is for" />
            </Field>
          </div>
          <div>
            <div className="field-label">Team Members *</div>
            <div className="muted small" style={{ marginBottom: 8 }}>
              Select team members who can participate in this flow based on available roles.
            </div>
            <div className="member-grid">
              {roleNames.map((r) => {
                const sel = teamByRole.has(r);
                return (
                  <div key={r} className={`member-card${sel ? ' sel' : ''}`} style={roleStyle(r)}
                    onClick={() => toggleRole(r)}>
                    <input type="checkbox" checked={sel} readOnly tabIndex={-1} />
                    <span className="member-avatar">👤</span>
                    <span className="member-name">
                      <b>{abbr(r)}</b> <span className="muted small">({r})</span>
                    </span>
                    {sel && (
                      <span className="member-count" onClick={(e) => e.stopPropagation()}>
                        <button className="mc-btn" onClick={() => setCount(r, teamByRole.get(r)!.count - 1)}>−</button>
                        <span>{teamByRole.get(r)!.count}</span>
                        <button className="mc-btn" onClick={() => setCount(r, teamByRole.get(r)!.count + 1)}>+</button>
                      </span>
                    )}
                  </div>
                );
              })}
              <AddMemberButton roles={roleNames} selected={teamByRole} onAdd={toggleRole} />
            </div>
          </div>
        </div>
      </section>

      {/* 2 — Design Phase */}
      <section className="flowsec flowsec-2">
        <SectionHead n={2} title="Design Phase"
          sub="Define the design phase workflow from initial requirement to supporting documents. Select documents for each role and configure approval." />
        <div className="flowsec-body">
          <div className="flow-table">
            <div className="ft-row ft-head">
              <span className="ft-seq">Sequence</span>
              <span className="ft-role">Role</span>
              <span className="ft-docs">Documents (Multi-select)</span>
              <span className="ft-approve">Approval Required</span>
              <span className="ft-act" />
            </div>
            {design.length === 0 && (
              <div className="small muted" style={{ padding: '10px 4px' }}>
                No steps — the project goes straight from the requirement to the development flow.
              </div>
            )}
            {design.map((s, i) => (
              <div className="ft-row" key={i}>
                <input className="seq-input" type="number" min={1} max={design.length} value={i + 1}
                  onChange={(e) => { const to = (Number(e.target.value) || 1) - 1; if (to !== i) moveDesign(i, to); }} />
                <select className="input ft-role" value={s.role}
                  onChange={(e) => patchStep(i, { role: e.target.value })}>
                  {!roleNames.includes(s.role) && <option value={s.role}>{s.role || 'select role…'}</option>}
                  {roleNames.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
                </select>
                <div className="ft-docs">
                  <DocMultiSelect value={s.processes} options={processes}
                    onChange={(next) => patchStep(i, { processes: next })} />
                </div>
                <span className="ft-approve">
                  <input type="checkbox" checked={s.approval_required}
                    onChange={(e) => patchStep(i, { approval_required: e.target.checked })} />
                </span>
                <span className="ft-act">
                  <button className="icon-btn danger" title="Remove step"
                    onClick={() => removeDesignStep(i)}>🗑</button>
                </span>
              </div>
            ))}
          </div>
          <button className="btn small add-btn" onClick={addDesignStep}
            disabled={roleNames.length === 0}>+ Add Design Phase Step</button>
        </div>
      </section>

      {/* 3 — Development Flow */}
      <section className="flowsec flowsec-3">
        <SectionHead n={3} title="Development Flow"
          sub="Define the development flow sequence. Agents cannot be duplicated in this flow." />
        <div className="flowsec-body">
          <div className="flow-table">
            <div className="ft-row ft-head">
              <span className="ft-seq">Sequence</span>
              <span className="ft-role2">Agent / Role</span>
              <span className="ft-cr">Code Review</span>
              <span className="ft-act">Actions</span>
            </div>
            {devRows.map((row, i) => {
              const k = kindOf(row.key);
              const crAllowed = !!k && (k.kind === 'implement' || k.kind === 'test');
              return (
                <div className="ft-row" key={i}>
                  <input className="seq-input" type="number" min={1} max={devRows.length} value={i + 1}
                    disabled={row.key === 'dev'}
                    onChange={(e) => { const to = (Number(e.target.value) || 1) - 1; if (to !== i) moveDev(i, to); }} />
                  <select className="input ft-role2" value={row.key} disabled={row.key === 'dev'}
                    onChange={(e) => setDevStep(i, e.target.value)}>
                    {addableKinds.map((kk) => {
                      const usedElsewhere = devRows.some((r, x) => r.key === kk.key && x !== i);
                      return <option key={kk.key} value={kk.key} disabled={usedElsewhere}>
                        {roleLabel(kk.role)}{kk.key === 'dev' ? '' : ` · ${kk.label}`}
                      </option>;
                    })}
                  </select>
                  <span className="ft-cr">
                    <input type="checkbox" checked={row.cr} disabled={!crAllowed}
                      onChange={(e) => toggleDevCR(i, e.target.checked)}
                      title={crAllowed ? 'Insert a Senior-Developer code-review gate after this step'
                        : 'Code review applies to implementation / test steps'} />
                  </span>
                  <span className="ft-act">
                    <button className="icon-btn danger" title="Remove step" disabled={row.key === 'dev'}
                      onClick={() => removeDevStep(i)}>🗑</button>
                  </span>
                </div>
              );
            })}
          </div>
          <div className="row" style={{ marginTop: 8, gap: 10 }}>
            <button className="btn small add-btn" onClick={addDevStep}
              disabled={!addableKinds.some((k) => !devRows.some((r) => r.key === k.key))}>+ Add Development Step</button>
            <span className="muted small">ⓘ Agents can not be duplicated. Code review adds a
              Senior-Developer gate after that step.</span>
          </div>
        </div>
      </section>

      {/* 4 — Other Settings */}
      <section className="flowsec flowsec-4">
        <SectionHead n={4} title="Other Settings" />
        <div className="flowsec-body">
          <label className="opt-row">
            <input type="checkbox" checked={poEnabled === 1}
              onChange={(e) => setPoEnabled(e.target.checked ? 1 : 0)} />
            <span>PO autonomous <span className="muted small">(auto-reviews design-phase approval gates)</span></span>
          </label>
          <label className="opt-row">
            <input type="checkbox" checked={isDefault === 1}
              onChange={(e) => setIsDefault(e.target.checked ? 1 : 0)} />
            <span>Default for new projects</span>
          </label>
        </div>
        <div className="editor-footer">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !name.trim()} onClick={save}>
            {flow ? 'Save Flow' : 'Save Flow'}</button>
        </div>
      </section>
    </div>
  );
}

function SectionHead({ n, title, sub }: { n: number; title: string; sub?: string }) {
  return (
    <div className="flowsec-head">
      <span className="num-badge">{n}</span>
      <span className="sec-title">{title}</span>
      {sub && <span className="sec-sub">{sub}</span>}
    </div>
  );
}

function AddMemberButton({ roles, selected, onAdd }: {
  roles: string[]; selected: Map<string, FlowTeam>; onAdd: (r: string) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);
  const remaining = roles.filter((r) => !selected.has(r));
  return (
    <div className="add-member" ref={ref}>
      <button className="btn small add-btn" onClick={() => setOpen((o) => !o)}
        disabled={remaining.length === 0}>+ Add Member</button>
      {open && remaining.length > 0 && (
        <div className="add-member-list">
          {remaining.map((r) => (
            <button key={r} className="add-member-item" onClick={() => { onAdd(r); setOpen(false); }}>
              <b>{abbr(r)}</b> <span className="muted small">({r})</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Multi-select of design-phase documents rendered as removable chips + a
// dropdown of the options that are not yet chosen.
function DocMultiSelect({ value, options, onChange }: {
  value: string[]; options: ProcessItem[]; onChange: (next: string[]) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);
  const add = (p: string) => onChange([...value, p]);
  const remove = (p: string) => onChange(value.filter((x) => x !== p));
  const remaining = options.filter((o) => !value.includes(o.process));
  return (
    <div className={`msel${open ? ' open' : ''}`} ref={ref}>
      <div className="msel-box" onClick={() => setOpen((o) => !o)}>
        {value.map((p) => (
          <span className="msel-chip" key={p} onClick={(e) => { e.stopPropagation(); remove(p); }}>
            {p} <span className="msel-x">×</span>
          </span>
        ))}
        {!value.length && <span className="muted small">— select documents —</span>}
        <span className="msel-caret">▾</span>
      </div>
      {open && (
        <div className="msel-list">
          {remaining.map((o) => (
            <button type="button" key={o.process} className="msel-item" onClick={() => add(o.process)}>
              {o.process} <span className="muted small">({o.kind})</span>
            </button>
          ))}
          {!remaining.length && <div className="msel-empty">All documents selected.</div>}
        </div>
      )}
    </div>
  );
}
