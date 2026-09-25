import React from 'react';
import { get, fmtTime, AGENT_STATE_CLASS, TASK_STATE_CLASS } from '../api';
import { Badge, ErrorNote } from '../components';

interface AgentRow {
  id: string; name: string; lifecycle_state: string; current_activity: string;
  current_task_id: string | null; role_name: string; persona_name?: string; provider_model_id?: string;
}
interface TaskRow {
  id: string; title: string; status: string; progress: number; agent_name: string | null;
  assigned_agent_id?: string | null;
}
interface RunRow { id: string; task_id: string; agent_id: string; status: string; current_step: string; mode: string }
interface EventRow { seq: number; event_type: string; payload: any; created_at: string }
interface Flow { id: string | null; name: string; stages: string[] }
interface Summary {
  project: any; agents: AgentRow[]; sprint: any; sprint_tasks: TaskRow[];
  events: EventRow[]; usage: any; active_runs: RunRow[]; flow?: Flow;
  po?: { has_po: boolean; po_enabled: boolean; agent_name: string | null };
}

type Stage = 'dev' | 'cr' | 'sa' | 'ba' | 'qa' | 'approve' | 'done';

const STAGE_LABEL: Record<Stage, string> = {
  dev: 'Developer', cr: 'Code review', sa: 'Solution Architect', ba: 'Business Analyst',
  qa: 'QA Engineer', approve: 'Approval', done: 'Delivered',
};
const STAGE_ICON: Record<Stage, string> = {
  dev: '💻', cr: '🔍', sa: '🏛️', ba: '📋', qa: '🧪', approve: '👑', done: '✅',
};
// Which role's agents sit on each node. Code review is a Senior Developer
// activity (peer of the author), so it shares the dev-family listing.
function roleToStage(role: string): Stage {
  const r = (role || '').toLowerCase();
  if (r.includes('architect') || r.includes('tech lead')) return 'sa';
  if (r.includes('business analyst')) return 'ba';
  if (r.includes('product owner')) return 'approve';
  if (r.includes('qa') || r.includes('test')) return 'qa';
  return 'dev';
}
function stageOfStatus(status: string): Stage | null {
  switch (status) {
    case 'In Progress': case 'Rework': return 'dev';
    case 'Code Review': return 'cr';
    case 'SA Review': return 'sa';
    case 'BA Review': return 'ba';
    case 'Waiting QA': case 'Testing': case 'Review': return 'qa';
    case 'Pending Approval': return 'approve';
    case 'Done': return 'done';
    default: return null;
  }
}

// Canvas nodes in delivery order, derived from the project's flow. The
// terminal "Delivered" node is always present even if the flow omits it.
function flowNodes(flow?: Flow): Stage[] {
  const map: Record<string, Stage> = {
    dev: 'dev', cr: 'cr', sa: 'sa', ba: 'ba', qa: 'qa', approve: 'approve',
  };
  const nodes: Stage[] = [];
  for (const s of flow?.stages ?? ['dev', 'qa']) {
    const n = map[s];
    if (n && !nodes.includes(n)) nodes.push(n);
  }
  if (!nodes.length) nodes.push('dev');
  if (nodes[nodes.length - 1] !== 'done') nodes.push('done');
  return nodes;
}

// Fixed pipeline layout in an abstract 1000x520 canvas: nodes run left→right
// in flow order with a light zig-zag, so any chain (with/without cr/approve)
// renders sensibly.
function layout(nodes: Stage[]): Record<Stage, { x: number; y: number }> {
  const a = {} as Record<Stage, { x: number; y: number }>;
  const n = nodes.length;
  const left = 95, right = 905, mid = 270, hi = 150, lo = 365;
  nodes.forEach((s, i) => {
    const x = n <= 1 ? 500 : Math.round(left + (right - left) * (i / (n - 1)));
    let y = mid;
    if (n > 2 && i !== 0 && i !== n - 1) y = (i % 2 === 1) ? hi : lo;
    a[s] = { x, y };
  });
  return a;
}

