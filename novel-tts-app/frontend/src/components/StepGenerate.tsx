'use client';

import { useEffect, useMemo, useState } from 'react';
import { api, PrepareResp, SynthResp, SynthSegment, Voice } from '@/lib/api';

export default function StepGenerate({
  voices,
  prepareResult,
  synthResult,
  setSynthResult,
  busy,
  setBusy,
  onBack,
}: {
  voices: Voice[];
  prepareResult: PrepareResp;
  synthResult: SynthResp | null;
  setSynthResult: (s: SynthResp | null) => void;
  busy: boolean;
  setBusy: (b: boolean) => void;
  onBack: () => void;
}) {
  const [err, setErr] = useState<string | null>(null);
  const [segmentOverrides, setSegmentOverrides] = useState<Record<number, string>>({});
  const [previewAudio, setPreviewAudio] = useState<string | null>(null);

  const { narrator, assignments }: any = useMemo(() => {
    const v = (typeof window !== 'undefined' && (window as any).__novel_voices) || {};
    return v;
  }, []);

  const narratorVoiceId =
    narrator || voices.find(v => v.id === 'neutral_03')?.id || voices[0]?.id || '';

  const dialoguesOnly = useMemo(
    () => prepareResult.dialogue_attributions.filter(d => d.speaker),
    [prepareResult]
  );

  const runSynthesize = async () => {
    setErr(null);
    setBusy(true);
    try {
      // 确保所有角色都有 voice assignment
      const va: Record<string, string> = { ...(assignments || {}) };
      prepareResult.characters.forEach(c => {
        if (!va[c.name]) {
          const fb = voices.find(v =>
            c.gender === '男'
              ? v.gender === '男声'
              : c.gender === '女'
              ? v.gender === '女声'
              : true
          );
          if (fb) va[c.name] = fb.id;
        }
      });
      const r = await api.synthesize({
        job_id: prepareResult.job_id,
        voice_assignments: va,
        narrator_voice_id: narratorVoiceId,
        segment_overrides: Object.keys(segmentOverrides).length
          ? segmentOverrides
          : undefined,
      });
      setSynthResult(r);
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  const narratorSegments =
    synthResult?.segments.filter(s => s.kind === 'narrator') || [];
  const dialogueSegments =
    synthResult?.segments.filter(s => s.kind === 'dialogue') || [];

  return (
    <section className="space-y-6">
      <div className="card flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Step 3 · 合成与试听</h2>
          <p className="text-sm text-white/60 mt-1">
            对白共 {dialoguesOnly.length} 段 · 章节 {prepareResult.chapters.length} · 旁白与对白交替合成
          </p>
        </div>
        <div className="flex gap-2">
          <button className="btn-outline" onClick={onBack}>← 调整音色</button>
          <button
            className="btn-primary"
            onClick={runSynthesize}
            disabled={busy || !narratorVoiceId}
          >
            {busy ? '🎙 合成中（可能几分钟）…' : '🔊 开始合成 MP3'}
          </button>
        </div>
      </div>

      {err && (
        <div className="card border-red-500/30 bg-red-500/5">
          <div className="text-sm text-red-300">❌ {err}</div>
        </div>
      )}

      {/* 对白卡片列表 + 每段独立选音色 */}
      <div className="card space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">💬 对白列表（{dialoguesOnly.length} 段）</h3>
          <span className="text-xs text-white/50">
            橙色边框 = 低置信度（&lt; 0.7），建议人工复核 speaker
          </span>
        </div>
        <div className="grid md:grid-cols-2 gap-3 max-h-[520px] overflow-auto pr-2">
          {prepareResult.dialogue_attributions.map((d, i) => {
            const synced = dialogueSegments.find(
              s => s.kind === 'dialogue' && s.text === d.text
            );
            const lowConf = d.confidence < 0.7;
            return (
              <div
                key={i}
                className={`rounded-xl border p-3 ${
                  lowConf
                    ? 'border-amber-500/50 bg-amber-500/5 ring-1 ring-amber-500/30'
                    : 'border-white/10 bg-white/5'
                }`}
              >
                <div className="flex items-start justify-between gap-2 mb-2">
                  <div>
                    <span className="chip bg-white/10">Speaker: <b>{d.speaker}</b></span>
                    <span
                      className={`chip ml-1 ${
                        lowConf ? 'bg-amber-500/20 text-amber-300' : 'bg-white/10'
                      }`}
                    >
                      置信度 {d.confidence.toFixed(2)}
                    </span>
                  </div>
                  {synced?.audio_url && (
                    <button
                      className="btn-ghost text-xs py-1 px-2"
                      onClick={() => setPreviewAudio(synced.audio_url!)}
                    >
                      ▶ 试听
                    </button>
                  )}
                </div>
                <div className="text-sm border-l-2 border-brand-500/60 pl-3 my-2 italic text-white/80">
                  「{d.text}」
                </div>
                <label className="text-xs text-white/50 block mb-1">
                  段级音色覆盖（可选，优先级最高）：
                </label>
                <select
                  className="text-sm"
                  value={segmentOverrides[i] || ''}
                  onChange={e =>
                    setSegmentOverrides(prev => ({
                      ...prev,
                      [i]: e.target.value,
                    }))
                  }
                >
                  <option value="">（默认使用角色音色）</option>
                  {voices.map(v => (
                    <option key={v.id} value={v.id}>
                      {v.name} · {v.gender}
                    </option>
                  ))}
                </select>
              </div>
            );
          })}
        </div>
      </div>

      {/* 旁白折叠区 */}
      <details className="card">
        <summary className="cursor-pointer font-semibold select-none">
          📖 旁白折叠区（共 {narratorSegments.length} 段）
        </summary>
        <div className="mt-3 space-y-2 text-sm text-white/75 max-h-[300px] overflow-auto pr-2">
          {(synthResult
            ? synthResult.segments.filter(s => s.kind === 'narrator')
            : ([{ idx: 0, text: '合成后显示每段旁白及对应音频' }] as any)
          ).map((s: any, i: number) => (
            <div
              key={i}
              className="rounded-lg border border-white/10 bg-white/5 p-3"
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs text-white/50">旁白段 #{s.idx ?? i}</span>
                {s.audio_url && (
                  <button
                    className="text-xs btn-ghost py-0.5 px-2"
                    onClick={() => setPreviewAudio(s.audio_url)}
                  >
                    ▶
                  </button>
                )}
              </div>
              <div className="line-clamp-3">{s.text}</div>
            </div>
          ))}
        </div>
      </details>

      {/* 合成结果 + 下载 */}
      {synthResult && (
        <div className="card border-brand-500/40 bg-brand-500/5 space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-3">
            <div>
              <div className="text-lg font-semibold">🎉 合成完成</div>
              <div className="text-sm text-white/60">
                总时长 {synthResult.duration_sec.toFixed(1)}s · 共{' '}
                {synthResult.segments.length} 段 · 文件名 {synthResult.audio_filename}
              </div>
            </div>
            <a
              className="btn-primary"
              href={synthResult.audio_url}
              download={`novel_${prepareResult.job_id}.mp3`}
            >
              ⬇️ 下载 MP3
            </a>
          </div>
          <audio controls src={synthResult.audio_url} className="w-full" />
          {previewAudio && previewAudio !== synthResult.audio_url && (
            <div className="pt-3 border-t border-white/10">
              <div className="text-xs text-white/60 mb-1">段级试听：</div>
              <audio controls src={previewAudio} className="w-full" />
            </div>
          )}
        </div>
      )}
      {!synthResult && previewAudio && (
        <div className="card">
          <audio controls src={previewAudio} className="w-full" autoPlay />
        </div>
      )}
    </section>
  );
}
