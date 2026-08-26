'use client';

import { useEffect, useRef, useState, useCallback } from 'react';

/**
 * ElevenLabs 风格的极简波形播放器
 * 设计要点：
 *  - 圆形播放按钮：品牌紫渐变 + 悬浮微抬升 + 激活外环发光
 *  - 波形条：非对称圆弧顶部（Eleven 特有的「融化」风格）
 *  - 播放进度：左侧紫色渐变 + 右侧柔和灰，播放到哪里颜色就过渡到哪里
 *  - 右侧：时间 + 下载 icon 小按钮，统一 chip 风格
 */

const BAR_COUNT = 52;

// 伪波形：生成一批非对称高度，Eleven 风格中间高两端稍低，带轻微随机
const BAR_HEIGHTS = Array.from({ length: BAR_COUNT }, (_, i) => {
  // 中心钟形曲线 + 抖动
  const bell = Math.sin((i / BAR_COUNT) * Math.PI);
  const base = 0.25 + bell * 0.55;
  const jitter = (Math.sin(i * 1.7) + Math.cos(i * 0.9)) * 0.08;
  return Math.max(0.18, Math.min(1, base + jitter));
});

export default function WaveformPlayer({
  src,
  onDownload,
  compact = false,
  autoPlay = false,
}: {
  src: string | null;
  onDownload?: () => void;
  compact?: boolean;
  autoPlay?: boolean;
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [progress, setProgress] = useState(0); // 0~1
  const [duration, setDuration] = useState(0);
  const [loaded, setLoaded] = useState(false);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const [inView, setInView] = useState(false);

  useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    const obs = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            setInView(true);
            obs.disconnect();
          }
        });
      },
      { rootMargin: '160px' }
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  const togglePlay = useCallback(() => {
    if (!audioRef.current) return;
    if (isPlaying) {
      audioRef.current.pause();
    } else {
      audioRef.current.play().catch(() => {});
    }
  }, [isPlaying]);

  useEffect(() => {
    if (inView && autoPlay && audioRef.current && !isPlaying) {
      audioRef.current.play().catch(() => {});
    }
  }, [inView, autoPlay, isPlaying]);

  const fmtTime = (s: number) => {
    if (!s || !isFinite(s)) return '0:00';
    const m = Math.floor(s / 60);
    const sec = Math.round(s % 60);
    return `${m}:${sec.toString().padStart(2, '0')}`;
  };

  const activeBar = Math.floor(progress * BAR_COUNT);
  const barHeightPx = compact ? 22 : 34;
  const playSize = compact ? 'w-9 h-9' : 'w-11 h-11';
  const iconSize = compact ? 'text-[11px]' : 'text-xs';

  return (
    <div
      ref={containerRef}
      className={`group flex items-center gap-3 w-full rounded-2xl px-3 py-2.5
        border border-white/[0.05] bg-white/[0.02]
        hover:border-white/[0.10] hover:bg-white/[0.035]
        transition-all duration-200`}
    >
      {/* 播放/暂停按钮 — Eleven 圆形品牌紫渐变 */}
      <button
        onClick={togglePlay}
        disabled={!src}
        className={`relative shrink-0 grid place-items-center rounded-full transition-all duration-200
          ${playSize}
          ${isPlaying
            ? 'ring-2 ring-brand-500/40 ring-offset-2 ring-offset-ink-100'
            : ''
          }
          disabled:opacity-40 disabled:cursor-not-allowed`}
        style={{
          backgroundImage: isPlaying
            ? 'linear-gradient(180deg, rgba(255,255,255,0.18) 0%, rgba(255,255,255,0) 50%), linear-gradient(135deg, #9b6bff 0%, #8b5cf6 100%)'
            : 'linear-gradient(180deg, rgba(255,255,255,0.12) 0%, rgba(255,255,255,0) 50%), linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)',
          boxShadow: isPlaying
            ? '0 0 0 1px rgba(168,85,247,0.45), 0 8px 20px -6px rgba(139,92,246,0.55)'
            : '0 0 0 1px rgba(168,85,247,0.35), 0 6px 18px -8px rgba(139,92,246,0.45)',
        }}
        title={isPlaying ? '暂停' : '播放'}
      >
        <span className={`${iconSize} text-white font-bold translate-x-[1px]`}>
          {isPlaying ? '❚❚' : '▶'}
        </span>
        {isPlaying && (
          <span
            className="absolute inset-0 rounded-full animate-ping pointer-events-none"
            style={{
              background: 'rgba(139,92,246,0.25)',
              animationDuration: '1.8s',
            }}
          />
        )}
      </button>

      {/* 波形区 */}
      <div
        className="relative flex-1 flex items-end gap-[2px] min-w-0"
        style={{ height: barHeightPx }}
      >
        {BAR_HEIGHTS.map((h, i) => {
          const played = i <= activeBar;
          // Eleven 风格：波形条不是平直的方柱，而是顶部带小圆弧，底部固定
          const heightPct = h * 100;
          return (
            <div
              key={i}
              className="flex-1 rounded-full transition-[background-color,box-shadow] duration-150"
              style={{
                height: `${heightPct}%`,
                minHeight: '2px',
                background: played
                  ? `linear-gradient(180deg, #c4b5fd 0%, #8b5cf6 55%, #6d28d9 100%)`
                  : 'rgba(255,255,255,0.12)',
                boxShadow: played
                  ? '0 0 6px 0 rgba(168,85,247,0.35)'
                  : 'none',
              }}
            />
          );
        })}
      </div>

      {/* 时间：Eleven 使用 tabular 数字 + 弱化 */}
      <span className="text-[11px] tabular-nums shrink-0 w-[78px] text-right text-ink-600">
        {fmtTime(progress * duration)}
        <span className="mx-1 opacity-40">/</span>
        {fmtTime(duration)}
      </span>

      {/* 下载按钮：Eleven 风格的小 chip */}
      {onDownload && (
        <button
          onClick={onDownload}
          className="shrink-0 inline-flex items-center justify-center rounded-[10px]
            border border-white/[0.06] bg-white/[0.03] text-ink-700
            hover:border-brand-500/30 hover:bg-brand-500/10 hover:text-brand-300
            transition-all duration-150"
          style={{
            width: compact ? 30 : 34,
            height: compact ? 30 : 34,
          }}
          title="下载 MP3"
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M12 3v12" />
            <path d="m7 10 5 5 5-5" />
            <path d="M5 21h14" />
          </svg>
        </button>
      )}

      {inView && src && (
        <audio
          ref={audioRef}
          src={src}
          preload="metadata"
          onLoadedMetadata={(e) => {
            setDuration(e.currentTarget.duration || 0);
            setLoaded(true);
          }}
          onTimeUpdate={(e) => {
            const d = e.currentTarget.duration;
            setProgress(d > 0 ? e.currentTarget.currentTime / d : 0);
          }}
          onPlay={() => setIsPlaying(true)}
          onPause={() => setIsPlaying(false)}
          onEnded={() => {
            setIsPlaying(false);
            setProgress(0);
          }}
        />
      )}
    </div>
  );
}
