'use client';

import { createContext, useContext, ReactNode } from 'react';

/**
 * Edtech Dark 主题：全站强制深色，不提供切换。
 * 保留 Provider 结构以避免破坏 @/components/* 和 layout.tsx 的既有 import。
 * toggle() 为 no-op，theme 永远返回 'dark'。
 */
type Theme = 'dark';

interface ThemeCtx {
  theme: Theme;
  toggle: () => void;
}

const Ctx = createContext<ThemeCtx>({ theme: 'dark', toggle: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  // 保证 <html> 上 class=dark / color-scheme=dark 永远生效（SSR 后也强设一次）
  if (typeof document !== 'undefined') {
    const root = document.documentElement;
    root.classList.remove('light');
    if (!root.classList.contains('dark')) root.classList.add('dark');
    root.style.colorScheme = 'dark';
  }
  return <Ctx.Provider value={{ theme: 'dark', toggle: () => {} }}>{children}</Ctx.Provider>;
}

export const useTheme = () => useContext(Ctx);
