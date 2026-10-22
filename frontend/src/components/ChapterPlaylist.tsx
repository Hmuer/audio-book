'use client';

import { useEffect, useState } from 'react';
import ChapterActions from './ChapterActions';

export interface PlaylistItem {
  id: string;
  chapterIdx: number;
  title: string;
  meta?: string;
  playable: boolean;
  /** failed / pending / synthesizing / done，仅用于展示 */
  status?: string;
  error?: string | null;
}

export default function ChapterPlaylist({
  items,
  activeId,
  playing,
  loadingId,
  onPlay,
  onDownloadMp3,
  onDownloadLrc,
  pageSize = 80,
  emptyText = '暂无可播放章节',
  className = '',
}: {
  items: PlaylistItem[];
  activeId?: string | null;
  playing?: boolean;
  loadingId?: string | null;
  onPlay: (item: PlaylistItem) => void;
  onDownloadMp3: (item: PlaylistItem) => void;
  onDownloadLrc: (item: PlaylistItem) => void;
  pageSize?: number;
  emptyText?: string;
  className?: string;
}) {
  const [visibleCount, setVisibleCount] = useState(pageSize);
  useEffect(() => {
    setVisibleCount(pageSize);
  }, [items.length, pageSize]);

  if (items.length === 0) {
    return <div className={`text-sm text-ink-500 py-6 text-center ${className}`}>{emptyText}</div>;
  }

  return (
    <div className={`space-y-1.5 ${className}`}>
      {items.slice(0, visibleCount).map(item => {
        const active = activeId === item.id;
        return (
          <div
            key={item.id}
            className={`rounded-lg border px-2.5 py-2 transition-colors ${
              active
                ? 'border-brand-500/45 bg-brand-500/10'
                : 'border-ink-300/60 bg-ink-200/60 hover:border-ink-400/70'
            }`}
          >
            <div className="flex items-center gap-2 min-w-0">
              <span
                className={`text-[11px] font-mono tabular-nums shrink-0 rounded-md px-1.5 py-1 ${
                  active ? 'text-brand-200' : 'text-ink-500'
                }`}
              >
                #{String(item.chapterIdx + 1).padStart(3, '0')}
              </span>
              <div className="flex-1 min-w-0">
                <div className="text-sm text-ink-800 truncate" title={item.title}>
                  {item.title || '(无标题)'}
                </div>
                {item.meta && (
                  <div className="text-[11px] text-ink-500 truncate">{item.meta}</div>
                )}
              </div>
              <ChapterActions
                playable={item.playable}
                active={active}
                playing={playing}
                loading={loadingId === item.id}
                onToggle={() => onPlay(item)}
                onDownloadMp3={() => onDownloadMp3(item)}
                onDownloadLrc={() => onDownloadLrc(item)}
              />
            </div>
            {item.error && (
              <div className="mt-1 text-[11px] text-red-300/90 truncate" title={item.error}>
                {item.error}
              </div>
            )}
          </div>
        );
      })}
      {visibleCount < items.length && (
        <button
          type="button"
          className="btn-ghost w-full !py-1.5 text-xs"
          onClick={() => setVisibleCount(n => n + pageSize)}
        >
          加载更多（还有 {items.length - visibleCount} 章）
        </button>
      )}
    </div>
  );
}
