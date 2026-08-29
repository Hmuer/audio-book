'use client';

import { useEffect, useMemo, useRef, useState, useCallback } from 'react';

/**
 * 阿布平台的标准音频播放器。
 *
 * 功能：
 *  - 播放 / 暂停（圆形渐变按钮 + 发光环）
 *  - 进度条：可点击跳转、可拖动 seek，已播放部分紫色渐变 + 进度光晕
 *  - 时间显示：当前时间 / 总时长
 *  - 音量控制：滑块 + 一键静音，悬停展开，保留上次音量
 *  - 倍速：0.75x / 1.0x / 1.25x / 1.5x / 2.0x
 *  - 下载按钮（可选）
 *
 * 设计风格：ElevenLabs + Spotify 混合，深色玻璃感、紫色品牌色。
 */

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
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [loaded, setLoaded] = useState(false);

  const [volume, setVolume] = useState<number>(0.8);
  const [lastVolume, setLastVolume] = useState<number>(0.8);
  const [muted, setMuted] = useState(false);

  const [speed, setSpeed] = useState<number>(1.0);
  const [showSpeedMenu, setShowSpeedMenu] = useState(false);

  // 进度条拖动
  const [dragging, setDragging] = useState(false);
  const dragPercentRef = useRef<number | null>(null);
  const durationRef = useRef(0);              // 避免 useCallback 闭包陈旧
  const wasPlayingBeforeDragRef = useRef(false);
  const trackRef = useRef<HTMLDivElement | null>(null);

  // 同步 durationRef（seek 时只读 ref，避免依赖 state 造成陈旧闭包）
  useEffect(() => {
    durationRef.current = duration;
  }, [duration]);

  // 进入可视区才加载 audio DOM（性能 + 可支持 autoPlay）
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
      { rootMargin: '200px' }
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  const fmtTime = (s: number) => {
    if (!s || !isFinite(s)) return '0:00';
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = Math.round(s % 60);
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${sec.toString().padStart(2, '0')}`;
    return `${m}:${sec.toString().padStart(2, '0')}`;
  };

  // 播放 / 暂停
  const togglePlay = useCallback(() => {
    if (!audioRef.current || !src) return;
    if (isPlaying) {
      audioRef.current.pause();
    } else {
      audioRef.current.play().catch(() => {});
    }
  }, [isPlaying, src]);

  // 初始化音量 / 倍速
  useEffect(() => {
    if (!audioRef.current) return;
    audioRef.current.volume = muted ? 0 : volume;
  }, [volume, muted, inView, src]);

  useEffect(() => {
    if (!audioRef.current) return;
    audioRef.current.playbackRate = speed;
  }, [speed, inView, src]);

  // 自动播放
  useEffect(() => {
    if (inView && autoPlay && audioRef.current && !isPlaying && src) {
      audioRef.current.play().catch(() => {});
    }
  }, [inView, autoPlay, isPlaying, src]);

  // seek：把百分比转为实际位置。注意：必须直接从 audioRef.duration / durationRef 读取，
  // 避免 useCallback(duration) 陈旧闭包，否则 pointer 事件捕获的 duration=0 会让 seek 被 early return。
  const seekByPercent = useCallback((pct: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const dur = Number.isFinite(audio.duration) && audio.duration > 0
      ? audio.duration
      : durationRef.current;
    if (!dur || dur <= 0) return;
    const clamped = Math.max(0, Math.min(1, pct));
    const target = clamped * dur;
    try {
      audio.currentTime = target;
    } catch (err) {
      // 某些浏览器在元数据未加载完写 currentTime 会抛错（DOM Exception），吞掉不影响体验
      console.warn('[WaveformPlayer] seek 失败', err);
    }
    setCurrentTime(target);
  }, []);

  // 进度条拖动处理
  const computePercentFromClientX = useCallback((clientX: number): number => {
    if (!trackRef.current) return 0;
    const rect = trackRef.current.getBoundingClientRect();
    if (rect.width <= 0) return 0;
    const pct = (clientX - rect.left) / rect.width;
    return Math.max(0, Math.min(1, pct));
  }, []);

  const onTrackPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!src) return;
    e.preventDefault();
    (e.target as Element).setPointerCapture?.(e.pointerId);
    const pct = computePercentFromClientX(e.clientX);
    dragPercentRef.current = pct;
    wasPlayingBeforeDragRef.current = !!audioRef.current && !audioRef.current.paused;
    setDragging(true);
    // 按下时先更新 UI 预览；是否 seek 立即执行交给 seekByPercent（内部已经能正确处理 duration 未知场景）
    // 但这里不 seek，按下只是进入拖动态；短按（未移动）的 seek 交给 pointerUp 统一提交，
    // 这样可以避免"在 duration 还是 0 时 seek"造成的错觉。
    const dur = durationRef.current || (audioRef.current?.duration as number);
    if (Number.isFinite(dur) && dur > 0) {
      setCurrentTime(pct * dur);
    }
  };

  const onTrackPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    const pct = computePercentFromClientX(e.clientX);
    dragPercentRef.current = pct;
    // 拖动过程里，只更新显示位置（避免反复 seek 卡顿）
    const dur = durationRef.current || (audioRef.current?.duration as number);
    if (Number.isFinite(dur) && dur > 0) {
      setCurrentTime(pct * dur);
    }
  };

  const onTrackPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    (e.target as Element).releasePointerCapture?.(e.pointerId);
    const pct = dragPercentRef.current ?? computePercentFromClientX(e.clientX);
    dragPercentRef.current = null;
    setDragging(false);
    // 真正的 seek 只在释放时提交一次
    seekByPercent(pct);
  };

  // 音量：点静音图标切换，记录上次音量
  const toggleMute = () => {
    if (muted) {
      setMuted(false);
      setVolume(lastVolume > 0 ? lastVolume : 0.6);
    } else {
      setLastVolume(volume);
      setMuted(true);
      setVolume(0);
    }
  };

  const SPEEDS = [0.75, 1, 1.25, 1.5, 2];

  // 显示用的进度（%）。拖动时使用正在拖的值，否则使用 currentTime
  const displayPercent = useMemo(() => {
    if (dragPercentRef.current != null) return dragPercentRef.current;
    return duration > 0 ? currentTime / duration : 0;
  }, [currentTime, duration]);

  const gradientStyle = {
    background: `linear-gradient(90deg,
      #c4b5fd 0%,
      #a78bfa 40%,
      #7c3aed 100%)
    `,
  };

  // 静音图标
  const VolumeIcon = () => {
    if (muted || volume === 0) {
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
          <line x1="22" y1="9" x2="16" y2="15"/>
          <line x1="16" y1="9" x2="22" y2="15"/>
        </svg>
      );
    }
    if (volume < 0.5) {
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
          <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
        </svg>
      );
    }
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
        <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
        <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
      </svg>
    );
  };

  const disabled = !src;

  return (
    <div
      ref={containerRef}
      className={`group relative w-full rounded-lg
        border border-white/[0.06] bg-white/[0.025] backdrop-blur-sm
        hover:border-white/[0.12] hover:bg-white/[0.04]
        transition-all duration-200 ${compact ? 'px-4 py-3' : 'px-5 py-4'}
        ${disabled ? 'opacity-60' : ''}`}
    >
      {/* ============ 第一行：播放 + 时间 + 倍速 + 下载 ============ */}
      <div className="flex items-center gap-3">
        {/* 播放/暂停按钮 */}
        <button
          onClick={togglePlay}
          disabled={disabled}
          className={`relative shrink-0 grid place-items-center rounded-full transition-all duration-200
            ${compact ? 'w-11 h-11' : 'w-12 h-12'}
            ${isPlaying ? 'ring-2 ring-brand-500/40 ring-offset-2 ring-offset-[#0b0a14]' : ''}
            disabled:opacity-40 disabled:cursor-not-allowed`}
          style={{
            backgroundImage: isPlaying
              ? 'linear-gradient(180deg, rgba(255,255,255,0.20) 0%, rgba(255,255,255,0) 50%), linear-gradient(135deg, #9b6bff 0%, #8b5cf6 100%)'
              : 'linear-gradient(180deg, rgba(255,255,255,0.14) 0%, rgba(255,255,255,0) 50%), linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)',
            boxShadow: isPlaying
              ? '0 0 0 1px rgba(168,85,247,0.45), 0 10px 24px -8px rgba(139,92,246,0.55)'
              : '0 0 0 1px rgba(168,85,247,0.35), 0 8px 20px -10px rgba(139,92,246,0.45)',
          }}
          title={isPlaying ? '暂停' : '播放'}
        >
          {isPlaying ? (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" className="text-white translate-x-[1px]">
              <rect x="6" y="5" width="4" height="14" rx="1"/>
              <rect x="14" y="5" width="4" height="14" rx="1"/>
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" className="text-white translate-x-[1.5px]">
              <path d="M8 5.14v13.72a1 1 0 0 0 1.52.86l11.14-6.86a1 1 0 0 0 0-1.72L9.52 4.28A1 1 0 0 0 8 5.14z"/>
            </svg>
          )}
          {isPlaying && (
            <span
              className="absolute inset-0 rounded-full animate-ping pointer-events-none"
              style={{ background: 'rgba(139,92,246,0.20)', animationDuration: '1.8s' }}
            />
          )}
        </button>

        {/* 时间：Eleven / Spotify 风格，左当前 / 右总时长 */}
        <div className="flex items-baseline gap-1 min-w-[108px] shrink-0">
          <span className="text-[12px] tabular-nums text-white/85">
            {fmtTime(dragging || duration > 0 ? displayPercent * duration : 0)}
          </span>
          <span className="text-[11px] text-white/25">/</span>
          <span className="text-[11px] tabular-nums text-white/40">
            {fmtTime(duration)}
          </span>
        </div>

        {/* 右侧：音量 + 倍速 + 下载 */}
        <div className="flex-1" />

        <div className="flex items-center gap-1.5">
          {/* 音量控制 */}
          <div className="relative group/vol flex items-center">
            <button
              type="button"
              onClick={toggleMute}
              disabled={disabled}
              className="shrink-0 grid place-items-center rounded-md
                w-8 h-8
                text-white/60 hover:text-white/90
                hover:bg-white/[0.05] border border-white/[0.05] hover:border-white/[0.1]
                transition-all"
              title={muted || volume === 0 ? '取消静音' : '静音'}
            >
              <VolumeIcon />
            </button>
            {/* 悬停展开的音量滑条 */}
            <div className="
              hidden group-hover/vol:flex items-center gap-2 px-2
              ml-1 h-8 rounded-md border border-white/[0.06] bg-white/[0.04]
            ">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="text-white/35">
                <circle cx="11" cy="11" r="8"/>
                <path d="M21 21l-4.35-4.35"/>
              </svg>
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={muted ? 0 : volume}
                onChange={(e) => {
                  const v = parseFloat(e.target.value);
                  setVolume(v);
                  if (v === 0) setMuted(true);
                  else { setMuted(false); setLastVolume(Math.max(v, 0.1)); }
                }}
                className="volume-slider w-24 h-1.5"
                style={{
                  background: muted || volume === 0
                    ? 'rgba(255,255,255,0.08)'
                    : `linear-gradient(90deg, #a78bfa 0%, #8b5cf6 ${(muted ? 0 : volume) * 100}%, rgba(255,255,255,0.08) ${(muted ? 0 : volume) * 100}%, rgba(255,255,255,0.08) 100%)`,
                }}
              />
              <span className="text-[10px] tabular-nums w-7 text-right text-white/50">
                {Math.round((muted ? 0 : volume) * 100)}%
              </span>
            </div>
          </div>

          {/* 倍速 */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setShowSpeedMenu(v => !v)}
              onBlur={() => setTimeout(() => setShowSpeedMenu(false), 120)}
              className="shrink-0 inline-flex items-center justify-center gap-1
                h-8 px-2.5 rounded-md border border-white/[0.06] bg-white/[0.03]
                text-white/70 hover:text-white/95 hover:bg-white/[0.06] hover:border-white/[0.12]
                transition-all font-medium text-[11.5px]"
              title="倍速"
            >
              <span className="tabular-nums">{speed.toFixed(2).replace(/\.?0+$/, '') || '1'}x</span>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="6 9 12 15 18 9"/>
              </svg>
            </button>
            {showSpeedMenu && (
              <div className="absolute bottom-9 right-0 z-30
                rounded-md border border-white/[0.10] bg-[#12101d]/95 backdrop-blur-md
                shadow-[0_12px_40px_-12px_rgba(0,0,0,0.7)] p-1.5 w-24 animate-fade-in">
                {SPEEDS.map(s => (
                  <button
                    key={s}
                    type="button"
                    onMouseDown={(e) => {
                      e.preventDefault();
                      setSpeed(s);
                      setShowSpeedMenu(false);
                    }}
                    className={`w-full text-left text-[12px] px-2.5 py-1.5 rounded-lg transition-colors
                      ${Math.abs(s - speed) < 1e-6
                        ? 'bg-brand-500/20 text-brand-200'
                        : 'text-white/70 hover:bg-white/[0.06] hover:text-white/95'
                      }`}
                  >
                    {s.toFixed(2).replace(/\.?0+$/, '') || '1'}x
                    {s === 1 && <span className="text-white/25 ml-1">默认</span>}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* 下载 */}
          {onDownload && (
            <button
              type="button"
              onClick={onDownload}
              className="shrink-0 grid place-items-center rounded-md
                w-8 h-8
                text-white/60 hover:text-brand-300
                hover:bg-brand-500/10 border border-white/[0.05] hover:border-brand-500/30
                transition-all"
              title="下载 MP3"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 3v12"/>
                <path d="m7 10 5 5 5-5"/>
                <path d="M5 21h14"/>
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* ============ 第二行：进度条 ============ */}
      <div className="mt-3 select-none">
        <div
          ref={trackRef}
          onPointerDown={onTrackPointerDown}
          onPointerMove={onTrackPointerMove}
          onPointerUp={onTrackPointerUp}
          onPointerCancel={onTrackPointerUp}
          className={`relative h-2 w-full rounded-full cursor-pointer touch-none
            bg-white/[0.07]
            hover:bg-white/[0.10] transition-colors`}
          style={{ touchAction: 'none' }}
        >
          {/* 已播放部分 */}
          <div
            className="absolute top-0 left-0 h-full rounded-full pointer-events-none"
            style={{
              width: `${displayPercent * 100}%`,
              ...gradientStyle,
              boxShadow: '0 0 10px 0 rgba(168,85,247,0.35)',
            }}
          />
          {/* 缓冲进度（buffered） */}
          {loaded && audioRef.current && (
            <BufferedBar audioRef={audioRef} percent={displayPercent} />
          )}
          {/* 拖拽指针球 */}
          <div
            className={`absolute top-1/2 -translate-y-1/2 -translate-x-1/2
              rounded-full pointer-events-none transition-transform duration-150
              ${dragging ? 'scale-125' : 'scale-100'}
            `}
            style={{
              left: `${displayPercent * 100}%`,
              width: dragging ? 14 : 12,
              height: dragging ? 14 : 12,
              background: '#fff',
              boxShadow: dragging
                ? '0 0 0 4px rgba(139,92,246,0.25), 0 2px 6px rgba(0,0,0,0.5)'
                : '0 0 0 3px rgba(139,92,246,0.22), 0 1px 4px rgba(0,0,0,0.45)',
            }}
          />
        </div>
      </div>

      {/* 隐藏的 audio 元素 */}
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
            // 拖动时不更新 currentTime（由 onPointerMove 控制）
            if (dragging) return;
            const d = e.currentTarget.duration;
            setCurrentTime(d > 0 ? e.currentTarget.currentTime : 0);
          }}
          onPlay={() => setIsPlaying(true)}
          onPause={() => setIsPlaying(false)}
          onEnded={() => {
            setIsPlaying(false);
            setCurrentTime(0);
          }}
          onLoadedData={() => {
            // 让浏览器显示首帧即可
          }}
        />
      )}
    </div>
  );
}

/** 缓冲进度条（次要指示，更浅的灰） */
function BufferedBar({
  audioRef,
  percent,
}: {
  audioRef: React.MutableRefObject<HTMLAudioElement | null>;
  percent: number;
}) {
  const [bufferedPct, setBufferedPct] = useState<number>(0);

  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const update = () => {
      try {
        if (!el.duration || !el.buffered.length) return;
        const end = el.buffered.end(el.buffered.length - 1);
        setBufferedPct(Math.min(1, Math.max(0, end / el.duration)));
      } catch {}
    };
    update();
    el.addEventListener('progress', update);
    return () => el.removeEventListener('progress', update);
  }, [audioRef]);

  const display = Math.max(bufferedPct, percent);
  if (display <= 0) return null;

  return (
    <div
      className="absolute top-0 left-0 h-full rounded-full pointer-events-none"
      style={{
        width: `${display * 100}%`,
        background: 'rgba(255,255,255,0.06)',
        zIndex: -1,
      }}
    />
  );
}
