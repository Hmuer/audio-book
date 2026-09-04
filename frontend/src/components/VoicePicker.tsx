'use client';

import { useMemo, useState, useEffect, useRef, useLayoutEffect } from 'react';
import { createPortal } from 'react-dom';
import { Voice } from '@/lib/api';
import {
  GenderKey,
  normalizeGender,
  voiceDescription,
  voiceProvider,
  ProviderKey,
  PROVIDER_LABELS,
  sceneOptions,
  ageOptions,
  filterVoices,
  AGE_LABELS,
} from '@/lib/voiceUtils';

/**
 * ElevenLabs 风格音色选择器
 *  - 触发器：玻璃态卡片，左侧有圆形头像（按性别品牌色）+ 名称 + 性别标签
 *  - 试听按钮：与触发器并列，同高，小图标，hover 变品牌紫
 *  - 下拉面板：浮岛式，多维筛选（厂商 Tab / 性别 chip / 场景 / 年龄 / 方言）
 *    + 搜索框 + 分组（男声 / 女声 / 中性）
 *  - 每个音色行：avatar + 名称 + 描述，行尾试听 icon；选中行带紫色左边条
 */

function avatarBg(gender: string, id: string): string {
  // 品牌色背景，基于 id 取模让同一音色头像颜色稳定
  // 8 种"莫兰迪灰调"palette：从 globals.css 的 --palette-xxx-bg 通道变量取，
  // 随主题深浅模式自动变，不再写死高饱和 candy 色。
  const g: GenderKey = normalizeGender({ gender } as Voice);
  const palettes: Record<string, string[]> = {
    '男声': [
      'rgb(var(--palette-blue-bg))',   'rgb(var(--palette-cyan-bg))',
      'rgb(var(--palette-mint-bg))',   'rgb(var(--status-info-bg))',
    ],
    '女声': [
      'rgb(var(--palette-rose-bg))',   'rgb(var(--palette-pink-bg))',
      'rgb(var(--palette-purple-bg))', 'rgb(var(--palette-yellow-bg))',
    ],
    '中性': [
      'rgb(var(--brand-500))',         'rgb(var(--palette-green-bg))',
      'rgb(var(--palette-yellow-bg))', 'rgb(var(--status-synth-bg))',
    ],
  };
  const pals = palettes[g] || palettes['中性'];
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  return pals[hash % pals.length];
}

