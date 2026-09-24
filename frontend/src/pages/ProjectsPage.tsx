import React from 'react';
import { get, getPage, post, patch, del, Page } from '../api';
import { Badge, Field, Modal, useAsyncData, ErrorNote, Pagination } from '../components';
import { TASK_STATE_CLASS } from '../api';
import { BacklogSchema, ProjectSchema, SprintSchema, TaskSchema } from '../validation';

export default function ProjectsPage({ activeProject, setActiveProject, onOpenControl }:
  { activeProject: string | null; setActiveProject: (id: string) => void; onOpenControl: () => void }) {
  const [projectOffset, setProjectOffset] = React.useState(0);
  const projectLimit = 50;
  const { data: projectPage, reload, loading } = useAsyncData<Page<any>>(
    () => getPage(`/api/v1/projects?limit=${projectLimit}&offset=${projectOffset}`), [projectOffset]);
  const { data: teams } = useAsyncData<any[]>(() => get('/api/v1/teams'), []);
  const { data: gateways } = useAsyncData<any[]>(() => get('/api/v1/gateways'), []);
  const [selected, setSelected] = React.useState<string | null>(activeProject);
  const [tab, setTab] = React.useState<'backlog' | 'governance' | 'sprints' | 'tasks' | 'settings'>('tasks');
  const [modal, setModal] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => { setSelected(activeProject); }, [activeProject]);
  const projects = projectPage?.items ?? [];
  React.useEffect(() => { if (!selected && projects?.length) setSelected(projects[0].id); }, [projects, selected]);

  const project = projects?.find((p) => p.id === selected);
  const refresh = () => { reload(); };
  const fail = (e: any) => setError(e.message || String(e));

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Projects</h1>
          <div className="sub">Define delivery boundaries: goal, stack, repository, team and gateway.</div>
        </div>
        <button className="btn primary" onClick={() => setModal('newProject')}>+ New Project</button>
      </div>
      <ErrorNote error={error} />
      <div className="grid" style={{ gridTemplateColumns: '280px 1fr', alignItems: 'start' }}>
        <div>
          {loading && <div className="muted">Loading…</div>}
          {(projects ?? []).map((p) => (
            <div key={p.id} className={`list-row ${selected === p.id ? '' : ''}`}
              style={{ cursor: 'pointer', borderColor: selected === p.id ? 'var(--accent)' : 'var(--border)' }}
              onClick={() => { setSelected(p.id); setActiveProject(p.id); }}>
              <div className="grow">
                <b>{p.name}</b>
                <div className="kv">{p.team_name ?? 'no team'} · {p.status}</div>
              </div>
            </div>
          ))}
          {!projects?.length && !loading && <div className="empty">No projects yet.</div>}
          {projectPage && (
            <Pagination total={projectPage.total} limit={projectPage.limit}
              offset={projectPage.offset} onChange={setProjectOffset} />
          )}
        </div>

        {project ? (
          <div className="card">
            <div className="spread">
              <div>
                <h3 style={{ marginBottom: 2 }}>{project.name}</h3>
                <div className="muted small">{project.goal}</div>
              </div>
              <div className="btn-row">
                <button className="btn small primary" onClick={() => { setActiveProject(project.id); onOpenControl(); }}>
                  Open Project Control
                </button>
              </div>
            </div>
            <div className="row" style={{ marginTop: 10 }}>
              {(['tasks', 'backlog', 'sprints', 'governance', 'settings'] as const).map((t) => (
                <button key={t} className={`btn small ${tab === t ? 'primary' : ''}`}
                  onClick={() => setTab(t)}>{t[0].toUpperCase() + t.slice(1)}</button>
              ))}
            </div>
            <div style={{ marginTop: 14 }}>
              {tab === 'tasks' && <TasksTab projectId={project.id} onError={fail} onChanged={refresh} />}
              {tab === 'backlog' && <BacklogTab projectId={project.id} onError={fail} onChanged={refresh} />}
              {tab === 'sprints' && <SprintsTab projectId={project.id} onError={fail} onChanged={refresh} />}
              {tab === 'governance' && <GovernanceTab project={project} onError={fail} onChanged={refresh} />}
              {tab === 'settings' && (
                <SettingsTab project={project} teams={teams ?? []} gateways={gateways ?? []}
                  onError={fail} onChanged={refresh} />
              )}
            </div>
          </div>
        ) : <div className="empty">Select a project</div>}
      </div>

      {modal === 'newProject' && (
        <ProjectForm onClose={() => setModal(null)} teams={teams ?? []} gateways={gateways ?? []}
          onSaved={(id) => { setModal(null); refresh(); setSelected(id); setActiveProject(id); }} />
      )}
    </div>
  );
}

