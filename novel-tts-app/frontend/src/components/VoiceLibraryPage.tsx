'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { api, errToLog, Voice, IclTask } from '@/lib/api';
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

/**
 * 音色库页面（#/audiobooks/voices）
 *  Tab 1 音色库：MiniMax + 豆包 + 我的 ICL 复刻音色，多维筛选 + 试听
 *  Tab 2 声音复刻：上传参考音频创建 ICL 训练任务，轮询训练状态，管理我的音色
 */

const PREVIEW_TEXT = '夜色渐深，风穿过巷口，远处传来零星的犬吠声。';

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
  const [tab, setTab] = useState<'library' | 'icl'>('library');

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
              共 {voices.length} 个音色 · MiniMax / 豆包官方 / 自定义声音复刻
            </p>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <button
              onClick={() => setTab('library')}
              className={`px-3.5 py-1.5 rounded-md text-[13px] font-medium border transition-all
                ${tab === 'library'
                  ? 'bg-brand-500/15 border-brand-500/40 text-brand-300'
                  : 'bg-ink-100 border-ink-300/70 text-ink-600 hover:bg-ink-200 hover:text-ink-700'}`}
            >
              音色库
            </button>
            <button
              onClick={() => setTab('icl')}
              className={`px-3.5 py-1.5 rounded-md text-[13px] font-medium border transition-all
                ${tab === 'icl'
                  ? 'bg-brand-500/15 border-brand-500/40 text-brand-300'
                  : 'bg-ink-100 border-ink-300/70 text-ink-600 hover:bg-ink-200 hover:text-ink-700'}`}
            >
              声音复刻
            </button>
          </div>
        </div>
      </div>

      {tab === 'library' ? <LibraryTab voices={voices} /> : <IclTab />}
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
    playingRef.current = null;
    setPlayingId(null);
  };

  const preview = async (voiceId: string) => {
    if (playingRef.current === voiceId) {
      stop();
      return;
    }
    stop();
    setErr(null);
    setLoadingId(voiceId);
    try {
      const r = await api.preview(PREVIEW_TEXT, voiceId, 1.0);
      if (!audioRef.current) return;
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
      audioRef.current.src = r.audio_url;
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
            ['minimax', PROVIDER_LABELS.minimax],
            ['doubao', PROVIDER_LABELS.doubao],
            ['icl', PROVIDER_LABELS.icl],
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
                  {v.id.replace(/^(minimax|doubao|icl):/, '')}
                </span>
              </div>
              <button
                onClick={() => preview(v.id)}
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

// =====================================================================
// Tab 2：ICL 声音复刻
// =====================================================================
function IclTab() {
  const [tasks, setTasks] = useState<IclTask[]>([]);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const [voiceName, setVoiceName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [creating, setCreating] = useState(false);
  const [createErr, setCreateErr] = useState<string | null>(null);
  const [okTip, setOkTip] = useState<string | null>(null);

  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);

  const refresh = async () => {
    try {
      setTasks(await api.iclListTasks());
      setLoadErr(null);
    } catch (e) {
      setLoadErr(String((e as Error)?.message || e));
    } finally {
      setLoaded(true);
    }
  };

  useEffect(() => { refresh(); }, []);

  // 有排队/训练中任务时 5s 轮询：逐个打详情接口（会触发后端刷新豆包侧状态，
  // 服务重启后 worker 丢失也能恢复），再拉全量列表
  const runningTaskIds = tasks
    .filter(t => t.status === 0 || t.status === 1)
    .map(t => t.task_id)
    .join(',');
  const hasRunning = runningTaskIds !== '';
  useEffect(() => {
    if (!hasRunning) return;
    let cancelled = false;
    const ids = runningTaskIds.split(',').filter(Boolean);
    const poll = async () => {
      // 详情接口失败静默（后端出站查询已按任务节流，堆积属预期）
      await Promise.all(ids.map(async id => {
        try { await api.iclGetTask(id); } catch {}
      }));
      if (!cancelled) await refresh();
    };
    const timer = setInterval(poll, 5000);
    return () => { cancelled = true; clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runningTaskIds]);

  // 轮询到新可用的音色 → 通知 HomePage 重拉 /api/voices
  const usableCount = tasks.filter(t => t.usable).length;
  const prevUsableRef = useRef<number | null>(null);
  useEffect(() => {
    if (prevUsableRef.current === null) {
      prevUsableRef.current = usableCount;
      return;
    }
    if (usableCount > prevUsableRef.current) {
      window.dispatchEvent(new CustomEvent('app:voices-refreshed'));
    }
    prevUsableRef.current = usableCount;
  }, [usableCount]);

  const submit = async () => {
    setCreateErr(null);
    setOkTip(null);
    if (!voiceName.trim()) {
      setCreateErr('请填写音色名称');
      return;
    }
    if (!file) {
      setCreateErr('请选择 3~10 秒的参考音频文件（mp3/wav/m4a）');
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setCreateErr('参考音频过大：请上传 10MB 以内的录音片段');
      return;
    }
    setCreating(true);
    try {
      await api.iclCreateVoice(voiceName.trim(), file);
      setVoiceName('');
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
      setOkTip('训练任务已创建，训练通常需要几十秒到几分钟');
      await refresh();
    } catch (e) {
      setCreateErr(String((e as Error)?.message || e));
    } finally {
      setCreating(false);
    }
  };

  const doDelete = async (taskId: string) => {
    setDeletingId(taskId);
    try {
      await api.iclDeleteTask(taskId);
      window.dispatchEvent(new CustomEvent('app:voices-refreshed'));
      await refresh();
    } catch (e) {
      setLoadErr(String((e as Error)?.message || e));
    } finally {
      setDeletingId(null);
      setConfirmDeleteId(null);
    }
  };

  return (
    <div className="space-y-5">
      {/* 创建训练任务 */}
      <div className="glass-panel p-5 sm:p-6 space-y-4">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-md grid place-items-center shrink-0 bg-brand-600 text-white">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2v10" />
              <path d="m7.5 7.5 9 9" />
              <path d="M2 12h10" />
              <path d="m16.5 16.5 4.5 4.5" />
            </svg>
          </div>
          <div>
            <h3 className="text-base font-semibold text-ink-800">创建复刻音色</h3>
            <p className="text-xs text-ink-500 mt-0.5">
              上传 3~10 秒清晰人声录音（建议无背景音），基于豆包 ICL 2.0 复刻专属音色；
              训练完成后可在构建时为角色选择 <code className="text-brand-300">icl:</code> 音色。
            </p>
          </div>
        </div>

        <div className="grid sm:grid-cols-[1fr_1fr_auto] gap-2.5 items-end">
          <div className="space-y-1">
            <label className="text-xs text-ink-600">音色名称</label>
            <input
              className="input !py-2"
              placeholder="如：温柔女声·小雅"
              value={voiceName}
              maxLength={64}
              onChange={e => setVoiceName(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs text-ink-600">参考音频（mp3 / wav / m4a）</label>
            <input
              ref={fileInputRef}
              type="file"
              accept=".mp3,.wav,.m4a,audio/mpeg,audio/wav,audio/mp4"
              className="input !py-1.5 !text-[12px] file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:bg-ink-200 file:text-ink-700"
              onChange={e => setFile(e.target.files?.[0] || null)}
            />
          </div>
          <button className="btn-primary h-[38px]" onClick={submit} disabled={creating}>
            {creating ? (
              <>
                <span className="inline-block w-4 h-4 border-2 border-ink-400/70 border-t-white rounded-full animate-spin" />
                创建中…
              </>
            ) : (
              '开始训练'
            )}
          </button>
        </div>

        {createErr && (
          <div className="rounded-lg px-4 py-3 text-sm text-red-200 border border-red-500/40 bg-red-500/10">
            {createErr}
          </div>
        )}
        {okTip && (
          <div className="rounded-lg px-4 py-3 text-sm text-emerald-200 border border-emerald-500/40 bg-emerald-500/10">
            {okTip}
          </div>
        )}
      </div>

      {/* 任务列表 */}
      <div className="glass-panel p-5 sm:p-6 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-semibold text-ink-800">我的复刻音色</h3>
          <button className="btn-ghost !py-1 !px-2.5 text-xs" onClick={refresh}>
            刷新
          </button>
        </div>

        {loadErr && (
          <div className="rounded-lg px-4 py-3 text-sm text-red-200 border border-red-500/40 bg-red-500/10">
            {loadErr}
          </div>
        )}

        {loaded && tasks.length === 0 && !loadErr && (
          <div className="text-center py-12 text-sm text-ink-500">
            还没有复刻音色，上传参考音频开始训练
          </div>
        )}

        <div className="space-y-2.5">
          {tasks.map(t => {
            const running = t.status === 0 || t.status === 1;
            const failed = t.status === 3;
            // 以 cloned_voice_id（voice_id）判断"音色可选"，与 /api/voices 聚合口径一致
            const ready = !!t.voice_id;
            return (
              <div
                key={t.task_id}
                className="rounded-lg border border-ink-300/70 bg-ink-100 p-3.5 flex items-center gap-3 hover:border-ink-400 transition-all"
              >
                <div
                  className={`w-9 h-9 rounded-lg grid place-items-center shrink-0 text-white text-sm font-bold
                    ${failed ? 'bg-red-500/60' : running ? 'bg-brand-500/60' : 'bg-emerald-500/60'}`}
                >
                  {t.voice_name?.trim()?.[0] || '音'}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-ink-800 truncate">{t.voice_name}</span>
                    <span
                      className={`chip-soft !px-1.5 !py-0.5 !text-[11px]
                        ${failed ? '!text-red-300' : ready ? '!text-emerald-300' : ''}`}
                    >
                      {running && (
                        <span className="inline-block w-2 h-2 border-2 border-brand-400/50 border-t-brand-300 rounded-full animate-spin mr-1 align-middle" />
                      )}
                      {t.status_label}
                    </span>
                    {ready && t.voice_id && (
                      <code className="text-[10px] text-ink-500 truncate">{t.voice_id}</code>
                    )}
                  </div>
                  <div className="text-[11px] text-ink-500 mt-0.5 truncate">
                    {failed && t.error_msg
                      ? t.error_msg
                      : ready
                        ? '训练完成 · 可在构建时为旁白或角色选择该音色'
                        : running
                          ? '正在豆包侧训练，完成后自动出现在音色库…'
                          : '排队等待训练…'}
                  </div>
                </div>
                <div className="shrink-0">
                  {confirmDeleteId === t.task_id ? (
                    <div className="flex items-center gap-1.5">
                      <button
                        className="btn-primary !py-1 !px-2.5 text-xs"
                        disabled={deletingId === t.task_id}
                        onClick={() => doDelete(t.task_id)}
                      >
                        {deletingId === t.task_id ? '删除中…' : '确认删除'}
                      </button>
                      <button
                        className="btn-ghost !py-1 !px-2.5 text-xs"
                        onClick={() => setConfirmDeleteId(null)}
                      >
                        取消
                      </button>
                    </div>
                  ) : (
                    <button
                      className="btn-ghost !py-1 !px-2.5 text-xs hover:!text-red-300"
                      onClick={() => setConfirmDeleteId(t.task_id)}
                      title="删除训练任务与参考音频"
                    >
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M3 6h18" />
                        <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6" />
                        <path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2" />
                      </svg>
                      删除
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
