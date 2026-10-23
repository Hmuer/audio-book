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

/**
 * 页面底部唯一的章节播放器。
 *
 * 只有选中某一章时才把该章的签名 URL 交给 <audio>，不再为列表里的每章
 * 创建播放器或批量签发 URL，避免展开章节列表时触发 COS 回源/缓存费用。
 */
const AudioPlayerBar = forwardRef<AudioPlayerHandle, {
  track: PlayerTrack | null;
  onClose: () => void;
  onPlayingChange?: (playing: boolean) => void;
  onNeedNewSrc?: (track: PlayerTrack) => Promise<string | null>;
}>(function AudioPlayerBar(
  { track, onClose, onPlayingChange, onNeedNewSrc },
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
        <div
          className="relative overflow-hidden border-t border-white/10 px-3 sm:px-5 pt-2.5 pb-2"
          style={{
            background: 'linear-gradient(180deg, rgba(17,24,39,0.78) 0%, rgba(7,11,18,0.96) 100%)',
            backdropFilter: 'blur(24px) saturate(140%)',
            WebkitBackdropFilter: 'blur(24px) saturate(140%)',
            boxShadow: '0 -24px 60px -24px rgba(0,0,0,0.9), inset 0 1px 0 rgba(255,255,255,0.06)',
          }}
        >
          {/* 顶部细光带 + 左侧品牌色光晕：透明质感 */}
          <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-brand-400/70 to-transparent" />
          <div className="pointer-events-none absolute -left-20 top-1/2 h-28 w-80 -translate-y-1/2 rounded-full bg-brand-500/10 blur-3xl" />

          <div className="relative flex items-center gap-2.5 sm:gap-3">
            <button
              type="button"
              className="w-10 h-10 sm:w-11 sm:h-11 shrink-0 rounded-full grid place-items-center text-white
                bg-gradient-to-br from-brand-400 to-brand-600
                shadow-[0_0_24px_rgba(99,102,241,0.35)] hover:brightness-110 transition-all disabled:opacity-50"
              onClick={toggle}
              disabled={loading || !resolvedSrc}
              aria-label={playing ? '暂停' : '播放'}
            >
              {loading ? (
                <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
              ) : playing ? (
                <svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>
              ) : (
                <svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z"/></svg>
              )}
            </button>

            <div
              className="hidden sm:grid w-10 h-10 shrink-0 place-items-center rounded-xl border border-white/10"
              style={{ background: 'linear-gradient(135deg, rgba(99,102,241,0.32), rgba(14,165,233,0.12))' }}
              aria-hidden
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.75)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>
              </svg>
            </div>

            <div className="min-w-0 flex-1 sm:flex-none sm:w-[min(28vw,280px)]">
              <div className="text-[13px] font-medium text-white/90 truncate">{track.title}</div>
              <div className="text-[11px] text-white/45 truncate">
                {track.subtitle || (track.chapterIdx != null ? `第 ${track.chapterIdx + 1} 章` : '章节音频')}
                {error ? <span className="text-rose-300"> · {error}</span> : null}
              </div>
            </div>

            <div className="hidden md:flex flex-1 items-center gap-2 min-w-0">
              <span className="text-[10px] text-white/40 tabular-nums w-9 text-right">{fmtTime(currentTime)}</span>
              <input
                type="range"
                min={0}
                max={durationSafe || 1}
                step={0.1}
                value={Math.min(currentTime, durationSafe || 1)}
                onChange={e => {
                  const next = Number(e.target.value);
                  setCurrentTime(next);
                  if (audioRef.current) audioRef.current.currentTime = next;
                }}
                className="player-range flex-1"
                aria-label="播放进度"
                style={{
                  background: `linear-gradient(to right, rgb(var(--brand-400)) ${progress}%, rgba(255,255,255,0.14) ${progress}%)`,
                }}
              />
              <span className="text-[10px] text-white/40 tabular-nums w-9">{fmtTime(durationSafe)}</span>
            </div>

            <div className="flex items-center gap-1 shrink-0">
              <button
                type="button"
                className="text-white/45 hover:text-white/90 p-1 transition-colors"
                onClick={() => setMuted(v => !v)}
                title={muted ? '取消静音' : '静音'}
                aria-label={muted ? '取消静音' : '静音'}
              >
                {muted || volume === 0 ? (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>
                ) : (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>
                )}
              </button>
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={muted ? 0 : volume}
                onChange={e => {
                  const v = Number(e.target.value);
                  setVolume(v);
                  setMuted(v === 0);
                }}
                className="player-range w-12 sm:w-16 lg:w-20"
                aria-label="音量"
                style={{
                  background: `linear-gradient(to right, rgba(255,255,255,0.65) ${(muted ? 0 : volume) * 100}%, rgba(255,255,255,0.14) ${(muted ? 0 : volume) * 100}%)`,
                }}
              />
            </div>

            <span className="hidden sm:block text-[10px] text-white/40 tabular-nums shrink-0">
              {fmtTime(currentTime)} / {fmtTime(durationSafe)}
            </span>

            <button
              type="button"
              className="text-white/40 hover:text-white/90 p-1 shrink-0 transition-colors"
              onClick={() => {
                audioRef.current?.pause();
                onClose();
              }}
              title="关闭播放器"
              aria-label="关闭播放器"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
            </button>
          </div>

          {/* 移动端/窄屏把进度条放到第二行，避免挤压标题 */}
          <div className="relative mt-1.5 flex md:hidden items-center gap-2">
            <span className="text-[10px] text-white/40 tabular-nums w-9 text-right">{fmtTime(currentTime)}</span>
            <input
              type="range"
              min={0}
              max={durationSafe || 1}
              step={0.1}
              value={Math.min(currentTime, durationSafe || 1)}
              onChange={e => {
                const next = Number(e.target.value);
                setCurrentTime(next);
                if (audioRef.current) audioRef.current.currentTime = next;
              }}
              className="player-range flex-1"
              aria-label="播放进度"
              style={{
                background: `linear-gradient(to right, rgb(var(--brand-400)) ${progress}%, rgba(255,255,255,0.14) ${progress}%)`,
              }}
            />
            <span className="text-[10px] text-white/40 tabular-nums w-9">{fmtTime(durationSafe)}</span>
          </div>
        </div>
      </div>
    </>
  );
});

export default AudioPlayerBar;
