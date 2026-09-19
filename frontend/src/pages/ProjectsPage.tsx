import React from 'react';
import { get, post, patch, del } from '../api';
import { Badge, Field, Modal, useAsyncData, ErrorNote } from '../components';
import { TASK_STATE_CLASS } from '../api';

export default function ProjectsPage({ activeProject, setActiveProject, onOpenControl }:
  { activeProject: string | null; setActiveProject: (id: string) => void; onOpenControl: () => void }) {
  const { data: projects, reload, loading } = useAsyncData<any[]>(() => get('/api/v1/projects'), []);
  const { data: teams } = useAsyncData<any[]>(() => get('/api/v1/teams'), []);
  const { data: gateways } = useAsyncData<any[]>(() => get('/api/v1/gateways'), []);
  const [selected, setSelected] = React.useState<string | null>(activeProject);
  const [tab, setTab] = React.useState<'backlog' | 'sprints' | 'tasks' | 'settings'>('tasks');
  const [modal, setModal] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => { setSelected(activeProject); }, [activeProject]);
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
              {(['tasks', 'backlog', 'sprints', 'settings'] as const).map((t) => (
                <button key={t} className={`btn small ${tab === t ? 'primary' : ''}`}
                  onClick={() => setTab(t)}>{t[0].toUpperCase() + t.slice(1)}</button>
              ))}
            </div>
            <div style={{ marginTop: 14 }}>
              {tab === 'tasks' && <TasksTab projectId={project.id} onError={fail} onChanged={refresh} />}
              {tab === 'backlog' && <BacklogTab projectId={project.id} onError={fail} onChanged={refresh} />}
              {tab === 'sprints' && <SprintsTab projectId={project.id} onError={fail} onChanged={refresh} />}
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
  const [f, setF] = React.useState({ name: '', goal: '', description: '', technology_stack: '', repository_url: '', workspace_path: '', team_id: '', default_gateway_id: '' });
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
          <select value={f.default_gateway_id} onChange={upd('default_gateway_id')}>
            <option value="">— none —</option>
            {gateways.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </Field>
      </div>
      <button className="btn primary" disabled={!f.name} onClick={async () => {
        try { const p = await post('/api/v1/projects', { ...f, team_id: f.team_id || null, default_gateway_id: f.default_gateway_id || null }); onSaved(p.id); }
        catch (e: any) { alert(e.message); }
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
            {['Todo', 'Ready'].includes(t.status) && <button className="btn small" onClick={() => transition(t, 'In Progress')}>Start</button>}
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
  const upd = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return (
    <Modal title="Add Task" onClose={onClose}>
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
      <button className="btn primary" disabled={!f.title} onClick={async () => {
        try { await post(`/api/v1/projects/${projectId}/tasks`, { ...f, sprint_id: f.sprint_id || null, assigned_agent_id: f.assigned_agent_id || null }); onSaved(); }
        catch (e: any) { alert(e.message); }
      }}>Create Task</button>
    </Modal>
  );
}

function BacklogTab({ projectId, onError, onChanged }: { projectId: string; onError: (e: any) => void; onChanged: () => void }) {
  const [items, setItems] = React.useState<any[]>([]);
  const [modal, setModal] = React.useState(false);
  const load = React.useCallback(() => { get(`/api/v1/projects/${projectId}/backlog`).then(setItems).catch(onError); }, [projectId]);
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
      <button className="btn primary" disabled={!f.title} onClick={() => onSaved(f)}>Add Item</button>
    </Modal>
  );
}

function SprintsTab({ projectId, onError, onChanged }: { projectId: string; onError: (e: any) => void; onChanged: () => void }) {
  const [sprints, setSprints] = React.useState<any[]>([]);
  const [modal, setModal] = React.useState(false);
  const load = React.useCallback(() => { get(`/api/v1/projects/${projectId}/sprints`).then(setSprints).catch(onError); }, [projectId]);
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
  const upd = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return (
    <>
      <Field label="Name"><input value={f.name} onChange={upd('name')} /></Field>
      <Field label="Sprint goal"><input value={f.goal} onChange={upd('goal')} /></Field>
      <Field label="Capacity (story points)"><input type="number" value={f.capacity} onChange={upd('capacity')} /></Field>
      <button className="btn primary" disabled={!f.name} onClick={() => onSaved(f)}>Create Sprint</button>
    </>
  );
}

function SettingsTab({ project, teams, gateways, onError, onChanged }: any) {
  const [f, setF] = React.useState({ ...project });
  React.useEffect(() => setF({ ...project }), [project]);
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
          <select value={f.default_gateway_id ?? ''} onChange={upd('default_gateway_id')}>
            <option value="">— none —</option>
            {gateways.map((g: any) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </Field>
      </div>
      <div className="btn-row">
        <button className="btn primary" onClick={async () => {
          try { await patch(`/api/v1/projects/${project.id}`, { ...f, team_id: f.team_id || null, default_gateway_id: f.default_gateway_id || null }); onChanged(); }
          catch (e: any) { onError(e); }
        }}>Save Changes</button>
        <button className="btn danger" onClick={async () => {
          if (!confirm(`Delete project "${project.name}"?`)) return;
          try { await del(`/api/v1/projects/${project.id}`); onChanged(); } catch (e: any) { onError(e); }
        }}>Delete Project</button>
      </div>
    </>
  );
}
