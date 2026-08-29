'use client';

import { useState } from 'react';
import { useAuth } from '@/components/AuthContext';
import { ChangePasswordModal } from './UserMenu';
import { formatDate } from '@/lib/time';

// ================= 类型 =================
type MenuItem = {
  key: string;
  label: string;
  icon: string;   // SVG / 内联图标名，不再使用 emoji
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
  { key: 'list',   label: '我的有声书', href: '#/audiobooks',        icon: 'book' },
  { key: 'voices', label: '音色库',     href: '#/audiobooks/voices', icon: 'mic' },
];

const MODULES: Module[] = [
  { key: 'audiobooks', label: 'AI有声书',   icon: 'speaker', children: AUDIOBOOKS_CHILDREN },
  { key: 'novel',     label: 'AI小说创作', icon: 'pen',     disabled: true },
  { key: 'drama',     label: 'AI短剧',     icon: 'clapper', disabled: true },
  { key: 'comic',     label: 'AI漫画',     icon: 'palette', disabled: true },
];

const SETTINGS_ITEM: MenuItem = {
  key: 'settings', label: '设置', icon: 'cog', href: '#/settings',
};

// Edtech 工具化：分区标题使用中文简洁标签
const SECTION_LABEL_WORKSPACE = '创作中心';
const SECTION_LABEL_SYSTEM    = '系统';

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
        className="hidden md:flex flex-col w-[244px] shrink-0 h-screen sticky top-0 border-r border-ink-300/70"
        style={{
          background: 'rgb(var(--ink-50))',
        }}
      >
        {/* ============ 品牌区 ============ */}
        <div className="px-5 pt-6 pb-4">
          <div className="flex items-center gap-3">
            <BrandLogo />
            <div className="min-w-0">
              <div className="text-[15px] font-semibold text-white tracking-tight leading-none">阿布</div>
              <div className="mt-1 text-[11px] text-white/35 leading-none">AI 内容创作平台</div>
            </div>
          </div>
        </div>

        {/* ============ 菜单主体 ============ */}
        <nav className="flex-1 px-3 pb-4 overflow-y-auto space-y-7">
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

          <div className="space-y-1">
            <SectionLabel label={SECTION_LABEL_SYSTEM} />
            <TopLevelLink
              item={SETTINGS_ITEM}
              active={isPathActive(SETTINGS_ITEM.href)}
            />
          </div>
        </nav>

        {/* ============ 底部用户菜单 ============ */}
        <div className="border-t border-ink-300/70 px-3 py-3">
          <div className="relative">
            <button
              onClick={() => setUserMenuOpen((o) => !o)}
              className="w-full flex items-center gap-3 px-2.5 py-2
                hover:bg-white/[0.04] transition-all group"
              style={{ borderRadius: 'var(--radius-sm)' }}
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
                  border border-ink-300/70 bg-ink-50 backdrop-blur-md
                  shadow-[0_16px_48px_-12px_rgba(0,0,0,0.75)] p-1.5 w-auto animate-fade-in"
                style={{ borderRadius: 'var(--radius-md)' }}
              >
                <MenuItemButton
                  label="修改密码"
                  icon="lock"
                  onClick={() => { setPwdModal(true); setUserMenuOpen(false); }}
                />
                <MenuItemButton
                  label="退出登录"
                  icon="logout"
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
  // Edtech：简单中文分区标题 · 不做 uppercase / tracking 装饰
  return (
    <div className="px-3 pt-1 pb-2 flex items-center select-none">
      <span className="text-[11px] text-ink-700/45 font-medium">{label}</span>
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
    'group relative flex items-center gap-2.5 px-3 h-[40px] text-[14px] font-medium transition-all',
    m.disabled
      ? 'text-white/28 cursor-not-allowed'
      : moduleActive
        ? 'text-white bg-brand-600 shadow-[0_0_0_1px_rgb(var(--brand-500)/0.45),0_6px_16px_-8px_rgb(var(--brand-700)/0.85)]'
        : 'text-ink-700/78 hover:text-white hover:bg-white/[0.04]',
  ].join(' ');

  const headerInner = (
    <>
      {/* Edtech：激活时左侧 solid sky-400 hairline */}
      {!m.disabled && moduleActive && (
        <span className="absolute left-0 top-0 bottom-0 w-[3px]"
          style={{ background: 'rgb(var(--accent-cold))' }} />
      )}
      <IconCell icon={m.icon} active={!m.disabled && moduleActive} disabled={m.disabled} />
      <span className="flex-1">{m.label}</span>
      {m.disabled && <ComingSoonBadge />}
      {hasChildren && (
        <Chevron
          open={expanded}
          className={!m.disabled && moduleActive ? 'text-white/90' : 'text-white/30 group-hover:text-white/60'}
        />
      )}
    </>
  );

  const headerEl = hasChildren ? (
    <button
      type="button"
      onClick={onToggleExpand}
      aria-expanded={expanded}
      className={headerClass + ' w-full text-left'}
      style={{ borderRadius: 'var(--radius-sm)' }}
    >
      {headerInner}
    </button>
  ) : m.href && !m.disabled ? (
    <a href={m.href} className={headerClass} style={{ borderRadius: 'var(--radius-sm)' }}>
      {headerInner}
    </a>
  ) : (
    <div className={headerClass} style={{ borderRadius: 'var(--radius-sm)' }}>
      {headerInner}
    </div>
  );

  return (
    <div>
      {headerEl}

      {/* 二级菜单：Edtech 风格 — 缩进 14px，激活=实心 indigo */}
              {hasChildren && (
        <div
          className="overflow-hidden transition-all duration-200 ease-out"
          style={{
            maxHeight: expanded ? 240 : 0,
            opacity: expanded ? 1 : 0,
            marginTop: expanded ? 4 : 0,
          }}
        >
          <div className="ml-[14px] py-1 space-y-0.5">
            {m.children!.map((c) => {
              const active = pathActive(c.href);
              return (
                <a
                  key={c.key}
                  href={c.href}
                  className={[
                    'relative flex items-center gap-2 px-2.5 h-[34px] text-[13px] transition-all',
                    active
                      ? 'text-white bg-brand-600/90 shadow-[0_0_0_1px_rgb(var(--brand-500)/0.4)]'
                      : 'text-ink-700/68 hover:text-white hover:bg-white/[0.04]',
                  ].join(' ')}
                  style={{ borderRadius: 'var(--radius-xs)' }}
                >
                  {active && (
                    <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-[14px] rounded-r-full"
                      style={{ background: 'rgb(var(--accent-cold))' }} />
                  )}
                  <MenuIcon
                    name={c.icon}
                    size={14}
                    className={`shrink-0 ${active ? 'text-white' : 'text-white/70'}`}
                  />
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
        'group relative flex items-center gap-2.5 px-3 h-[40px] text-[14px] font-medium transition-all',
        active
          ? 'text-white bg-brand-600 shadow-[0_0_0_1px_rgb(var(--brand-500)/0.45),0_6px_16px_-8px_rgb(var(--brand-700)/0.85)]'
          : 'text-ink-700/78 hover:text-white hover:bg-white/[0.04]',
      ].join(' ')}
      style={{ borderRadius: 'var(--radius-sm)' }}
    >
      {active && (
        <span className="absolute left-0 top-0 bottom-0 w-[3px]"
          style={{ background: 'rgb(var(--accent-cold))' }} />
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
      className={`w-7 h-7 shrink-0 grid place-items-center transition-all
        ${active ? 'bg-white/18 text-white ring-1 ring-white/25'
          : disabled ? 'bg-white/[0.02] text-white/35'
          : 'bg-white/[0.04] text-white/85 ring-1 ring-white/[0.05] group-hover:bg-white/[0.07]'}`}
      style={{ borderRadius: 'var(--radius-xs)' }}
    >
      <MenuIcon name={icon} size={15} />
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
  // Edtech：冷灰「即将上线」小标签
  return (
    <span
      className="text-[10px] px-1.5 py-0.5 font-medium"
      style={{
        borderRadius: 'var(--radius-xs)',
        background: 'rgb(var(--ink-300) / 0.55)',
        color: 'rgb(var(--ink-600))',
        border: '1px solid rgb(var(--ink-400) / 0.55)',
      }}
    >
      即将上线
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
          'linear-gradient(180deg, rgb(var(--color-white) / 0.14) 0%, rgb(var(--color-white) / 0) 45%), linear-gradient(135deg, rgb(var(--brand-500)) 0%, rgb(var(--brand-700)) 100%)',
        boxShadow: '0 0 0 1px rgb(var(--brand-400) / 0.35), 0 8px 16px -8px rgb(var(--brand-600) / 0.5)',
      }}
    >
      {initial}
    </div>
  );
}

function BrandLogo() {
  // Edtech：纯 indigo-600 实心 logo · 无渐变装饰
  return (
    <div
      className="w-9 h-9 grid place-items-center shrink-0 relative"
      style={{
        borderRadius: 'var(--radius-md)',
        background: 'rgb(var(--brand-600))',
        boxShadow: '0 0 0 1px rgb(var(--brand-500) / 0.4), 0 10px 20px -10px rgb(var(--brand-600) / 0.6)',
      }}
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" className="text-white">
        <g stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
          <rect x="4"  y="14" width="2.2" height="6"  rx="1.1" />
          <rect x="8"  y="10" width="2.2" height="10" rx="1.1" />
          <rect x="12" y="5"  width="2.2" height="15" rx="1.1" />
          <rect x="16" y="9"  width="2.2" height="11" rx="1.1" />
          <rect x="20" y="13" width="2.2" height="7"  rx="1.1" />
        </g>
      </svg>
    </div>
  );
}

// ================= 内联 SVG 图标（Edtech · 线性，无 emoji） =================
function MenuIcon({ name, size = 16, className = '' }: { name: string; size?: number; className?: string }) {
  const common = {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.9,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
    className,
  };
  switch (name) {
    case 'speaker':
      return (<svg {...common}><path d="M11 5L6 9H3v6h3l5 4V5z"/><path d="M15.54 8.46a5 5 0 010 7.07"/><path d="M19.07 4.93a10 10 0 010 14.14"/></svg>);
    case 'book':
      return (<svg {...common}><path d="M4 4h10a4 4 0 014 4v12H8a4 4 0 01-4-4V4z"/><path d="M4 16a4 4 0 014-4h10"/></svg>);
    case 'mic':
      return (<svg {...common}><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0014 0"/><path d="M12 18v3"/></svg>);
    case 'pen':
      return (<svg {...common}><path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/><path d="M2 2l7.586 7.586"/><circle cx="11" cy="11" r="2"/></svg>);
    case 'clapper':
      return (<svg {...common}><path d="M3 8l2-2 4 2 4-2 4 2 4-2v12H3z"/><path d="M3 8v12h18V8"/></svg>);
    case 'palette':
      return (<svg {...common}><circle cx="13.5" cy="6.5" r="1.5"/><circle cx="17.5" cy="10.5" r="1.5"/><circle cx="8.5" cy="7.5" r="1.5"/><circle cx="6.5" cy="12.5" r="1.5"/><path d="M12 22a10 10 0 110-20 8 8 0 015.3 14A4 4 0 0015 22h-3z"/></svg>);
    case 'cog':
      return (<svg {...common}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 01-4 0v-.1A1.7 1.7 0 009 19.4a1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 010-4h.1A1.7 1.7 0 004.6 9a1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 014 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 010 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg>);
    case 'lock':
      return (<svg {...common}><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/></svg>);
    case 'logout':
      return (<svg {...common}><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>);
    default:
      return (<svg {...common}><rect x="4" y="4" width="16" height="16" rx="2"/></svg>);
  }
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
      style={{ borderRadius: 'var(--radius-xs)' }}
    >
      <span className="w-6 h-6 grid place-items-center bg-white/[0.04]" style={{ borderRadius: 'var(--radius-xs)' }}>
        <MenuIcon name={icon} size={14} />
      </span>
      <span className="flex-1 text-left">{label}</span>
    </button>
  );
}
