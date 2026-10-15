'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { api, errToLog, fetchMediaObjectUrl, Voice } from '@/lib/api';
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
  DIALECT_LABELS,
  dialectLabel,
} from '@/lib/voiceUtils';

const PREVIEW_TEXT_ZH = '夜色渐深，风穿过巷口，远处传来零星的犬吠声。';
// 纯外语音色（如 Stokie / Dacey / Tim，音色表声明语种只有 en）拿到中文文本时，
// 豆包 v3 会返回 code=20000000 / message=OK 但音频为空 → 试听直接 500。
// 所以试听文案必须跟着音色语种走。
const PREVIEW_TEXT_EN = 'The night grew deeper, and a cold wind slipped through the alley.';

function previewTextFor(voice: Voice): string {
  const langs = (voice.languages ?? []).map(l => String(l).toLowerCase());
  // 语种字段缺失（老数据 / 自定义音色）按中文处理，与历史行为一致
  if (langs.length === 0 || langs.includes('zh')) return PREVIEW_TEXT_ZH;
  return PREVIEW_TEXT_EN;
}

function avatarBg(gender: string, id: string): string {
  const g: GenderKey = normalizeGender({ gender } as Voice);
  const palettes: Record<string, string[]> = {
    '男声': [
      'rgb(var(--palette-blue-bg))', 'rgb(var(--palette-cyan-bg))',
      'rgb(var(--palette-mint-bg))', 'rgb(var(--status-info-bg))',
    ],
    '女声': [
      'rgb(var(--palette-rose-bg))', 'rgb(var(--palette-pink-bg))',
      'rgb(var(--palette-purple-bg))', 'rgb(var(--palette-yellow-bg))',
    ],
    '中性': [
      'rgb(var(--brand-500))', 'rgb(var(--palette-green-bg))',
      'rgb(var(--palette-yellow-bg))', 'rgb(var(--status-synth-bg))',
    ],
  };
  const pals = palettes[g] || palettes['中性'];
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  return pals[hash % pals.length];
}