function ProjectForm({ onClose, onSaved, teams, gateways }:
  { onClose: () => void; onSaved: (id: string) => void; teams: any[]; gateways: any[] }) {
  const [f, setF] = React.useState({ name: '', goal: '', description: '', technology_stack: '', repository_url: '', workspace_path: '', team_id: '', default_gateway_id: '', default_model_id: '' });
  const [validationError, setValidationError] = React.useState('');
  const { data: models } = useAsyncData<any[]>(
    () => (f.default_gateway_id ? get(`/api/v1/gateways/${f.default_gateway_id}/models`) : Promise.resolve([])),
    [f.default_gateway_id]);
  const upd = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return (
    <Modal title="New Project" onClose={onClose}>
      <div className="form-grid">
        <Field label="Name"><input value={f.name} onChange={upd('name')} /></Field>
        <Field label="Technology stack"><input value={f.technology_stack} onChange={upd('technology_stack')} placeholder="React, FastAPI…" /></Field>
      </div>
      <Field label="Goal"><input value={f.goal} onChange={upd('goal')} /></Field>
      <Field label="Description"><textarea value={f.description} onChange={upd('description')} /></Field>
      <div className="form-grid">
        <Field label="Repository URL"><input value={f.repository_url} onChange={upd('repository_url')} /></Field>
        <Field label="Workspace path"><input value={f.workspace_path} onChange={upd('workspace_path')} /></Field>
        <Field label="Team">
          <select value={f.team_id} onChange={upd('team_id')}>
            <option value="">— none —</option>
            {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </Field>
        <Field label="Default gateway">
          <select value={f.default_gateway_id} onChange={(e) => setF({ ...f, default_gateway_id: e.target.value, default_model_id: '' })}>
            <option value="">— none —</option>
            {gateways.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </Field>
        <Field label="Default model">
          <select value={f.default_model_id} disabled={!f.default_gateway_id} onChange={upd('default_model_id')}>
            <option value="">— preferred code model —</option>
            {(models ?? []).map((m) => <option key={m.id} value={m.id}>{m.provider_model_id}</option>)}
          </select>
        </Field>
      </div>
      <ErrorNote error={validationError} />
      <button className="btn primary" disabled={!f.name.trim()} onClick={async () => {
        const checked = ProjectSchema.safeParse(f);
        if (!checked.success) { setValidationError(checked.error.issues[0]?.message ?? 'Invalid project'); return; }
        setValidationError('');
        try { const p = await post('/api/v1/projects', { ...checked.data, team_id: f.team_id || null, default_gateway_id: f.default_gateway_id || null, default_model_id: f.default_model_id || null }); onSaved(p.id); }
        catch (e: any) { setValidationError(e.message || String(e)); }
      }}>Create Project</button>
    </Modal>
  );
}

function TasksTab({ projectId, onError, onChanged }: { projectId: string; onError: (e: any) => void; onChanged: () => void }) {
  const [tasks, setTasks] = React.useState<any[]>([]);
  const [sprints, setSprints] = React.useState<any[]>([]);
  const [agents, setAgents] = React.useState<any[]>([]);
  const [modal, setModal] = React.useState(false);

  const load = React.useCallback(() => {
    get(`/api/v1/projects/${projectId}/tasks`).then(setTasks).catch(onError);
    get(`/api/v1/projects/${projectId}/sprints`).then(setSprints).catch(() => undefined);
    get('/api/v1/agents').then(setAgents).catch(() => undefined);
  }, [projectId]);
  React.useEffect(load, [load]);

  const transition = async (t: any, status: string) => {
    try { await patch(`/api/v1/tasks/${t.id}`, { status }); load(); onChanged(); }
    catch (e: any) { onError(e); }
  };
  const assign = async (t: any, agentId: string) => {
    try { await patch(`/api/v1/tasks/${t.id}`, { assigned_agent_id: agentId || null }); load(); }
    catch (e: any) { onError(e); }
  };

  const activeSprint = sprints.find((s) => s.status === 'Active');
  return (
    <>
      <div className="spread" style={{ marginBottom: 10 }}>
        <span className="section-title" style={{ margin: 0 }}>Tasks {activeSprint ? `· ${activeSprint.name}` : ''}</span>
        <button className="btn small primary" onClick={() => setModal(true)}>+ Add Task</button>
      </div>
      {tasks.map((t) => (
        <div key={t.id} className="list-row">
          <div className="grow">
            <b>{t.title}</b>
            <div className="kv">{t.story_points} pts · {t.dependencies?.length ? `deps: ${t.dependencies.map((d: any) => d.title).join(', ')}` : 'no deps'}</div>
            {t.unmet_dependencies?.length > 0 && <Badge kind="warn">blocked by prerequisites</Badge>}
            {t.blocked_reason && <div className="small" style={{ color: 'var(--err)' }}>{t.blocked_reason}</div>}
          </div>
          <select style={{ width: 150 }} value={t.assigned_agent_id ?? ''} onChange={(e) => assign(t, e.target.value)}>
            <option value="">unassigned</option>
            {agents.map((a) => <option key={a.id} value={a.id}>{a.name} ({a.role_name})</option>)}
          </select>
          <Badge kind={TASK_STATE_CLASS[t.status] ?? 'dim'}>{t.status}</Badge>
          <div className="btn-row">
            {t.status === 'Todo' && <button className="btn small" onClick={() => transition(t, 'Ready')}>Ready</button>}
            {/* Backend TASK_TRANSITIONS only allows Ready -> In Progress; a
                Todo "Start" was guaranteed to fail with 409. */}
            {t.status === 'Ready' && <button className="btn small" onClick={() => transition(t, 'In Progress')}>Start</button>}
            {t.status === 'In Progress' && <button className="btn small" onClick={() => transition(t, 'Review')}>Review</button>}
            {['Review', 'Testing'].includes(t.status) && <button className="btn small" onClick={() => transition(t, 'Done')}>Done</button>}
            {!['Done', 'Cancelled'].includes(t.status) && <button className="btn small danger" onClick={() => transition(t, 'Cancelled')}>✕</button>}
            <button className="btn small danger" onClick={async () => { await del(`/api/v1/tasks/${t.id}`); load(); }}>Del</button>
          </div>
        </div>
      ))}
      {!tasks.length && <div className="empty">No tasks yet.</div>}
      {modal && <TaskForm projectId={projectId} sprints={sprints} tasks={tasks} agents={agents}
        onClose={() => setModal(false)} onSaved={() => { setModal(false); load(); onChanged(); }} />}
    </>
  );
}

function TaskForm({ projectId, sprints, tasks, agents, onClose, onSaved }: any) {
  const [f, setF] = React.useState({ title: '', description: '', acceptance_criteria: '', story_points: 3, priority: 2, sprint_id: '', backlog_item_id: null, assigned_agent_id: '', depends_on: [] as string[] });
  const [validationError, setValidationError] = React.useState('');
  const { data: templates } = useAsyncData<any[]>(() => get('/api/v1/task-templates'), []);
  const [tplId, setTplId] = React.useState('');
  const [tplVars, setTplVars] = React.useState<Record<string, string>>({});
  const tpl = (templates ?? []).find((t: any) => t.id === tplId);
  const upd = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const applyTemplate = async () => {
    try {
      const a = await post(`/api/v1/task-templates/${tplId}/preview`, tplVars);
      setF({ ...f, title: a.title, description: a.description,
        acceptance_criteria: a.acceptance_criteria,
        story_points: a.story_points ?? f.story_points,
        priority: a.priority ?? f.priority });
      setValidationError(a.missing_variables?.length
        ? `Template variables still missing: ${a.missing_variables.join(', ')}` : '');
    } catch (e: any) { setValidationError(e.message || String(e)); }
  };
  return (
    <Modal title="Add Task" onClose={onClose}>
      {!!templates?.length && (
        <Field label="Start from template">
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <select style={{ width: 220 }} value={tplId}
              onChange={(e) => { setTplId(e.target.value); setTplVars({ }); }}>
              <option value="">— blank task —</option>
              {templates.map((t: any) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
            {tpl && <button className="btn small primary" onClick={applyTemplate}>Apply to form</button>}
          </div>
          {tpl && (
            <div className="muted small" style={{ marginTop: 4 }}>
              {tpl.about}
              <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginTop: 6 }}>
                {(tpl.variables ?? []).map((v: any) => (
                  <input key={v.name} style={{ width: 170 }} placeholder={`${v.label} (e.g. ${v.example})`}
                    value={tplVars[v.name] ?? ''}
                    onChange={(e) => setTplVars({ ...tplVars, [v.name]: e.target.value })} />
                ))}
              </div>
            </div>
          )}
        </Field>
      )}
      <Field label="Title"><input value={f.title} onChange={upd('title')} /></Field>
      <Field label="Description"><textarea value={f.description} onChange={upd('description')} /></Field>
      <Field label="Acceptance criteria"><textarea value={f.acceptance_criteria} onChange={upd('acceptance_criteria')} /></Field>
      <div className="form-grid">
        <Field label="Story points"><input type="number" value={f.story_points} onChange={upd('story_points')} /></Field>
        <Field label="Priority (1 high)"><input type="number" min={1} max={5} value={f.priority} onChange={upd('priority')} /></Field>
        <Field label="Sprint">
          <select value={f.sprint_id} onChange={upd('sprint_id')}>
            <option value="">— backlog —</option>
            {sprints.map((s: any) => <option key={s.id} value={s.id}>{s.name} ({s.status})</option>)}
          </select>
        </Field>
        <Field label="Assign to agent">
          <select value={f.assigned_agent_id} onChange={upd('assigned_agent_id')}>
            <option value="">— unassigned —</option>
            {agents.map((a: any) => <option key={a.id} value={a.id}>{a.name} ({a.role_name})</option>)}
          </select>
        </Field>
      </div>
      <Field label="Depends on">
        <select multiple value={f.depends_on} onChange={(e) => setF({ ...f, depends_on: Array.from(e.target.selectedOptions).map((o) => o.value) })} style={{ height: 90 }}>
          {tasks.filter((t: any) => t.sprint_id).map((t: any) => <option key={t.id} value={t.id}>{t.title}</option>)}
        </select>
      </Field>
      <ErrorNote error={validationError} />
      <button className="btn primary" disabled={!f.title.trim()} onClick={async () => {
        const checked = TaskSchema.safeParse(f);
        if (!checked.success) { setValidationError(checked.error.issues[0]?.message ?? 'Invalid task'); return; }
        setValidationError('');
        try { await post(`/api/v1/projects/${projectId}/tasks`, { ...checked.data, sprint_id: f.sprint_id || null, assigned_agent_id: f.assigned_agent_id || null }); onSaved(); }
        catch (e: any) { setValidationError(e.message || String(e)); }
      }}>Create Task</button>
    </Modal>
  );
}

function BacklogTab({ projectId, onError, onChanged }: { projectId: string; onError: (e: any) => void; onChanged: () => void }) {
  const [items, setItems] = React.useState<any[]>([]);
  const [page, setPage] = React.useState<Page<any> | null>(null);
  const [offset, setOffset] = React.useState(0);
  const limit = 50;
  const [modal, setModal] = React.useState(false);
  const load = React.useCallback(() => {
    getPage(`/api/v1/projects/${projectId}/backlog?limit=${limit}&offset=${offset}`)
      .then((p) => { setPage(p); setItems(p.items); }).catch(onError);
  }, [projectId, offset]);
  React.useEffect(load, [load]);
  return (
    <>
      <div className="spread" style={{ marginBottom: 10 }}>
        <span className="section-title" style={{ margin: 0 }}>Product Backlog</span>
        <button className="btn small primary" onClick={() => setModal(true)}>+ Add Item</button>
      </div>
      {items.map((it) => (
        <div key={it.id} className="list-row">
          <div className="grow">
            <b>{it.title}</b>
            <div className="kv">P{it.priority} · {it.story_points} pts · {it.acceptance_criteria || 'no acceptance criteria'}</div>
          </div>
          <Badge kind={it.status === 'Backlog' ? 'dim' : 'info'}>{it.status}</Badge>
          <button className="btn small danger" onClick={async () => { await del(`/api/v1/backlog/${it.id}`); load(); onChanged(); }}>Del</button>
        </div>
      ))}
      {!items.length && <div className="empty">Backlog is empty.</div>}
      {page && <Pagination total={page.total} limit={page.limit} offset={page.offset} onChange={setOffset} />}
      {modal && (
        <BacklogForm onClose={() => setModal(false)} onSaved={async (body: any) => {
          try { await post(`/api/v1/projects/${projectId}/backlog`, body); setModal(false); load(); onChanged(); }
          catch (e: any) { onError(e); }
        }} />
      )}
    </>
  );
}

function BacklogForm({ onClose, onSaved }: any) {
  const [f, setF] = React.useState({ title: '', description: '', acceptance_criteria: '', priority: 2, story_points: 3 });
  const [validationError, setValidationError] = React.useState('');
  const upd = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return (
    <Modal title="Add Backlog Item" onClose={onClose}>
      <Field label="Title"><input value={f.title} onChange={upd('title')} /></Field>
      <Field label="Description"><textarea value={f.description} onChange={upd('description')} /></Field>
      <Field label="Acceptance criteria"><textarea value={f.acceptance_criteria} onChange={upd('acceptance_criteria')} placeholder="Given… when… then…" /></Field>
      <div className="form-grid">
        <Field label="Priority (1 high)"><input type="number" min={1} max={5} value={f.priority} onChange={upd('priority')} /></Field>
        <Field label="Story points"><input type="number" value={f.story_points} onChange={upd('story_points')} /></Field>
      </div>
      <ErrorNote error={validationError} />
      <button className="btn primary" disabled={!f.title.trim()} onClick={() => {
        const checked = BacklogSchema.safeParse(f);
        if (!checked.success) { setValidationError(checked.error.issues[0]?.message ?? 'Invalid backlog item'); return; }
        setValidationError('');
        onSaved(checked.data);
      }}>Add Item</button>
    </Modal>
  );
}

function SprintsTab({ projectId, onError, onChanged }: { projectId: string; onError: (e: any) => void; onChanged: () => void }) {
  const [sprints, setSprints] = React.useState<any[]>([]);
  const [page, setPage] = React.useState<Page<any> | null>(null);
  const [offset, setOffset] = React.useState(0);
  const limit = 50;
  const [modal, setModal] = React.useState(false);
  const load = React.useCallback(() => {
    getPage(`/api/v1/projects/${projectId}/sprints?limit=${limit}&offset=${offset}`)
      .then((p) => { setPage(p); setSprints(p.items); }).catch(onError);
  }, [projectId, offset]);
  React.useEffect(load, [load]);
  const setStatus = async (sid: string, status: string) => {
    try { await patch(`/api/v1/sprints/${sid}`, { status }); load(); onChanged(); }
    catch (e: any) { onError(e); }
  };
  return (
    <>
      <div className="spread" style={{ marginBottom: 10 }}>
        <span className="section-title" style={{ margin: 0 }}>Sprints</span>
        <button className="btn small primary" onClick={() => setModal(true)}>+ Create Sprint</button>
      </div>
      {sprints.map((s) => (
        <div key={s.id} className="list-row">
          <div className="grow">
            <b>{s.name}</b>
            <div className="kv">{s.goal || 'no goal'} · capacity {s.capacity} pts · committed {s.committed_points} pts</div>
          </div>
          <Badge kind={s.status === 'Active' ? 'ok' : s.status === 'Completed' ? 'dim' : 'info'}>{s.status}</Badge>
          <div className="btn-row">
            {s.status === 'Planned' && <button className="btn small primary" onClick={() => setStatus(s.id, 'Active')}>Activate</button>}
            {s.status === 'Active' && <button className="btn small" onClick={() => setStatus(s.id, 'Completed')}>Complete</button>}
            <button className="btn small danger" onClick={async () => { await del(`/api/v1/sprints/${s.id}`); load(); }}>Del</button>
          </div>
        </div>
      ))}
      {!sprints.length && <div className="empty">No sprints yet.</div>}
      {page && <Pagination total={page.total} limit={page.limit} offset={page.offset} onChange={setOffset} />}
      {modal && (
        <Modal title="Create Sprint" onClose={() => setModal(false)}>
          <SprintFields onSaved={async (body: any) => {
            try { await post(`/api/v1/projects/${projectId}/sprints`, body); setModal(false); load(); onChanged(); }
            catch (e: any) { onError(e); }
          }} />
        </Modal>
      )}
    </>
  );
}

function SprintFields({ onSaved }: { onSaved: (body: any) => void }) {
  const [f, setF] = React.useState({ name: '', goal: '', capacity: 40 });
  const [validationError, setValidationError] = React.useState('');
  const upd = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return (
    <>
      <Field label="Name"><input value={f.name} onChange={upd('name')} /></Field>
      <Field label="Sprint goal"><input value={f.goal} onChange={upd('goal')} /></Field>
      <Field label="Capacity (story points)"><input type="number" value={f.capacity} onChange={upd('capacity')} /></Field>
      <ErrorNote error={validationError} />
      <button className="btn primary" disabled={!f.name.trim()} onClick={() => {
        const checked = SprintSchema.safeParse(f);
        if (!checked.success) { setValidationError(checked.error.issues[0]?.message ?? 'Invalid sprint'); return; }
        setValidationError('');
        onSaved(checked.data);
      }}>Create Sprint</button>
    </>
  );
}

function GovernanceTab({ project, onError, onChanged }: any) {
  const { data: view, reload } = useAsyncData<any>(
    () => get(`/api/v1/projects/${project.id}/lifecycle`), [project.id]);
  const [to, setTo] = React.useState('');
  const [req, setReq] = React.useState({ title: '', content: '' });
  const [decisionModal, setDecisionModal] = React.useState<{
    kind: 'baseline' | 'change'; id: string; code: string; decision: string;
  } | null>(null);
  const [decisionNotes, setDecisionNotes] = React.useState('');
  const run = async (fn: () => Promise<any>) => {
    try { await fn(); reload(); onChanged(); } catch (e: any) { onError(e); }
  };
  const decide = (bid: string, decision: string) => {
    setDecisionNotes('');
    setDecisionModal({ kind: 'baseline', id: bid, code: 'Baseline', decision });
  };
  const decideCr = (cid: string, code: string, decision: string) => {
    setDecisionNotes('');
    setDecisionModal({ kind: 'change', id: cid, code, decision });
  };
  const submitDecision = async () => {
    if (!decisionModal) return;
    try {
      const path = decisionModal.kind === 'baseline'
        ? `/api/v1/projects/${project.id}/baseline/${decisionModal.id}/decision`
        : `/api/v1/projects/${project.id}/change_requests/${decisionModal.id}/decision`;
      await post(path, { decision: decisionModal.decision, notes: decisionNotes });
      setDecisionModal(null);
      reload();
      onChanged();
    } catch (e: any) { onError(e); }
  };
  if (!view) return <div className="muted">Loading governance…</div>;
  const badge = view.state === 'Completed' ? 'ok'
    : ['Blocked', 'Cancelled', 'Change Requested'].includes(view.state) ? 'err'
      : ['Pending PO Approval', 'Paused'].includes(view.state) ? 'warn' : 'info';
  const stateBadge = (s: string) => s === 'approved' ? 'ok' : s === 'pending_approval' ? 'warn'
    : ['rejected', 'revision_requested'].includes(s) ? 'err' : 'dim';
  return (
    <>
      <div className="spread">
        <div>
          <b>Lifecycle </b><Badge kind={badge}>{view.state}</Badge>
          <div className="muted small">
            {view.governance_enabled
              ? 'Enforcement ON — sprints and tasks only run in Active Development / Final Validation.'
              : 'Enforcement OFF — the state machine tracks and audits, execution is unaffected.'}
          </div>
        </div>
        <label className="kv" style={{ cursor: 'pointer' }}>
          <input type="checkbox" checked={!!project.governance_enabled} onChange={() =>
            run(() => patch(`/api/v1/projects/${project.id}`, { governance_enabled: !project.governance_enabled }))
          } /> governance gate
        </label>
      </div>

      <div style={{ marginTop: 12 }}>
        <b className="small">Move to state</b>
        <div className="btn-row" style={{ marginTop: 4 }}>
          <select value={to} onChange={(e) => setTo(e.target.value)} style={{ width: 220 }}>
            <option value="">— allowed next states —</option>
            {(view.allowed_transitions ?? []).map((s: string) => <option key={s} value={s}>{s}</option>)}
          </select>
          <button className="btn small primary" disabled={!to} onClick={() =>
            run(() => post(`/api/v1/projects/${project.id}/lifecycle/transition`, { to, reason: 'moved via UI' }))
          }>Transition</button>
        </div>
      </div>

      {(view.pending_baselines ?? []).length > 0 && (
        <div style={{ marginTop: 12 }}>
          <b className="small">Awaiting PO approval</b>
          {view.pending_baselines.map((b: any) => (
            <div key={b.id} className="list-row" style={{ borderColor: 'var(--warn)' }}>
              <div className="grow"><b>{b.code}</b> <span className="muted small">{b.kind}</span></div>
              <div className="btn-row">
                <button className="btn small primary" onClick={() => decide(b.id, 'approve')}>Approve</button>
                <button className="btn small" onClick={() => decide(b.id, 'request_revision')}>Request revision</button>
                <button className="btn small danger" onClick={() => decide(b.id, 'reject')}>Reject</button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: 12 }}>
        <div className="spread"><b className="small">Baselines</b>
          <button className="btn small" onClick={() =>
            run(() => post(`/api/v1/projects/${project.id}/baseline`, { kind: 'requirement' }))
          }>+ Freeze requirement baseline (RB)</button>
        </div>
        {(view.baselines ?? []).map((b: any) => (
          <div key={b.id} className="kv">
            {b.code} · <Badge kind={stateBadge(b.status)}>{b.status}</Badge>
            {b.approved_by ? ` by ${b.approved_by}` : ''} · {b.created_at?.slice(0, 10)}
          </div>
        ))}
        {!view.baselines?.length && <div className="muted small">No baselines yet — record requirements, then freeze RB-1.0 for approval.</div>}
      </div>

      <div style={{ marginTop: 12 }}>
        <b className="small">Requirements (versioned, append-only)</b>
        {(view.requirements ?? []).map((r: any) => (
          <div key={r.id} className="kv">{r.title} · v{r.version} <span className="muted">({r.source_type})</span></div>
        ))}
        {!view.requirements?.length && <div className="muted small">None recorded yet.</div>}
        <div className="form-grid" style={{ marginTop: 6 }}>
          <Field label="Title"><input value={req.title} onChange={(e) => setReq({ ...req, title: e.target.value })} /></Field>
          <Field label="Content">
            <textarea value={req.content} rows={2} onChange={(e) => setReq({ ...req, content: e.target.value })} />
          </Field>
        </div>
        <button className="btn small primary" disabled={!req.title.trim()} onClick={() => run(async () => {
          await post(`/api/v1/projects/${project.id}/requirements`,
            { title: req.title.trim(), content: req.content });
          setReq({ title: '', content: '' });
        })}>Add requirement</button>
      </div>

      <div style={{ marginTop: 12 }}>
        <b className="small">Change requests</b>
        {(view.change_requests ?? []).map((c: any) => (
          <div key={c.id} className="list-row">
            <div className="grow">
              <b>{c.code}</b> {c.title} <Badge kind={c.status === 'open' ? 'warn' : 'dim'}>{c.status}</Badge>
              {c.description && <div className="muted small">{c.description}</div>}
            </div>
            {c.status === 'open' && (
              <div className="btn-row">
                <button className="btn small" onClick={() => decideCr(c.id, c.code, 'incorporate')}>Incorporate</button>
                <button className="btn small danger" onClick={() => decideCr(c.id, c.code, 'decline')}>Decline</button>
              </div>
            )}
          </div>
        ))}
        {!view.change_requests?.length && <div className="muted small">None.</div>}
      </div>
      {decisionModal && <Modal title={`${decisionModal.decision.replace('_', ' ')} ${decisionModal.code}`}
        onClose={() => setDecisionModal(null)}>
        <Field label="Notes or reason">
          <textarea value={decisionNotes} onChange={(e) => setDecisionNotes(e.target.value)} rows={3} />
        </Field>
        <div className="btn-row">
          <button className="btn" onClick={() => setDecisionModal(null)}>Cancel</button>
          <button className="btn primary" onClick={submitDecision}>Confirm decision</button>
        </div>
      </Modal>}
    </>
  );
}

function SettingsTab({ project, teams, gateways, onError, onChanged }: any) {
  const [f, setF] = React.useState({ ...project });
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  React.useEffect(() => setF({ ...project }), [project]);
  const { data: models } = useAsyncData<any[]>(
    () => (f.default_gateway_id ? get(`/api/v1/gateways/${f.default_gateway_id}/models`) : Promise.resolve([])),
    [f.default_gateway_id]);
  const upd = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return (
    <>
      <div className="form-grid">
        <Field label="Name"><input value={f.name} onChange={upd('name')} /></Field>
        <Field label="Technology stack"><input value={f.technology_stack ?? ''} onChange={upd('technology_stack')} /></Field>
      </div>
      <Field label="Goal"><input value={f.goal ?? ''} onChange={upd('goal')} /></Field>
      <div className="form-grid">
        <Field label="Repository URL"><input value={f.repository_url ?? ''} onChange={upd('repository_url')} /></Field>
        <Field label="Workspace path"><input value={f.workspace_path ?? ''} onChange={upd('workspace_path')} /></Field>
        <Field label="Team">
          <select value={f.team_id ?? ''} onChange={upd('team_id')}>
            <option value="">— none —</option>
            {teams.map((t: any) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </Field>
        <Field label="Default gateway">
          <select value={f.default_gateway_id ?? ''} onChange={(e) => setF({ ...f, default_gateway_id: e.target.value, default_model_id: '' })}>
            <option value="">— none —</option>
            {gateways.map((g: any) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </Field>
        <Field label="Default model">
          <select value={f.default_model_id ?? ''} disabled={!f.default_gateway_id} onChange={upd('default_model_id')}>
            <option value="">— preferred code model —</option>
            {(models ?? []).map((m: any) => <option key={m.id} value={m.id}>{m.provider_model_id}</option>)}
          </select>
        </Field>
        <Field label="LLM budget USD (0 = unlimited; tasks/sprints block when spent)">
          <input type="number" min={0} step="0.5" value={f.budget_usd ?? 0} onChange={upd('budget_usd')} />
        </Field>
      </div>
      <div className="btn-row">
        <button className="btn primary" onClick={async () => {
          try { await patch(`/api/v1/projects/${project.id}`, { ...f, team_id: f.team_id || null, default_gateway_id: f.default_gateway_id || null, default_model_id: f.default_model_id || null }); onChanged(); }
          catch (e: any) { onError(e); }
        }}>Save Changes</button>
        <button className="btn danger" onClick={() => setConfirmDelete(true)}>Delete Project</button>
      </div>
      {confirmDelete && <Modal title="Delete project" onClose={() => setConfirmDelete(false)}>
        <p>Delete “{project.name}” and its project data?</p>
        <div className="btn-row">
          <button className="btn" onClick={() => setConfirmDelete(false)}>Cancel</button>
          <button className="btn danger" onClick={async () => {
            try {
              await del(`/api/v1/projects/${project.id}`);
              setConfirmDelete(false);
              onChanged();
            } catch (e: any) { onError(e); }
          }}>Delete project</button>
        </div>
      </Modal>}
    </>
  );
}
