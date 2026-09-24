import type { Dispatch, SetStateAction } from 'react';
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

type Theme = 'dark' | 'bright';
type Setter<T> = Dispatch<SetStateAction<T>>;

interface AppState {
  dataRevision: number;
  notifyMutation: () => void;
  activeProject: string | null;
  theme: Theme;
  collapsed: boolean;
  setActiveProject: Setter<string | null>;
  setTheme: Setter<Theme>;
  setCollapsed: Setter<boolean>;
}

const saved = (key: string, fallback: string) => {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
};

export const useAppStore = create<AppState>()(persist((set) => ({
  dataRevision: 0,
  notifyMutation: () => set((state) => ({ dataRevision: state.dataRevision + 1 })),
  activeProject: saved('ao.project', '') || null,
  theme: saved('ao.theme', 'dark') === 'bright' ? 'bright' : 'dark',
  collapsed: saved('ao.sidebar', '') === 'collapsed',
  setActiveProject: (value) => set((state) => ({
    activeProject: typeof value === 'function' ? value(state.activeProject) : value,
  })),
  setTheme: (value) => set((state) => ({
    theme: typeof value === 'function' ? value(state.theme) : value,
  })),
  setCollapsed: (value) => set((state) => ({
    collapsed: typeof value === 'function' ? value(state.collapsed) : value,
  })),
}), { name: 'ao.app', partialize: (state) => ({
  activeProject: state.activeProject, theme: state.theme, collapsed: state.collapsed,
}) as AppState }));
