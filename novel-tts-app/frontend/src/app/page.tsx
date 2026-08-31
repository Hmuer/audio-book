'use client';

import { useEffect, useState } from 'react';
import { useAuth } from '@/components/AuthContext';
import AppSidebar from '@/components/AppSidebar';
import ProjectListPage from '@/components/ProjectListPage';
import ProjectDetailPage from '@/components/ProjectDetailPage';
import SettingsPage from '@/components/SettingsPage';
import LoginPage from '@/components/LoginPage';
import { api, errToLog, Voice, ProjectListItem } from '@/lib/api';

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
  const { user, loading: authLoading } = useAuth();
  const [routeInfo, setRouteInfo] = useState(() => parseHash());
  const [voices, setVoices] = useState<Voice[]>([]);
  const [projects, setProjects] = useState<ProjectListItem[]>([]);

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
    if (!user) { setVoices([]); setProjects([]); return; }
    api.voices().then(setVoices).catch(e => console.error('voices 加载失败:', errToLog(e)));
    api.projectList().then(setProjects).catch(e => console.error('projects(header) 加载失败:', errToLog(e)));
  }, [user]);

  // 自定义事件：ProjectListPage 刷新项目时，通知 header 同步
  useEffect(() => {
    const onRefresh = (e: Event) => {
      const ce = e as CustomEvent<ProjectListItem[]>;
      if (ce.detail && Array.isArray(ce.detail)) setProjects(ce.detail);
      else api.projectList().then(setProjects).catch(() => {});
    };
    window.addEventListener('app:projects-refreshed', onRefresh as EventListener);
    return () => window.removeEventListener('app:projects-refreshed', onRefresh as EventListener);
  }, []);

  if (authLoading) {
    return (
      <div className="min-h-[60vh] grid place-items-center text-ink-500">
        <div className="text-center">
          <div className="inline-block w-8 h-8 border-2 border-ink-400/70 border-t-brand-500 rounded-full animate-spin mb-3" />
          <div className="text-sm">加载中…</div>
        </div>
      </div>
    );
  }

  if (!user) return <LoginPage />;

  const R = routeInfo.route;

  // 顶栏状态胶囊（Edtech：实时资源计数 · 收敛字距）
  const statusCapsules = (() => {
    if (R.name === 'ab-list' || R.name === 'ab-detail') {
      const total = projects.length;
      const running = projects.filter(p => p.status === 'processing' || p.status === 'pending').length;
      const done    = projects.filter(p => p.status === 'ready').length;
      return [
        { label: '项目',   value: String(total).padStart(2, '0'), tone: 'brand' as const },
        ...(total > 0 ? [
          { label: '进行中', value: String(running).padStart(2, '0'), tone: 'cold' as const },
          { label: '已完成', value: String(done).padStart(2, '0'),    tone: 'ok' as const },
        ] : []),
      ];
    }
    if (R.name === 'ab-voices') {
      return [
        { label: '音色', value: String(voices.length).padStart(2, '0'), tone: 'brand' as const },
        { label: '预设', value: '16', tone: 'cold' as const },
      ];
    }
    return [
      { label: '版本', value: 'v0.1', tone: 'cold' as const },
    ];
  })();

  return (
    <div className="flex min-h-screen">
      {/* 左侧全局侧栏 */}
      <AppSidebar currentPath={routeInfo.path} />

      {/* 右侧主内容 */}
      <div className="flex-1 min-w-0 flex flex-col">
        {/* 顶部状态条：面包屑 + 状态胶囊（Edtech 参考） */}
        <header className="h-12 shrink-0 border-b border-ink-300/70 px-5 lg:px-7 flex items-center justify-between bg-ink-50">
          <div className="flex items-center gap-3 min-w-0">
            {/* 面包屑（收敛字距） */}
            <div className="text-[11px] font-semibold text-ink-500">
              {R.name === 'ab-list' && '有声书 · 我的作品库'}
              {R.name === 'ab-detail' && '有声书 · 工作台'}
              {R.name === 'ab-voices' && '有声书 · 音色库'}
              {R.name === 'settings' && '系统 · 设置'}
            </div>
            {/* 状态胶囊组 */}
            <div className="hidden sm:flex items-center gap-1.5 pl-3 ml-1 border-l border-ink-300/60">
              {statusCapsules.map((c, i) => (
                <StatusCapsule key={i} {...c} />
              ))}
            </div>
          </div>
        </header>

        {/* 主内容 —— 统一右侧、底部边距：画布画布 */}
        <main
          className="flex-1 min-w-0 p-5 lg:p-7 pb-14 lg:pb-20 animate-fade-in"
          key={routeInfo.path}
        >
          {R.name === 'ab-list' && <ProjectListPage />}
          {R.name === 'ab-detail' && <ProjectDetailPage projectId={R.id} voices={voices} />}
          {R.name === 'ab-voices' && <PlaceholderPage title="音色库" desc="音色库功能即将上线" iconName="mic" />}
          {R.name === 'settings' && <SettingsPage />}
          {R.name === 'unknown' && (
            <PlaceholderPage title="页面不存在" desc="该路由暂未实现" iconName="unknown" actionHref="#/audiobooks" actionLabel="返回有声书列表" />
          )}
        </main>
      </div>
    </div>
  );
}

