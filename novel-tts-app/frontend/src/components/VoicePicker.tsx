'use client';

import { useMemo, useState, useEffect, useRef, useLayoutEffect } from 'react';
import { createPortal } from 'react-dom';
import { Voice } from '@/lib/api';

export default function VoicePicker({
  voices,
  value,
  onChange,
  onPreview,
  compact = false,
  isPlaying = false,
  isLoading = false,
  playingVoiceId = null,
  loadingVoiceId = null,
}: {
  voices: Voice[];
  value: string;
  onChange: (id: string) => void;
  onPreview: (id: string) => void;
  compact?: boolean;
  isPlaying?: boolean;
  isLoading?: boolean;
  playingVoiceId?: string | null;
  loadingVoiceId?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelIdRef = useRef(`__voice_picker_panel_${Math.random().toString(36).slice(2)}`);
  const [coords, setCoords] = useState<{ top: number; left: number; width: number } | null>(null);

  const groups = useMemo(() => {
    const g: Record<string, Voice[]> = { 男声: [], 女声: [], 中性: [] };
    voices.forEach(v => {
      const key = (['男声', '女声', '中性'].includes(v.gender) ? v.gender : '中性') as string;
      if (!g[key]) g[key] = [];
      if (
        !q ||
        v.name.includes(q) ||
        v.id.includes(q) ||
        v.description.includes(q)
      )
        g[key].push(v);
    });
    return g;
  }, [voices, q]);

  const selected = voices.find(v => v.id === value);

  useLayoutEffect(() => {
    if (!open || !btnRef.current) return;
    const update = () => {
      const r = btnRef.current?.getBoundingClientRect();
      if (r) setCoords({ top: r.bottom + 4, left: r.left, width: r.width });
    };
    update();
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update, true);
      window.removeEventListener('resize', update);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const target = e.target as Node;
      if (btnRef.current?.contains(target)) return;
      const panel = document.getElementById(panelIdRef.current);
      if (panel && panel.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  return (
    <div className="relative">
      <div className="flex items-center gap-2">
        <button
          ref={btnRef}
          onClick={() => setOpen(v => !v)}
          className={`${compact ? 'flex-1' : 'w-full'} text-left rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-sm`}
        >
          {selected ? (
            <span>
              🎵 <b>{selected.name}</b>
              <span className="text-white/50 ml-2 text-xs">
                {selected.gender} · {selected.description.slice(0, 20)}…
              </span>
            </span>
          ) : (
            <span className="text-white/50">请选择音色…</span>
          )}
        </button>
        <button
          className={`btn-ghost shrink-0 ${isPlaying ? 'ring-2 ring-brand-500/50' : ''}`}
          onClick={() => selected && onPreview(selected.id)}
          disabled={!selected || isLoading}
          title={isPlaying ? '停止播放' : '试听'}
        >
          {isLoading ? '…' : isPlaying ? '⏸' : '▶'}
        </button>
      </div>
      {open && coords && typeof document !== 'undefined' && createPortal(
        <div
          id={panelIdRef.current}
          style={{
            position: 'fixed',
            top: `${coords.top}px`,
            left: `${coords.left}px`,
            width: `${coords.width}px`,
            zIndex: 9999,
          }}
          className="rounded-2xl border border-white/10 bg-gray-950 p-3 shadow-2xl max-h-[420px] overflow-hidden flex flex-col"
        >
          <input
            autoFocus
            className="input mb-2"
            placeholder="搜索音色（名称/标签）"
            value={q}
            onChange={e => setQ(e.target.value)}
          />
          <div className="overflow-auto pr-1 space-y-3">
            {Object.entries(groups).map(([k, list]) =>
              list.length > 0 ? (
                <div key={k}>
                  <div className="text-xs uppercase tracking-wider text-white/40 mb-1 px-1">
                    {k} ({list.length})
                  </div>
                  <div className={`grid ${compact ? 'gap-1' : 'gap-2'}`}>
                    {list.map(v => (
                      <button
                        key={v.id}
                        onClick={() => {
                          onChange(v.id);
                          setOpen(false);
                        }}
                        className={`text-left rounded-lg px-3 py-2 border transition text-sm ${
                          v.id === value
                            ? 'bg-brand-600/20 border-brand-500/60'
                            : 'bg-white/5 border-white/5 hover:bg-white/10'
                        }`}
                      >
                        <div className="flex justify-between gap-2">
                          <span className="font-medium">{v.name}</span>
                          <button
                            className={`opacity-80 hover:opacity-100 text-xs ${
                              playingVoiceId === v.id ? 'text-brand-400' : ''
                            } ${loadingVoiceId === v.id ? 'animate-pulse' : ''}`}
                            onClick={e => {
                              e.stopPropagation();
                              onPreview(v.id);
                            }}
                          >
                            {loadingVoiceId === v.id
                              ? '…'
                              : playingVoiceId === v.id
                              ? '⏸'
                              : '▶'}
                          </button>
                        </div>
                        <div className="text-[11px] text-white/50 line-clamp-1 mt-0.5">
                          {v.description}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              ) : null
            )}
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
