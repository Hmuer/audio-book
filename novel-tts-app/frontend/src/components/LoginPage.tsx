'use client';

import { useState, FormEvent, useMemo } from 'react';
import { useAuth } from '@/components/AuthContext';

/** 声波条配置：高度比例 + 动画延迟，用于品牌区动态视觉 */
const WAVE_BARS = [
  { h: 35, d: '0.00s' }, { h: 60, d: '0.10s' }, { h: 85, d: '0.05s' },
  { h: 100, d: '0.15s' }, { h: 75, d: '0.08s' }, { h: 50, d: '0.20s' },
  { h: 90, d: '0.03s' }, { h: 65, d: '0.12s' }, { h: 45, d: '0.18s' },
  { h: 70, d: '0.06s' }, { h: 55, d: '0.14s' }, { h: 80, d: '0.09s' },
  { h: 40, d: '0.16s' }, { h: 60, d: '0.11s' }, { h: 95, d: '0.04s' },
  { h: 50, d: '0.13s' }, { h: 75, d: '0.07s' }, { h: 45, d: '0.19s' },
];

/** 左侧品牌区特性列表：体现平台多形态（有声书 / 小说 / 短剧 / 漫画） */
const FEATURES = [
  { icon: 'sparkles', title: 'AI 全程驱动', desc: '从灵感到成品，一站式内容创作工作流' },
  { icon: 'microphone', title: '多形态创作', desc: '小说 · 有声书 · 短剧 · 漫画，持续扩展' },
  { icon: 'wand', title: '角色与世界观', desc: '自动识别人物、设定并复用至各形态' },
];