function Avatar({ voice, size = 32 }: { voice: Voice; size?: number }) {
  const bg = avatarBg(voice.gender, voice.id);
  const initial = voice.name?.trim()?.[0] || '音';
  return (
    <div
      className="shrink-0 grid place-items-center rounded-full font-semibold text-white select-none"
      style={{
        width: size,
        height: size,
        fontSize: Math.floor(size * 0.42),
        background: `linear-gradient(135deg, ${bg} 0%, ${bg}bb 100%)`,
        boxShadow: `0 0 0 1px rgba(var(--color-white), 0.12) inset, 0 4px 10px -4px ${bg}77`,
      }}
    >
      {initial}
    </div>
  );
}

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
  const [providerTab, setProviderTab] = useState<ProviderKey | 'all'>('all');
  const [genderFilter, setGenderFilter] = useState<GenderKey | 'all'>('all');
  const [sceneFilter, setSceneFilter] = useState('');
  const [ageFilter, setAgeFilter] = useState('');
  const [dialectOnly, setDialectOnly] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelIdRef = useRef(`__voice_picker_panel_${Math.random().toString(36).slice(2)}`);
  const [coords, setCoords] = useState<{ top: number; left: number; width: number } | null>(null);

  // 当前厂商 Tab 下可选的场景/年龄选项
  const providerVoices = useMemo(
    () => (providerTab === 'all' ? voices : voices.filter(v => voiceProvider(v) === providerTab)),
    [voices, providerTab]
  );
  const scenes = useMemo(() => sceneOptions(providerVoices), [providerVoices]);
  const ages = useMemo(() => ageOptions(providerVoices), [providerVoices]);

  const hasAdvanced = scenes.length > 1 || ages.length > 1 || providerVoices.some(v => v.dialect);

  const groups = useMemo(() => {
    const filtered = filterVoices(voices, {
      q,
      provider: providerTab,
      gender: genderFilter,
      scene: sceneFilter || undefined,
      age: ageFilter || undefined,
      dialectOnly: dialectOnly || undefined,
    });
    const g: Record<GenderKey, Voice[]> = { 男声: [], 女声: [], 中性: [] };
    filtered.forEach(v => g[normalizeGender(v)].push(v));
    return g;
  }, [voices, q, providerTab, genderFilter, sceneFilter, ageFilter, dialectOnly]);

  const total = groups.男声.length + groups.女声.length + groups.中性.length;

  const selected = voices.find(v => v.id === value);

  useLayoutEffect(() => {
    if (!open || !btnRef.current) return;
    const update = () => {
      const r = btnRef.current?.getBoundingClientRect();
      if (r) setCoords({ top: r.bottom + 6, left: r.left, width: r.width });
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
    <div className="relative w-full">
      <div className="flex items-center gap-2">
        {/* 触发器 — Eleven 玻璃态卡片风格 */}
        <button
          ref={btnRef}
          onClick={() => setOpen(v => !v)}
          className={`group relative flex items-center gap-3 ${compact ? 'flex-1' : 'w-full'}
            rounded-lg border border-ink-300/70 bg-ink-100
            px-3 py-2.5 text-left
            hover:border-ink-400 hover:bg-ink-200
            focus:outline-none focus:ring-2 focus:ring-brand-500/40
            transition-all duration-200`}
        >
          {selected ? (
            <>
              <Avatar voice={selected} size={compact ? 30 : 36} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-semibold text-ink-800 truncate">
                    {selected.name}
                  </span>
                  <span className="chip-soft !px-2 !py-0.5">
                    {normalizeGender(selected)}
                  </span>
                  <span className="chip-soft !px-2 !py-0.5 !text-[11px]">
                    {PROVIDER_LABELS[voiceProvider(selected)]}
                  </span>
                </div>
                <div className="text-[11px] text-ink-600 truncate mt-0.5">
                  {voiceDescription(selected)}
                </div>
              </div>
              <span
                className={`shrink-0 text-ink-500 transition-transform duration-200 ${open ? 'rotate-180 text-brand-400' : 'group-hover:text-ink-700'}`}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="m6 9 6 6 6-6" />
                </svg>
              </span>
            </>
          ) : (
            <>
              <div className="shrink-0 w-9 h-9 rounded-full grid place-items-center bg-ink-200 border border-ink-300/70 text-ink-500">
                ?
              </div>
              <div className="flex-1 text-sm text-ink-600">请选择音色…</div>
            </>
          )}
        </button>

        {/* 试听按钮 — Eleven 小 icon pill */}
        <button
          onClick={() => selected && onPreview(selected.id)}
          disabled={!selected || isLoading}
          title={isPlaying ? '停止播放' : '试听'}
          className={`relative shrink-0 grid place-items-center rounded-md
            border transition-all duration-200
            ${isPlaying
              ? 'border-brand-500/50 bg-brand-500/15 text-brand-300 ring-2 ring-brand-500/25'
              : 'border-ink-300/70 bg-ink-100 text-ink-700 hover:border-brand-500/30 hover:bg-brand-500/10 hover:text-brand-300'
            }
            disabled:opacity-40 disabled:cursor-not-allowed`}
          style={{ width: compact ? 40 : 44, height: compact ? 40 : 44 }}
        >
          {isLoading ? (
            <span className="w-3.5 h-3.5 border-2 border-brand-400/50 border-t-brand-300 rounded-full animate-spin" />
          ) : isPlaying ? (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
              <rect x="6" y="5" width="4" height="14" rx="1" />
              <rect x="14" y="5" width="4" height="14" rx="1" />
            </svg>
          ) : (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
              <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z" />
            </svg>
          )}
        </button>
      </div>

      {open && coords && typeof document !== 'undefined' && createPortal(
        <div
          id={panelIdRef.current}
          style={{
            position: 'fixed',
            top: `${coords.top}px`,
            left: `${coords.left}px`,
            width: `${Math.max(coords.width, 400)}px`,
            zIndex: 9999,
          }}
          className="glass-panel !rounded-lg p-3 shadow-el-xl animate-scale-in"
        >
          {/* 搜索框 */}
          <div className="relative mb-2.5">
            <svg
              className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-600"
              width="14" height="14" viewBox="0 0 24 24"
              fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
            >
              <circle cx="11" cy="11" r="7" />
              <path d="m21 21-4.3-4.3" />
            </svg>
            <input
              autoFocus
              className="input !pl-9 !py-2.5 !text-[13px]"
              placeholder="搜索音色（名称/标签/场景/方言）"
              value={q}
              onChange={e => setQ(e.target.value)}
            />
          </div>

          {/* 厂商 Tab */}
          <div className="flex items-center gap-1 mb-2.5">
            {([
              ['all', `全部 ${voices.length}`],
              ['minimax', PROVIDER_LABELS.minimax],
              ['doubao', PROVIDER_LABELS.doubao],
              ['icl', PROVIDER_LABELS.icl],
            ] as [ProviderKey | 'all', string][]).map(([key, label]) => {
              const count = key === 'all'
                ? voices.length
                : voices.filter(v => voiceProvider(v) === key).length;
              const disabled = count === 0;
              const active = providerTab === key;
              return (
                <button
                  key={key}
                  disabled={disabled}
                  onClick={() => {
                  setProviderTab(key);
                  setSceneFilter('');
                  setAgeFilter('');
                  setDialectOnly(false);
                }}
                  className={`px-2.5 py-1 rounded-md text-[12px] font-medium border transition-all
                    ${active
                      ? 'bg-brand-500/15 border-brand-500/40 text-brand-300'
                      : 'bg-ink-100 border-ink-300/70 text-ink-600 hover:bg-ink-200 hover:text-ink-700'}
                    disabled:opacity-35 disabled:cursor-not-allowed`}
                >
                  {key === 'all' ? '全部' : label}
                  <span className="ml-1 text-[10px] tabular-nums opacity-70">{count}</span>
                </button>
              );
            })}
          </div>

          {/* 性别 chip + 场景/年龄下拉 */}
          <div className="flex items-center gap-1.5 mb-2.5 flex-wrap">
            {(['all', '男声', '女声', '中性'] as (GenderKey | 'all')[]).map(gk => (
              <button
                key={gk}
                onClick={() => setGenderFilter(gk)}
                className={`px-2 py-0.5 rounded-md text-[11px] border transition-all
                  ${genderFilter === gk
                    ? 'bg-brand-500/15 border-brand-500/40 text-brand-300'
                    : 'bg-ink-100 border-ink-300/60 text-ink-600 hover:bg-ink-200'}`}
              >
                {gk === 'all' ? '不限' : gk}
              </button>
            ))}
            {hasAdvanced && (
              <div className="flex items-center gap-1.5 ml-auto">
                {scenes.length > 1 && (
                  <select
                    className="!py-1 !text-[11px] !w-auto"
                    value={sceneFilter}
                    onChange={e => setSceneFilter(e.target.value)}
                  >
                    <option value="">场景：全部</option>
                    {scenes.map(s => <option key={s} value={s}>{s}</option>)}
                  </select>
                )}
                {ages.length > 1 && (
                  <select
                    className="!py-1 !text-[11px] !w-auto"
                    value={ageFilter}
                    onChange={e => setAgeFilter(e.target.value)}
                  >
                    <option value="">年龄：全部</option>
                    {ages.map(a => <option key={a} value={a}>{AGE_LABELS[a]}</option>)}
                  </select>
                )}
                {providerVoices.some(v => v.dialect) && (
                  <button
                    onClick={() => setDialectOnly(v => !v)}
                    className={`px-2 py-0.5 rounded-md text-[11px] border transition-all
                      ${dialectOnly
                        ? 'bg-brand-500/15 border-brand-500/40 text-brand-300'
                        : 'bg-ink-100 border-ink-300/60 text-ink-600 hover:bg-ink-200'}`}
                  >
                    方言
                  </button>
                )}
              </div>
            )}
          </div>

          <div className="overflow-auto pr-1 space-y-4 max-h-[440px]">
            {Object.entries(groups).map(([k, list]) =>
              list.length > 0 ? (
                <div key={k}>
                  <div className="text-[11px] uppercase tracking-[0.08em] text-ink-500 mb-1.5 px-1 flex items-center gap-2 font-semibold">
                    <span className="inline-block w-1.5 h-1.5 rounded-full"
                      style={{
                        background: k === '男声' ? 'rgb(var(--status-info-bg))' : k === '女声' ? 'rgb(var(--status-error-bg))' : 'rgb(var(--brand-500))'
                      }}
                    />
                    {k}
                    <span className="ml-1 tracking-normal text-[11px] text-ink-600">({list.length})</span>
                  </div>
                  <div className={`grid ${compact ? 'gap-1.5' : 'gap-2'}`}>
                    {list.map(v => {
                      const active = v.id === value;
                      const pv = playingVoiceId === v.id;
                      const lv = loadingVoiceId === v.id;
                      return (
                        <button
                          key={v.id}
                          onClick={() => {
                            onChange(v.id);
                            setOpen(false);
                          }}
                          className={`group relative text-left rounded-lg px-2.5 py-2 border transition-all duration-150
                            flex items-center gap-3 w-full
                            ${active
                              ? 'bg-brand-500/12 border-brand-500/40'
                              : 'bg-ink-100 border-ink-300/70 hover:bg-ink-200 hover:border-ink-400'
                            }`}
                        >
                          {active && (
                            <span className="absolute left-0 top-2 bottom-2 w-[3px] rounded-full bg-brand-500" />
                          )}
                          <Avatar voice={v} size={34} />
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="text-sm font-medium text-ink-800 truncate">
                                {v.name}
                              </span>
                              <span className="chip-soft !px-1.5 !py-0.5 !text-[11px]">
                                {normalizeGender(v)}
                              </span>
                              <span className="chip-soft !px-1.5 !py-0.5 !text-[11px]">
                                {PROVIDER_LABELS[voiceProvider(v)]}
                              </span>
                            </div>
                            <div className="text-[11px] text-ink-600 truncate mt-0.5">
                              {voiceDescription(v)}
                            </div>
                          </div>
                          {/* 行尾试听按钮 */}
                          <span
                            onClick={(e) => {
                              e.stopPropagation();
                              onPreview(v.id);
                            }}
                            className={`shrink-0 grid place-items-center rounded-full transition-all duration-150
                              ${pv
                                ? 'bg-brand-500/20 text-brand-300 ring-2 ring-brand-500/30'
                                : lv
                                ? 'bg-white/5 text-brand-400'
                                : 'bg-ink-200 text-ink-500 opacity-0 group-hover:opacity-100 hover:bg-brand-500/15 hover:text-brand-300'
                              }`}
                            style={{ width: 30, height: 30 }}
                            title={pv ? '停止试听' : '试听'}
                          >
                            {lv ? (
                              <span className="w-3 h-3 border-2 border-brand-400/50 border-t-brand-300 rounded-full animate-spin" />
                            ) : pv ? (
                              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                                <rect x="6" y="5" width="4" height="14" rx="1" />
                                <rect x="14" y="5" width="4" height="14" rx="1" />
                              </svg>
                            ) : (
                              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z" />
                              </svg>
                            )}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              ) : null
            )}
            {total === 0 && (
              <div className="text-sm text-center text-ink-500 py-6">
                没有匹配的音色
              </div>
            )}
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
