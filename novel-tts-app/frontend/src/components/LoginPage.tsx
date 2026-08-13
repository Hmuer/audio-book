'use client';

import { useState, FormEvent } from 'react';
import { useAuth } from '@/components/AuthContext';

export default function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) {
      setErr('请输入用户名和密码');
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await login(username.trim(), password);
      // 登录成功 → AuthContext 更新 user → page.tsx 自动跳转
    } catch (e: any) {
      const msg = String(e?.message || e);
      // 后端返回 "用户名或密码错误" 时 HTTP 是 401，_fetch 会拼成 "HTTP 401: xxx"
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
    <div className="min-h-[70vh] flex items-center justify-center">
      <div className="w-full max-w-sm">
        {/* Logo / 标题 */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-brand-500/20 border border-brand-500/30 mb-4">
            <span className="text-3xl">🎧</span>
          </div>
          <h1 className="text-2xl font-bold">AI 有声小说生成器</h1>
          <p className="text-white/50 text-sm mt-2">请登录以继续</p>
        </div>

        <form
          onSubmit={onSubmit}
          className="bg-white/5 border border-white/10 rounded-2xl p-6 space-y-4 backdrop-blur"
        >
          <div>
            <label className="block text-sm text-white/70 mb-1.5">用户名</label>
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              autoFocus
              autoComplete="username"
              disabled={busy}
              className="input-base w-full"
              placeholder="admin"
            />
          </div>

          <div>
            <label className="block text-sm text-white/70 mb-1.5">密码</label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoComplete="current-password"
              disabled={busy}
              className="input-base w-full"
              placeholder="••••••"
            />
          </div>

          {err && (
            <div className="text-sm text-red-300 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">
              {err}
            </div>
          )}

          <button
            type="submit"
            disabled={busy || !username.trim() || !password}
            className="btn-primary w-full justify-center"
          >
            {busy ? '登录中…' : '登 录'}
          </button>

          <div className="text-xs text-white/40 text-center pt-2 border-t border-white/5">
            默认账号：<code className="px-1 py-0.5 bg-white/10 rounded">admin</code> / <code className="px-1 py-0.5 bg-white/10 rounded">admin</code>
            <br />
            <span className="text-white/30">登录后请尽快修改密码</span>
          </div>
        </form>
      </div>
    </div>
  );
}
