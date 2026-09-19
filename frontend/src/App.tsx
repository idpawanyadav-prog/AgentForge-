import React from 'react';
import { get, patch } from './api';
import ControlPage from './pages/ControlPage';
import ProjectsPage from './pages/ProjectsPage';
import TeamsPage from './pages/TeamsPage';
import MemoryPage from './pages/MemoryPage';
import SettingsPage from './pages/SettingsPage';

type Page = 'control' | 'projects' | 'teams' | 'memory' | 'settings';

const NAV: { id: Page; icon: string; label: string }[] = [
  { id: 'control', icon: '◉', label: 'Project Control' },
  { id: 'projects', icon: '▤', label: 'Projects' },
  { id: 'teams', icon: '⧉', label: 'Teams' },
  { id: 'memory', icon: '✦', label: 'Agent Memory' },
  { id: 'settings', icon: '⚙', label: 'Settings' },
];

export default function App() {
  const [page, setPage] = React.useState<Page>('control');
  const [theme, setTheme] = React.useState<'dark' | 'bright'>('dark');
  const [activeProject, setActiveProject] = React.useState<string | null>(null);

  React.useEffect(() => {
    get('/api/v1/settings')
      .then((s) => setTheme(s.theme === 'bright' ? 'bright' : 'dark'))
      .catch(() => undefined);
    get('/api/v1/projects')
      .then((ps: any[]) => {
        if (ps.length) setActiveProject((prev) => prev ?? ps[0].id);
      })
      .catch(() => undefined);
  }, []);

  React.useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  const toggleTheme = () => {
    const next = theme === 'dark' ? 'bright' : 'dark';
    setTheme(next);
    patch('/api/v1/settings', { key: 'theme', value: next }).catch(() => undefined);
  };

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">Agent<span>Office</span></div>
        {NAV.map((n) => (
          <button
            key={n.id}
            className={`nav-item ${page === n.id ? 'active' : ''}`}
            onClick={() => setPage(n.id)}
          >
            <span>{n.icon}</span> {n.label}
          </button>
        ))}
        <div className="sidebar-footer">
          <button className="theme-toggle" onClick={toggleTheme}>
            <span>{theme === 'dark' ? '🌙 Dark' : '☀️ Bright'}</span>
            <span className="small muted">switch</span>
          </button>
          <div style={{ marginTop: 12 }}>
            {activeProject ? 'Project session active' : 'No project selected'}
          </div>
        </div>
      </aside>
      <main className="main">
        {page === 'control' && (
          <ControlPage activeProject={activeProject} setActiveProject={setActiveProject} />
        )}
        {page === 'projects' && (
          <ProjectsPage activeProject={activeProject} setActiveProject={setActiveProject} onOpenControl={() => setPage('control')} />
        )}
        {page === 'teams' && <TeamsPage />}
        {page === 'memory' && <MemoryPage />}
        {page === 'settings' && <SettingsPage />}
      </main>
    </div>
  );
}
