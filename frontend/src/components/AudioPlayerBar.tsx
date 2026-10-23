'use client';

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react';

export interface PlayerTrack {
  id: string;
  title: string;
  subtitle?: string;
  src: string;
  buildId?: string;
  chapterIdx?: number;
  /** 封面主色（#rrggbb）；只作为标题旁的小圆点，给每本书一个身份色 */
  accent?: string | null;
}

export interface AudioPlayerHandle {
  toggle: () => void;
  play: () => void;
  pause: () => void;
}

function fmtTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/** 后退 / 前进 10 秒的环形箭头（语义放在 aria-label / title 上） */
function SkipIcon({ dir }: { dir: 'back' | 'forward' }) {
  return (
    <svg
      width="19"
      height="19"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
      style={dir === 'forward' ? { transform: 'scaleX(-1)' } : undefined}
    >
      <path
        d="M3.4 2.9v6.2h6.2"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M3.4 9.1a9 9 0 1 0 2.6-5.5"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </svg>
  );
}

function PlayIcon({ playing }: { playing: boolean }) {
  return playing ? (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <rect x="6.5" y="5" width="4" height="14" rx="1.2" />
      <rect x="13.5" y="5" width="4" height="14" rx="1.2" />
    </svg>
  ) : (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z" />
    </svg>
  );
}

/**
 * 细轨滑块：轨道视觉 4px，点击热区 14px；滑块是「挖」在轨道上的小圆点，
 * 常态就可见（深色底上必须能看见），不做发光环。
 */
function Rail({
  value,
  max,
  onChange,
  ariaLabel,
  step = 0.1,
  className = '',
}: {
  value: number;
  max: number;
  onChange: (v: number) => void;
  ariaLabel: string;
  step?: number;
  className?: string;
}) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  return (
    <div className={`relative flex h-3.5 w-full items-center ${className}`}>
      <div className="pointer-events-none absolute inset-x-0 h-1 overflow-hidden rounded-full bg-ink-300/80">
        <div
          className="h-full rounded-full bg-ink-700"
          style={{ width: `${pct}%`, transition: 'width 120ms linear' }}
        />
      </div>
      <input
        type="range"
        min={0}
        max={max || 1}
        step={step}
        value={Math.min(value, max || 1)}
        onChange={e => onChange(Number(e.target.value))}
        className="player-range absolute inset-0 w-full"
        aria-label={ariaLabel}
      />
    </div>
  );
}

/**
 * 页面底部唯一的章节播放器。
 *
 * 只有选中某一章时才把该章的签名 URL 交给 <audio>，不再为列表里的每章
 * 创建播放器或批量签发 URL，避免展开章节列表时触发 COS 回源/缓存费用。
 *
 * 视觉：单层扁平 dock —— 与左侧边栏同色、同一水平线，顶边一条 hairline；
 * 左「标题 + 来源」、中「后退/播放/前进 + 细轨进度 + 两端时间」、
 * 右「音量 / 下载 / 关闭」。不用渐变、光晕、大圆角，只靠留白与 1px 线分层。
 */