// ---- 状态胶囊（Edtech Header KPI · 收敛字距 · 冷灰系）----
function StatusCapsule({
  label, value, tone,
}: {
  label: string;
  value: string;
  tone: 'brand' | 'amber' | 'cold' | 'ok';
}) {
  const palette = {
    brand: { fg: 'rgb(var(--brand-400))',  bg: 'rgb(var(--ink-200))',       border: 'rgb(var(--ink-300))' },
    amber: { fg: 'rgb(var(--accent-amber))',bg: 'rgb(var(--ink-200))',      border: 'rgb(var(--ink-300))' },
    cold:  { fg: 'rgb(var(--ink-600))',    bg: 'rgb(var(--ink-200))',       border: 'rgb(var(--ink-300))' },
    ok:    { fg: 'rgb(var(--status-success-fg))', bg: 'rgb(var(--ink-200))', border: 'rgb(var(--ink-300))' },
  }[tone];
  return (
    <div
      className="inline-flex items-center gap-2 h-6 px-2.5 border"
      style={{
        borderRadius: 'var(--radius-xs)',
        background: palette.bg,
        borderColor: palette.border,
      }}
    >
      <span className="text-[11px] font-medium" style={{ color: palette.fg }}>
        {label}
      </span>
      <span
        className="text-[12px] font-semibold tabular-nums text-ink-700"
      >{value}</span>
    </div>
  );
}

// ---- 占位页（音色库 / 设置）----
function PlaceholderPage({
  title, desc, iconName, actionHref, actionLabel,
}: { title: string; desc: string; iconName: 'mic' | 'unknown'; actionHref?: string; actionLabel?: string }) {
  const common = {
    width: 44, height: 44, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'rgb(var(--ink-500))', strokeWidth: 1.6,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  };
  return (
    <div className="h-full grid place-items-center py-20">
      <div className="text-center">
        <div className="mx-auto w-16 h-16 grid place-items-center mb-4" style={{
          background: 'rgb(var(--ink-100))',
          borderRadius: 'var(--radius-md)',
          border: '1px solid rgb(var(--ink-300))',
        }}>
          {iconName === 'mic'
            ? <svg {...common}><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0014 0"/><path d="M12 18v3"/></svg>
            : <svg {...common}><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          }
        </div>
        <h2 className="text-xl font-semibold mb-1 text-white">{title}</h2>
        <p className="text-sm text-ink-500 mb-6">{desc}</p>
        {actionHref && actionLabel && (
          <a href={actionHref} className="btn-primary inline-flex justify-center">
            {actionLabel}
          </a>
        )}
      </div>
    </div>
  );
}
