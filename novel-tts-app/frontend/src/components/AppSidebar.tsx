'use client';

import { useState } from 'react';
import { useAuth } from '@/components/AuthContext';
import { ChangePasswordModal } from './UserMenu';
import { formatDate } from '@/lib/time';

// ================= 类型 =================
type MenuItem = {
  key: string;
  label: string;
  icon: string;   // emoji/SVG char 直接用
  href: string;
  disabled?: boolean;
  badge?: number | string;
};

type Module = {
  key: string;
  label: string;
  icon: string;
  href?: string;
  children?: MenuItem[];
  disabled?: boolean;
};

// ================= 菜单数据 =================
const AUDIOBOOKS_CHILDREN: MenuItem[] = [
  { key: 'list',   label: '我的有声书', href: '#/audiobooks',        icon: '📖' },
  { key: 'voices', label: '音色库',     href: '#/audiobooks/voices', icon: '🎙️' },
];

const MODULES: Module[] = [
  { key: 'audiobooks', label: 'AI有声书',   icon: '🔊', children: AUDIOBOOKS_CHILDREN },
  { key: 'novel',     label: 'AI小说创作', icon: '✍️', disabled: true },
  { key: 'drama',     label: 'AI短剧',     icon: '🎬', disabled: true },
  { key: 'comic',     label: 'AI漫画',     icon: '🎨', disabled: true },
];

const SETTINGS_ITEM: MenuItem = {
  key: 'settings', label: '设置', icon: '⚙️', href: '#/settings',
};

// 顶部「工作区」section label
const SECTION_LABEL_WORKSPACE = '工作区';
const SECTION_LABEL_SYSTEM = '系统';

interface Props {
  currentPath: string;
}