function curve(a: { x: number; y: number }, b: { x: number; y: number }): string {
  const mx = (a.x + b.x) / 2;
  return `M ${a.x} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}`;
}


export default function ProjectFlowPage({ activeProject, setActiveProject }:
  { activeProject: string | null; setActiveProject: (id: string) => void }) {
  const [projects, setProjects] = React.useState<any[]>([]);
  const [summary, setSummary] = React.useState<Summary | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [selectedAgent, setSelectedAgent] = React.useState<string | null>(null);
  const [paneOpen, setPaneOpen] = React.useState<boolean>(() => {
    try { return localStorage.getItem('ao.flow.pane') !== 'closed'; } catch { return true; }
  });
  const [flash, setFlash] = React.useState<{ key: number; from: Stage; to: Stage; label: string } | null>(null);
  const prevStatus = React.useRef<Record<string, string>>({});
  const flashTimer = React.useRef<number | undefined>(undefined);

  React.useEffect(() => { get('/api/v1/projects').then(setProjects).catch(() => undefined); }, []);
  React.useEffect(() => {
    try { localStorage.setItem('ao.flow.pane', paneOpen ? 'open' : 'closed'); } catch { /* private */ }
  }, [paneOpen]);

  React.useEffect(() => {
    if (!activeProject) { setSummary(null); return; }
    let alive = true, busy = false;
    const poll = async () => {
      if (busy) return;
      busy = true;
      try {
        const s: Summary = await get(`/api/v1/projects/${activeProject}/control/summary`);
        if (!alive) return;
        setError(null);
        // Forward hops come straight from the project's flow chain.
        const fwd = new Set<string>();
        const nodes = flowNodes(s.flow);
        for (let i = 0; i + 1 < nodes.length; i++) fwd.add(`${nodes[i]}>${nodes[i + 1]}`);
        // Detect forward handoffs to fire a one-shot traveling chip.
        const seen: Record<string, string> = {};
        for (const t of s.sprint_tasks || []) {
          seen[t.id] = t.status;
          const before = prevStatus.current[t.id];
          if (before && before !== t.status) {
            const a = stageOfStatus(before), b = stageOfStatus(t.status);
            if (a && b && a !== b && fwd.has(`${a}>${b}`)) {
              const k = Date.now();
              setFlash({ key: k, from: a, to: b, label: t.title.slice(0, 28) });
              window.clearTimeout(flashTimer.current);
              flashTimer.current = window.setTimeout(() => setFlash(null), 2400);
            }
          }
        }
        prevStatus.current = seen;
        setSummary(s);
      } catch (e: any) { if (alive) setError(e?.message || 'Failed to load'); }
      finally { busy = false; }
    };
    poll();
    const timer = window.setInterval(poll, 1500);
    return () => { alive = false; clearInterval(timer); window.clearTimeout(flashTimer.current); };
  }, [activeProject]);

  const agents = summary?.agents ?? [];
  const runs = summary?.active_runs ?? [];
  const tasks = summary?.sprint_tasks ?? [];
  const nodes = flowNodes(summary?.flow);
  const anchors = layout(nodes);

  // Agents bucketed per node by their role. Code review shares the dev family.
  const byStage: Record<Stage, AgentRow[]> = {
    dev: [], cr: [], sa: [], ba: [], qa: [], approve: [], done: [],
  };
  for (const a of agents) byStage[roleToStage(a.role_name)].push(a);
  byStage.cr = byStage.dev;

  const tasksAtStage = (st: Stage) => tasks.filter((t) => stageOfStatus(t.status) === st);
  const busyStages = new Set<Stage>();
  const MODE_STAGE: Record<string, Stage> = { dev: 'dev', cr: 'cr', sa: 'sa', ba: 'ba', qa: 'qa' };
  for (const r of runs) {
    const ag = agents.find((a) => a.id === r.agent_id);
    if (ag) busyStages.add(roleToStage(ag.role_name));
    if (MODE_STAGE[r.mode]) busyStages.add(MODE_STAGE[r.mode]);
  }

  // Connectors follow the flow's own stage order; every review/test stage
  // loops rework back to the developer.
  function hopLabel(b: Stage): string {
    return ({ cr: 'Code review', sa: 'SA review', ba: 'BA review',
      qa: 'QA test', approve: 'Approve', done: 'Done' } as Partial<Record<Stage, string>>)[b] ?? '';
  }
  const connectors: { from: Stage; to: Stage; label: string; kind: 'flow' | 'rework' }[] = [];
  for (let i = 0; i + 1 < nodes.length; i++) {
    connectors.push({ from: nodes[i], to: nodes[i + 1], label: hopLabel(nodes[i + 1]), kind: 'flow' });
  }
  for (const st of nodes) {
    if (st === 'cr' || st === 'sa' || st === 'ba' || st === 'qa') {
      connectors.push({ from: st, to: 'dev', label: 'rework', kind: 'rework' });
    }
  }

  const sel = agents.find((a) => a.id === selectedAgent) || null;
  const selRun = sel ? runs.find((r) => r.agent_id === sel.id) : null;
  const selTask = sel?.current_task_id ? tasks.find((t) => t.id === sel.current_task_id) : null;
  const selEvents = sel
    ? (summary?.events ?? []).filter((e) => e.payload?.agent === sel.name || e.payload?.agent_name === sel.name
        || e.payload?.agent_id === sel.id || e.payload?.qa === sel.name || e.payload?.dev === sel.name).slice(-6).reverse()
    : [];

  const sprint = summary?.sprint;
  const done = tasks.filter((t) => t.status === 'Done').length;
  const inReview = tasks.filter((t) => ['Code Review', 'SA Review', 'BA Review', 'Review',
    'Waiting QA', 'Testing', 'Pending Approval'].includes(t.status)).length;
  const rework = tasks.filter((t) => t.status === 'Rework').length;

  return (
    <div className="flow-page">
      <div className="flow-head">
        <div className="row">
          <h2 style={{ margin: 0 }}>Project Flow</h2>
          <select className="input" value={activeProject ?? ''}
            onChange={(e) => setActiveProject(e.target.value)} style={{ minWidth: 200 }}>
            <option value="" disabled>Select a project…</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>
        {summary?.po?.po_enabled && <Badge kind="warn">👑 PO autonomous</Badge>}
      </div>

      {error && <ErrorNote error={error} />}

      {!activeProject ? (
        <div className="card muted">Select a project to see its live delivery flow.</div>
      ) : (
        <>
          <div className="flow-sprint card">
            <div className="row" style={{ gap: 14 }}>
              <b>{sprint ? sprint.name : 'No active sprint'}</b>
              {sprint && <Badge kind={sprint.status === 'Active' ? 'ok' : 'info'}>{sprint.status}</Badge>}
              <span className="muted small">
                {sprint?.starts_at ? `${sprint.starts_at?.slice(0, 10)} → ${sprint.ends_at?.slice(0, 10)}` : ''}
              </span>
              <span className="flow-stats">
                <span className="chip">{tasks.length} tasks</span>
                <span className="chip ok">{done} done</span>
                <span className="chip warn">{inReview} in review</span>
                <span className="chip err">{rework} rework</span>
              </span>
            </div>
            <div className="progress-track">
              <div className="progress-fill" style={{ width: `${tasks.length ? Math.round((done / tasks.length) * 100) : 0}%` }} />
            </div>
          </div>

          <div className="flow-body">
            <div className="flow-canvas-wrap">
              <div className="flow-canvas">
                <svg className="flow-svg" viewBox="0 0 1000 520" preserveAspectRatio="none">
                  <defs>
                    <marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">
                      <path d="M0,0 L7,3 L0,6 Z" fill="var(--text-dim)" />
                    </marker>
                    <marker id="arrowRed" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">
                      <path d="M0,0 L7,3 L0,6 Z" fill="var(--err)" />
                    </marker>
                  </defs>
                  {connectors.map((c, i) => {
                    const a = anchors[c.from];
                    const b = anchors[c.to];
                    if (!a || !b) return null;
                    const active = c.kind === 'rework'
                      ? tasksAtStage(c.from).length > 0 && tasks.some((t) => t.status === 'Rework')
                      : busyStages.has(c.to) || tasksAtStage(c.to).length > 0;
                    const d = c.kind === 'rework' ? curve({ x: a.x, y: a.y + 30 }, { x: b.x, y: b.y + 30 }) : curve(a, b);
                    return (
                      <path key={i} className={`flow-line ${c.kind} ${active ? 'active' : ''}`} d={d}
                        markerEnd={c.kind === 'rework' ? 'url(#arrowRed)' : 'url(#arrow)'} />
                    );
                  })}
                </svg>

                {connectors.filter((c) => c.kind === 'flow').map((c, i) => {
                  const a = anchors[c.from];
                  const b = anchors[c.to];
                  if (!a || !b) return null;
                  return (
                    <div key={i} className="flow-line-label"
                      style={{ left: `${(((a.x + b.x) / 2) / 1000) * 100}%`, top: `${(((a.y + b.y) / 2) / 520) * 100}%` }}>
                      {c.label}
                    </div>
                  );
                })}

                {nodes.map((st) => {
                  const anchor = anchors[st];
                  const left = (anchor.x / 1000) * 100;
                  const top = (anchor.y / 520) * 100;
                  const list = byStage[st];
                  const tcount = tasksAtStage(st).length;
                  const busy = busyStages.has(st) || tcount > 0;
                  return (
                    <div key={st} className={`flow-node ${st} ${busy ? 'busy' : ''}`}
                      style={{ left: `${left}%`, top: `${top}%` }}>
                      <div className="flow-node-head">
                        <span className="flow-node-icon">{STAGE_ICON[st]}</span>
                        <span className="flow-node-title">{STAGE_LABEL[st]}</span>
                        {tcount > 0 && <span className="flow-node-badge">{tcount}</span>}
                      </div>
                      {list.length === 0 ? (
                        <div className="flow-node-empty small muted">no agent</div>
                      ) : list.map((a) => {
                        const working = a.lifecycle_state === 'Working' || a.lifecycle_state === 'Waiting';
                        return (
                          <button key={a.id}
                            className={`flow-agent ${selectedAgent === a.id ? 'sel' : ''}`}
                            onClick={() => { setSelectedAgent(a.id); setPaneOpen(true); }}
                            title={`${a.name} — ${a.role_name}`}>
                            <span className={`dot ${working ? 'dot-info pulse' : AGENT_STATE_CLASS[a.lifecycle_state] ?? 'dot-dim'}`} />
                            <span className="flow-agent-name">{a.name}</span>
                          </button>
                        );
                      })}
                    </div>
                  );
                })}

                {flash && (() => {
                  const a = anchors[flash.from];
                  const b = anchors[flash.to];
                  if (!a || !b) return null;
                  return (
                    <div key={flash.key} className="flow-chip"
                      style={{ ['--x1' as any]: `${(a.x / 1000) * 100}%`, ['--y1' as any]: `${(a.y / 520) * 100}%`,
                        ['--x2' as any]: `${(b.x / 1000) * 100}%`, ['--y2' as any]: `${(b.y / 520) * 100}%` }}>
                      📦 {flash.label}
                    </div>
                  );
                })()}
              </div>
              <div className="flow-legend small muted">
                {nodes.map((st) => (
                  <span key={st}><i className={`lg ${st}`} /> {STAGE_LABEL[st]}</span>
                ))}
                <span><i className="lg rework" /> rework</span>
                <span className="muted">· flow: {summary?.flow?.name ?? 'default'}</span>
              </div>
            </div>

            <aside className={`flow-pane ${paneOpen ? '' : 'closed'}`}>
              {paneOpen ? (
                <>
                  <div className="spread flow-pane-head">
                    <b>{sel ? sel.name : 'Agent detail'}</b>
                    <button className="btn small" onClick={() => setPaneOpen(false)} title="Collapse">»</button>
                  </div>
                  {!sel ? (
                    <div className="muted small">Click any agent on the canvas to see what they're doing.</div>
                  ) : (
                    <div className="flow-pane-body">
                      <div className="small muted">{sel.role_name}{sel.persona_name ? ` · ${sel.persona_name}` : ''}</div>
                      <div className="row" style={{ marginTop: 6 }}>
                        <Badge kind={AGENT_STATE_CLASS[sel.lifecycle_state] ?? 'dim'}>{sel.lifecycle_state}</Badge>
                        {sel.provider_model_id && <span className="chip">{sel.provider_model_id}</span>}
                      </div>
                      {selTask && (
                        <div className="flow-pane-sec">
                          <div className="flow-pane-lbl">Current task</div>
                          <div>{selTask.title}</div>
                          <div className="row small muted" style={{ marginTop: 4 }}>
                            <span>{selTask.status}</span><span>·</span><span>{selTask.progress}%</span>
                          </div>
                          <div className="progress-track"><div className="progress-fill" style={{ width: `${selTask.progress}%` }} /></div>
                        </div>
                      )}
                      {selRun && (
                        <div className="flow-pane-sec">
                          <div className="flow-pane-lbl">Live step</div>
                          <div className="row"><span className="dot dot-info pulse" /> <code>{selRun.current_step || selRun.mode}</code></div>
                        </div>
                      )}
                      {sel.current_activity && (
                        <div className="flow-pane-sec">
                          <div className="flow-pane-lbl">Activity</div>
                          <div className="small">{sel.current_activity}</div>
                        </div>
                      )}
                      <div className="flow-pane-sec">
                        <div className="flow-pane-lbl">Recent activity</div>
                        {selEvents.length === 0 ? <div className="muted small">Nothing recent.</div> : (
                          <ul className="flow-events">
                            {selEvents.map((e) => (
                              <li key={e.seq}>
                                <span className="muted small">{fmtTime(e.created_at)}</span> {describeEvent(e)}
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    </div>
                  )}
                </>
              ) : (
                <button className="btn small flow-reopen" onClick={() => setPaneOpen(true)} title="Expand">«</button>
              )}
            </aside>
          </div>
        </>
      )}
    </div>
  );
}

function describeEvent(e: EventRow): string {
  const p = e.payload || {};
  switch (e.event_type) {
    case 'task.review_requested': return `review requested: ${p.task || p.task_id || ''}`;
    case 'task.review_rejected': return `review rejected: ${p.summary || ''}`;
    case 'task.qa_handoff': return `handed off to QA: ${p.task || ''}`;
    case 'task.qa_passed': return `QA passed: ${p.task || ''}`;
    case 'task.status_changed': return `"${p.task || ''}" → ${p.status}`;
    case 'workflow.started': return `started: ${p.task || ''}`;
    case 'workflow.completed': return `completed: ${p.task || ''}`;
    case 'tool.started': return `tool ${p.tool} (${p.step || ''})`;
    case 'tool.completed': return p.tool === 'browser.smoke'
      ? `🌐 browser smoke ${p.result === 'ok' ? 'passed' : 'FAILED'}: ${String(p.summary || '').slice(0, 90)}`
      : `tool ${p.tool} ${p.result || ''}`;
    case 'workspace.run_isolated': return `🧪 isolated run workspace (no shared-file conflicts)`;
    case 'workspace.committed': return `💾 merged back ${p.files?.length ?? 0} file(s)${p.commit ? ` @ ${p.commit}` : ''}`;
    case 'workspace.file_conflict': return `⚠️ file conflict: ${p.files?.slice(0, 3).join(', ') || ''} (previous state committed)`;
    case 'agent.state_changed': return `${p.state}${p.activity ? ` — ${p.activity}` : ''}`;
    default: return e.event_type;
  }
}
