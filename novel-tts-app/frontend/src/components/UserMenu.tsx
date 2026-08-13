'use client';

import { useState } from 'react';
import { useAuth } from '@/components/AuthContext';

export default function UserMenu() {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [showChangePwd, setShowChangePwd] = useState(false);

  if (!user) return null;

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(o => !o)}
        className="chip border border-white/10 hover:border-white/30 flex items-center gap-2"
        title={user.username}
      >
        <span className="w-6 h-6 rounded-full bg-brand-500/30 text-brand-200 flex items-center justify-center text-xs font-bold">
          {user.username.slice(0, 1).toUpperCase()}
        </span>
        <span className="text-sm">{user.username}</span>
        <span className="text-white/40 text-xs">▾</span>
      </button>

      {open && (
        <>
          {/* 点击外部关闭 */}
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute right-0 mt-1 w-56 bg-zinc-900 border border-white/10 rounded-xl shadow-xl z-20 overflow-hidden">
            <div className="px-4 py-3 border-b border-white/5">
              <div className="text-sm font-medium">{user.username}</div>
              <div className="text-xs text-white/40 mt-0.5">
                {user.created_at
                  ? `创建于 ${new Date(user.created_at).toLocaleDateString('zh-CN')}`
                  : ''}
              </div>
            </div>
            <button
              className="w-full text-left px-4 py-2 text-sm hover:bg-white/5"
              onClick={() => {
                setOpen(false);
                setShowChangePwd(true);
              }}
            >
              🔒 修改密码
            </button>
            <button
              className="w-full text-left px-4 py-2 text-sm text-red-300 hover:bg-red-500/10"
              onClick={async () => {
                setOpen(false);
                await logout();
              }}
            >
              ↩ 退出登录
            </button>
          </div>
        </>
      )}

      {showChangePwd && (
        <ChangePasswordModal
          onClose={() => setShowChangePwd(false)}
        />
      )}
    </div>
  );
}

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
    if (newPwd.length < 6) {
      setErr('新密码至少 6 位');
      return;
    }
    if (newPwd !== confirmPwd) {
      setErr('两次输入的新密码不一致');
      return;
    }
    setBusy(true);
    try {
      // 动态导入避免循环依赖
      const { api } = await import('@/lib/api');
      await api.authChangePassword(oldPwd, newPwd);
      setOk(true);
      setTimeout(() => onClose(), 1500);
    } catch (e: any) {
      const msg = String(e?.message || e);
      if (msg.includes('400') && msg.includes('原密码')) {
        setErr('原密码不正确');
      } else if (msg.includes('401') || msg.includes('登录')) {
        setErr('登录已失效，请重新登录');
        setTimeout(() => onClose(), 1500);
      } else {
        setErr(msg);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="bg-zinc-900 border border-white/10 rounded-2xl p-6 w-full max-w-sm"
        onClick={e => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold mb-1">修改密码</h3>
        <p className="text-xs text-white/40 mb-4">当前账号：{user?.username}</p>

        {ok ? (
          <div className="text-center py-6 text-green-300">
            ✓ 密码已修改，下次登录请使用新密码
          </div>
        ) : (
          <form onSubmit={onSubmit} className="space-y-3">
            <div>
              <label className="block text-sm text-white/70 mb-1">原密码</label>
              <input
                type="password"
                value={oldPwd}
                onChange={e => setOldPwd(e.target.value)}
                autoFocus
                autoComplete="current-password"
                disabled={busy}
                className="input-base w-full"
              />
            </div>
            <div>
              <label className="block text-sm text-white/70 mb-1">新密码（≥6 位）</label>
              <input
                type="password"
                value={newPwd}
                onChange={e => setNewPwd(e.target.value)}
                autoComplete="new-password"
                disabled={busy}
                className="input-base w-full"
              />
            </div>
            <div>
              <label className="block text-sm text-white/70 mb-1">确认新密码</label>
              <input
                type="password"
                value={confirmPwd}
                onChange={e => setConfirmPwd(e.target.value)}
                autoComplete="new-password"
                disabled={busy}
                className="input-base w-full"
              />
            </div>

            {err && (
              <div className="text-sm text-red-300 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">
                {err}
              </div>
            )}

            <div className="flex gap-2 pt-2">
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="btn-ghost flex-1 justify-center"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={busy || !oldPwd || !newPwd || !confirmPwd}
                className="btn-primary flex-1 justify-center"
              >
                {busy ? '提交中…' : '确认修改'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
