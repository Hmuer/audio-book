'use client';

import type { MouseEvent } from 'react';

/**
 * 章节行内的媒体操作：播放 / 下载 MP3 / 下载 LRC。
 *
 * 关键约束：这里不创建 <audio>，也不预取任何媒体 URL。
 * 只有点击播放时才由父组件签发当前这一章的 URL，避免展开列表时把
 * 所有章节都签到 COS、产生额外流量/缓存费用。
 */
export default function ChapterActions({
  playable,
  active,
  playing,
  loading,
  onToggle,
  onDownloadMp3,
  onDownloadLrc,
}: {
  playable: boolean;
  active?: boolean;
  playing?: boolean;
  loading?: boolean;
  onToggle: () => void;
  onDownloadMp3: () => void;
  onDownloadLrc: () => void;
}) {
  const btnBase =
    'inline-flex items-center justify-center shrink-0 rounded-md border transition-all ' +
    'disabled:opacity-35 disabled:cursor-not-allowed';
  const stop = (fn: () => void) => (e: MouseEvent) => {
    e.stopPropagation();
    fn();
  };
  return (
    <div className="flex items-center gap-1.5 shrink-0">
      <button
        type="button"
        className={`${btnBase} w-8 h-8 ${
          active
            ? 'border-brand-500/50 bg-brand-500/20 text-brand-200 hover:bg-brand-500/30'
            : 'border-ink-300/70 bg-ink-200 text-ink-600 hover:text-brand-300 hover:bg-brand-500/10 hover:border-brand-500/30'
        }`}
        disabled={!playable || loading}
        onClick={stop(onToggle)}
        title={active && playing ? '暂停' : '播放本章'}
        aria-label={active && playing ? '暂停' : '播放本章'}
      >
        {loading ? (
          <span className="w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" />
        ) : active && playing ? (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>
        ) : (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z"/></svg>
        )}
      </button>
      <button
        type="button"
        className={`${btnBase} h-8 px-2 text-[11px] font-medium border-ink-300/70 bg-ink-200 text-ink-600 hover:text-brand-300 hover:bg-brand-500/10 hover:border-brand-500/30`}
        disabled={!playable}
        onClick={stop(onDownloadMp3)}
        title="下载本章 MP3"
      >
        MP3
      </button>
      <button
        type="button"
        className={`${btnBase} h-8 px-2 text-[11px] font-medium border-ink-300/70 bg-ink-200 text-ink-600 hover:text-brand-300 hover:bg-brand-500/10 hover:border-brand-500/30`}
        disabled={!playable}
        onClick={stop(onDownloadLrc)}
        title="下载本章 LRC 歌词"
      >
        LRC
      </button>
    </div>
  );
}
