'use client';

import { createContext, useContext, useEffect, useState, ReactNode } from 'react';

type Theme = 'dark' | 'light';

interface ThemeCtx {
  theme: Theme;
  toggle: () => void;
}

const Ctx = createContext<ThemeCtx>({ theme: 'dark', toggle: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>('dark');

  // 用 DOM 互斥 class 切换：确保 html 上同一时刻只有 dark 或 light 一个 class。
  // 之前只 toggle('light') 从不清理 'dark'，导致 html.dark.light 同存，
  // Tailwind darkMode:'class' 会误触发 .dark 前缀规则，颜色计算出现 fallback 杂色。
  const applyTheme = (t: Theme) => {
    const root = document.documentElement;
    root.classList.remove('dark', 'light');
    root.classList.add(t);
    try {
      // 同步设置 color-scheme 属性（给浏览器表单控件/滚动条/selection 参考）
      root.style.colorScheme = t === 'light' ? 'only light' : 'dark';
    } catch (_) {}
  };

  useEffect(() => {
    const saved = (typeof window !== 'undefined' && (localStorage.getItem('theme') as Theme)) || 'dark';
    setTheme(saved);
    applyTheme(saved);
  }, []);

  const toggle = () => {
    setTheme(prev => {
      const next = prev === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('theme', next); } catch (_) {}
      applyTheme(next);
      return next;
    });
  };

  return <Ctx.Provider value={{ theme, toggle }}>{children}</Ctx.Provider>;
}

export const useTheme = () => useContext(Ctx);
