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
interface Summary {
  project: any; agents: AgentRow[]; sprint: any; sprint_tasks: TaskRow[];
  events: EventRow[]; usage: any; active_runs: RunRow[];
  po?: { has_po: boolean; po_enabled: boolean; agent_name: string | null };
}

type Stage = 'dev' | 'sa' | 'ba' | 'qa' | 'done' | 'support';

// Fixed pipeline anchors in an abstract 1000x520 canvas space.
const ANCHOR: Record<Exclude<Stage, 'support'>, { x: number; y: number }> = {
  dev: { x: 110, y: 270 },
  sa: { x: 345, y: 150 },
  ba: { x: 565, y: 335 },
  qa: { x: 775, y: 160 },
  done: { x: 930, y: 320 },
};
const STAGE_LABEL: Record<Stage, string> = {
  dev: 'Developer', sa: 'Solution Architect', ba: 'Business Analyst',
  qa: 'QA Engineer', done: 'Delivered', support: 'Support',
};
const STAGE_ICON: Record<Stage, string> = {
  dev: '💻', sa: '🏛️', ba: '📋', qa: '🧪', done: '✅', support: '🧩',
};

function stageOfRole(role: string): Stage {
  const r = (role || '').toLowerCase();
  if (r.includes('architect') || r.includes('tech lead')) return 'sa';
  if (r.includes('business analyst')) return 'ba';
  if (r.includes('product owner')) return 'done';
  if (r.includes('qa') || r.includes('test')) return 'qa';
  if (r.includes('developer') || r.includes('engineer')) return 'dev';
  return 'support';
}
function stageOfStatus(status: string): Stage | null {
  switch (status) {
    case 'In Progress': return 'dev';
    case 'SA Review': return 'sa';
    case 'BA Review': return 'ba';
    case 'Waiting QA': case 'Testing': case 'Review': return 'qa';
    case 'Done': return 'done';
    case 'Rework': return 'dev';
    default: return null;
  }
}
// The forward hop that lands a task in this stage (for the traveling chip).
const ENTRY_FROM: Partial<Record<Stage, Stage>> = { sa: 'dev', ba: 'sa', qa: 'ba', done: 'qa' };

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
        // Detect forward handoffs to fire a one-shot traveling chip.
        const seen: Record<string, string> = {};
        for (const t of s.sprint_tasks || []) {
          seen[t.id] = t.status;
          const before = prevStatus.current[t.id];
          if (before && before !== t.status) {
            const a = stageOfStatus(before), b = stageOfStatus(t.status);
            if (a && b && a !== b && ENTRY_FROM[b] === a) {
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
  const saOn = !!summary?.project?.sa_review_enabled;
  const baOn = !!summary?.project?.ba_review_enabled;

  // Agents bucketed per stage; every stage node lists its agents.
  const byStage: Record<Stage, AgentRow[]> = { dev: [], sa: [], ba: [], qa: [], done: [], support: [] };
  for (const a of agents) byStage[stageOfRole(a.role_name)].push(a);

  const tasksAtStage = (st: Stage) => tasks.filter((t) => stageOfStatus(t.status) === st);
  const busyStages = new Set<Stage>();
  for (const r of runs) {
    const ag = agents.find((a) => a.id === r.agent_id);
    if (ag) busyStages.add(stageOfRole(ag.role_name));
    if (r.mode === 'dev') busyStages.add('dev');
    if (r.mode === 'sa') busyStages.add('sa');
    if (r.mode === 'ba') busyStages.add('ba');
    if (r.mode === 'qa') busyStages.add('qa');
  }

  // Connector list (only enabled gates get their review hop; skips collapse to QA).
  const connectors: { from: Stage; to: Stage; label: string; kind: 'flow' | 'skip' | 'rework' }[] = [];
  const nextAfterDev: Stage = saOn ? 'sa' : baOn ? 'ba' : 'qa';
  const nextAfterSa: Stage = baOn ? 'ba' : 'qa';
  const chain: Stage[] = ['dev', nextAfterDev];
  if (nextAfterDev === 'sa') chain.push(nextAfterSa);
  if (chain[chain.length - 1] !== 'qa') chain.push('qa');
  chain.push('done');
  for (let i = 0; i < chain.length - 1; i++) {
    connectors.push({ from: chain[i], to: chain[i + 1], label: hopLabel(chain[i], chain[i + 1]), kind: 'flow' });
  }
  // Rework loops back to dev from whichever review stages are enabled.
  for (const st of ['sa', 'ba', 'qa'] as Stage[]) {
    if (st === 'sa' && !saOn) continue;
    if (st === 'ba' && !baOn) continue;
    connectors.push({ from: st, to: 'dev', label: 'rework', kind: 'rework' });
  }

  function hopLabel(a: Stage, b: Stage): string {
    if (b === 'sa') return 'SA Review';
    if (b === 'ba') return 'BA Review';
    if (b === 'qa') return 'QA';
    if (b === 'done') return 'Done';
    return '';
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
  const inReview = tasks.filter((t) => ['SA Review', 'BA Review', 'Review', 'Waiting QA', 'Testing'].includes(t.status)).length;
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
                    const a = ANCHOR[c.from as Exclude<Stage, 'support'>];
                    const b = ANCHOR[c.to as Exclude<Stage, 'support'>];
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
                  const a = ANCHOR[c.from as Exclude<Stage, 'support'>];
                  const b = ANCHOR[c.to as Exclude<Stage, 'support'>];
                  return (
                    <div key={i} className="flow-line-label"
                      style={{ left: `${(((a.x + b.x) / 2) / 1000) * 100}%`, top: `${(((a.y + b.y) / 2) / 520) * 100}%` }}>
                      {c.label}
                    </div>
                  );
                })}

                {(['dev', 'sa', 'ba', 'qa', 'done'] as const).map((st) => {
                  const anchor = ANCHOR[st];
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
                  const a = ANCHOR[flash.from as Exclude<Stage, 'support'>];
                  const b = ANCHOR[flash.to as Exclude<Stage, 'support'>];
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
                <span><i className="lg dev" /> dev</span>
                <span><i className="lg sa" /> SA review</span>
                <span><i className="lg ba" /> BA review</span>
                <span><i className="lg qa" /> QA</span>
                <span><i className="lg rework" /> rework</span>
                <span className="muted">· gates: SA {saOn ? 'on' : 'off'}, BA {baOn ? 'on' : 'off'}</span>
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