function VoiceAvatar({ voice, size = 40 }: { voice: Voice; size?: number }) {
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

// =====================================================================
// 主组件
// =====================================================================
export default function VoiceLibraryPage({ voices }: { voices: Voice[] }) {
  return (
    <section className="animate-fade-in space-y-5">
      {/* 页头 */}
      <div className="glass-panel p-5 sm:p-6 relative overflow-hidden">
        <div className="flex items-start gap-4">
          <div className="w-11 h-11 rounded-md grid place-items-center shrink-0 bg-brand-600 text-white">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="9" y="3" width="6" height="12" rx="3" />
              <path d="M5 11a7 7 0 0014 0" />
              <path d="M12 18v3" />
            </svg>
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="headline text-xl">音色库</h1>
            <p className="text-xs text-ink-500 mt-1">
              共 {voices.length} 个音色 · 豆包官方 / MiniMax
            </p>
          </div>

        </div>
      </div>

      <LibraryTab voices={voices} />
    </section>
  );
}

// =====================================================================
// Tab 1：音色库浏览 + 试听
// =====================================================================
function LibraryTab({ voices }: { voices: Voice[] }) {
  const [q, setQ] = useState('');
  const [providerTab, setProviderTab] = useState<ProviderKey | 'all'>('all');
  const [genderFilter, setGenderFilter] = useState<GenderKey | 'all'>('all');
  const [sceneFilter, setSceneFilter] = useState('');
  const [ageFilter, setAgeFilter] = useState('');
  const [dialectOnly, setDialectOnly] = useState(false);

  const [playingId, setPlayingId] = useState<string | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const playingRef = useRef<string | null>(null);
  // 试听走 Blob URL（/media 需要鉴权头，<audio src> 带不上）—— 需手动释放
  const objUrlRef = useRef<string | null>(null);

  const revokeObjUrl = () => {
    if (objUrlRef.current) {
      URL.revokeObjectURL(objUrlRef.current);
      objUrlRef.current = null;
    }
  };

  // 卸载时释放最后一次的 Blob URL
  useEffect(() => () => revokeObjUrl(), []);

  const providerVoices = useMemo(
    () => (providerTab === 'all' ? voices : voices.filter(v => voiceProvider(v) === providerTab)),
    [voices, providerTab]
  );
  const scenes = useMemo(() => sceneOptions(providerVoices), [providerVoices]);
  const ages = useMemo(() => ageOptions(providerVoices), [providerVoices]);

  const filtered = useMemo(
    () => filterVoices(voices, {
      q,
      provider: providerTab,
      gender: genderFilter,
      scene: sceneFilter || undefined,
      age: ageFilter || undefined,
      dialectOnly: dialectOnly || undefined,
    }),
    [voices, q, providerTab, genderFilter, sceneFilter, ageFilter, dialectOnly]
  );

  const stop = () => {
    if (audioRef.current) {
      audioRef.current.onended = null;
      audioRef.current.onerror = null;
      try { audioRef.current.pause(); } catch {}
    }
    revokeObjUrl();
    playingRef.current = null;
    setPlayingId(null);
  };

  const preview = async (voice: Voice) => {
    const voiceId = voice.id;
    if (playingRef.current === voiceId) {
      stop();
      return;
    }
    stop();
    setErr(null);
    setLoadingId(voiceId);
    try {
      const r = await api.preview(previewTextFor(voice), voiceId, 1.0);
      // /media 挂载点要求鉴权，而 <audio src> 无法携带请求头 → 先取字节转 Blob URL，
      // 否则直接塞 /media/xxx.mp3 会 401（试听全线播不出）
      const objUrl = await fetchMediaObjectUrl(r.audio_url);
      if (!audioRef.current) {
        URL.revokeObjectURL(objUrl);
        return;
      }
      revokeObjUrl();
      objUrlRef.current = objUrl;
      playingRef.current = voiceId;
      setPlayingId(voiceId);
      audioRef.current.onended = () => {
        if (playingRef.current === voiceId) {
          playingRef.current = null;
          setPlayingId(null);
        }
      };
      audioRef.current.onerror = () => {
        if (playingRef.current === voiceId) {
          playingRef.current = null;
          setPlayingId(null);
          setErr('试听播放失败');
        }
      };
      audioRef.current.src = objUrl;
      audioRef.current.play().catch(() => {
        if (playingRef.current === voiceId) {
          playingRef.current = null;
          setPlayingId(null);
        }
      });
    } catch (e) {
      setErr(String((e as Error)?.message || e));
    } finally {
      setLoadingId(prev => (prev === voiceId ? null : prev));
    }
  };

  return (
    <>
      <audio ref={audioRef} className="hidden" />
      {err && (
        <div className="rounded-lg px-4 py-3 text-sm text-red-200 border border-red-500/40 bg-red-500/10">
          {err}
        </div>
      )}

      {/* 筛选面板 */}
      <div className="glass-panel p-4 sm:p-5 space-y-3">
        <div className="relative">
          <svg
            className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-600"
            width="14" height="14" viewBox="0 0 24 24"
            fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          <input
            className="input !pl-9 !py-2.5"
            placeholder="搜索音色（名称/标签/场景/方言）"
            value={q}
            onChange={e => setQ(e.target.value)}
          />
        </div>

        <div className="flex items-center gap-1.5 flex-wrap">
          {([
            ['all', '全部'],
            ['doubao', PROVIDER_LABELS.doubao],
            ['minimax', PROVIDER_LABELS.minimax],
          ] as [ProviderKey | 'all', string][]).map(([key, label]) => {
            const count = key === 'all'
              ? voices.length
              : voices.filter(v => voiceProvider(v) === key).length;
            const disabled = count === 0;
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
                className={`px-3 py-1 rounded-md text-[12px] font-medium border transition-all
                  ${providerTab === key
                    ? 'bg-brand-500/15 border-brand-500/40 text-brand-300'
                    : 'bg-ink-100 border-ink-300/70 text-ink-600 hover:bg-ink-200 hover:text-ink-700'}
                  disabled:opacity-35 disabled:cursor-not-allowed`}
              >
                {label}
                <span className="ml-1 text-[10px] tabular-nums opacity-70">{count}</span>
              </button>
            );
          })}

          <div className="flex items-center gap-1.5 ml-auto flex-wrap">
            {(['all', '男声', '女声', '中性'] as (GenderKey | 'all')[]).map(gk => (
              <button
                key={gk}
                onClick={() => setGenderFilter(gk)}
                className={`px-2 py-0.5 rounded-md text-[11px] border transition-all
                  ${genderFilter === gk
                    ? 'bg-brand-500/15 border-brand-500/40 text-brand-300'
                    : 'bg-ink-100 border-ink-300/60 text-ink-600 hover:bg-ink-200'}`}
              >
                {gk === 'all' ? '性别不限' : gk}
              </button>
            ))}
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
        </div>

        <div className="text-[11px] text-ink-500 pt-1">
          匹配 <b className="text-ink-800 tabular-nums">{filtered.length}</b> / {voices.length} 个音色
        </div>
      </div>

      {/* 音色列表（一行一个） */}
      <div className="glass-panel overflow-hidden divide-y divide-ink-300/50">
        {filtered.map(v => {
          const pv = playingId === v.id;
          const lv = loadingId === v.id;
          const tags = (v.zh_tags || []).slice(0, 4);
          return (
            <div
              key={v.id}
              className={`flex items-center gap-3 px-4 py-2.5 hover:bg-ink-100/60 transition-colors group ${
                pv ? 'bg-brand-500/[0.06]' : ''
              }`}
            >
              <VoiceAvatar voice={v} size={32} />
              <div className="flex-1 min-w-0 flex items-center gap-3 flex-wrap">
                <span className="text-sm font-semibold text-ink-800 truncate">{v.name}</span>
                <span className="chip-soft !px-1.5 !py-0.5 !text-[10px] !font-medium">
                  {normalizeGender(v)}
                </span>
                {v.age && AGE_LABELS[v.age] && (
                  <span className="chip-soft !px-1.5 !py-0.5 !text-[10px] !font-medium">
                    {AGE_LABELS[v.age]}
                  </span>
                )}
                {v.dialect && (
                  <span className="chip-soft !px-1.5 !py-0.5 !text-[10px] !font-medium !text-amber-300">
                    {dialectLabel(v.dialect)}
                  </span>
                )}
                {tags.map(t => (
                  <span key={t} className="text-[10px] text-ink-500 px-1.5 py-0.5 rounded bg-white/[0.04] border border-white/[0.04]">
                    {t}
                  </span>
                ))}
                <span className="text-[10px] text-ink-500 font-mono ml-auto opacity-60 hidden md:inline">
                  {v.id.replace(/^(minimax|doubao):/, '')}
                </span>
              </div>
              <button
                onClick={() => preview(v)}
                disabled={lv}
                title={pv ? '停止试听' : '试听'}
                className={`shrink-0 grid place-items-center rounded-md border transition-all
                  ${pv
                    ? 'border-brand-500/50 bg-brand-500/15 text-brand-300 ring-2 ring-brand-500/25'
                    : 'border-ink-300/70 bg-ink-100 text-ink-600 hover:border-brand-500/30 hover:bg-brand-500/10 hover:text-brand-300'}
                  disabled:opacity-40 disabled:cursor-not-allowed`}
                style={{ width: 32, height: 32 }}
              >
                {lv ? (
                  <span className="w-3.5 h-3.5 border-2 border-brand-400/50 border-t-brand-300 rounded-full animate-spin" />
                ) : pv ? (
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
                    <rect x="6" y="5" width="4" height="14" rx="1" />
                    <rect x="14" y="5" width="4" height="14" rx="1" />
                  </svg>
                ) : (
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z" />
                  </svg>
                )}
              </button>
            </div>
          );
        })}
      </div>

      {filtered.length === 0 && (
        <div className="glass-panel text-center py-14 text-sm text-ink-500">
          没有匹配的音色，试试调整筛选条件
        </div>
      )}
    </>
  );
}
