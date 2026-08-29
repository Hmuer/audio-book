'use client';

import { useState } from 'react';
import { useAuth } from '@/components/AuthContext';

// ---- 菜单结构 ----
type MenuItem = {
  key: string;
  label: string;
  icon: string;
  href: string;          // hash 路由
  disabled?: boolean;    // 未开放时灰显
  badge?: number;        // 可运行中项目数等
};

type Module = {
  key: string;
  label: string;
  icon: string;
  href?: string;         // 一级自己也可点击跳转（无子项时）
  children?: MenuItem[]; // 有子项则展开
  disabled?: boolean;    // 整个模块未开放
};

const AUDIOBOOKS_CHILDREN: MenuItem[] = [
  { key: 'list',   label: '我的有声书', href: '#/audiobooks',        icon: '📖' },
  { key: 'voices', label: '音色库',     href: '#/audiobooks/voices', icon: '🎙️' },
];

const MODULES: Module[] = [
  {
    key: 'audiobooks',
    label: 'AI有声书',
    icon: '🔊',
    children: AUDIOBOOKS_CHILDREN,
  },
  {
    key: 'novel',
    label: 'AI小说创作',
    icon: '✍️',
    disabled: true,
  },
  {
    key: 'drama',
    label: 'AI短剧',
    icon: '🎬',
    disabled: true,
  },
  {
    key: 'comic',
    label: 'AI漫画',
    icon: '🎨',
    disabled: true,
  },
];

const SETTINGS_ITEM: MenuItem = {
  key: 'settings',
  label: '设置',
  icon: '⚙️',
  href: '#/settings',
};

interface Props {
  /** 当前 hash 路径（不含 # 前缀），用来高亮 */
  currentPath: string;
}

