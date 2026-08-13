'use client';

import { useEffect, useState } from 'react';
import { useTheme } from '@/components/ThemeContext';
import { useAuth } from '@/components/AuthContext';
import ProjectListPage from '@/components/ProjectListPage';
import ProjectWizard from '@/components/ProjectWizard';
import ProjectDetailPage from '@/components/ProjectDetailPage';
import LoginPage from '@/components/LoginPage';
import UserMenu from '@/components/UserMenu';
import { api, Voice } from '@/lib/api';

// hash 路由解析后的路由对象（移除 single/book，只留项目制 + 登录）
type Route =
  | { name: 'login' }
  | { name: 'projects-list' }
  | { name: 'projects-new' }
  | { name: 'projects-detail'; id: string };

// 解析当前 hash，返回 Route
function parseHash(): Route {
  if (typeof window === 'undefined') return { name: 'projects-list' };
  const h = window.location.hash.replace(/^#/, '');
  if (h === '/login') return { name: 'login' };
  if (h === '' || h === '/' || h === '/projects' || h === '/projects/') return { name: 'projects-list' };
  if (h === '/projects/new') return { name: 'projects-new' };
  // /projects/{id}（id 不能是 new）
  const m = h.match(/^\/projects\/([^/]+)$/);
  if (m && m[1] !== 'new') return { name: 'projects-detail', id: decodeURIComponent(m[1]) };
  // 其他未知 hash → 列表页（兼容旧 single/book 书签，不显示 404）
  return { name: 'projects-list' };
}

export default function HomePage() {
  const { theme, toggle } = useTheme();
  const { user, loading: authLoading } = useAuth();
  const [route, setRoute] = useState<Route>({ name: 'projects-list' });
  const [voices, setVoices] = useState<Voice[]>([]);

  // 监听 hashchange + 初始化同步
  useEffect(() => {
    setRoute(parseHash());
    const onHash = () => setRoute(parseHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // 登录态变化时，做必要的跳转
  useEffect(() => {
    if (authLoading) return;
    if (!user && route.name !== 'login') {
      // 未登录且当前不在登录页 → 跳登录
      window.location.hash = '/login';
    } else if (user && route.name === 'login') {
      // 已登录但停留在登录页 → 跳默认页（项目工作台）
      window.location.hash = '/projects';
    }
  }, [user, authLoading, route.name]);

  // 已登录才拉音色列表
  useEffect(() => {
    if (!user) {
      setVoices([]);
      return;
    }
    api.voices()
      .then(setVoices)
      .catch(e => console.error('voices 加载失败:', e));
  }, [user]);

  // 初始化 / 鉴权校验期间，显示 loading
  if (authLoading) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center text-white/40">
        <div className="text-center">
          <div className="inline-block w-8 h-8 border-2 border-white/20 border-t-brand-500 rounded-full animate-spin mb-3" />
          <div className="text-sm">加载中…</div>
        </div>
      </div>
    );
  }

  // 未登录只展示登录页
  if (!user) {
    return <LoginPage />;
  }

  return (
    <div className="mx-auto max-w-7xl px-4 sm:px-6 py-8">
      {/* header */}
      <header className="flex items-center justify-between mb-6 flex-wrap gap-3">
        <div>
          <div className="flex items-center gap-3">
            <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-brand-500 to-blue-500 shadow-lg shadow-brand-500/30 grid place-items-center font-bold text-lg">
              声
            </div>
            <div>
              <h1 className="text-xl sm:text-2xl font-bold">AI 有声小说生成器</h1>
              <p className="text-xs text-white/50 mt-0.5">
                项目工作台 · 导入小说 → AI 识别角色与对白 → 多音色合成 → 导出 MP3
              </p>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn-ghost" onClick={toggle} title="切换深色/浅色">
            {theme === 'dark' ? '🌙 深色' : '☀️ 浅色'}
          </button>
          <UserMenu />
        </div>
      </header>

      {/* 项目工作台（唯一入口） */}
      {route.name === 'projects-list' && <ProjectListPage />}
      {route.name === 'projects-new' && <ProjectWizard />}
      {route.name === 'projects-detail' && (
        <ProjectDetailPage projectId={route.id} voices={voices} />
      )}

      <footer className="mt-16 text-center text-xs text-white/40">
        ⚠ 仅供本地使用 · 所有 AI 调用都会产生 API 费用
      </footer>
    </div>
  );
}
