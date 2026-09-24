import React from 'react';
import { get, post, patch, del, fmtTime, AGENT_STATE_CLASS, TASK_STATE_CLASS } from '../api';
import { MarkdownText } from '../MarkdownText';
import { Badge, ErrorNote } from '../components';

interface Msg { id: string; role: string; content: string; meta: string; created_at: string }
interface Conv { id: string; title: string }
interface AgentRow {
  id: string; name: string; lifecycle_state: string; current_activity: string;
  current_task_id: string | null; role_name: string; persona_name?: string; provider_model_id?: string;
}
interface EventRow { seq: number; event_type: string; payload: any; created_at: string }
interface TaskRow {
  id: string; title: string; status: string; progress: number; story_points: number;
  agent_name: string | null; dependencies: { id: string; title: string; status: string }[];
  unmet_dependencies: any[]; blocked_reason: string; evidence: string;
}
interface SprintGateSummary {
  sprint_id: string; sprint: string; status: string; failure_reason: string;
  gates: { gate: string; status: string }[];
  acceptance_criteria: { code: string; description: string; required: number; status: string; result: string; failure_reason: string }[];
}
interface Summary {
  project: any; agents: AgentRow[]; sprint: any; sprint_tasks: TaskRow[];
  sprint_gates?: SprintGateSummary | null;
  next_locked_sprint?: { id: string; name: string; status: string } | null;
  events: EventRow[]; usage: any; active_runs: any[]; backlog_count: number; scheduler_running: boolean;
  po?: { has_po: boolean; po_enabled: boolean; agent_name: string | null };
}

function eventColor(t: string): string {
  if (/(failed|error|blocked)/.test(t)) return 'dot-err';
  if (/conflict/.test(t)) return 'dot-warn';
  if (/(completed|done|approved)/.test(t)) return 'dot-ok';
  if (/(paused|waiting|requested)/.test(t)) return 'dot-warn';
  return 'dot-info';
}

const TASK_ICON: Record<string, string> = {
  'Done': '✅', 'In Progress': '🔄', 'Review': '🔍', 'Testing': '🧪',
  'Ready': '🟡', 'Todo': '⏳', 'Blocked': '⛔', 'Cancelled': '✖️',
  'Waiting QA': '🧫', 'Rework': '🔁', 'SA Review': '🏛️', 'BA Review': '📋',
};
const taskIcon = (s: string) => TASK_ICON[s] ?? '⏳';

const TASK_STATUSES = ['Todo', 'Ready', 'In Progress', 'Testing', 'Review',
  'SA Review', 'BA Review', 'Waiting QA', 'Rework', 'Blocked', 'Done', 'Cancelled'];

const loadUi = (key: string, fallback: string) => {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
};
const saveUi = (key: string, value: string) => {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
};

