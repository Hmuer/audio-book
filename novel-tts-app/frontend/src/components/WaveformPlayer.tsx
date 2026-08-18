'use client';

import { useEffect, useRef, useState, useCallback } from 'react';

/**
 * 纯 CSS 波形播放器（借鉴 Qwen3-TTS-WebUI 的 WaveformPlayer 设计）
 * 不依赖 wavesurfer.js，用 N 根 div 条模拟波形 + HTML5 audio 播放。
 *
 * 特性：
 * - 40 根波形条，播放时从左到右渐变高亮
 * - 主题感知配色（暗色紫 / 亮色靛蓝）
 * - IntersectionObserver 懒加载（借鉴 Qwen3 LazyAudioPlayer）
 * - 下载按钮 ghost icon 放右侧
 */

const BAR_COUNT = 40;
// 预生成随机高度（伪波形），0.2~1.0 之间
const BAR_HEIGHTS = Array.from({ length: BAR_COUNT }, () => 0.2 + Math.random() * 0.8);

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

  // 懒加载：进入视口才挂载 audio 元素（借鉴 Qwen3 LazyAudioPlayer）
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
      { rootMargin: '120px' }
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

  // autoPlay 时进入视口自动播放
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

  return (
    <div ref={containerRef} className="flex items-center gap-3 w-full">
      {/* 播放/暂停按钮 */}
      <button
        onClick={togglePlay}
        disabled={!src}
        className={`shrink-0 grid place-items-center rounded-full transition
          ${compact ? 'w-8 h-8' : 'w-10 h-10'}
          ${isPlaying
            ? 'bg-brand-500/20 ring-2 ring-brand-500/50'
            : 'bg-white/10 hover:bg-white/20'
          } disabled:opacity-30`}
        title={isPlaying ? '暂停' : '播放'}
      >
        {isPlaying ? (
          <span className={compact ? 'text-xs' : 'text-sm'}>⏸</span>
        ) : (
          <span className={compact ? 'text-xs' : 'text-sm'}>▶</span>
        )}
      </button>

      {/* 波形条 */}
      <div className="flex-1 flex items-center gap-[2px] min-w-0" style={{ height: compact ? 24 : 36 }}>
        {BAR_HEIGHTS.map((h, i) => (
          <div
            key={i}
            className="flex-1 rounded-full transition-colors duration-150"
            style={{
              height: `${h * 100}%`,
              minHeight: '3px',
              backgroundColor: i <= activeBar
                ? 'var(--wave-progress, #a78bfa)'
                : 'var(--wave-bar, #3f3f46)',
            }}
          />
        ))}
      </div>

      {/* 时间 */}
      <span className="text-xs text-white/50 tabular-nums shrink-0">
        {fmtTime(progress * duration)} / {fmtTime(duration)}
      </span>

      {/* 下载按钮 */}
      {onDownload && (
        <button
          onClick={onDownload}
          className="shrink-0 btn-ghost text-xs py-1 px-2"
          title="下载 MP3"
        >
          ⬇
        </button>
      )}

      {/* 懒挂载的 audio 元素 */}
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

      <style>{`
        :root {
          --wave-bar: #3f3f46;
          --wave-progress: #a78bfa;
        }
        html.light {
          --wave-bar: #d1d5db;
          --wave-progress: #7c3aed;
        }
      `}</style>
    </div>
  );
}
