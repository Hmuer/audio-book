'use client';

import { useMemo, useState, useEffect, useRef } from 'react';
import { api, Character, PrepareResp, Voice } from '@/lib/api';
import VoicePicker from './VoicePicker';

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

  // 试听状态：统一用一个隐藏 audio 元素管理播放
  //   - playingVoice: 当前正在播放的 voiceId（用于按钮显示 ⏸）
  //   - loadingVoice: 正在等待 TTS 返回 URL 的 voiceId（用于按钮显示 loading）
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playingVoice, setPlayingVoice] = useState<string | null>(null);
  const [loadingVoice, setLoadingVoice] = useState<string | null>(null);
  // URL 缓存：voiceId -> {url, text, speed}，text+speed 变化时重新合成
  const urlCacheRef = useRef<Map<string, { url: string; text: string; speed: number }>>(new Map());

  const isPlaying = (vid: string) => playingVoice === vid;
  const isLoading = (vid: string) => loadingVoice === vid;

  const stopPlayback = () => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
    setPlayingVoice(null);
  };

  const togglePlay = async (voiceId: string, text: string) => {
    // 如果正在播放同一个，点击就暂停
    if (playingVoice === voiceId) {
      stopPlayback();
      return;
    }
    // 否则停止当前播放，开始新的
    stopPlayback();

    // 检查缓存
    const sampleKey = `${voiceId}|${text}|${speed}`;
    const cached = urlCacheRef.current.get(sampleKey);
    if (cached) {
      playUrl(voiceId, cached.url);
      return;
    }

    // 调用后端合成
    setLoadingVoice(voiceId);
    try {
      const r = await api.preview(text.slice(0, 80), voiceId, speed);
      urlCacheRef.current.set(sampleKey, { url: r.audio_url, text, speed });
      playUrl(voiceId, r.audio_url);
    } finally {
      setLoadingVoice(prev => (prev === voiceId ? null : prev));
    }
  };

  const playUrl = (voiceId: string, url: string) => {
    if (!audioRef.current) return;
    audioRef.current.src = url;
    audioRef.current.onended = () => setPlayingVoice(null);
    audioRef.current.onerror = () => setPlayingVoice(null);
    setPlayingVoice(voiceId);
    audioRef.current.play().catch(() => {
      setPlayingVoice(null);
    });
  };

  // 兼容旧的 preview() 函数（其他地方若有调用）
  const preview = togglePlay;

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
          onPreview={vid => togglePlay(vid, '这是一段旁白示例文本，用于试听所选音色的朗读效果。')}
          isPlaying={isPlaying(narratorVoice)}
          isLoading={isLoading(narratorVoice)}
          playingVoiceId={playingVoice}
          loadingVoiceId={loadingVoice}
        />
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
                  onPreview={vid => togglePlay(vid, characterSample(c))}
                  isPlaying={isPlaying(charVoices[c.name])}
                  isLoading={isLoading(charVoices[c.name])}
                  playingVoiceId={playingVoice}
                  loadingVoiceId={loadingVoice}
                  compact
                />
              </div>
            );
          })}
        </div>
        {/* 持久化到 window 上，下一步合成直接拿 */}
        <PersistToWindow narrator={narratorVoice} assignments={charVoices} speed={speed} />
      </div>
      {/* 全局隐藏 audio 元素：统一播放管理 */}
      <audio ref={audioRef} style={{ display: 'none' }} />
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
