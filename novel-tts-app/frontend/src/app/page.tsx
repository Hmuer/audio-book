'use client';

import { useEffect, useState } from 'react';
import { useTheme } from '@/components/ThemeContext';
import { useAuth } from '@/components/AuthContext';
import AppSidebar from '@/components/AppSidebar';
import ProjectListPage from '@/components/ProjectListPage';
import ProjectDetailPage from '@/components/ProjectDetailPage';
import SettingsPage from '@/components/SettingsPage';
import LoginPage from '@/components/LoginPage';
import { api, Voice } from '@/lib/api';

// ---- 新路由结构 ----
type Route =
  | { name: 'login' }
  | { name: 'ab-list' }                  // #/audiobooks         列表
  | { name: 'ab-detail'; id: string }    // #/audiobooks/:id     工作台
  | { name: 'ab-voices' }                // #/audiobooks/voices  音色库
  | { name: 'settings' }                 // #/settings           设置
  | { name: 'unknown' };                 // fallback

// 旧路由 → 新路由重定向表
const LEGACY_REDIRECTS: Record<string, string> = {
  '/projects':     '/audiobooks',
  '/projects/':    '/audiobooks',
  '/projects/new': '/audiobooks',
};

function parseHash(): { route: Route; path: string } {
  if (typeof window === 'undefined') return { route: { name: 'ab-list' }, path: '/audiobooks' };
  let h = window.location.hash.replace(/^#/, '');

  // 旧路由重定向
  if (LEGACY_REDIRECTS[h]) {
    window.location.hash = LEGACY_REDIRECTS[h];
    // 先按旧 hash 解析，让下一轮渲染用新值
    return { route: { name: 'ab-list' }, path: LEGACY_REDIRECTS[h] };
  }
  const mLegacyDetail = h.match(/^\/projects\/([^/]+)$/);
  if (mLegacyDetail && mLegacyDetail[1] !== 'new') {
    window.location.hash = `/audiobooks/${mLegacyDetail[1]}`;
    return { route: { name: 'ab-detail', id: mLegacyDetail[1] }, path: `/audiobooks/${mLegacyDetail[1]}` };
  }

  // 登录
  if (h === '/login') return { route: { name: 'login' }, path: '/login' };
  // 一级路由
  if (h === '' || h === '/' || h === '/audiobooks' || h === '/audiobooks/')
    return { route: { name: 'ab-list' }, path: '/audiobooks' };
  if (h === '/audiobooks/voices') return { route: { name: 'ab-voices' }, path: '/audiobooks/voices' };
  if (h === '/settings')          return { route: { name: 'settings' }, path: '/settings' };
  // 详情
  const m = h.match(/^\/audiobooks\/([^/]+)$/);
  if (m) return { route: { name: 'ab-detail', id: decodeURIComponent(m[1]) }, path: h };

  return { route: { name: 'unknown' }, path: h };
}

export default function HomePage() {
  const { theme, toggle } = useTheme();
  const { user, loading: authLoading } = useAuth();
  const [routeInfo, setRouteInfo] = useState(() => parseHash());
  const [voices, setVoices] = useState<Voice[]>([]);

  // hash 监听
  useEffect(() => {
    const sync = () => setRouteInfo(parseHash());
    sync();
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }, []);

  // 登录态变化跳转
  useEffect(() => {
    if (authLoading) return;
    if (!user && routeInfo.route.name !== 'login') {
      window.location.hash = '/login';
    } else if (user && routeInfo.route.name === 'login') {
      window.location.hash = '/audiobooks';
    }
  }, [user, authLoading, routeInfo.route.name]);

  // 拉音色
  useEffect(() => {
    if (!user) { setVoices([]); return; }
    api.voices().then(setVoices).catch(e => console.error('voices 加载失败:', e));
  }, [user]);

  if (authLoading) {
    return (
      <div className="min-h-[60vh] grid place-items-center text-white/40">
        <div className="text-center">
          <div className="inline-block w-8 h-8 border-2 border-white/20 border-t-brand-500 rounded-full animate-spin mb-3" />
          <div className="text-sm">加载中…</div>
        </div>
      </div>
    );
  }

  if (!user) return <LoginPage />;

  const R = routeInfo.route;

  return (
    <div className="flex min-h-screen">
      {/* 左侧全局侧栏 */}
      <AppSidebar currentPath={routeInfo.path} />

      {/* 右侧主内容 */}
      <div className="flex-1 min-w-0 flex flex-col">
        {/* 顶部小条：主题切换（品牌名已在侧栏） */}
        <header className="h-14 border-b border-white/[0.06] px-6 flex items-center justify-between shrink-0">
          <div className="text-xs text-white/40">
            {R.name === 'ab-list' && 'AI有声书 · 我的有声书'}
            {R.name === 'ab-detail' && 'AI有声书 · 工作台'}
            {R.name === 'ab-voices' && 'AI有声书 · 音色库'}
            {R.name === 'settings' && '设置'}
            {R.name === 'unknown' && ''}
          </div>
          <button className="btn-ghost h-8 text-xs" onClick={toggle}>
            {theme === 'dark' ? '🌙 深色' : '☀️ 浅色'}
          </button>
        </header>

        {/* 主内容 */}
        <main className="flex-1 min-w-0">
          {R.name === 'ab-list' && <ProjectListPage />}
          {R.name === 'ab-detail' && <ProjectDetailPage projectId={R.id} voices={voices} />}
          {R.name === 'ab-voices' && <PlaceholderPage title="音色库" desc="音色库功能即将上线" icon="🎙️" />}
          {R.name === 'settings' && <SettingsPage />}
          {R.name === 'unknown' && (
            <PlaceholderPage title="页面不存在" desc="该路由暂未实现" icon="🤔" actionHref="#/audiobooks" actionLabel="返回有声书列表" />
          )}
        </main>
      </div>
    </div>
  );
}

// ---- 占位页（音色库 / 设置）----
function PlaceholderPage({
  title, desc, icon, actionHref, actionLabel,
}: { title: string; desc: string; icon: string; actionHref?: string; actionLabel?: string }) {
  return (
    <div className="h-full grid place-items-center py-20">
      <div className="text-center">
        <div className="text-5xl mb-4">{icon}</div>
        <h2 className="text-xl font-semibold mb-1">{title}</h2>
        <p className="text-sm text-white/50 mb-6">{desc}</p>
        {actionHref && actionLabel && (
          <a href={actionHref} className="btn-primary inline-flex justify-center">
            {actionLabel}
          </a>
        )}
      </div>
    </div>
  );
}