export default function AppSidebar({ currentPath }: Props) {
  const { user, logout } = useAuth();
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [pwdModal, setPwdModal] = useState(false);

  if (!user) return null;

  // ---- 高亮逻辑 ----
  const isPathActive = (href: string) => {
    const path = href.replace(/^#/, '');
    if (path === currentPath) return true;
    // 子路由也高亮父菜单：#/audiobooks/xxx → #/audiobooks 高亮
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
        className="hidden md:flex flex-col w-[220px] shrink-0 h-screen sticky top-0 border-r border-white/[0.06]"
        style={{
          background: 'linear-gradient(180deg, rgba(20,20,25,0.9) 0%, rgba(15,15,20,0.95) 100%)',
        }}
      >
        {/* 品牌 */}
        <div className="px-5 pt-5 pb-4">
          <div className="flex items-center gap-2.5">
            <div
              className="w-9 h-9 rounded-xl grid place-items-center text-white shadow-brand"
              style={{
                backgroundImage: 'linear-gradient(180deg, rgba(255,255,255,0.14) 0%, rgba(255,255,255,0) 50%), linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)',
              }}
            >
              <span className="text-lg leading-none">🎧</span>
            </div>
            <div className="min-w-0">
              <div className="text-[17px] font-semibold text-white leading-none">阿布</div>
              <div className="text-[10px] text-white/40 mt-1 tracking-wider">ABU · AI 创作平台</div>
            </div>
          </div>
        </div>

        {/* 菜单主体 */}
        <nav className="flex-1 px-3 pb-3 overflow-y-auto">
          {MODULES.map(m => {
            const active = isModuleActive(m);
            return (
              <div key={m.key} className="mb-1">
                {/* 一级 */}
                <a
                  href={m.disabled ? undefined : (m.href || '#')}
                  onClick={m.disabled ? (e) => e.preventDefault() : undefined}
                  className={[
                    'flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-medium transition-all',
                    m.disabled ? 'text-white/30 cursor-not-allowed' :
                    active ? 'text-white bg-white/[0.06]' : 'text-white/70 hover:text-white hover:bg-white/[0.04]',
                  ].join(' ')}
                >
                  <span className="text-[15px]">{m.icon}</span>
                  <span className="flex-1">{m.label}</span>
                </a>

                {/* 二级 */}
                {m.children && !m.disabled && (
                  <div className="mt-1 ml-2 pl-4 border-l border-white/[0.06] space-y-0.5">
                    {m.children.map(c => {
                      const childActive = isPathActive(c.href);
                      return (
                        <a
                          key={c.key}
                          href={c.href}
                          className={[
                            'flex items-center gap-2 px-3 py-1.5 rounded-md text-[13px] transition-colors',
                            childActive
                              ? 'text-white bg-white/[0.06]'
                              : 'text-white/55 hover:text-white hover:bg-white/[0.03]',
                          ].join(' ')}
                        >
                          <span className="text-[12px] opacity-70">{c.icon}</span>
                          <span>{c.label}</span>
                          {c.badge ? (
                            <span className="ml-auto text-[10px] text-white/40">{c.badge}</span>
                          ) : null}
                        </a>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}

          {/* 分隔 */}
          <div className="h-px bg-white/[0.06] my-3" />

          {/* 设置（一级） */}
          <a
            href={SETTINGS_ITEM.href}
            className={[
              'flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-medium transition-all',
              isPathActive(SETTINGS_ITEM.href)
                ? 'text-white bg-white/[0.06]'
                : 'text-white/70 hover:text-white hover:bg-white/[0.04]',
            ].join(' ')}
          >
            <span className="text-[15px]">{SETTINGS_ITEM.icon}</span>
            <span>{SETTINGS_ITEM.label}</span>
          </a>
        </nav>

        {/* 底部用户菜单 */}
        <div className="border-t border-white/[0.06] p-2">
          <div className="relative">
            <button
              onClick={() => setUserMenuOpen(o => !o)}
              className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg hover:bg-white/[0.04] transition-colors"
            >
              <span className="w-8 h-8 rounded-full bg-brand-500/30 text-brand-200 grid place-items-center text-xs font-bold shrink-0">
                {user.username.slice(0, 1).toUpperCase()}
              </span>
              <div className="min-w-0 flex-1 text-left">
                <div className="text-sm text-white truncate">{user.username}</div>
                <div className="text-[11px] text-white/40 truncate">
                  {user.created_at ? `加入于 ${new Date(user.created_at).toLocaleDateString('zh-CN')}` : ''}
                </div>
              </div>
              <span className="text-white/30 text-xs shrink-0">▾</span>
            </button>

            {userMenuOpen && (
              <>
                <div className="fixed inset-0 z-30" onClick={() => setUserMenuOpen(false)} />
                <div className="absolute bottom-full left-0 right-0 mb-2 bg-zinc-900 border border-white/10 rounded-xl shadow-xl z-40 overflow-hidden">
                  <button
                    className="w-full text-left px-3 py-2 text-sm hover:bg-white/5 flex items-center gap-2"
                    onClick={() => { setUserMenuOpen(false); setPwdModal(true); }}
                  >
                    🔒 修改密码
                  </button>
                  <button
                    className="w-full text-left px-3 py-2 text-sm text-red-300 hover:bg-red-500/10 flex items-center gap-2"
                    onClick={async () => { setUserMenuOpen(false); await logout(); }}
                  >
                    ↩ 退出登录
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </aside>

      {/* 修改密码 Modal — 复用 UserMenu 里的逻辑 */}
      {pwdModal && <ChangePasswordModal onClose={() => setPwdModal(false)} />}
    </>
  );
}

// ---- 修改密码 Modal（与 UserMenu 内部一致，抽出复用） ----
function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const { user } = useAuth();
  const [oldPwd, setOldPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [confirmPwd, setConfirmPwd] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (newPwd.length < 6) { setErr('新密码至少 6 位'); return; }
    if (newPwd !== confirmPwd) { setErr('两次输入的新密码不一致'); return; }
    setBusy(true);
    try {
      const { api } = await import('@/lib/api');
      await api.authChangePassword(oldPwd, newPwd);
      setOk(true);
      setTimeout(() => onClose(), 1200);
    } catch (e: any) {
      const msg = String(e?.message || e);
      if (msg.includes('原密码')) setErr('原密码不正确');
      else setErr(msg);
    } finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className="bg-zinc-900 border border-white/10 rounded-2xl p-6 w-full max-w-sm mx-4" onClick={e => e.stopPropagation()}>
        <h3 className="text-lg font-semibold mb-1">修改密码</h3>
        <p className="text-xs text-white/40 mb-4">当前账号：{user?.username}</p>

        {ok ? (
          <div className="text-center py-6 text-green-300">✓ 密码已修改</div>
        ) : (
          <form onSubmit={onSubmit} className="space-y-3">
            {[
              { label: '原密码', v: oldPwd,     set: setOldPwd,     auto: 'current-password' },
              { label: '新密码（≥6位）', v: newPwd,      set: setNewPwd,      auto: 'new-password' },
              { label: '确认新密码',   v: confirmPwd,  set: setConfirmPwd,  auto: 'new-password' },
            ].map(f => (
              <div key={f.label}>
                <label className="block text-sm text-white/70 mb-1">{f.label}</label>
                <input
                  type="password" value={f.v} onChange={e => f.set(e.target.value)}
                  autoComplete={f.auto} disabled={busy} className="input-base w-full"
                />
              </div>
            ))}
            {err && <div className="text-sm text-red-300 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">{err}</div>}
            <div className="flex gap-2 pt-2">
              <button type="button" onClick={onClose} disabled={busy} className="btn-ghost flex-1 justify-center">取消</button>
              <button type="submit" disabled={busy || !oldPwd || !newPwd || !confirmPwd} className="btn-primary flex-1 justify-center">
                {busy ? '提交中…' : '确认修改'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