const AudioPlayerBar = forwardRef<AudioPlayerHandle, {
  track: PlayerTrack | null;
  onClose: () => void;
  onPlayingChange?: (playing: boolean) => void;
  onNeedNewSrc?: (track: PlayerTrack) => Promise<string | null>;
  onDownload?: (track: PlayerTrack) => void;
}>(function AudioPlayerBar(
  { track, onClose, onPlayingChange, onNeedNewSrc, onDownload },
  ref,
) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [resolvedSrc, setResolvedSrc] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(0.85);
  const [muted, setMuted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refreshAttemptsRef = useRef(0);

  const setPlayingState = (next: boolean) => {
    setPlaying(next);
    onPlayingChange?.(next);
  };

  useEffect(() => {
    if (!track) {
      setResolvedSrc(null);
      setPlayingState(false);
      setCurrentTime(0);
      setDuration(0);
      setError(null);
      return;
    }
    refreshAttemptsRef.current = 0;
    setResolvedSrc(track.src);
    setCurrentTime(0);
    setDuration(0);
    setError(null);
    setLoading(true);
  }, [track?.id, track?.src]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !resolvedSrc) return;
    audio.volume = muted ? 0 : volume;
    audio.load();
    const promise = audio.play();
    if (promise) {
      promise.catch(() => {
        // 浏览器自动播放策略可能拦截；保留暂停态，让用户点播放按钮。
        setPlayingState(false);
        setLoading(false);
      });
    }
  }, [resolvedSrc]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.volume = muted ? 0 : volume;
  }, [volume, muted]);

  const toggle = () => {
    const audio = audioRef.current;
    if (!audio || !resolvedSrc) return;
    if (audio.paused) {
      audio.play().catch(() => setError('播放失败，请检查音频地址是否过期'));
    } else {
      audio.pause();
    }
  };

  const seekBy = (delta: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const end = Number.isFinite(audio.duration) ? audio.duration : 0;
    const next = Math.max(0, Math.min(end || Number.MAX_SAFE_INTEGER, audio.currentTime + delta));
    audio.currentTime = next;
    setCurrentTime(next);
  };

  const seekTo = (next: number) => {
    setCurrentTime(next);
    if (audioRef.current) audioRef.current.currentTime = next;
  };

  const changeVolume = (next: number) => {
    setVolume(next);
    setMuted(next === 0);
  };

  useImperativeHandle(ref, () => ({
    toggle,
    play: () => {
      audioRef.current?.play().catch(() => setPlayingState(false));
    },
    pause: () => audioRef.current?.pause(),
  }));

  if (!track) return null;

  const durationSafe = duration > 0 ? duration : 0;
  const progress = durationSafe > 0 ? Math.min(100, (currentTime / durationSafe) * 100) : 0;
  const chapterLabel = track.chapterIdx != null ? `第 ${track.chapterIdx + 1} 章` : '章节音频';
  const meta = [chapterLabel, track.subtitle].filter(Boolean).join(' · ');
  const accent = track.accent || 'rgb(var(--brand-500))';

  const iconBtn =
    'grid place-items-center rounded-[10px] text-ink-600 transition-colors ' +
    'hover:bg-ink-200 hover:text-ink-900 active:bg-ink-300/70 disabled:opacity-40 ' +
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/60';

  return (
    <>
      <audio
        ref={audioRef}
        src={resolvedSrc ?? undefined}
        preload="auto"
        playsInline
        onLoadedMetadata={e => {
          setDuration(e.currentTarget.duration || 0);
          setLoading(false);
        }}
        onTimeUpdate={e => setCurrentTime(e.currentTarget.currentTime || 0)}
        onPlay={() => setPlayingState(true)}
        onPause={() => setPlayingState(false)}
        onWaiting={() => setLoading(true)}
        onCanPlay={() => setLoading(false)}
        onEnded={() => {
          setPlayingState(false);
          setCurrentTime(0);
        }}
        onError={() => {
          setLoading(false);
          if (!onNeedNewSrc || !track || refreshAttemptsRef.current >= 1) {
            setError('音频加载失败，可能链接已过期');
            return;
          }
          refreshAttemptsRef.current += 1;
          onNeedNewSrc(track)
            .then(next => {
              if (next) {
                setError(null);
                setResolvedSrc(next);
              } else {
                setError('音频链接已过期，请重新点击播放');
              }
            })
            .catch(() => setError('音频加载失败，请重新点击播放'));
        }}
      />

      <div
        className="fixed bottom-0 left-0 right-0 z-[80] md:left-[244px]"
        role="region"
        aria-label="章节播放器"
      >
        <div className="relative border-t border-ink-300/70 bg-ink-50 pb-[env(safe-area-inset-bottom)]">
          {/* 窄屏：进度贴在 dock 顶边，不额外占一行 */}
          <div className="absolute inset-x-0 top-0 h-[2px] bg-ink-300/70 lg:hidden">
            <div className="h-full bg-ink-600" style={{ width: `${progress}%` }} />
          </div>

          <div className="flex h-[68px] items-center gap-2 px-3 lg:h-20 lg:gap-6 lg:px-4">
            {/* ── 左：书名/章节 + 来源（与左侧栏底部齐平） ── */}
            <div className="flex min-w-0 flex-1 items-center">
              <div className="min-w-0 flex-1">
                <p
                  className="truncate text-[13px] font-medium leading-5 text-ink-900 lg:text-sm"
                  title={track.title}
                >
                  {track.title}
                </p>
                <div className="mt-0.5 flex min-w-0 items-center gap-1.5">
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${playing ? 'animate-pulse' : ''}`}
                    style={{ background: accent }}
                    aria-hidden
                  />
                  <p className="truncate text-[11px] leading-4 text-ink-500 lg:text-xs">
                    <span className="lg:hidden">
                      {chapterLabel} · {fmtTime(currentTime)} / {fmtTime(durationSafe)}
                    </span>
                    <span className="hidden lg:inline">{meta}</span>
                    {error ? <span className="text-rose-300"> · {error}</span> : null}
                  </p>
                </div>
              </div>
            </div>

            {/* ── 中：控制 + 细轨进度 ── */}
            <div className="hidden flex-col justify-center gap-1 lg:flex lg:shrink-0 lg:grow-0 lg:basis-1/2">
              <div className="flex items-center justify-between gap-0.5 px-2">
                <div className="w-12" />
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className={`${iconBtn} h-9 w-9`}
                    onClick={() => seekBy(-10)}
                    title="后退 10 秒"
                    aria-label="后退 10 秒"
                  >
                    <SkipIcon dir="back" />
                  </button>
                  <button
                    type="button"
                    className="grid h-10 w-10 place-items-center rounded-full bg-ink-900 text-ink-0
                      transition hover:brightness-105 active:scale-[0.97] disabled:opacity-45
                      focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/60
                      focus-visible:ring-offset-2 focus-visible:ring-offset-ink-50"
                    onClick={toggle}
                    disabled={loading || !resolvedSrc}
                    aria-label={playing ? '暂停' : '播放'}
                  >
                    {loading ? (
                      <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                    ) : (
                      <PlayIcon playing={playing} />
                    )}
                  </button>
                  <button
                    type="button"
                    className={`${iconBtn} h-9 w-9`}
                    onClick={() => seekBy(10)}
                    title="快进 10 秒"
                    aria-label="快进 10 秒"
                  >
                    <SkipIcon dir="forward" />
                  </button>
                </div>
                <div className="flex w-12 justify-end" />
              </div>

              <div className="flex w-full items-center gap-2">
                <span className="w-12 shrink-0 text-center text-[11px] tabular-nums text-ink-500">
                  {fmtTime(currentTime)}
                </span>
                <Rail
                  value={currentTime}
                  max={durationSafe}
                  onChange={seekTo}
                  ariaLabel="播放进度"
                />
                <span className="w-12 shrink-0 text-center text-[11px] tabular-nums text-ink-500">
                  {fmtTime(durationSafe)}
                </span>
              </div>
            </div>

            {/* ── 右：窄屏播放键 / 音量 / 下载 / 关闭 ── */}
            <div className="flex items-center justify-end gap-1 lg:flex-1 lg:gap-1.5">
              <button
                type="button"
                className="grid h-10 w-10 place-items-center rounded-full bg-ink-900 text-ink-0
                  transition hover:brightness-105 active:scale-[0.97] disabled:opacity-45 lg:hidden
                  focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/60
                  focus-visible:ring-offset-2 focus-visible:ring-offset-ink-50"
                onClick={toggle}
                disabled={loading || !resolvedSrc}
                aria-label={playing ? '暂停' : '播放'}
              >
                {loading ? (
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                ) : (
                  <PlayIcon playing={playing} />
                )}
              </button>

              <div className="hidden items-center gap-1 lg:flex">
                <button
                  type="button"
                  className={`${iconBtn} h-9 w-9`}
                  onClick={() => setMuted(v => !v)}
                  title={muted ? '取消静音' : '静音'}
                  aria-label={muted ? '取消静音' : '静音'}
                >
                  {muted || volume === 0 ? (
                    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                      <path d="M11 5 6 9H3v6h3l5 4V5Z" />
                      <path d="m16 9 5 6M21 9l-5 6" />
                    </svg>
                  ) : (
                    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                      <path d="M11 5 6 9H3v6h3l5 4V5Z" />
                      <path d="M15.5 8.5a5 5 0 0 1 0 7" />
                      <path d="M18.5 6a9 9 0 0 1 0 12" />
                    </svg>
                  )}
                </button>
                <div className="w-10 xl:w-16">
                  <Rail
                    value={muted ? 0 : volume}
                    max={1}
                    step={0.01}
                    onChange={changeVolume}
                    ariaLabel="音量"
                  />
                </div>
              </div>

              {onDownload && track.buildId ? (
                <button
                  type="button"
                  className={`${iconBtn} hidden h-9 w-9 lg:grid`}
                  onClick={() => onDownload(track)}
                  title="下载本章 MP3"
                  aria-label="下载本章 MP3"
                >
                  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                    <path d="M12 3v11" />
                    <path d="m7.5 10 4.5 4.5L16.5 10" />
                    <path d="M4 18.5v1A1.5 1.5 0 0 0 5.5 21h13a1.5 1.5 0 0 0 1.5-1.5v-1" />
                  </svg>
                </button>
              ) : null}

              <button
                type="button"
                className={`${iconBtn} h-9 w-9`}
                onClick={() => {
                  audioRef.current?.pause();
                  onClose();
                }}
                title="关闭播放器"
                aria-label="关闭播放器"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" aria-hidden>
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      </div>
    </>
  );
});

export default AudioPlayerBar;