function eventText(e: EventRow): string {
  const p = e.payload || {};
  switch (e.event_type) {
    case 'agent.state_changed': return `${p.agent_name} → ${p.state}${p.activity ? ` — ${p.activity}` : ''}`;
    case 'agent.activity': return `${p.activity}`;
    case 'task.status_changed': return `Task "${p.task || p.task_id}" → ${p.status}${p.blocked_reason ? ` (${p.blocked_reason})` : ''}`;
    case 'workflow.started': return `Execution started for "${p.task}" on ${p.agent} (${p.model}, persona v${p.persona_version})`;
    case 'workflow.completed': return `Execution completed: "${p.task}" — est. $${p.usage?.cost_usd}`;
    case 'workflow.failed': return `Execution failed: ${p.error}`;
    case 'workflow.cancelled': return `Execution cancelled`;
    case 'workflow.paused': return `Execution paused`; 
    case 'workflow.resumed': return `Execution resumed`;
    case 'tool.started': return `Tool ${p.tool} started (${p.step})`;
    case 'tool.completed': return p.tool === 'browser.smoke'
      ? `🌐 Browser smoke ${p.result === 'ok' ? 'passed' : 'FAILED'}: ${String(p.summary || '').slice(0, 110)}`
      : `Tool ${p.tool} completed`;
    case 'tool.failed': return `Tool ${p.tool} failed: ${p.error}`;
    case 'usage.recorded': return `Usage: ${p.input_tokens + p.output_tokens} tokens, $${p.cost_usd}`;
    case 'project.updated': return p.note || 'Project updated';
    case 'chat.message': return p.text || 'Chat message';
    case 'po.enabled': return `👑 ${p.note || `Product Owner ${p.agent || ''} enabled`}`;
    case 'po.disabled': return `Product Owner authority disabled — control returned to you`;
    case 'po.review': return p.ok
      ? `👑 PO review: ${p.summary || 'no changes needed'}${p.actions?.length ? ` (${p.actions.length} action(s))` : ''}`
      : `👑 PO review failed: ${p.error || 'model unavailable'}`;
    case 'po.action': return `👑 PO: ${p.action}${p.task ? ` — ${p.task}` : ''}${p.agent ? ` → ${p.agent}` : ''}${p.count ? ` (${p.count})` : ''}`;
    case 'sprint.tasks_drafted': return `Auto-drafted ${p.tasks?.length ?? 0} task(s) for "${p.sprint}"`;
    case 'sprint.activated': return `Sprint "${p.sprint}" activated`;
    case 'sprint.status.changed': return `Sprint "${p.sprint}" → ${p.status}${p.reason ? ` (${p.reason})` : ''}`;
    case 'sprint.gate.changed': return `${p.sprint} — ${p.gate} gate: ${p.status}${p.summary ? ` (${String(p.summary).slice(0, 80)})` : ''}`;
    case 'sprint.unlocked': return `🔓 Sprint "${p.sprint}" unlocked (next sprint ready)`;
    case 'task.blocked_by_sprint_gate': return `⛔ Task "${p.task}" blocked by sprint gate: ${p.reason}`;
    case 'workspace.run_isolated': return `🧪 "${p.task}" running on an isolated workspace copy (no shared-file conflicts)`;
    case 'workspace.committed': return `💾 committed ${p.commit || ''} — ${p.files?.length ?? 0} file(s) merged back`;
    case 'workspace.file_conflict': return `⚠️ File conflict on ${p.files?.slice(0, 3).join(', ')}${(p.files?.length ?? 0) > 3 ? '…' : ''} — previous live state committed to git before overwrite`;
    case 'agent.status.changed': return p.activity ? `${p.agent}: ${p.activity}` : `${p.agent}: ${p.state}`;
    default: return e.event_type;
  }
}

const GATE_ICON: Record<string, string> = {
  'Passed': '✅', 'Failed': '❌', 'Running': '🔄', 'Pending': '⏳',
};
const SPRINT_STATUS_ICON: Record<string, string> = {
  'Planned': '📝', 'Ready': '🔓', 'Active': '🟢',
  'Development Complete': '🔧', 'Build Validation': '🏗️', 'Automated Testing': '🧪',
  'Functional Validation': '🧫', 'Sprint Acceptance': '📋', 'Rework': '🔁',
  'Completed': '✅', 'Failed': '⛔', 'Cancelled': '✖️',
};

function SprintGatePanel({ s }: { s: Summary | null }) {
  const g = s?.sprint_gates;
  const next = s?.next_locked_sprint;
  if (!g) return null;
  const acs = g.acceptance_criteria ?? [];
  const passedAcs = acs.filter((a) => a.status === 'Passed').length;
  return (
    <div className="sprint-gate" style={{ marginBottom: 12 }}>
      <div className="spread" style={{ marginBottom: 6 }}>
        <b style={{ fontSize: 13 }}>🚧 SPRINT GATE — {g.sprint}</b>
        <Badge kind={g.status === 'Completed' ? 'ok' : g.status === 'Failed' ? 'err' : 'info'}>
          {SPRINT_STATUS_ICON[g.status] ?? ''} {g.status}
        </Badge>
      </div>
      <div className="gate-list">
        {g.gates.map((gate) => (
          <div key={gate.gate} className="gate-item">
            <span>{GATE_ICON[gate.status] ?? '⏳'} {gate.gate}</span>
            <span className={`gate-status gate-${gate.status.toLowerCase()}`}>{gate.status}</span>
          </div>
        ))}
      </div>
      {acs.length > 0 && (
        <div className="small muted" style={{ marginTop: 6 }}>
          Acceptance criteria: {passedAcs}/{acs.length} passed
        </div>
      )}
      {g.failure_reason && (
        <div className="small" style={{ color: 'var(--err)', marginTop: 6 }}>⛔ {g.failure_reason}</div>
      )}
      {next && (
        <div className="small muted" style={{ marginTop: 6 }}>
          🔒 {next.name} locked — waiting for {g.sprint} to complete
        </div>
      )}
    </div>
  );
}

