'use client';

import { useMemo, useState, useRef, useEffect } from 'react';
import { api, PrepareResp, SynthResp, SynthSegment, Voice } from '@/lib/api';
import VoicePicker from './VoicePicker';

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

  // 合成结果卡的 ref，用于自动滚动
  const resultRef = useRef<HTMLDivElement>(null);

  // 试听播放管理（与 StepRoles 类似的统一 audio 元素）
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playingVoice, setPlayingVoice] = useState<string | null>(null);
  const [loadingVoice, setLoadingVoice] = useState<string | null>(null);
  const urlCacheRef = useRef<Map<string, { url: string; text: string; speed: number }>>(new Map());
  const synthSpeed = (typeof window !== 'undefined' && (window as any).__novel_voices?.speed) || 1.0;

  const isPlaying = (vid: string) => playingVoice === vid;
  const isLoading = (vid: string) => loadingVoice === vid;

  const stopPlayback = () => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
    setPlayingVoice(null);
  };

  // 直接播放已有 URL（用于合成后段级试听）
  const playUrl = (key: string, url: string) => {
    if (!audioRef.current) return;
    audioRef.current.src = url;
    audioRef.current.onended = () => setPlayingVoice(null);
    audioRef.current.onerror = () => setPlayingVoice(null);
    setPlayingVoice(key);
    audioRef.current.play().catch(() => setPlayingVoice(null));
  };

  const togglePlay = async (voiceId: string, text: string) => {
    if (playingVoice === voiceId) {
      stopPlayback();
      return;
    }
    stopPlayback();
    const sampleKey = `${voiceId}|${text}|${synthSpeed}`;
    const cached = urlCacheRef.current.get(sampleKey);
    if (cached) {
      playUrl(voiceId, cached.url);
      return;
    }
    setLoadingVoice(voiceId);
    try {
      const r = await api.preview(text.slice(0, 80), voiceId, synthSpeed);
      urlCacheRef.current.set(sampleKey, { url: r.audio_url, text, speed: synthSpeed });
      playUrl(voiceId, r.audio_url);
    } finally {
      setLoadingVoice(prev => (prev === voiceId ? null : prev));
    }
  };

  // 合成后段级试听：已有 URL，直接播放/停止
  const togglePlaySegment = (segKey: string, url: string) => {
    if (playingVoice === segKey) {
      stopPlayback();
      return;
    }
    stopPlayback();
    playUrl(segKey, url);
  };

  // 从 window 读取 Step 2 中选择的音色与语速
  const { narrator, assignments }: any =
    (typeof window !== 'undefined' && (window as any).__novel_voices) || {};

  const narratorVoiceId =
    narrator || voices.find(v => v.id === 'male-qn-jingying')?.id || voices[0]?.id || '';

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
        speed: synthSpeed,
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

  // 合成完成后自动滚动到结果卡
  useEffect(() => {
    if (synthResult && resultRef.current) {
      resultRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [synthResult]);

  return (
    <section className="space-y-6">
      <div className="card flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Step 3 · 合成与试听</h2>
          <p className="text-sm text-white/60 mt-1">
            对白共 {dialoguesOnly.length} 段 · 章节 {prepareResult.chapters.length} · 旁白与对白交替合成 · 语速 {synthSpeed.toFixed(1)}x
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

      {/* 合成结果 + 下载（置顶，合成完成后自动滚动到此） */}
      {synthResult && (
        <div
          ref={resultRef}
          className="card border-brand-500/40 bg-brand-500/5 space-y-3 sticky top-4 z-10"
        >
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
            const segKey = `dialogue_${i}`;
            const segPlaying = isPlaying(segKey);
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
                      className={`btn-ghost text-xs py-1 px-2 ${segPlaying ? 'ring-2 ring-brand-500/50 text-brand-300' : ''}`}
                      onClick={() => togglePlaySegment(segKey, synced.audio_url!)}
                      title={segPlaying ? '停止播放' : '试听'}
                    >
                      {segPlaying ? '⏸ 试听中' : '▶ 试听'}
                    </button>
                  )}
                </div>
                <div className="text-sm border-l-2 border-brand-500/60 pl-3 my-2 italic text-white/80">
                  「{d.text}」
                </div>
                <label className="text-xs text-white/50 block mb-1">
                  段级音色覆盖（可选，优先级最高）：
                </label>
                <VoicePicker
                  voices={voices}
                  value={segmentOverrides[i] || ''}
                  onChange={vid =>
                    setSegmentOverrides(prev => ({ ...prev, [i]: vid }))
                  }
                  onPreview={vid => togglePlay(vid, d.text)}
                  isPlaying={isPlaying(segmentOverrides[i])}
                  isLoading={isLoading(segmentOverrides[i])}
                  playingVoiceId={playingVoice}
                  loadingVoiceId={loadingVoice}
                  compact
                />
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
          ).map((s: any, i: number) => {
            const narKey = `narrator_${s.idx ?? i}`;
            const narPlaying = isPlaying(narKey);
            return (
            <div
              key={i}
              className="rounded-lg border border-white/10 bg-white/5 p-3"
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs text-white/50">旁白段 #{s.idx ?? i}</span>
                {s.audio_url && (
                  <button
                    className={`text-xs btn-ghost py-0.5 px-2 ${narPlaying ? 'ring-2 ring-brand-500/50 text-brand-300' : ''}`}
                    onClick={() => togglePlaySegment(narKey, s.audio_url)}
                    title={narPlaying ? '停止播放' : '试听'}
                  >
                    {narPlaying ? '⏸' : '▶'}
                  </button>
                )}
              </div>
              <div className="line-clamp-3">{s.text}</div>
            </div>
            );
          })}
        </div>
      </details>

      {/* 全局隐藏 audio 元素：统一播放管理 */}
      <audio ref={audioRef} style={{ display: 'none' }} />
    </section>
  );
}
