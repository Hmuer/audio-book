'use client';

import { useState, FormEvent, useMemo } from 'react';
import { useAuth } from '@/components/AuthContext';

export default function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [shake, setShake] = useState(false);
  const [showPwd, setShowPwd] = useState(false);

  const canSubmit = useMemo(
    () => username.trim().length > 0 && password.length > 0 && !busy,
    [username, password, busy],
  );

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) {
      setErr('登录失败');
      setShake(true); setTimeout(() => setShake(false), 400);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await login(username.trim(), password);
    } catch (e: any) {
      const msg = String(e?.message || e);
      setShake(true); setTimeout(() => setShake(false), 400);
      if (msg.includes('401') || msg.includes('密码') || msg.includes('用户名')) {
        setErr('登录失败');
      } else {
        setErr('登录失败');
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-[100vh] flex items-center justify-center px-6 py-12"
         style={{ background: 'rgb(var(--ink-0))' }}>
      <div className="w-full max-w-[380px]">
        {/* Logo + 平台名（极简） */}
        <div className="mb-10 flex items-center justify-center gap-3">
          <div
            className="w-11 h-11 grid place-items-center shrink-0"
            style={{
              borderRadius: 'var(--radius-md)',
              background: 'rgb(var(--brand-600))',
              boxShadow: '0 0 0 1px rgba(var(--brand-500), 0.4), 0 10px 20px -10px rgba(var(--brand-600), 0.6)',
            }}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" className="text-white">
              <g stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                <rect x="4"  y="14" width="2.2" height="6"  rx="1.1" />
                <rect x="8"  y="10" width="2.2" height="10" rx="1.1" />
                <rect x="12" y="5"  width="2.2" height="15" rx="1.1" />
                <rect x="16" y="9"  width="2.2" height="11" rx="1.1" />
                <rect x="20" y="13" width="2.2" height="7"  rx="1.1" />
              </g>
            </svg>
          </div>
          <div>
            <div className="text-[15px] font-semibold text-white leading-none">阿布</div>
            <div className="mt-1 text-[11px] text-ink-500 leading-none">AI 内容创作平台</div>
          </div>
        </div>

        {/* 表单卡 */}
        <div
          className={`glass-panel animate-fade-in ${shake ? 'animate-shake' : ''}`}
        >
          <form onSubmit={onSubmit} className="space-y-4">
            {/* 账号输入 */}
            <div className="relative">
              <span className="absolute left-3.5 top-1/2 -translate-y-1/2 text-ink-500 pointer-events-none">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="w-[18px] h-[18px]">
                  <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                  <circle cx="12" cy="7" r="4" />
                </svg>
              </span>
              <input
                type="text"
                value={username}
                onChange={e => setUsername(e.target.value)}
                autoFocus
                autoComplete="username"
                disabled={busy}
                className="input !pl-11"
              />
            </div>

            {/* 密码输入 */}
            <div className="relative">
              <span className="absolute left-3.5 top-1/2 -translate-y-1/2 text-ink-500 pointer-events-none">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="w-[18px] h-[18px]">
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
                  <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                </svg>
              </span>
              <input
                type={showPwd ? 'text' : 'password'}
                value={password}
                onChange={e => setPassword(e.target.value)}
                autoComplete="current-password"
                disabled={busy}
                className="input !pl-11 !pr-11"
                onKeyDown={(e) => { if (e.key === 'Enter' && canSubmit) onSubmit(e); }}
              />
              <button
                type="button"
                onClick={() => setShowPwd(v => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-ink-500 hover:text-ink-700 transition-colors"
                tabIndex={-1}
              >
                {showPwd ? (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="w-[18px] h-[18px]">
                    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                    <line x1="1" y1="1" x2="23" y2="23" />
                  </svg>
                ) : (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="w-[18px] h-[18px]">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                )}
              </button>
            </div>

            {/* 错误提示（极简） */}
            {err && (
              <div
                className="text-sm rounded-md px-3 py-2"
                style={{
                  borderColor: 'rgba(var(--status-error-bg), 0.28)',
                  background: 'rgba(var(--status-error-bg), 0.08)',
                  color: 'rgb(var(--status-error-fg))',
                  borderWidth: 1,
                  borderStyle: 'solid',
                }}
              >
                {err}
              </div>
            )}

            {/* 登录按钮 */}
            <button
              type="submit"
              disabled={!canSubmit}
              className="btn-primary w-full !py-2.5 !text-[14px] justify-center"
            >
              {busy ? (
                <span className="inline-flex items-center gap-2">
                  <span className="inline-block w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  登录中
                </span>
              ) : '登录'}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