function FeatureIcon({ name }: { name: string }) {
  const icons: Record<string, JSX.Element> = {
    microphone: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="w-5 h-5">
        <rect x="9" y="2" width="6" height="11" rx="3" />
        <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
        <line x1="12" y1="18" x2="12" y2="22" />
      </svg>
    ),
    sparkles: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="w-5 h-5">
        <path d="M12 3l1.9 4.8L18.5 9l-4.6 1.2L12 15l-1.9-4.8L5.5 9l4.6-1.2L12 3z" />
        <path d="M19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14z" />
      </svg>
    ),
    bolt: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="w-5 h-5">
        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
      </svg>
    ),
    wand: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="w-5 h-5">
        <path d="M15 4V2M15 10V8M18 7h2M12 7h2M3 21l9-9" />
        <path d="M15.5 6.5l3 3" strokeWidth="1.8" strokeLinecap="round" />
        <path d="M18.5 3.5l2 2" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    ),
  };
  return icons[name] ?? null;
}

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
    <div className="min-h-[100vh] flex">
      {/* ============ 左侧：品牌展示区 ============ */}
      <div className="hidden lg:flex lg:w-[52%] xl:w-[48%] relative overflow-hidden flex-col justify-between p-12 xl:p-16">
        {/* 背景：深紫渐变 + 网格 + 光晕 */}
        <div
          className="absolute inset-0"
          style={{
            background:
              'linear-gradient(145deg, #0f0f12 0%, #13111c 40%, #1a1428 100%)',
          }}
        />
        <div className="absolute inset-0 login-grid-bg" />
        <div
          className="glow-orb w-[500px] h-[500px] bg-brand-500/30"
          style={{ top: '-150px', left: '-120px', opacity: 0.5 }}
        />
        <div
          className="glow-orb w-[400px] h-[400px] bg-accent-teal/15"
          style={{ bottom: '-100px', right: '-80px', opacity: 0.4 }}
        />

        {/* 顶部：Logo + 品牌名 */}
        <div className="relative z-10 animate-float-in" style={{ animationDelay: '0.1s' }}>
          <div className="flex items-center gap-3">
            <div className="relative">
              <div className="absolute inset-0 rounded-lg bg-brand-500/40 blur-lg" />
              <div
                className="relative w-12 h-12 rounded-lg grid place-items-center shadow-el-lg"
                style={{
                  backgroundImage:
                    'linear-gradient(135deg, #8b5cf6 0%, #7c3aed 55%, #6366f1 100%)',
                }}
              >
                <div className="flex items-end gap-[2px] h-5">
                  <span className="w-[3px] rounded-full bg-white/70" style={{ height: '40%' }} />
                  <span className="w-[3px] rounded-full bg-white/85" style={{ height: '80%' }} />
                  <span className="w-[3px] rounded-full bg-white" style={{ height: '100%' }} />
                  <span className="w-[3px] rounded-full bg-white/80" style={{ height: '60%' }} />
                </div>
              </div>
            </div>
            <span className="font-display text-2xl font-bold text-white tracking-tight">
              阿布
            </span>
          </div>
        </div>

        {/* 中部：声波动画 + 标语 */}
        <div className="relative z-10 flex-1 flex flex-col justify-center max-w-md">
          <div className="animate-float-in" style={{ animationDelay: '0.25s' }}>
            <h2 className="font-display text-[2.5rem] xl:text-[3rem] font-bold text-white leading-[1.1] tracking-tight">
              AI 内容创作平台
              <br />
              <span style={{ background: 'linear-gradient(135deg, #c4b5fd 0%, #a78bfa 100%)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', backgroundClip: 'text' }}>
                阿布
              </span>
            </h2>
            <p className="mt-4 text-[15px] text-white/50 leading-relaxed">
              小说 · 有声书 · 短剧 · 漫画 —— AI 驱动，一个平台搞定全链路创作。
            </p>
          </div>

          {/* 动态声波 */}
          <div className="mt-10 flex items-end gap-[5px] h-16 animate-float-in" style={{ animationDelay: '0.4s' }}>
            {WAVE_BARS.map((bar, i) => (
              <div
                key={i}
                className="wave-bar w-[4px]"
                style={{
                  height: `${bar.h}%`,
                  animationDelay: bar.d,
                  background: i % 3 === 0
                    ? 'linear-gradient(to top, #8b5cf6, #c4b5fd)'
                    : i % 3 === 1
                    ? 'linear-gradient(to top, #7c3aed, #a78bfa)'
                    : 'linear-gradient(to top, #6366f1, #a5b4fc)',
                }}
              />
            ))}
          </div>
        </div>

        {/* 底部：特性列表 */}
        <div className="relative z-10 space-y-3.5 animate-float-in" style={{ animationDelay: '0.55s' }}>
          {FEATURES.map((f, i) => (
            <div key={i} className="flex items-center gap-3">
              <div
                className="w-10 h-10 rounded-md grid place-items-center shrink-0"
                style={{
                  background: 'rgba(139,92,246,0.10)',
                  border: '1px solid rgba(139,92,246,0.18)',
                  color: '#c4b5fd',
                }}
              >
                <FeatureIcon name={f.icon} />
              </div>
              <div>
                <div className="text-sm font-medium text-white/85">{f.title}</div>
                <div className="text-xs text-white/40 mt-0.5">{f.desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ============ 右侧：登录表单区 ============ */}
      <div className="flex-1 flex items-center justify-center px-6 py-12 relative">
        {/* 移动端背景光晕 */}
        <div
          className="glow-orb w-[320px] h-[320px] bg-brand-500/20 lg:hidden"
          style={{ top: '-80px', left: '-80px' }}
        />

        <div className="relative w-full max-w-[400px]">
          {/* 移动端 Logo（lg 以下显示） */}
          <div className="lg:hidden mb-8 text-center animate-float-in">
            <div className="inline-flex items-center gap-2.5">
              <div
                className="w-10 h-10 rounded-md grid place-items-center shadow-el-lg"
                style={{
                  backgroundImage:
                    'linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)',
                }}
              >
                <div className="flex items-end gap-[2px] h-4">
                  <span className="w-[3px] rounded-full bg-white/70" style={{ height: '40%' }} />
                  <span className="w-[3px] rounded-full bg-white" style={{ height: '100%' }} />
                  <span className="w-[3px] rounded-full bg-white/80" style={{ height: '60%' }} />
                </div>
              </div>
              <span className="font-display text-xl font-bold text-white">阿布</span>
            </div>
          </div>

          {/* 标题 */}
          <div className="mb-8 animate-float-in" style={{ animationDelay: '0.15s' }}>
            <h1 className="font-display text-[26px] font-bold text-white tracking-tight">
              欢迎回来
            </h1>
            <p className="mt-2 text-sm text-ink-600">
              登录你的账户，继续创作之旅
            </p>
          </div>

          {/* 表单卡片 */}
          <div
            className={`glass-panel p-7 sm:p-8 animate-float-in ${shake ? 'animate-shake' : ''}`}
            style={{ animationDelay: '0.25s' }}
          >
            <form onSubmit={onSubmit} className="space-y-5">
              {/* 用户名 */}
              <div>
                <label className="block text-xs font-medium tracking-wide text-ink-600 mb-2">
                  用户名
                </label>
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
                    placeholder=""
                    className="input !pl-11"
                  />
                </div>
              </div>

              {/* 密码 */}
              <div>
                <label className="block text-xs font-medium tracking-wide text-ink-600 mb-2">
                  密码
                </label>
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
                    placeholder=""
                    className="input !pl-11 !pr-11"
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
              </div>

              {/* 错误提示 */}
              {err && (
                <div
                  className="text-sm rounded-md px-3.5 py-2.5 flex items-start gap-2"
                  style={{
                    borderColor: 'rgba(251,113,133,0.28)',
                    background: 'rgba(251,113,133,0.08)',
                    color: '#fecdd3',
                    borderWidth: 1,
                    borderStyle: 'solid',
                  }}
                >
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4 mt-0.5 shrink-0">
                    <circle cx="12" cy="12" r="10" />
                    <line x1="12" y1="8" x2="12" y2="12" />
                    <line x1="12" y1="16" x2="12.01" y2="16" />
                  </svg>
                  <span>{err}</span>
                </div>
              )}

              {/* 登录按钮 */}
              <button
                type="submit"
                disabled={!canSubmit}
                className="btn-primary w-full !py-3 !text-[15px] justify-center"
              >
                {busy ? (
                  <>
                    <span className="inline-block w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                    登录中...
                  </>
                ) : '登 录'}
              </button>
            </form>

            {/* 占位分隔：保持卡片视觉节奏 */}
            <div className="mt-6 pt-5 border-t border-white/[0.05]" />
          </div>

          {/* 底部品牌语 */}
          <div className="mt-8 text-center text-[11px] text-ink-500/70 tracking-wide animate-float-in" style={{ animationDelay: '0.35s' }}>
            Powered by AI · 阿布创作平台
          </div>
        </div>
      </div>
    </div>
  );
}
