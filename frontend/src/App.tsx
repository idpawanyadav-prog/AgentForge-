import React from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { get, put } from './api';
import { isApiReachable, DisconnectedBanner } from './offline';
import { useAppStore } from './store';

// Route-level code splitting: each page ships as its own lazy chunk so the
// initial download only includes the shell + the page being opened.
const ControlPage = React.lazy(() => import('./pages/ControlPage'));
const ProjectsPage = React.lazy(() => import('./pages/ProjectsPage'));
const ProjectFlowPage = React.lazy(() => import('./pages/ProjectFlowPage'));
const FlowSetupPage = React.lazy(() => import('./pages/FlowSetupPage'));
const TeamsPage = React.lazy(() => import('./pages/TeamsPage'));
const PlaygroundPage = React.lazy(() => import('./pages/PlaygroundPage'));
const ModelsPage = React.lazy(() => import('./pages/ModelsPage'));
const MemoryPage = React.lazy(() => import('./pages/MemoryPage'));
const SettingsPage = React.lazy(() => import('./pages/SettingsPage'));

type Page = 'control' | 'flow' | 'flowsetup' | 'projects' | 'teams' | 'playground' | 'models' | 'memory' | 'settings';

const loadUi = (key: string, fallback: string) => {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
};
const saveUi = (key: string, value: string) => {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
};

const NAV: { id: Page; icon: string; label: string }[] = [
  { id: 'control', icon: '◉', label: 'Project Control' },
  { id: 'flow', icon: '⇄', label: 'Project Flow' },
  { id: 'flowsetup', icon: '⌗', label: 'Flow Setup' },
  { id: 'projects', icon: '▤', label: 'Projects' },
  { id: 'teams', icon: '⧉', label: 'Teams' },
  { id: 'playground', icon: '⚗', label: 'Playground' },
  { id: 'models', icon: '◈', label: 'Models' },
  { id: 'memory', icon: '✦', label: 'Agent Memory' },
  { id: 'settings', icon: '⚙', label: 'Settings' },
];

export default function App() {
  return <BrowserRouter><AppShell /></BrowserRouter>;
}

function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const page = location.pathname.slice(1) as Page;
  const theme = useAppStore((s) => s.theme);
  const setTheme = useAppStore((s) => s.setTheme);
  const activeProject = useAppStore((s) => s.activeProject);
  const setActiveProject = useAppStore((s) => s.setActiveProject);
  const collapsed = useAppStore((s) => s.collapsed);
  const setCollapsed = useAppStore((s) => s.setCollapsed);
  const [online, setOnline] = React.useState(true);

  const probeApi = React.useCallback(() => {
    isApiReachable().then(setOnline);
  }, []);

  React.useEffect(() => {
    probeApi();
    const id = setInterval(probeApi, 8000);
    return () => clearInterval(id);
  }, [probeApi]);

  React.useEffect(() => {
    get('/api/v1/settings')
      .then((s) => {
        const t = s.theme === 'bright' ? 'bright' : 'dark';
        setTheme(t);
        saveUi('ao.theme', t);
      })
      .catch(() => undefined);
    get('/api/v1/projects')
      .then((ps: any[]) => {
        if (!ps.length) return;
        // Only keep a saved project id if it still exists; otherwise fall
        // back to the first project so a stale localStorage value (e.g.
        // after a DB restore) can't leave the Control page polling a 404.
        setActiveProject((prev) =>
          prev && ps.some((p) => p.id === prev) ? prev : ps[0].id);
      })
      .catch(() => undefined);
  }, []);

  React.useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  React.useEffect(() => {
    if (NAV.some((n) => n.id === page)) saveUi('ao.page', page);
  }, [page]);
  React.useEffect(() => { saveUi('ao.sidebar', collapsed ? 'collapsed' : 'open'); }, [collapsed]);
  React.useEffect(() => { if (activeProject) saveUi('ao.project', activeProject); }, [activeProject]);

  const toggleTheme = () => {
    const next = theme === 'dark' ? 'bright' : 'dark';
    setTheme(next);
    saveUi('ao.theme', next);
    put('/api/v1/settings', { key: 'theme', value: next }).catch(() => undefined);
  };

  return (
    <div className="app">
      <aside className={`sidebar ${collapsed ? 'collapsed' : ''}`}>
        <div className="sidebar-head">
          {collapsed
            ? <img className="brand-logo" src="/logo.png" alt="AgentForge" title="AgentForge" />
            : (
              <div className="brand">
                <img className="brand-logo" src="/logo.png" alt="" />
                <span>Agent<em>Forge</em></span>
              </div>
            )}
        </div>
        {NAV.map((n) => (
          <button
            key={n.id}
            className={`nav-item ${page === n.id ? 'active' : ''}`}
            onClick={() => navigate(`/${n.id}`)}
            title={n.label}
          >
            <span className="nav-icon">{n.icon}</span>
            {!collapsed && <span className="nav-label">{n.label}</span>}
          </button>
        ))}
        <div className="sidebar-footer">
          <button className="theme-toggle" onClick={toggleTheme} title={`Switch to ${theme === 'dark' ? 'bright' : 'dark'} theme`}>
            <span>{theme === 'dark' ? '🌙' : '☀️'}</span>
            {!collapsed && (
              <>
                <span className="nav-label">{theme === 'dark' ? 'Dark' : 'Bright'}</span>
                <span className="small muted">switch</span>
              </>
            )}
          </button>
          {!collapsed && (
            <div style={{ marginTop: 12 }}>
              {activeProject ? 'Project session active' : 'No project selected'}
            </div>
          )}
          <button
            className="sidebar-toggle"
            onClick={() => setCollapsed((c) => !c)}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {collapsed ? '»' : '« Collapse'}
          </button>
        </div>
      </aside>
      <main className="main">
        {!online && <DisconnectedBanner onRetry={probeApi} />}
        <React.Suspense fallback={<div className="empty" style={{ padding: 24 }}>Loading…</div>}>
          <Routes>
            <Route path="/" element={<Navigate to={`/${loadUi('ao.page', 'control')}`} replace />} />
            <Route path="/control" element={<ControlPage activeProject={activeProject} setActiveProject={setActiveProject} />} />
            <Route path="/flow" element={<ProjectFlowPage activeProject={activeProject} setActiveProject={setActiveProject} />} />
            <Route path="/flowsetup" element={<FlowSetupPage />} />
            <Route path="/projects" element={<ProjectsPage activeProject={activeProject} setActiveProject={setActiveProject} onOpenControl={() => navigate('/control')} />} />
            <Route path="/teams" element={<TeamsPage />} />
            <Route path="/playground" element={<PlaygroundPage />} />
            <Route path="/models" element={<ModelsPage />} />
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/control" replace />} />
          </Routes>
        </React.Suspense>
      </main>
    </div>
  );
}
