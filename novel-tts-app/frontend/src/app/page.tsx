'use client';

import { useEffect, useState } from 'react';
import { useTheme } from '@/components/ThemeContext';
import StepInput from '@/components/StepInput';
import StepRoles from '@/components/StepRoles';
import StepGenerate from '@/components/StepGenerate';
import BookFlow from '@/components/BookFlow';
import { api, PrepareResp, SynthResp, Voice } from '@/lib/api';

type Step = 1 | 2 | 3;
type Mode = 'single' | 'book';

export default function HomePage() {
  const { theme, toggle } = useTheme();
  const [mode, setMode] = useState<Mode>('single');
  const [step, setStep] = useState<Step>(1);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [prepareResult, setPrepareResult] = useState<PrepareResp | null>(null);
  const [synthResult, setSynthResult] = useState<SynthResp | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.voices()
      .then(setVoices)
      .catch(e => console.error('voices 加载失败:', e));
  }, []);

  return (
    <div className="mx-auto max-w-7xl px-4 sm:px-6 py-8">
      {/* header */}
      <header className="flex items-center justify-between mb-6 flex-wrap gap-3">
        <div>
          <div className="flex items-center gap-3">
            <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-brand-500 to-blue-500 shadow-lg shadow-brand-500/30 grid place-items-center font-bold text-lg">
              声
            </div>
            <div>
              <h1 className="text-xl sm:text-2xl font-bold">AI 有声小说生成器</h1>
              <p className="text-xs text-white/50 mt-0.5">
                LLM 驱动 · 角色识别 · 对白归属 · 多音色合成 · 一键 MP3
              </p>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {mode === 'single' && <Stepper step={step} setStep={setStep} />}
          <button className="btn-ghost" onClick={toggle} title="切换深色/浅色">
            {theme === 'dark' ? '🌙 深色' : '☀️ 浅色'}
          </button>
        </div>
      </header>

      {/* 模式切换 */}
      <div className="flex items-center gap-2 mb-6 p-1 rounded-xl bg-white/5 border border-white/10 max-w-md">
        <button
          className={`flex-1 chip h-9 justify-center ${mode === 'single' ? 'bg-brand-600 text-white' : 'text-white/60 hover:text-white'}`}
          onClick={() => setMode('single')}
        >
          📝 单章模式
        </button>
        <button
          className={`flex-1 chip h-9 justify-center ${mode === 'book' ? 'bg-brand-600 text-white' : 'text-white/60 hover:text-white'}`}
          onClick={() => setMode('book')}
        >
          📚 整本模式
        </button>
      </div>

      {/* 整本模式 */}
      {mode === 'book' && <BookFlow voices={voices} />}

      {/* 单章模式 */}
      {mode === 'single' && (
        <>
          {step === 1 && (
            <StepInput
              voices={voices}
              busy={busy}
              setBusy={setBusy}
              onDone={pr => {
                setPrepareResult(pr);
                setSynthResult(null);
                setStep(2);
              }}
            />
          )}
          {step === 2 && prepareResult && (
            <StepRoles
              voices={voices}
              prepareResult={prepareResult}
              onBack={() => setStep(1)}
              onNext={() => setStep(3)}
            />
          )}
          {step === 3 && prepareResult && (
            <StepGenerate
              voices={voices}
              prepareResult={prepareResult}
              synthResult={synthResult}
              setSynthResult={setSynthResult}
              busy={busy}
              setBusy={setBusy}
              onBack={() => setStep(2)}
            />
          )}
        </>
      )}

      <footer className="mt-16 text-center text-xs text-white/40">
        ⚠ 仅供本地使用 · 所有 AI 调用都会产生 API 费用
      </footer>
    </div>
  );
}

function Stepper({
  step,
  setStep,
}: {
  step: Step;
  setStep: (s: Step) => void;
}) {
  const items = [
    { n: 1, label: '输入' },
    { n: 2, label: '角色+音色' },
    { n: 3, label: '生成+试听' },
  ];
  return (
    <ol className="hidden sm:flex items-center gap-2 p-1 rounded-xl bg-white/5 border border-white/10">
      {items.map((it, idx) => (
        <li key={it.n} className="flex items-center gap-2">
          <button
            disabled={it.n > step}
            onClick={() => setStep(it.n as Step)}
            className={`chip h-8 min-w-[76px] justify-center ${
              it.n === step
                ? 'bg-brand-600 text-white shadow'
                : it.n < step
                ? 'bg-white/10 text-white/80 hover:bg-white/15'
                : 'text-white/30'
            }`}
          >
            <span className="opacity-70">{it.n}.</span>
            {it.label}
          </button>
          {idx < items.length - 1 && (
            <span className="w-4 h-px bg-white/20" />
          )}
        </li>
      ))}
    </ol>
  );
}