// ================= 组件 =================
export default function AppSidebar({ currentPath }: Props) {
  const { user, logout } = useAuth();
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [pwdModal, setPwdModal] = useState(false);

  // 一级菜单折叠状态：有子菜单的模块默认展开当前激活的，其余折叠
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => {
    // 初始化：展开当前路由匹配到的模块
    const keys = new Set<string>();
    for (const m of MODULES) {
      if (m.children?.some(c => {
        const p = c.href.replace(/^#/, '');
        if (typeof window === 'undefined') return false;
        const current = window.location.hash.replace(/^#/, '');
        return current === p || current.startsWith(p + '/');
      })) {
        keys.add(m.key);
      }
    }
    return keys;
  });

  const toggleExpand = (key: string) => {
    setExpandedKeys(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  if (!user) return null;

  const isPathActive = (href: string) => {
    const path = href.replace(/^#/, '');
    if (path === currentPath) return true;
    if (currentPath.startsWith(path + '/')) return true;
    return false;
  };
  const isModuleActive = (m: Module) => {
    if (m.href && isPathActive(m.href)) return true;
    if (m.children?.some(c => isPathActive(c.href))) return true;
    return false;
  };

  return (
    <>
      <aside
        className="hidden md:flex flex-col w-[244px] shrink-0 h-screen sticky top-0 border-r border-white/[0.06]"
        style={{
          background:
            'linear-gradient(180deg, rgba(22,20,32,0.95) 0%, rgba(15,14,22,0.98) 100%)',
        }}
      >
        {/* ============ 品牌区 ============ */}
        <div className="px-5 pt-6 pb-4">
          <div className="flex items-center gap-3">
            <BrandLogo />
            <div className="min-w-0">
              <div className="text-[17px] font-semibold text-white tracking-tight leading-none">阿布</div>
              <div className="mt-1 flex items-center gap-1.5">
                <span
                  className="inline-block w-1.5 h-1.5 rounded-full animate-pulse-soft"
                  style={{ background: 'linear-gradient(135deg, #a78bfa, #22d3ee)' }}
                />
                <span className="text-[10.5px] text-white/40 tracking-[0.04em] uppercase">
                  ABU · Creator Studio
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* ============ 菜单主体 ============ */}
        <nav className="flex-1 px-3 pb-3 overflow-y-auto space-y-6">
          {/* ---- 工作区 Section ---- */}
          <div className="space-y-1">
            <SectionLabel label={SECTION_LABEL_WORKSPACE} />
            {MODULES.map((m) => (
              <ModuleNav
                key={m.key}
                m={m}
                moduleActive={isModuleActive(m)}
                pathActive={isPathActive}
                expanded={expandedKeys.has(m.key)}
                onToggleExpand={() => toggleExpand(m.key)}
              />
            ))}
          </div>

          {/* ---- 系统 Section ---- */}
          <div className="space-y-1">
            <SectionLabel label={SECTION_LABEL_SYSTEM} />
            <TopLevelLink
              item={SETTINGS_ITEM}
              active={isPathActive(SETTINGS_ITEM.href)}
            />
          </div>
        </nav>

        {/* ============ 底部用户菜单 ============ */}
        <div className="border-t border-white/[0.05] px-3 py-3">
          <div className="relative">
            <button
              onClick={() => setUserMenuOpen((o) => !o)}
              className="w-full flex items-center gap-3 px-2.5 py-2
                hover:bg-white/[0.04] transition-all group"
            >
              <Avatar username={user.username} />
              <div className="min-w-0 flex-1 text-left">
                <div className="text-[13.5px] font-medium text-white truncate">
                  {user.username}
                </div>
                <div className="text-[11px] text-white/38 truncate">
                  {user.created_at
                    ? `加入于 ${formatDate(user.created_at)}`
                    : ''}
                </div>
              </div>
              <svg
                width="14" height="14" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="2.2"
                strokeLinecap="round" strokeLinejoin="round"
                className={`text-white/40 transition-transform duration-200 ${userMenuOpen ? 'rotate-180' : ''}`}
              >
                <polyline points="6 9 12 15 18 9" />
              </svg>
            </button>

            {userMenuOpen && (
              <div
                className="absolute left-0 right-0 bottom-[calc(100%+6px)] z-40
                  border border-white/[0.09] bg-[#14121f]/98 backdrop-blur-md
                  shadow-[0_16px_48px_-12px_rgba(0,0,0,0.75)] p-1.5 w-auto animate-fade-in"
                style={{ borderRadius: 'var(--radius-md)' }}
              >
                <MenuItemButton
                  label="修改密码"
                  icon="🔐"
                  onClick={() => { setPwdModal(true); setUserMenuOpen(false); }}
                />
                <MenuItemButton
                  label="退出登录"
                  icon="↩"
                  danger
                  onClick={() => { logout(); setUserMenuOpen(false); }}
                />
              </div>
            )}
          </div>
        </div>
      </aside>

      {pwdModal && <ChangePasswordModal onClose={() => setPwdModal(false)} />}
    </>
  );
}

// ================= 子组件 =================
function SectionLabel({ label }: { label: string }) {
  return (
    <div className="px-2.5 pt-1 pb-1.5 flex items-center gap-2 select-none">
      <span
        className="inline-block h-px w-4 rounded-full"
        style={{ background: 'linear-gradient(90deg, rgba(167,139,250,0.7), rgba(34,211,238,0.0))' }}
      />
      <span className="text-[10.5px] uppercase tracking-[0.14em] text-white/32 font-semibold">
        {label}
      </span>
    </div>
  );
}

function ModuleNav({
  m, moduleActive, pathActive, expanded, onToggleExpand,
}: {
  m: Module;
  moduleActive: boolean;
  pathActive: (href: string) => boolean;
  expanded: boolean;
  onToggleExpand: () => void;
}) {
  const hasChildren = !!m.children && !m.disabled;

  const headerClass = [
    'group relative flex items-center gap-2.5 px-3 h-[38px] text-[14px] font-medium transition-all',
    m.disabled
      ? 'text-white/28 cursor-not-allowed'
      : moduleActive
        ? 'text-white'
        : 'text-white/68 hover:text-white hover:bg-white/[0.04]',
  ].join(' ');
  const headerStyle =
    !m.disabled && moduleActive
      ? {
          background:
            'linear-gradient(135deg, rgba(139,92,246,0.16) 0%, rgba(139,92,246,0.04) 70%), rgba(255,255,255,0.02)',
          boxShadow: 'inset 0 0 0 1px rgba(139,92,246,0.18)',
        }
      : undefined;

  const headerInner = (
    <>
      {/* 激活时左侧渐变条 */}
      {!m.disabled && moduleActive && (
        <span
          className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 rounded-r-full"
          style={{ background: 'linear-gradient(180deg, #c4b5fd 0%, #7c3aed 100%)' }}
        />
      )}
      <IconCell icon={m.icon} active={!m.disabled && moduleActive} disabled={m.disabled} />
      <span className="flex-1">{m.label}</span>
      {m.disabled && <ComingSoonBadge />}
      {hasChildren && (
        <Chevron
          open={expanded}
          className={!m.disabled && moduleActive ? 'text-brand-300' : 'text-white/30'}
        />
      )}
    </>
  );

  // 一级菜单头：有子菜单时用 button 切换展开；有 href 时用链接；禁用则静态展示
  const headerEl = hasChildren ? (
    <button
      type="button"
      onClick={onToggleExpand}
      aria-expanded={expanded}
      className={headerClass + ' w-full text-left'}
      style={headerStyle}
    >
      {headerInner}
    </button>
  ) : m.href && !m.disabled ? (
    <a href={m.href} className={headerClass} style={headerStyle}>
      {headerInner}
    </a>
  ) : (
    <div className={headerClass} style={headerStyle}>
      {headerInner}
    </div>
  );

  return (
    <div>
      {headerEl}

      {/* 二级菜单：基于 expanded 折叠/展开，带过渡动画 */}
      {hasChildren && (
        <div
          className="overflow-hidden transition-all duration-200 ease-out"
          style={{
            maxHeight: expanded ? 240 : 0,
            opacity: expanded ? 1 : 0,
            marginTop: expanded ? 4 : 0,
          }}
        >
          <div
            className="ml-2 px-1.5 py-1.5 bg-white/[0.025] border border-white/[0.05] space-y-0.5"
            style={{ borderRadius: 'var(--radius-sm)' }}
          >
            {m.children!.map((c) => {
              const active = pathActive(c.href);
              return (
                <a
                  key={c.key}
                  href={c.href}
                  className={[
                    'relative flex items-center gap-2 px-2.5 h-[34px] text-[13px] transition-all',
                    active
                      ? 'text-white'
                      : 'text-white/58 hover:text-white hover:bg-white/[0.04]',
                  ].join(' ')}
                  style={
                    active
                      ? {
                          background:
                            'linear-gradient(90deg, rgba(139,92,246,0.22) 0%, rgba(139,92,246,0.06) 100%)',
                          boxShadow: 'inset 0 0 0 1px rgba(139,92,246,0.18)',
                        }
                      : undefined
                  }
                >
                  {active && (
                    <span
                      className="absolute left-1 top-1/2 -translate-y-1/2 w-[2.5px] h-4 rounded-r-full"
                      style={{ background: 'linear-gradient(180deg,#c4b5fd,#8b5cf6)' }}
                    />
                  )}
                  <span
                    className={`w-6 h-6 shrink-0 rounded-md grid place-items-center text-[12px]
                      ${active ? 'bg-brand-500/20 text-brand-200' : 'bg-white/[0.03] text-white/65'}`}
                  >
                    {c.icon}
                  </span>
                  <span className="flex-1">{c.label}</span>
                  {c.badge != null && typeof c.badge !== 'undefined' && (
                    <span className="text-[10px] text-white/35 tabular-nums">{c.badge}</span>
                  )}
                </a>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function TopLevelLink({ item, active }: { item: MenuItem; active: boolean }) {
  return (
    <a
      href={item.href}
      className={[
        'group relative flex items-center gap-2.5 px-3 h-[38px] text-[14px] font-medium transition-all',
        active
          ? 'text-white'
          : 'text-white/68 hover:text-white hover:bg-white/[0.04]',
      ].join(' ')}
      style={
        active
          ? {
              background:
                'linear-gradient(135deg, rgba(139,92,246,0.16) 0%, rgba(139,92,246,0.04) 70%), rgba(255,255,255,0.02)',
              boxShadow: 'inset 0 0 0 1px rgba(139,92,246,0.18)',
            }
          : undefined
      }
    >
      {active && (
        <span
          className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 rounded-r-full"
          style={{ background: 'linear-gradient(180deg, #c4b5fd 0%, #7c3aed 100%)' }}
        />
      )}
      <IconCell icon={item.icon} active={active} />
      <span className="flex-1">{item.label}</span>
    </a>
  );
}

function IconCell({
  icon, active, disabled,
}: { icon: string; active: boolean; disabled?: boolean }) {
  return (
    <div
      className={`w-7 h-7 shrink-0 grid place-items-center text-[14px] transition-all
        ${active ? 'bg-brand-500/25 text-brand-100'
          : disabled ? 'bg-white/[0.02] text-white/35'
          : 'bg-white/[0.03] text-white/80 group-hover:bg-white/[0.05]'}`}
      style={{ borderRadius: 'var(--radius-sm)', ...(active ? { boxShadow: 'inset 0 0 0 1px rgba(167,139,250,0.25)' } : {}) }}
    >
      {icon}
    </div>
  );
}

function Chevron({
  open, className = '',
}: { open: boolean; className?: string }) {
  return (
    <svg
      width="14" height="14" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
      className={`transition-transform duration-200 ${open ? 'rotate-90' : ''} ${className}`}
    >
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}

function ComingSoonBadge() {
  return (
    <span
      className="text-[9.5px] px-1.5 py-0.5 font-medium tracking-wide"
      style={{
        borderRadius: 'var(--radius-xs)',
        background: 'rgba(255,255,255,0.04)',
        color: 'rgba(255,255,255,0.38)',
        border: '1px solid rgba(255,255,255,0.06)',
      }}
    >
      SOON
    </span>
  );
}

function Avatar({ username }: { username: string }) {
  const initial = (username || 'A').slice(0, 1).toUpperCase();
  return (
    <div
      className="w-9 h-9 grid place-items-center shrink-0 text-[13px] font-bold text-white"
      style={{
        borderRadius: 'var(--radius-sm)',
        backgroundImage:
          'linear-gradient(180deg, rgba(255,255,255,0.14) 0%, rgba(255,255,255,0) 45%), linear-gradient(135deg, #8b5cf6 0%, #6366f1 100%)',
        boxShadow: '0 0 0 1px rgba(167,139,250,0.35), 0 8px 16px -8px rgba(99,102,241,0.5)',
      }}
    >
      {initial}
    </div>
  );
}

function BrandLogo() {
  return (
    <div
      className="w-10 h-10 grid place-items-center shrink-0 relative"
      style={{
        borderRadius: 'var(--radius-md)',
        backgroundImage:
          'linear-gradient(180deg, rgba(255,255,255,0.18) 0%, rgba(255,255,255,0) 50%), linear-gradient(135deg, #8b5cf6 0%, #6366f1 60%, #22d3ee 100%)',
        boxShadow: '0 0 0 1px rgba(167,139,250,0.35), 0 12px 24px -10px rgba(139,92,246,0.6)',
      }}
    >
      {/* 波形 logo */}
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" className="text-white">
        <g stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" fill="currentColor">
          <rect x="4"  y="14" width="2.2" height="6"  rx="1.1" />
          <rect x="8"  y="10" width="2.2" height="10" rx="1.1" />
          <rect x="12" y="5"  width="2.2" height="15" rx="1.1" opacity="0.95" />
          <rect x="16" y="9"  width="2.2" height="11" rx="1.1" />
          <rect x="20" y="13" width="2.2" height="7"  rx="1.1" />
        </g>
      </svg>
    </div>
  );
}

function MenuItemButton({
  label, icon, danger, onClick,
}: {
  label: string;
  icon: string;
  danger?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full flex items-center gap-2.5 px-3 h-10 text-[13.5px] transition-all
        ${danger
          ? 'text-rose-200/90 hover:bg-rose-500/10 hover:text-rose-100'
          : 'text-white/80 hover:bg-white/[0.06] hover:text-white'}`}
    >
      <span className="w-6 h-6 grid place-items-center text-[13px] bg-white/[0.04]" style={{ borderRadius: 'var(--radius-xs)' }}>{icon}</span>
      <span className="flex-1 text-left">{label}</span>
    </button>
  );
}
