'use client';

import { useState, FormEvent } from 'react';
import { useAuth } from '@/components/AuthContext';

export default function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [shake, setShake] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) {
      setErr('请输入用户名和密码');
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
      if (msg.includes('401') || msg.includes('密码')) {
        setErr('用户名或密码错误');
      } else {
        setErr(msg);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-[86vh] flex items-center justify-center py-12">
      <div className="relative w-full max-w-md px-4">
        {/* 背景光晕（Eleven 风格 corner glows） */}
        <div
          className="glow-orb w-[420px] h-[420px] bg-brand-500/25"
          style={{ top: '-160px', left: '-140px' }}
        />
        <div
          className="glow-orb w-[360px] h-[360px] bg-accent-teal/15"
          style={{ bottom: '-120px', right: '-100px' }}
        />

        {/* Logo / 标题 */}
        <div className="text-center mb-8 animate-fade-in">
          <div className="inline-flex items-center justify-center mb-6">
            <div className="relative">
              <div className="absolute inset-0 rounded-[18px] bg-brand-500/30 blur-xl" />
              <div className="relative w-16 h-16 rounded-[18px] grid place-items-center shadow-el-xl overflow-hidden"
                style={{
                  backgroundImage:
                    'linear-gradient(135deg, rgba(139,92,246,0.95) 0%, rgba(124,58,237,0.95) 55%, rgba(99,102,241,0.9) 100%)',
                }}
              >
                {/* 声波图形：Eleven 的 logo 是"波形"+字母，这里用 5 根渐高声波条抽象表达有声书 */}
                <div className="flex items-end gap-[3px] h-6">
                  <span className="w-1 rounded-full bg-white/70" style={{ height: '45%' }} />
                  <span className="w-1 rounded-full bg-white/80" style={{ height: '75%' }} />
                  <span className="w-1 rounded-full bg-white" style={{ height: '100%' }} />
                  <span className="w-1 rounded-full bg-white/85" style={{ height: '65%' }} />
                  <span className="w-1 rounded-full bg-white/65" style={{ height: '40%' }} />
                </div>
              </div>
            </div>
          </div>
          <h1 className="headline text-[28px] sm:text-3xl leading-tight">
            AI 有声小说生成器
          </h1>
          <p className="mt-3 text-sm text-ink-600">
            上传一本 TXT · 自动识别角色对白 · 多音色有声书
          </p>
        </div>

        {/* 登录卡片 */}
        <div
          className={`glass-panel p-6 sm:p-8 animate-scale-in ${shake ? 'animate-shake' : ''}`}
        >
          <form onSubmit={onSubmit} className="space-y-5">
            <div>
              <label className="block text-xs font-medium tracking-wide text-ink-600 mb-2 uppercase">
                用户名
              </label>
              <input
                type="text"
                value={username}
                onChange={e => setUsername(e.target.value)}
                autoFocus
                autoComplete="username"
                disabled={busy}
                placeholder="admin"
                className="input"
              />
            </div>

            <div>
              <label className="block text-xs font-medium tracking-wide text-ink-600 mb-2 uppercase">
                密码
              </label>
              <input
                type="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                autoComplete="current-password"
                disabled={busy}
                placeholder="••••••"
                className="input"
              />
            </div>

            {err && (
              <div className="text-sm rounded-[10px] px-3.5 py-2.5 border"
                style={{
                  borderColor: 'rgba(251,113,133,0.28)',
                  background: 'rgba(251,113,133,0.08)',
                  color: '#fecdd3',
                }}
              >
                <span className="mr-1.5">⚠</span>{err}
              </div>
            )}

            <button
              type="submit"
              disabled={busy || !username.trim() || !password}
              className="btn-primary w-full !py-2.5 !text-[15px] justify-center"
            >
              {busy ? (
                <>
                  <span className="inline-block w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  登录中…
                </>
              ) : '登 录'}
            </button>

            <div className="pt-2 border-t border-white/[0.05] text-center text-[12px] text-ink-600 space-y-1">
              <div>
                默认账号：
                <code className="mx-1 px-2 py-0.5 rounded-md bg-white/[0.05] border border-white/[0.06] text-ink-700">admin</code>
                /
                <code className="mx-1 px-2 py-0.5 rounded-md bg-white/[0.05] border border-white/[0.06] text-ink-700">admin</code>
              </div>
              <div className="text-ink-500/80">登录后请尽快修改密码</div>
            </div>
          </form>
        </div>

        {/* 底部品牌说明（Eleven 常用：小字一行软语 + 社交符号） */}
        <div className="mt-10 text-center text-[11px] text-ink-500/80 tracking-wide">
          Powered by MiniMax / 角色识别 · 对白归属 · 多音色合成
        </div>
      </div>
    </div>
  );
}