export default function ControlPage({ activeProject, setActiveProject }: { activeProject: string | null; setActiveProject: (id: string) => void }) {
  const [projects, setProjects] = React.useState<any[]>([]);
  const [convos, setConvos] = React.useState<Conv[]>([]);
  const [activeConv, setActiveConv] = React.useState<string | null>(null);
  const [messages, setMessages] = React.useState<Msg[]>([]);
  const [input, setInput] = React.useState('');
  const [sending, setSending] = React.useState(false);
  const [pending, setPending] = React.useState<string | null>(null);
  const [suggestions, setSuggestions] = React.useState<string[]>([]);
  const [summary, setSummary] = React.useState<Summary | null>(null);
  const [liveEvents, setLiveEvents] = React.useState<EventRow[]>([]);
  const [sprints, setSprints] = React.useState<any[]>([]);
  const [selectedSprint, setSelectedSprint] = React.useState<string>('');
  const [sprintTasks, setSprintTasks] = React.useState<TaskRow[]>([]);
  // Sprint task status filter (multi-select). Empty set = show all.
  const [statusFilter, setStatusFilter] = React.useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem('ao.statusFilter');
      const arr = saved ? JSON.parse(saved) : null;
      return Array.isArray(arr) && arr.length ? new Set(arr) : new Set(TASK_STATUSES);
    } catch { return new Set(TASK_STATUSES); }
  });
  React.useEffect(() => {
    try { localStorage.setItem('ao.statusFilter', JSON.stringify([...statusFilter])); } catch { /* private mode */ }
  }, [statusFilter]);
  const toggleStatusFilter = (st: string) => {
    setStatusFilter((prev) => {
      const next = new Set(prev);
      if (next.has(st)) next.delete(st); else next.add(st);
      return next.size ? next : new Set(TASK_STATUSES); // never filter to nothing
    });
  };
  const filteredSprintTasks = statusFilter.size === TASK_STATUSES.length
    ? sprintTasks
    : sprintTasks.filter((t) => statusFilter.has(t.status));
  // Chat windowing: show only the most recent messages; older ones load in
  // batches of 12 via the "Load previous chat history" button.
  const [visibleMsgs, setVisibleMsgs] = React.useState(12);
  // Activity windowing: server-paged — fetch the latest 15 events (desc),
  // then page older history 15 at a time via the "Load earlier activity" button.
  const [eventsExhausted, setEventsExhausted] = React.useState(false);
  const [loadingOlder, setLoadingOlder] = React.useState(false);
  const [tab, setTab] = React.useState<'team' | 'activity' | 'tasks'>(() => {
    const saved = loadUi('ao.tab', 'team');
    return saved === 'activity' || saved === 'tasks' ? saved : 'team';
  });
  const [paneOpen, setPaneOpen] = React.useState<boolean>(() => loadUi('ao.pane', 'open') !== 'closed');
  React.useEffect(() => { saveUi('ao.tab', tab); }, [tab]);
  React.useEffect(() => { saveUi('ao.pane', paneOpen ? 'open' : 'closed'); }, [paneOpen]);
  const [expanded, setExpanded] = React.useState<Record<string, boolean>>({});
  const [error, setError] = React.useState<string | null>(null);
  const msgEndRef = React.useRef<HTMLDivElement>(null);
  const lastSeqRef = React.useRef(0);

  // Product Owner chat mode + autonomous-authority toggle
  const [poMode, setPoMode] = React.useState<boolean>(() => loadUi('ao.pomode', 'chat') === 'po');
  React.useEffect(() => { saveUi('ao.pomode', poMode ? 'po' : 'chat'); }, [poMode]);
  const [poMessages, setPoMessages] = React.useState<Msg[]>([]);
  const [poInput, setPoInput] = React.useState('');
  const [poSending, setPoSending] = React.useState(false);
  const [poFiles, setPoFiles] = React.useState<{ name: string; content: string }[]>([]);
  const poEndRef = React.useRef<HTMLDivElement>(null);
  const fileRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    get('/api/v1/projects').then(setProjects).catch(() => undefined);
  }, []);

  React.useEffect(() => {
    if (!activeProject) return;
    get(`/api/v1/projects/${activeProject}/conversations`)
      .then((cs: Conv[]) => {
        setConvos(cs);
        if (cs.length && !cs.find((c) => c.id === activeConv)) {
          // Restore the last chat window of this project across sessions.
          setActiveConv(loadUi(`ao.conv.${activeProject}`, '') || cs[0].id);
        }
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeProject]);

  React.useEffect(() => {
    if (activeConv && activeProject) saveUi(`ao.conv.${activeProject}`, activeConv);
  }, [activeConv, activeProject]);

  const loadMessages = React.useCallback((cid: string) => {
    get(`/api/v1/conversations/${cid}/messages`).then(setMessages).catch(() => undefined);
    setVisibleMsgs(12);
    setSuggestions([]);
    setPending(null);
  }, []);

  React.useEffect(() => { if (activeConv) loadMessages(activeConv); }, [activeConv, loadMessages]);
  React.useEffect(() => { msgEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  // Live messages: the Product Owner narrates autonomous decisions into the
  // main chat — poll so they appear without the user sending anything.
  React.useEffect(() => {
    if (!activeConv || poMode) return;
    const t = setInterval(() => {
      get(`/api/v1/conversations/${activeConv}/messages`).then((ms: Msg[]) => {
        setMessages((prev) => (ms.length !== prev.length || ms[ms.length - 1]?.content !== prev[prev.length - 1]?.content ? ms : prev));
      }).catch(() => undefined);
    }, 3000);
    return () => clearInterval(t);
  }, [activeConv, poMode]);

  // Initial Activity load: ONLY the latest 15 events, newest first (server-side).
  React.useEffect(() => {
    if (!activeProject) return;
    setEventsExhausted(false);
    get(`/api/v1/projects/${activeProject}/events?latest=15`).then((r: any) => {
      lastSeqRef.current = r.last_seq ?? 0;
      setLiveEvents(r.events ?? []);
      setEventsExhausted(!!r.exhausted);
    }).catch(() => undefined);
  }, [activeProject]);

  // Live polling: summary (team state), incremental event stream, selected sprint tasks.
  React.useEffect(() => {
    if (!activeProject) return;
    let alive = true;
    let busy = false;
    const poll = async () => {
      if (busy) return;
      busy = true;
      try {
        const s: Summary = await get(`/api/v1/projects/${activeProject}/control/summary`);
        if (!alive) return;
        setSummary(s);
        // Events: incremental forward poll once the initial latest-15 page is in
        // (lastSeqRef > 0); new events are prepended so the list stays desc.
        if (lastSeqRef.current > 0) {
          const evs: any = await get(`/api/v1/projects/${activeProject}/events?after=${lastSeqRef.current}`);
          if (!alive) return;
          if (evs.events?.length) {
            lastSeqRef.current = evs.last_seq;
            setLiveEvents((prev) => {
              const known = new Set(prev.map((e) => e.seq));
              const fresh = (evs.events as EventRow[]).filter((e) => !known.has(e.seq));
              return [...fresh.reverse(), ...prev];
            });
          }
        }
      } catch { /* transient */ }
      finally { busy = false; }
    };
    poll();
    const timer = setInterval(poll, 1200);
    return () => { alive = false; clearInterval(timer); };
  }, [activeProject]);

  const loadOlderEvents = async () => {
    if (!activeProject || !liveEvents.length || loadingOlder || eventsExhausted) return;
    setLoadingOlder(true);
    try {
      const oldest = liveEvents[liveEvents.length - 1].seq;
      const r: any = await get(`/api/v1/projects/${activeProject}/events?before=${oldest}&limit=15`);
      setLiveEvents((prev) => [...prev, ...(r.events ?? [])]);
      if (typeof r.exhausted === 'boolean') setEventsExhausted(r.exhausted);
    } catch { /* transient */ }
    finally { setLoadingOlder(false); }
  };

  // Sprints list + default selection. Auto-follows the ACTIVE sprint (e.g.
  // when the Product Owner activates a new one) unless the user manually
  // picked a sprint in this session.
  const userPickedSprint = React.useRef(false);
  React.useEffect(() => {
    if (!activeProject) return;
    get(`/api/v1/projects/${activeProject}/sprints`).then((list: any[]) => {
      setSprints(list);
      setSelectedSprint((cur) => {
        const active = list.find((x) => x.status === 'Active');
        if (userPickedSprint.current && cur && list.some((x) => x.id === cur)) return cur;
        if (active) return active.id;
        const saved = loadUi(`ao.sprint.${activeProject}`, '');
        if (saved && list.some((x) => x.id === saved)) return saved;
        return (active ?? list[0])?.id ?? '';
      });
    }).catch(() => undefined);
  }, [activeProject, summary?.sprint?.id]);

  React.useEffect(() => {
    if (selectedSprint && activeProject) saveUi(`ao.sprint.${activeProject}`, selectedSprint);
  }, [selectedSprint, activeProject]);

  // Tasks of the selected sprint, refreshed on the same live cadence
  React.useEffect(() => {
    if (!activeProject || !selectedSprint) { setSprintTasks([]); return; }
    let alive = true;
    const load = () => get(`/api/v1/projects/${activeProject}/tasks?sprint_id=${selectedSprint}`)
      .then((t: TaskRow[]) => { if (alive) setSprintTasks(t); }).catch(() => undefined);
    load();
    const t = setInterval(load, 2000);
    return () => { alive = false; clearInterval(t); };
  }, [activeProject, selectedSprint]);

  const send = async (text?: string) => {
    const content = (text ?? input).trim();
    if (!content || !activeProject || !activeConv || sending) return;
    setSending(true); setError(null); setInput('');
    setMessages((m) => [...m, { id: 'tmp', role: 'user', content, meta: '', created_at: '' }]);
    try {
      const res = await post(`/api/v1/projects/${activeProject}/conversations/${activeConv}/messages`, { content });
      setMessages(res.messages);
      setPending(res.pending_command ?? null);
      setSuggestions(Array.isArray(res.suggestions) ? res.suggestions : []);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSending(false);
    }
  };

  const newConversation = async () => {
    const c = await post(`/api/v1/projects/${activeProject}/conversations`);
    setConvos((cs) => [c, ...cs]);
    setActiveConv(c.id);
  };

  const deleteConversation = async (cid: string) => {
    await del(`/api/v1/conversations/${cid}`);
    const rest = convos.filter((c) => c.id !== cid);
    setConvos(rest);
    setActiveConv(rest[0]?.id ?? null);
    if (!rest[0]) setMessages([]);
  };

  // Product Owner conversation (dedicated endpoint, survives sessions)
  React.useEffect(() => {
    if (!poMode || !activeProject) { setPoMessages([]); return; }
    get(`/api/v1/projects/${activeProject}/po/messages`).then(setPoMessages).catch(() => undefined);
  }, [poMode, activeProject]);
  React.useEffect(() => { if (poMode) poEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [poMessages]);
  // If the project has no Product Owner (or it was removed), drop out of PO chat mode.
  React.useEffect(() => { if (poMode && summary && !summary.po?.has_po) setPoMode(false); }, [poMode, summary]);

  const togglePo = async () => {
    if (!activeProject || !s?.po?.has_po) return;
    setError(null);
    try {
      await post(`/api/v1/projects/${activeProject}/po/${s.po.po_enabled ? 'disable' : 'enable'}`);
    } catch (e: any) { setError(e.message); }
  };

  const attachFiles = (files: FileList | null) => {
    if (!files?.length) return;
    Array.from(files).slice(0, 5 - poFiles.length).forEach((f) => {
      if (f.size > 200 * 1024) { setError(`${f.name} is too large (max 200 KB)`); return; }
      const reader = new FileReader();
      reader.onload = () => {
        setPoFiles((cur) => cur.length < 5
          ? [...cur, { name: f.name, content: String(reader.result ?? '') }]
          : cur);
      };
      reader.readAsText(f);
    });
    if (fileRef.current) fileRef.current.value = '';
  };

  const poSend = async (text?: string) => {
    const content = (text ?? poInput).trim();
    if ((!content && !poFiles.length) || !activeProject || poSending) return;
    setPoSending(true); setError(null); setPoInput('');
    setPoMessages((m) => [...m, { id: 'tmp-po', role: 'user', content: content || '(files only)', meta: '', created_at: '' }]);
    try {
      const res = await post(`/api/v1/projects/${activeProject}/po/chat`, { content, attachments: poFiles });
      setPoMessages(res.messages ?? []);
      setPoFiles([]);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setPoSending(false);
    }
  };

  const startTask = async (tid: string) => {
    setError(null);
    try { await post('/api/v1/executions', { project_id: activeProject, task_id: tid }); }
    catch (e: any) { setError(e.message); }
  };

  const runAction = async (runId: string, action: string) => {
    setError(null);
    try { await post(`/api/v1/executions/${runId}/${action}`); }
    catch (e: any) { setError(e.message); }
  };

  const sprintAction = async (action: 'start' | 'stop') => {
    setError(null);
    try { await post(`/api/v1/projects/${activeProject}/sprint-execution/${action}`); }
    catch (e: any) { setError(e.message); }
  };

  const setTaskStatus = async (tid: string, status: string) => {
    setError(null);
    try { await patch(`/api/v1/tasks/${tid}`, { status }); }
    catch (e: any) { setError(e.message); }
  };

  if (!activeProject) {
    return (
      <div className="page"><div className="empty">
        No projects yet. Create one on the <b>Projects</b> page to open Project Control.
      </div></div>
    );
  }

  const s = summary;
  const running = s?.active_runs?.length ?? 0;

  return (
    <div className="control-layout">
      {/* ------------- chat column ------------- */}
      <div className="chat-col">
        <div className="chat-toolbar">
          <select value={activeProject} onChange={(e) => { setActiveProject(e.target.value); lastSeqRef.current = 0; }}>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <select value={activeConv ?? ''} onChange={(e) => setActiveConv(e.target.value)}>
            {convos.map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}
          </select>
          <button className="btn small" onClick={newConversation}>+ New chat</button>
          {activeConv && !poMode && <button className="btn small danger" onClick={() => deleteConversation(activeConv)}>Delete</button>}
          <span style={{ flex: 1 }} />
          {s?.po?.has_po && (
            <button
              className={`btn small ${s.po.po_enabled ? 'primary po-on' : ''}`}
              onClick={togglePo}
              title={s.po.po_enabled
                ? 'Product Owner authority is ON — click to disable and take back control'
                : `Enable ${s.po.agent_name} (Product Owner) to run this project autonomously`}
            >
              🤖 Product Owner: {s.po.po_enabled ? 'On' : 'Off'}
              {s.po.po_enabled && <span className="live-dot po-live-dot" style={{ marginLeft: 6 }} title="PO authority active" />}
            </button>
          )}
          {s?.po?.has_po && (
            <button
              className={`btn small ${poMode ? 'primary' : ''}`}
              onClick={() => setPoMode((m) => !m)}
              title="Talk to the Product Owner agent about requirements (supports file attachments)"
            >
              👑 Chat with Product Owner
            </button>
          )}
          {s?.sprint && (
            s.scheduler_running
              ? <button className="btn small danger" onClick={() => sprintAction('stop')}>⏹ Stop sprint execution</button>
              : <button className="btn small primary" onClick={() => sprintAction('start')}>▶ Start sprint execution</button>
          )}
          {running > 0 && (
            <span className="row" style={{ gap: 4 }}>
              <button className="btn small" onClick={() => s!.active_runs.forEach((r: any) => runAction(r.id, 'pause'))}>Pause</button>
              <button className="btn small" onClick={() => s!.active_runs.forEach((r: any) => runAction(r.id, 'resume'))}>Resume</button>
              <button className="btn small danger" onClick={() => s!.active_runs.forEach((r: any) => runAction(r.id, 'cancel'))}>Cancel</button>
            </span>
          )}
        </div>
        <div className="chat-messages">
          {poMode ? (
            <>
              {poMessages.length === 0 && (
                <div className="empty">
                  This is the direct line to the <b>Product Owner</b> agent. Share requirements here —
                  plain text or attached files (.md, .txt, .json, source code). Enable the Product Owner
                  toggle to let it act autonomously on your behalf.
                </div>
              )}
              {poMessages.map((m) => (
                <div key={m.id} className={`msg ${m.role}`}
                  children={<MarkdownText text={m.content} />} />
              ))}
              <div ref={poEndRef} />
            </>
          ) : (
            <>
              {messages.length === 0 && (
                <div className="empty">Ask the assistant to create roles, agents, teams, backlog items, or to start work. Type <b>help</b> for the command catalog.</div>
              )}
              {messages.length > visibleMsgs && (
                <button
                  className="btn small"
                  style={{ alignSelf: 'center', margin: '4px 0 8px' }}
                  onClick={() => setVisibleMsgs((n) => n + 12)}>
                  ↑ Load previous chat history ({messages.length - visibleMsgs} older)
                </button>
              )}
              {messages.slice(-visibleMsgs).map((m) => (
                <div key={m.id} className={`msg ${m.role}`}
                  children={<MarkdownText text={m.content} />} />
              ))}
              {pending && (
                <div className="row" style={{ alignSelf: 'flex-start', gap: 6 }}>
                  <button className="btn small primary" onClick={() => send('confirm')}>✓ Confirm</button>
                  <button className="btn small danger" onClick={() => send('cancel that')}>✕ Cancel</button>
                </div>
              )}
              <div ref={msgEndRef} />
            </>
          )}
        </div>
        {error && <div style={{ padding: '0 14px 6px' }}><Badge kind="err">{error}</Badge></div>}
        {poMode ? (
          <>
            {poFiles.length > 0 && (
              <div className="row" style={{ padding: '4px 14px 0', gap: 6, flexWrap: 'wrap' }}>
                {poFiles.map((f, i) => (
                  <span key={`${f.name}-${i}`} className="btn small" style={{ cursor: 'default' }}>
                    📎 {f.name}
                    <button className="btn small danger" style={{ marginLeft: 6, padding: '0 6px' }}
                      onClick={() => setPoFiles((cur) => cur.filter((_, j) => j !== i))}>✕</button>
                  </span>
                ))}
              </div>
            )}
            <div className="chat-input">
              <input ref={fileRef} type="file" multiple style={{ display: 'none' }}
                onChange={(e) => attachFiles(e.target.files)} />
              <button className="btn" disabled={poSending || poFiles.length >= 5}
                onClick={() => fileRef.current?.click()} title="Attach requirement files">📎</button>
              <textarea
                value={poInput}
                placeholder="Message the Product Owner… (share requirements; attach files with 📎)"
                onChange={(e) => setPoInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); poSend(); } }}
              />
              <button className="btn primary" disabled={poSending || (!poInput.trim() && !poFiles.length)} onClick={() => poSend()}>
                {poSending ? '…' : 'Send'}
              </button>
            </div>
          </>
        ) : (
          <>
            {!pending && suggestions.length > 0 && (
              <div className="row" style={{ padding: '4px 14px 8px', gap: 6, flexWrap: 'wrap' }}>
                {suggestions.map((s) => (
                  <button key={s} className="btn small" disabled={sending}
                    onClick={() => send(s)} title={`Send: ${s}`}>{s}</button>
                ))}
              </div>
            )}
            <div className="chat-input">
              <textarea
                value={input}
                placeholder="Message Project Control assistant… (help for commands)"
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
              />
              <button className="btn primary" disabled={sending || !input.trim()} onClick={() => send()}>
                {sending ? '…' : 'Send'}
              </button>
            </div>
          </>
        )}
      </div>

      {/* ------------- right operations pane ------------- */}
      <div className={`right-pane ${paneOpen ? '' : 'collapsed'}`}>
        {paneOpen ? (
          <>
            <div className="pane-tabs">
              <button className={`pane-tab ${tab === 'team' ? 'active' : ''}`} onClick={() => setTab('team')}>
                Team {(s?.scheduler_running || running > 0) && <span className="live-dot" title="Live" />}
              </button>
              <button className={`pane-tab ${tab === 'activity' ? 'active' : ''}`} onClick={() => setTab('activity')}>
                Activity {(s?.scheduler_running || running > 0) && <span className="live-dot" title="Live" />}
              </button>
              <button className={`pane-tab ${tab === 'tasks' ? 'active' : ''}`} onClick={() => setTab('tasks')}>Sprint Tasks</button>
            </div>
            <div className="pane-body">
              <ErrorNote error={error} />
              {tab === 'team' && (
                <>
                  {s?.usage && (
                    <div className="row" style={{ marginBottom: 12, gap: 8 }}>
                      <div className="stat-chip"><div className="v">{s.agents.length}</div><div className="l">Agents</div></div>
                      <div className="stat-chip"><div className="v">{(s.usage.input_tokens + s.usage.output_tokens).toLocaleString()}</div><div className="l">Tokens</div></div>
                      <div className="stat-chip"><div className="v">${Number(s.usage.cost).toFixed(4)}</div><div className="l">Est. cost</div></div>
                    </div>
                  )}
                  {s?.agents.map((a) => (
                    <div key={a.id} className="agent-card" onClick={() => setExpanded((x) => ({ ...x, [a.id]: !x[a.id] }))}>
                      <div className="head">
                        <div>
                          <b>{a.name}</b> <span className="muted small">{a.role_name}</span>
                          <div className="kv">{a.provider_model_id ?? 'no model'} · {a.persona_name ?? 'no persona'}</div>
                        </div>
                        <Badge kind={AGENT_STATE_CLASS[a.lifecycle_state] ?? 'dim'}>{a.lifecycle_state}</Badge>
                      </div>
                      {(expanded[a.id] || a.lifecycle_state === 'Working') && (
                        <div className="agent-activity">
                          {a.current_activity || 'Idle — no current activity'}
                          {a.current_task_id && <div style={{ marginTop: 4 }}>Task: {s?.sprint_tasks.find((t) => t.id === a.current_task_id)?.title ?? a.current_task_id}</div>}
                        </div>
                      )}
                    </div>
                  ))}
                  {!s?.agents.length && <div className="empty">No team assigned to this project.</div>}
                </>
              )}

              {tab === 'activity' && (
                <>
                  <div className="small muted" style={{ marginBottom: 8 }}>
                    <span className={`live-dot ${s?.scheduler_running || running > 0 ? '' : 'idle'}`} /> Live event stream
                  </div>
                  {liveEvents.map((e) => (
                    <div key={e.seq} className="event-item">
                      <span className={`event-dot ${eventColor(e.event_type)}`} />
                      <div>
                        <div>{eventText(e)}</div>
                        <div className="kv">{e.event_type}</div>
                      </div>
                      <span style={{ flex: 1 }} />
                      <span className="event-time">{fmtTime(e.created_at)}</span>
                    </div>
                  ))}
                  {!eventsExhausted && liveEvents.length > 0 && (
                    <button
                      className="btn small"
                      style={{ alignSelf: 'center', margin: '8px 0 2px' }}
                      disabled={loadingOlder}
                      onClick={loadOlderEvents}>
                      {loadingOlder ? 'Loading…' : '↓ Load earlier activity'}
                    </button>
                  )}
                  {!liveEvents.length && <div className="empty">No activity yet. Start a task to see live events.</div>}
                </>
              )}

              {tab === 'tasks' && (
                <>
                  <SprintGatePanel s={s} />
                  <div className="row" style={{ marginBottom: 10, gap: 8 }}>
                    <select style={{ flex: 1 }} value={selectedSprint}
                      onChange={(e) => { userPickedSprint.current = true; setSelectedSprint(e.target.value); }}>
                      <option value="">— select sprint —</option>
                      {sprints.map((sp) => (
                        <option key={sp.id} value={sp.id}>
                          {SPRINT_STATUS_ICON[sp.status] ?? '📝'} {sp.name} · {sp.status} · {sp.committed_points ?? 0}/{sp.capacity} pts
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="row" style={{ marginBottom: 10, flexWrap: 'wrap', gap: 6 }}>
                    {TASK_STATUSES.map((st) => {
                      const n = sprintTasks.filter((t) => t.status === st).length;
                      if (!n) return null;
                      const on = statusFilter.has(st);
                      return (
                        <button key={st} title={on ? `Hide ${st} tasks` : `Show ${st} tasks`}
                          className={`chip ${on ? 'chip-on' : ''}`}
                          onClick={() => toggleStatusFilter(st)}>
                          {taskIcon(st)} {st} · {n}
                        </button>
                      );
                    })}
                  </div>
                  {filteredSprintTasks.map((t) => (
                    <div key={t.id} className="task-row">
                      <div className="spread">
                        <b style={{ fontSize: 13 }}>{taskIcon(t.status)} {t.title}</b>
                        <Badge kind={TASK_STATE_CLASS[t.status] ?? 'dim'}>{t.status}</Badge>
                      </div>
                      <div className="row small muted" style={{ marginTop: 4, gap: 10 }}>
                        <span>{t.agent_name ?? 'unassigned'}</span>
                        <span>{t.story_points} pts</span>
                        <span>{t.progress}%</span>
                        {t.dependencies.length > 0 && <span>deps: {t.dependencies.map((d) => `${taskIcon(d.status)} ${d.title}`).join(', ')}</span>}
                      </div>
                      {t.blocked_reason && <div className="small" style={{ color: 'var(--err)', marginTop: 4 }}>⛔ {t.blocked_reason}</div>}
                      {t.evidence && <div className="small mono" style={{ marginTop: 4, color: 'var(--ok)' }}>evidence: {t.evidence}</div>}
                      <div className="progress-track"><div className="progress-fill" style={{ width: `${t.progress}%` }} /></div>
                      <div className="btn-row" style={{ marginTop: 8 }}>
                        {['Todo', 'Ready', 'Blocked'].includes(t.status) && (
                          <button className="btn small primary" onClick={() => startTask(t.id)}>▶ Start</button>
                        )}
                        {t.unmet_dependencies.length === 0 && t.status === 'Todo' && (
                          <button className="btn small" onClick={() => setTaskStatus(t.id, 'Ready')}>Mark Ready</button>
                        )}
                        {['Review', 'Testing'].includes(t.status) && (
                          <button className="btn small" onClick={() => setTaskStatus(t.id, 'Done')}>✓ Accept (Done)</button>
                        )}
                        {t.status === 'Blocked' && (
                          <button className="btn small" onClick={() => setTaskStatus(t.id, 'Ready')}>Clear blocker</button>
                        )}
                      </div>
                    </div>
                  ))}
                  {!sprintTasks.length && (
                    <div className="empty">
                      {selectedSprint ? 'No tasks committed to this sprint.' : 'No sprints yet. Plan one on the Projects page.'}
                    </div>
                  )}
                  {!!sprintTasks.length && !filteredSprintTasks.length && (
                    <div className="empty">No tasks match the status filter — tap a status chip above to widen it.</div>
                  )}
                </>
              )}
            </div>
            <button
              className="pane-toggle"
              onClick={() => setPaneOpen(false)}
              title="Collapse pane"
            >
              Collapse »
            </button>
          </>
        ) : (
          <>
            <button className="pane-tab" style={{ flex: 1, writingMode: 'vertical-rl' }} onClick={() => setPaneOpen(true)}>
              « Operations pane
            </button>
            <button className="pane-toggle" onClick={() => setPaneOpen(true)} title="Expand pane">
              «
            </button>
          </>
        )}
      </div>
    </div>
  );
}
