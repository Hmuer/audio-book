'use client';

import { useMemo, useState, useEffect, useRef, useLayoutEffect } from 'react';
import { createPortal } from 'react-dom';
import { api, Character, PrepareResp, Voice } from '@/lib/api';

export default function StepRoles({
  voices,
  prepareResult,
  onBack,
  onNext,
}: {
  voices: Voice[];
  prepareResult: PrepareResp;
  onBack: () => void;
  onNext: () => void;
}) {
  // 存 narrator voice + 每个角色的 voice
  const narratorDefault = voices.find(v => v.id === 'male-qn-jingying') || voices[0];
  const [narratorVoice, setNarratorVoice] = useState<string>(narratorDefault?.id || '');
  // 语速控制（0.5-2.0，默认 1.0），持久化到 window 供 StepGenerate 读取
  const [speed, setSpeed] = useState<number>(1.0);

  const [charVoices, setCharVoices] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    prepareResult.characters.forEach(c => {
      const rec = prepareResult.voice_recommendations.find(
        r => r.character_name === c.name
      );
      if (rec && voices.some(v => v.id === rec.suggested_voice_id)) {
        init[c.name] = rec.suggested_voice_id;
      } else {
        const fallback = voices.find(v =>
          c.gender === '男'
            ? v.gender === '男声'
            : c.gender === '女'
            ? v.gender === '女声'
            : true
        );
        if (fallback) init[c.name] = fallback.id;
      }
    });
    return init;
  });

  // 音色预览 cache：避免重复试听同一个 voice+text
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);

  const preview = async (voiceId: string, text: string) => {
    setPreviewUrl(null);
    setPreviewing(true);
    try {
      const r = await api.preview(text.slice(0, 80), voiceId, speed);
      setPreviewUrl(r.audio_url);
    } finally {
      setPreviewing(false);
    }
  };

  const voiceById = useMemo(() => {
    const m = new Map<string, Voice>();
    voices.forEach(v => m.set(v.id, v));
    return m;
  }, [voices]);

  return (
    <section className="space-y-6">
      {/* polish diff + warning */}
      <div className="card">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">Step 2 · 纠错结果 + 角色与音色</h2>
          <div className="flex gap-2">
            <button className="btn-outline" onClick={onBack}>← 上一步</button>
            <button className="btn-primary" onClick={onNext}>
              前往合成 →
            </button>
          </div>
        </div>

        {prepareResult.polish_warning && (
          <div className="mb-3 rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
            ⚠️ {prepareResult.polish_warning}
          </div>
        )}

        {prepareResult.diff.length > 0 ? (
          <div className="rounded-xl border border-white/10 bg-white/5 p-4 text-sm">
            <div className="font-semibold mb-2">Diff（共 {prepareResult.diff.length} 处修改，LLM 已评估为合理）</div>
            <ul className="space-y-1 max-h-40 overflow-auto">
              {prepareResult.diff.slice(0, 50).map((d, i) => (
                <li key={i} className="flex gap-2">
                  <span className="chip bg-red-500/20 text-red-300 shrink-0">-{d.old}</span>
                  <span className="chip bg-green-500/20 text-green-300 shrink-0">+{d.new}</span>
                  <span className="text-white/40 text-xs">@{d.position}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="rounded-xl border border-white/10 bg-white/5 p-4 text-sm text-white/60">
            ✅ 未做任何文本修改（原文/无需修改/或修改被 LLM 自我评估驳回）
          </div>
        )}
      </div>

      {/* Narrator */}
      <div className="card">
        <h3 className="font-semibold mb-3">🎙 旁白音色</h3>
        <VoicePicker
          voices={voices}
          value={narratorVoice}
          onChange={setNarratorVoice}
          onPreview={vid => preview(vid, '这是一段旁白示例文本，用于试听所选音色的朗读效果。')}
        />
        {previewing && <div className="text-xs text-white/50 mt-2">TTS 合成中…</div>}
        {previewUrl && (
          <audio controls src={previewUrl} className="mt-2 w-full" />
        )}
      </div>

      {/* 语速控制 */}
      <div className="card">
        <div className="flex items-center justify-between mb-2">
          <h3 className="font-semibold">⚡ 语速控制</h3>
          <span className="chip bg-brand-500/15 text-brand-300 font-mono">
            {speed.toFixed(1)}x
          </span>
        </div>
        <input
          type="range"
          min={0.5}
          max={2.0}
          step={0.1}
          value={speed}
          onChange={e => setSpeed(parseFloat(e.target.value))}
          className="w-full accent-brand-500 cursor-pointer"
        />
        <div className="flex justify-between text-[11px] text-white/40 mt-1">
          <span>0.5x 慢速</span>
          <span>1.0x 正常</span>
          <span>2.0x 快速</span>
        </div>
        <p className="text-xs text-white/50 mt-2">
          调整后试听音色与最终合成都将使用此语速。
        </p>
      </div>

      {/* 角色列表 */}
      <div className="card">
        <h3 className="font-semibold mb-3">
          🧑‍🤝‍🧑 角色列表（{prepareResult.characters.length}）
        </h3>
        <div className="grid md:grid-cols-2 gap-4">
          {prepareResult.characters.map(c => {
            const rec = prepareResult.voice_recommendations.find(
              r => r.character_name === c.name
            );
            return (
              <div
                key={c.name}
                className="rounded-xl border border-white/10 bg-white/5 p-4 space-y-3"
              >
                <CharacterHeader c={c} />
                {rec && (
                  <div className="text-xs text-brand-300/90 rounded-lg bg-brand-500/10 px-3 py-2 border border-brand-500/20">
                    💡 LLM 推荐音色：
                    <b>{voiceById.get(rec.suggested_voice_id)?.name || rec.suggested_voice_id}</b>
                    <div className="text-white/60 mt-1">{rec.reason}</div>
                  </div>
                )}
                <VoicePicker
                  voices={voices}
                  value={charVoices[c.name] || ''}
                  onChange={vid =>
                    setCharVoices(prev => ({ ...prev, [c.name]: vid }))
                  }
                  onPreview={vid => preview(vid, characterSample(c))}
                  compact
                />
              </div>
            );
          })}
        </div>
        {/* 持久化到 window 上，下一步合成直接拿 */}
        <PersistToWindow narrator={narratorVoice} assignments={charVoices} speed={speed} />
      </div>
    </section>
  );
}

function PersistToWindow({
  narrator,
  assignments,
  speed,
}: {
  narrator: string;
  assignments: Record<string, string>;
  speed: number;
}) {
  useEffect(() => {
    (window as any).__novel_voices = { narrator, assignments, speed };
  }, [narrator, assignments, speed]);
  return null;
}

function characterSample(c: Character): string {
  if (c.gender === '男') return `你好，我是${c.name}。${c.personality || ''}。`;
  if (c.gender === '女') return `我是${c.name}，很高兴认识你。`;
  return `${c.name} 角色试听文本示例。`;
}

function CharacterHeader({ c }: { c: Character }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div>
        <div className="text-base font-semibold">{c.name}</div>
        <div className="text-xs text-white/60 mt-1">{c.age || '年龄未知'} · {c.personality || '—'}</div>
      </div>
      <span
        className={`chip ${
          c.gender === '男'
            ? 'bg-blue-500/20 text-blue-300'
            : c.gender === '女'
            ? 'bg-pink-500/20 text-pink-300'
            : 'bg-white/10 text-white/70'
        }`}
      >
        {c.gender}
      </span>
    </div>
  );
}

function VoicePicker({
  voices,
  value,
  onChange,
  onPreview,
  compact = false,
}: {
  voices: Voice[];
  value: string;
  onChange: (id: string) => void;
  onPreview: (id: string) => void;
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelIdRef = useRef(`__voice_picker_panel_${Math.random().toString(36).slice(2)}`);
  const [coords, setCoords] = useState<{ top: number; left: number; width: number } | null>(null);

  const groups = useMemo(() => {
    const g: Record<string, Voice[]> = { 男声: [], 女声: [], 中性: [] };
    voices.forEach(v => {
      const key = (['男声', '女声', '中性'].includes(v.gender) ? v.gender : '中性') as string;
      if (!g[key]) g[key] = [];
      if (
        !q ||
        v.name.includes(q) ||
        v.id.includes(q) ||
        v.description.includes(q)
      )
        g[key].push(v);
    });
    return g;
  }, [voices, q]);

  const selected = voices.find(v => v.id === value);

  // 测量触发按钮位置，供 fixed 浮层定位
  useLayoutEffect(() => {
    if (!open || !btnRef.current) return;
    const update = () => {
      const r = btnRef.current?.getBoundingClientRect();
      if (r) setCoords({ top: r.bottom + 4, left: r.left, width: r.width });
    };
    update();
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update, true);
      window.removeEventListener('resize', update);
    };
  }, [open]);

  // 点击浮层外关闭
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const target = e.target as Node;
      if (btnRef.current?.contains(target)) return;
      // 浮层自身在 portal 中，无法用 ref 直接比对，靠 data 属性
      const panel = document.getElementById(panelIdRef.current);
      if (panel && panel.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  return (
    <div className="relative">
      <div className="flex items-center gap-2">
        <button
          ref={btnRef}
          onClick={() => setOpen(v => !v)}
          className={`${compact ? 'flex-1' : 'w-full'} text-left rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-sm`}
        >
          {selected ? (
            <span>
              🎵 <b>{selected.name}</b>
              <span className="text-white/50 ml-2 text-xs">
                {selected.gender} · {selected.description.slice(0, 20)}…
              </span>
            </span>
          ) : (
            <span className="text-white/50">请选择音色…</span>
          )}
        </button>
        <button
          className="btn-ghost shrink-0"
          onClick={() => selected && onPreview(selected.id)}
          disabled={!selected}
          title="试听"
        >
          ▶
        </button>
      </div>
      {open && coords && typeof document !== 'undefined' && createPortal(
        <div
          id={panelIdRef.current}
          style={{
            position: 'fixed',
            top: `${coords.top}px`,
            left: `${coords.left}px`,
            width: `${coords.width}px`,
            zIndex: 9999,
          }}
          className="rounded-2xl border border-white/10 bg-gray-950 p-3 shadow-2xl max-h-[420px] overflow-hidden flex flex-col"
        >
          <input
            autoFocus
            className="input mb-2"
            placeholder="搜索音色（名称/标签）"
            value={q}
            onChange={e => setQ(e.target.value)}
          />
          <div className="overflow-auto pr-1 space-y-3">
            {Object.entries(groups).map(([k, list]) =>
              list.length > 0 ? (
                <div key={k}>
                  <div className="text-xs uppercase tracking-wider text-white/40 mb-1 px-1">
                    {k} ({list.length})
                  </div>
                  <div className={`grid ${compact ? 'gap-1' : 'gap-2'}`}>
                    {list.map(v => (
                      <button
                        key={v.id}
                        onClick={() => {
                          onChange(v.id);
                          setOpen(false);
                        }}
                        className={`text-left rounded-lg px-3 py-2 border transition text-sm ${
                          v.id === value
                            ? 'bg-brand-600/20 border-brand-500/60'
                            : 'bg-white/5 border-white/5 hover:bg-white/10'
                        }`}
                      >
                        <div className="flex justify-between gap-2">
                          <span className="font-medium">{v.name}</span>
                          <button
                            className="opacity-70 hover:opacity-100 text-xs"
                            onClick={e => {
                              e.stopPropagation();
                              onPreview(v.id);
                            }}
                          >
                            ▶
                          </button>
                        </div>
                        <div className="text-[11px] text-white/50 line-clamp-1 mt-0.5">
                          {v.description}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              ) : null
            )}
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
