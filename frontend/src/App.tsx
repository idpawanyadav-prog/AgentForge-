import React from 'react';
import { get, put } from './api';
import { isApiReachable, DisconnectedBanner } from './offline';
import ControlPage from './pages/ControlPage';
import ProjectsPage from './pages/ProjectsPage';
import ProjectFlowPage from './pages/ProjectFlowPage';
import TeamsPage from './pages/TeamsPage';
import ModelsPage from './pages/ModelsPage';
import MemoryPage from './pages/MemoryPage';
import SettingsPage from './pages/SettingsPage';

type Page = 'control' | 'flow' | 'projects' | 'teams' | 'models' | 'memory' | 'settings';

const loadUi = (key: string, fallback: string) => {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
};
const saveUi = (key: string, value: string) => {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
};

const NAV: { id: Page; icon: string; label: string }[] = [
  { id: 'control', icon: '◉', label: 'Project Control' },
  { id: 'flow', icon: '⇄', label: 'Project Flow' },
  { id: 'projects', icon: '▤', label: 'Projects' },
  { id: 'teams', icon: '⧉', label: 'Teams' },
  { id: 'models', icon: '◈', label: 'Models' },
  { id: 'memory', icon: '✦', label: 'Agent Memory' },
  { id: 'settings', icon: '⚙', label: 'Settings' },
];

export default function App() {
  const [page, setPage] = React.useState<Page>(() => {
    const saved = loadUi('ao.page', 'control');
    return (NAV.some((n) => n.id === saved) ? saved : 'control') as Page;
  });
  const [theme, setTheme] = React.useState<'dark' | 'bright'>(() => {
    const saved = loadUi('ao.theme', 'dark');
    return saved === 'bright' ? 'bright' : 'dark';
  });
  const [activeProject, setActiveProject] = React.useState<string | null>(() => loadUi('ao.project', '') || null);
  const [collapsed, setCollapsed] = React.useState<boolean>(() => loadUi('ao.sidebar', '') === 'collapsed');
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

  React.useEffect(() => { saveUi('ao.page', page); }, [page]);
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
            onClick={() => setPage(n.id)}
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
        {page === 'control' && (
          <ControlPage activeProject={activeProject} setActiveProject={setActiveProject} />
        )}
        {page === 'flow' && (
          <ProjectFlowPage activeProject={activeProject} setActiveProject={setActiveProject} />
        )}
        {page === 'projects' && (
          <ProjectsPage activeProject={activeProject} setActiveProject={setActiveProject} onOpenControl={() => setPage('control')} />
        )}
        {page === 'teams' && <TeamsPage />}
        {page === 'models' && <ModelsPage />}
        {page === 'memory' && <MemoryPage />}
        {page === 'settings' && <SettingsPage />}
      </main>
    </div>
  );
}
