'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  api,
  ProjectDetailResp,
  BuildListItem,
  BuildDetailResp,
  BuildStatusResp,
  Voice,
  CharacterWithVoice,
  ChapterSummary,
} from '@/lib/api';
import VoicePicker from './VoicePicker';
import WaveformPlayer from './WaveformPlayer';
import { StatusBadge } from './ProjectListPage';

type Tab = 'overview' | 'chapters' | 'voices' | 'builds' | 'settings';

const TAB_LABELS: { key: Tab; label: string; icon: JSX.Element }[] = [
  {
    key: 'overview',
    label: '概览',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>,
  },
  {
    key: 'chapters',
    label: '章节',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/></svg>,
  },
  {
    key: 'voices',
    label: '音色',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg>,
  },
  {
    key: 'builds',
    label: '构建',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>,
  },
  {
    key: 'settings',
    label: '设置',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>,
  },
];

function formatSize(bytes: number | null | undefined): string {
  if (bytes == null) return '—';
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}
function formatDuration(sec: number | null | undefined): string {
  if (sec == null) return '—';
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}分${s}秒`;
}
function formatMs(ms: number | null | undefined): string {
  if (ms == null) return '';
  if (ms >= 60000) return `${(ms / 60000).toFixed(1)}m`;
  return `${(ms / 1000).toFixed(1)}s`;
}
function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    const t = new Date(iso).getTime();
    const diff = Date.now() - t;
    if (diff < 60 * 1000) return '刚刚';
    if (diff < 60 * 60 * 1000) return `${Math.floor(diff / 60000)} 分钟前`;
    if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / 3600000)} 小时前`;
    if (diff < 7 * 24 * 60 * 60 * 1000) return `${Math.floor(diff / 86400000)} 天前`;
    return new Date(iso).toLocaleString('zh-CN');
  } catch {
    return iso;
  }
}

export default function ProjectDetailPage({
  projectId,
  voices,
}: {
  projectId: string;
  voices: Voice[];
}) {
  const [project, setProject] = useState<ProjectDetailResp | null>(null);
  const [builds, setBuilds] = useState<BuildListItem[]>([]);
  const [tab, setTab] = useState<Tab>('overview');
  const [err, setErr] = useState<string | null>(null);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playingKey, setPlayingKey] = useState<string | null>(null);
  const [loadingVoice, setLoadingVoice] = useState<string | null>(null);

  const narratorDefault = useMemo(
    () => voices.find(v => v.id === 'male-qn-jingying') || voices[0],
    [voices]
  );

  const reload = async () => {
    setErr(null);
    try {
      const [detail, bl] = await Promise.all([
        api.projectGet(projectId),
        api.buildList(projectId),
      ]);
      setProject(detail);
      setBuilds(
        [...bl].sort(
          (a, b) =>
            new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
        )
      );
    } catch (e: any) {
      setErr(String(e?.message || e));
    }
  };

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const preparing = project?.status === 'preparing';
  useEffect(() => {
    if (!preparing) return;
    const timer = setInterval(() => {
      reload();
    }, 2000);
    return () => clearInterval(timer);
  }, [preparing, projectId]);

  const stopPlayback = () => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
    setPlayingKey(null);
  };
  const playUrl = (key: string, url: string) => {
    if (!audioRef.current) return;
    audioRef.current.src = url;
    audioRef.current.onended = () => setPlayingKey(null);
    audioRef.current.onerror = () => setPlayingKey(null);
    setPlayingKey(key);
    audioRef.current.play().catch(() => setPlayingKey(null));
  };
  const togglePlay = (key: string, url: string | null) => {
    if (!url) return;
    if (playingKey === key) {
      stopPlayback();
      return;
    }
    stopPlayback();
    playUrl(key, url);
  };

  const togglePreviewVoice = async (voiceId: string, text: string, speed: number) => {
    const key = `voice_${voiceId}`;
    if (playingKey === key) {
      stopPlayback();
      return;
    }
    stopPlayback();
    setLoadingVoice(voiceId);
    try {
      const r = await api.preview(text.slice(0, 80), voiceId, speed);
      playUrl(key, r.audio_url);
    } finally {
      setLoadingVoice(prev => (prev === voiceId ? null : prev));
    }
  };

  const goBack = () => {
    window.location.hash = '#/projects';
  };

  const loading = project === null;

  if (loading) {
    return (
      <div className="glass-panel text-center py-16 text-ink-600">
        <div className="inline-block w-8 h-8 border-[3px] border-brand-500 border-t-transparent rounded-full animate-spin mb-3" />
        <div className="text-sm">加载项目详情…</div>
      </div>
    );
  }

  return (
    <section className="space-y-5">
      <audio ref={audioRef} className="hidden" />

      {err && (
        <div className="rounded-2xl border border-red-500/40 bg-red-500/10 backdrop-blur px-4 py-3 text-sm text-red-200 flex items-center gap-3">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          <span className="flex-1 min-w-0">{err}</span>
          <button className="btn-ghost !py-1 !px-2.5 text-xs" onClick={reload}>重试</button>
        </div>
      )}

      {/* ============ Header ============ */}
      <div className="glass-panel p-5 sm:p-6 relative overflow-hidden animate-fade-in">
        <div className="glow-orb w-72 h-72 bg-brand-500/15" style={{ top: '-40px', right: '-40px' }} />
        <div className="glow-orb w-64 h-64 bg-accent-teal/10" style={{ bottom: '-60px', left: '-30px' }} />
        <div
          className="stripe rounded-l-3xl"
          style={{ backgroundColor: project!.cover_color || '#8b5cf6' }}
        />
        <div className="pl-3 flex items-center justify-between gap-3 flex-wrap relative">
          <div className="flex items-center gap-3 min-w-0 flex-1">
            <button
              className="btn-ghost shrink-0 !px-2.5 !py-2"
              onClick={goBack}
              title="返回项目列表"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="m15 18-6-6 6-6"/></svg>
            </button>
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <h2 className="headline text-xl sm:text-2xl truncate">
                  {project!.book_title || project!.name}
                </h2>
                <StatusBadge status={project!.status} />
              </div>
              <div className="text-xs text-ink-500 mt-1 flex flex-wrap items-center gap-x-2">
                <span>{project!.name}</span>
                {project!.source_filename && <span>· {project!.source_filename}</span>}
                {project!.chapter_count > 0 && <span>· {project!.chapter_count} 章</span>}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button className="btn-ghost !py-2" onClick={reload}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>
              刷新
            </button>
          </div>
        </div>
      </div>

      {/* ============ Tab 切换条（pill 风格） ============ */}
      <div className="glass-panel !p-1.5 relative overflow-hidden">
        <div className="flex items-center gap-1 overflow-x-auto py-0.5">
          {TAB_LABELS.map(t => {
            const active = tab === t.key;
            return (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={`flex items-center gap-2 px-3.5 sm:px-4 py-2 rounded-xl text-sm font-medium whitespace-nowrap transition-all duration-200
                  ${active
                    ? 'text-white shadow-brand'
                    : 'text-ink-600 hover:text-ink-800 hover:bg-white/[0.04]'
                  }`}
                style={active ? {
                  backgroundImage: 'linear-gradient(180deg, rgba(255,255,255,0.14) 0%, rgba(255,255,255,0) 50%), linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)',
                } : {}}
              >
                <span className={active ? 'text-white/90' : ''}>{t.icon}</span>
                <span>{t.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* ============ Tab 内容 ============ */}
      {tab === 'overview' && (
        <OverviewTab project={project!} onTab={setTab} onReload={reload} />
      )}
      {tab === 'chapters' && (
        <ChaptersTab
          project={project!}
          playingKey={playingKey}
          onTogglePlay={togglePlay}
        />
      )}
      {tab === 'voices' && (
        <VoicesTab
          project={project!}
          voices={voices}
          narratorDefault={narratorDefault}
          playingKey={playingKey}
          loadingVoice={loadingVoice}
          onPreviewVoice={togglePreviewVoice}
          onReload={reload}
        />
      )}
      {tab === 'builds' && (
        <BuildsTab
          projectId={projectId}
          project={project!}
          builds={builds}
          voices={voices}
          narratorDefault={narratorDefault}
          playingKey={playingKey}
          onTogglePlay={togglePlay}
          onReload={reload}
        />
      )}
      {tab === 'settings' && (
        <SettingsTab project={project!} voices={voices} onReload={reload} />
      )}
    </section>
  );
}

// =================== Overview Tab ===================
function OverviewTab({
  project,
  onTab,
  onReload,
}: {
  project: ProjectDetailResp;
  onTab: (t: Tab) => void;
  onReload: () => void;
}) {
  const [prepareTip, setPrepareTip] = useState<string | null>(null);
  const prog = project.prepare_progress;

  const needsPrepare =
    project.status === 'draft' || (project.status === 'imported' && project.chapter_count === 0);

  const isPreparing = project.status === 'preparing';
  const hasPrepareError = Boolean(prog?.last_error);

  const failedCharSlicesN = prog?.char_failed_slices_n ?? 0;
  const failedDialogueBatchesN = prog?.dialogue_failed_batch_count ?? 0;
  const hasPartialFailures = failedCharSlicesN > 0 || failedDialogueBatchesN > 0;

  const handlePrepare = async () => {
    setPrepareTip(null);
    try {
      const res = await api.projectPrepare(project.project_id);
      setPrepareTip(res.message || '已开始后台识别，请稍候刷新查看进度。');
      setTimeout(() => setPrepareTip(null), 8000);
      await onReload();
    } catch (e: any) {
      alert(`识别触发失败: ${e?.message || e}`);
    }
  };

  function StageProgressBar(props: { label: string; done: number; total: number; failed?: number }) {
    const { label, done, total, failed = 0 } = props;
    if (total == null || total <= 0) {
      return (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-xs text-ink-500">
            <span>{label}</span>
            <span>等待中…</span>
          </div>
          <div className="progress-track" />
        </div>
      );
    }
    const donePct = Math.min(100, Math.round((done / total) * 100));
    const failedPct = Math.min(100 - donePct, Math.round((failed / total) * 100));
    return (
      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-xs text-ink-500 flex-wrap gap-2">
          <span>{label}</span>
          <span className="tabular-nums">
            {done}/{total}
            {failed > 0 && <span className="text-red-400 ml-2">失败 {failed}</span>}
          </span>
        </div>
        <div className="progress-track flex">
          <div
            className="h-full rounded-full"
            style={{
              width: `${donePct}%`,
              backgroundImage: 'linear-gradient(90deg, #8b5cf6 0%, #6366f1 60%, #2dd4bf 100%)',
            }}
          />
          {failed > 0 && (
            <div className="bg-red-500 h-full rounded-full" style={{ width: `${failedPct}%` }} />
          )}
        </div>
      </div>
    );
  }

  function stageLabel(s?: string): string {
    switch (s) {
      case 'start': return '🔧 初始化';
      case 'split': return '📑 切分章节';
      case 'characters': return '🧑 角色识别';
      case 'dedup': return '🔀 角色去重';
      case 'dialogues': return '💬 对白归属';
      case 'voice_recs': return '🎙 音色推荐';
      case 'done': return '✅ 已完成';
      default: return s ? `运行中（${s}）` : '运行中';
    }
  }

  return (
    <div className="space-y-4">
      {prepareTip && (
        <div className="rounded-2xl border border-blue-500/40 bg-blue-500/10 backdrop-blur px-4 py-3 text-sm text-blue-200 flex items-center gap-2">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/><path d="m9 12 2 2 4-4"/></svg>
          {prepareTip}
        </div>
      )}

      {hasPrepareError && (
        <div className="glass-panel border-red-500/40 !bg-red-500/5 space-y-3">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div className="min-w-0 flex items-start gap-2">
              <div className="w-9 h-9 rounded-xl grid place-items-center bg-red-500/20 text-red-300 shrink-0">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
              </div>
              <div className="min-w-0">
                <div className="font-semibold text-red-200 mb-1">识别失败</div>
                <div className="text-sm text-red-200/90 break-words whitespace-pre-wrap">
                  {prog!.last_error}
                </div>
                {prog!.last_error_at && (
                  <div className="text-[11px] text-ink-500 mt-1 tabular-nums">
                    时间: {prog!.last_error_at}
                    {prog!.last_error_type && ` · 类型: ${prog!.last_error_type}`}
                  </div>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <button className="btn-ghost text-xs !py-1.5" onClick={() => onReload()}>刷新</button>
              <button className="btn-danger !py-1.5 !px-3 text-xs" onClick={handlePrepare}>🔁 重新识别</button>
            </div>
          </div>
          {prog?.prev_error?.msg && (
            <details className="text-[11px] text-ink-500">
              <summary className="cursor-pointer select-none">上次失败记录</summary>
              <div className="mt-1 whitespace-pre-wrap break-words">
                {prog.prev_error.at && `${prog.prev_error.at}  `}
                {prog.prev_error.msg}
              </div>
            </details>
          )}
        </div>
      )}

      {isPreparing && (
        <div className="glass-panel space-y-4 relative overflow-hidden">
          <div className="glow-orb w-48 h-48 bg-brand-500/10" style={{ top: '-30px', right: '-20px' }} />
          <div className="flex items-center justify-between gap-3 flex-wrap relative">
            <div className="flex items-center gap-3 min-w-0">
              <div className="w-10 h-10 rounded-xl grid place-items-center shrink-0 shadow-brand"
                style={{ backgroundImage: 'linear-gradient(135deg, #8b5cf6, #7c3aed)' }}>
                <span className="w-4 h-4 border-[2.5px] border-white/40 border-t-white rounded-full animate-spin" />
              </div>
              <div className="min-w-0">
                <div className="font-semibold text-ink-800 truncate">{stageLabel(prog?.stage)}</div>
                {prog?.updated_at && (
                  <div className="text-[11px] text-ink-500 tabular-nums mt-0.5">
                    更新于 {relativeTime(prog.updated_at)}
                  </div>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <button className="btn-ghost text-xs !py-1.5" onClick={() => onReload()}>刷新</button>
              <button className="btn-ghost text-xs !py-1.5" onClick={handlePrepare}>🔁 重跑</button>
            </div>
          </div>

          <div className="space-y-3 relative">
            {(prog?.char_slice_total != null) && (
              <StageProgressBar
                label="角色识别（切片）"
                done={prog?.char_slice_completed_n ?? 0}
                total={prog.char_slice_total}
                failed={failedCharSlicesN}
              />
            )}
            {prog?.dedup_done && (
              <div className="text-xs text-ink-500 flex items-center gap-2">
                <span className="inline-flex w-4 h-4 rounded-full bg-accent-lime/20 text-accent-lime items-center justify-center text-[10px]">✓</span>
                角色去重完成
              </div>
            )}
            {(prog?.dialogue_total_chapters != null || prog?.dialogue_total_batches != null) && (
              <StageProgressBar
                label={
                  prog?.dialogue_total_batches != null
                    ? `对白归属（${prog.dialogue_total_batches} 批）`
                    : '对白归属（章节）'
                }
                done={
                  prog?.dialogue_total_batches != null
                    ? (prog?.dialogue_completed_batches_count ?? 0)
                    : (prog?.dialogue_completed_chapters_count ?? 0)
                }
                total={
                  prog?.dialogue_total_batches != null
                    ? prog.dialogue_total_batches
                    : (prog?.dialogue_total_chapters ?? 0)
                }
                failed={failedDialogueBatchesN}
              />
            )}
            {prog?.voice_recs_done && (
              <div className="text-xs text-ink-500 flex items-center gap-2">
                <span className="inline-flex w-4 h-4 rounded-full bg-accent-lime/20 text-accent-lime items-center justify-center text-[10px]">✓</span>
                音色推荐完成
              </div>
            )}
          </div>

          {hasPartialFailures && !hasPrepareError && (
            <div className="rounded-2xl border border-orange-500/30 bg-orange-500/10 px-3 py-2 text-xs text-orange-200">
              ⚠️ 部分切片/批失败，将在重跑识别时自动补跑：
              {failedCharSlicesN > 0 && <span className="ml-2">角色切片失败 {failedCharSlicesN} 个</span>}
              {failedCharSlicesN > 0 && failedDialogueBatchesN > 0 && '，'}
              {failedDialogueBatchesN > 0 && <span className="ml-2">对白批失败 {failedDialogueBatchesN} 个</span>}
            </div>
          )}
        </div>
      )}

      {needsPrepare && !isPreparing && (
        <div className="glass-panel !border-brand-500/40 !bg-brand-500/[0.06] relative overflow-hidden">
          <div className="glow-orb w-48 h-48 bg-brand-500/20" style={{ bottom: '-40px', right: '-30px' }} />
          <div className="flex items-center justify-between gap-3 flex-wrap relative">
            <div className="flex items-start gap-3 min-w-0 flex-1">
              <div className="w-10 h-10 rounded-xl grid place-items-center bg-brand-500/20 text-brand-300 shrink-0">
                🚀
              </div>
              <div className="min-w-0">
                <div className="font-semibold text-ink-800 mb-0.5">还没识别章节与角色</div>
                <div className="text-sm text-ink-600">
                  {project.source_filename
                    ? `已上传文件「${project.source_filename}」，点击右侧按钮开始识别章节、角色与对白归属。`
                    : '请先到「设置」上传源文件或粘贴文本，然后开始识别。'}
                </div>
              </div>
            </div>
            {project.source_filename && (
              <button className="btn-primary shrink-0" onClick={handlePrepare}>
                🚀 开始识别
              </button>
            )}
          </div>
        </div>
      )}

      {project.status === 'ready' && hasPartialFailures && !isPreparing && (
        <div className="glass-panel !border-orange-500/30 !bg-orange-500/[0.05]">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="text-sm">
              <span className="font-semibold text-ink-800">🔁 可补跑失败部分：</span>{' '}
              <span className="text-ink-600">
                {failedCharSlicesN > 0 && <>角色识别失败切片 {failedCharSlicesN} 个</>}
                {failedCharSlicesN > 0 && failedDialogueBatchesN > 0 && '，'}
                {failedDialogueBatchesN > 0 && <>对白归属失败批 {failedDialogueBatchesN} 个</>}
              </span>
            </div>
            <button className="btn-ghost text-xs !py-1.5" onClick={handlePrepare}>
              🔁 重新识别（自动补跑失败部分）
            </button>
          </div>
        </div>
      )}

      {/* 概览 4 卡 */}
      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
        <OverviewStat label="章节数" value={String(project.chapter_count)} icon="📜" color="#8b5cf6" />
        <OverviewStat label="角色数" value={String(project.characters.length)} icon="🧑" color="#ec4899" />
        <OverviewStat label="文件大小" value={formatSize(project.source_file_size)} icon="📄" color="#0ea5e9" />
        <OverviewStat label="创建时间" value={new Date(project.created_at).toLocaleDateString('zh-CN')} icon="🗓" color="#c6f44a" />
      </div>

      {(project.description || (project.tags && project.tags.length > 0)) && (
        <div className="glass-panel space-y-3">
          {project.description && (
            <div>
              <div className="text-xs text-ink-500 mb-1 tracking-wider uppercase">描述</div>
              <div className="text-sm text-ink-700 leading-relaxed">{project.description}</div>
            </div>
          )}
          {project.tags && project.tags.length > 0 && (
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs text-ink-500">标签</span>
              {project.tags.map((t, i) => (
                <span key={i} className="chip-soft">#{t}</span>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="glass-panel space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-ink-800 flex items-center gap-2">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>
            最近一次构建
          </h3>
          <button className="btn-ghost !py-1.5 text-xs" onClick={() => onTab('builds')}>
            查看全部 →
          </button>
        </div>
        {project.last_build ? (
          <LastBuildSummary
            build={project.last_build}
            onGotoBuilds={() => onTab('builds')}
          />
        ) : (
          <div className="text-sm text-ink-500 py-3">
            还没有构建记录，到「构建」Tab 启动第一次生成。
          </div>
        )}
      </div>

      <div className="grid sm:grid-cols-3 gap-3">
        {[
          { key: 'chapters' as Tab, title: '查看章节', sub: `共 ${project.chapter_count} 章 · 试听下载`, icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/></svg>, color: '#8b5cf6' },
          { key: 'voices' as Tab, title: '配置音色', sub: `${project.characters.length} 个角色 · 旁白与语速`, icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>, color: '#ec4899' },
          { key: 'settings' as Tab, title: '项目设置', sub: '名称 · 描述 · 标签 · 默认配置', icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 1v6m0 10v6m11-11h-6M7 12H1m15.5-7.5-4.2 4.2M11.7 12.3 7.5 16.5m0-9 4.2 4.2m4.8 4.8 4.2 4.2"/></svg>, color: '#0ea5e9' },
        ].map(it => (
          <button
            key={it.key}
            onClick={() => onTab(it.key)}
            className="glass-panel text-left !p-4 flex items-start gap-3 hover:!border-brand-500/40 transition-all group"
          >
            <div
              className="w-10 h-10 rounded-xl grid place-items-center shrink-0 transition-all group-hover:scale-105"
              style={{
                background: `linear-gradient(135deg, ${it.color}33 0%, ${it.color}11 100%)`,
                border: `1px solid ${it.color}55`,
                color: it.color,
              }}
            >
              {it.icon}
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-ink-800">{it.title}</div>
              <div className="text-xs text-ink-500 mt-0.5">{it.sub}</div>
            </div>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-ink-500 shrink-0 group-hover:translate-x-0.5 group-hover:text-brand-400 transition-all"><path d="m9 18 6-6-6-6"/></svg>
          </button>
        ))}
      </div>
    </div>
  );
}

function OverviewStat({
  label, value, icon, color,
}: { label: string; value: string; icon: string; color: string }) {
  return (
    <div className="glass-panel !p-4 relative overflow-hidden group">
      <div
        className="absolute -right-8 -top-8 w-24 h-24 rounded-full pointer-events-none opacity-40 transition-opacity group-hover:opacity-70"
        style={{ background: `radial-gradient(circle, ${color}33 0%, transparent 70%)` }}
      />
      <div className="flex items-center justify-between relative">
        <span className="text-xs text-ink-500 tracking-wide">{label}</span>
        <span className="text-lg opacity-70">{icon}</span>
      </div>
      <div
        className="text-2xl sm:text-[28px] font-bold font-display tracking-tight mt-2 truncate relative"
        style={{
          background: `linear-gradient(135deg, ${color} 0%, ${color}cc 100%)`,
          WebkitBackgroundClip: 'text',
          WebkitTextFillColor: 'transparent',
        }}
      >
        {value}
      </div>
    </div>
  );
}

function LastBuildSummary({
  build,
  onGotoBuilds,
}: {
  build: NonNullable<ProjectDetailResp['last_build']>;
  onGotoBuilds: () => void;
}) {
  const pct =
    build.total_chapters > 0
      ? Math.round((build.completed_chapters / build.total_chapters) * 100)
      : 0;
  return (
    <div className="space-y-2.5">
      <div className="flex items-center gap-3 flex-wrap">
        <StatusBadge status={build.status} />
        <span className="text-sm text-ink-600 tabular-nums">
          {build.completed_chapters} / {build.total_chapters} 章 · {pct}%
        </span>
        <span className="text-xs text-ink-500 ml-auto tabular-nums">
          {relativeTime(build.created_at)}
        </span>
      </div>
      <div className="progress-track">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{
            width: `${pct}%`,
            backgroundImage:
              build.status === 'done'
                ? 'linear-gradient(90deg, #c6f44a 0%, #2dd4bf 100%)'
                : build.status === 'failed'
                ? 'linear-gradient(90deg, #ef4444, #dc2626)'
                : build.status === 'synthesizing' || build.status === 'preparing'
                ? 'linear-gradient(90deg, #f59e0b, #fbbf24)'
                : 'linear-gradient(90deg, #8b5cf6, #6366f1)',
          }}
        />
      </div>
      <button className="btn-ghost !py-1 text-xs" onClick={onGotoBuilds}>
        查看构建详情 →
      </button>
    </div>
  );
}

// =================== Chapters Tab ===================
function ChaptersTab({
  project,
  playingKey,
  onTogglePlay,
}: {
  project: ProjectDetailResp;
  playingKey: string | null;
  onTogglePlay: (key: string, url: string | null) => void;
}) {
  const chapters: ChapterSummary[] = project.chapters;
  const lastBuild = project.last_build;
  const hasAudio = lastBuild && lastBuild.status === 'done';

  if (chapters.length === 0) {
    return (
      <div className="glass-panel text-center py-16 text-ink-500">
        <div className="mx-auto mb-3 w-14 h-14 rounded-2xl grid place-items-center bg-white/[0.04] border border-white/[0.07] text-3xl">
          📭
        </div>
        <div className="text-sm font-medium text-ink-700">还没有章节</div>
        <div className="text-xs text-ink-500 mt-1">请先到「概览」触发识别</div>
      </div>
    );
  }

  return (
    <div className="glass-panel space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h3 className="font-semibold text-ink-800 flex items-center gap-2">
          <span className="chip-soft">章节</span>
          <span className="text-sm text-ink-500">{chapters.length} 章</span>
        </h3>
        {hasAudio && (
          <span className="chip-soft">🎧 试听来自最近一次完成的构建</span>
        )}
      </div>

      <div className="space-y-2 max-h-[640px] overflow-y-auto pr-1">
        {chapters.map(c => {
          const key = `ch_${c.idx}`;
          const playing = playingKey === key;
          const audioUrl = hasAudio
            ? api.buildChapterAudioUrl(project.project_id, lastBuild!.build_id, c.idx)
            : null;
          return (
            <div
              key={c.idx}
              className={`rounded-2xl p-3 transition-all duration-150 border
                ${playing
                  ? 'bg-brand-500/10 border-brand-500/40 shadow-brand/30'
                  : 'bg-white/[0.02] border-white/[0.04] hover:bg-white/[0.045] hover:border-white/[0.10]'
                }`}
            >
              <div className="flex items-center gap-2 mb-2">
                <span
                  className="text-[11px] font-mono tabular-nums shrink-0 rounded-lg px-2 py-1"
                  style={{
                    background: 'rgba(255,255,255,0.04)',
                    color: playing ? '#c4b5fd' : 'rgba(255,255,255,0.45)',
                    border: '1px solid rgba(255,255,255,0.05)',
                  }}
                >
                  #{String(c.idx + 1).padStart(3, '0')}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-ink-800 truncate">{c.title || '(无标题)'}</div>
                </div>
                <span className="text-[11px] text-ink-500 tabular-nums shrink-0 chip-soft">
                  {c.text_len} 字
                </span>
              </div>
              {hasAudio && audioUrl ? (
                <WaveformPlayer
                  src={audioUrl}
                  compact
                  onDownload={() => {
                    const a = document.createElement('a');
                    a.href = api.buildChapterDownload(
                      project.project_id,
                      lastBuild!.build_id,
                      c.idx
                    );
                    a.download = '';
                    a.click();
                  }}
                />
              ) : null}
            </div>
          );
        })}
      </div>
      {!hasAudio && (
        <div className="text-xs text-ink-500 pt-3 border-t border-white/[0.05]">
          完成一次构建后，此页面将显示每章的波形播放器与下载按钮。
        </div>
      )}
    </div>
  );
}

// =================== Voices Tab ===================
function VoicesTab({
  project, voices, narratorDefault, playingKey, loadingVoice, onPreviewVoice, onReload,
}: {
  project: ProjectDetailResp;
  voices: Voice[];
  narratorDefault: Voice | undefined;
  playingKey: string | null;
  loadingVoice: string | null;
  onPreviewVoice: (voiceId: string, text: string, speed: number) => void;
  onReload: () => void;
}) {
  const [narratorVoice, setNarratorVoice] = useState<string>(
    project.default_narrator_voice_id || narratorDefault?.id || ''
  );
  const [speed, setSpeed] = useState<number>(project.default_speed ?? 1.0);
  const [savingDefault, setSavingDefault] = useState(false);
  const [savedTip, setSavedTip] = useState(false);
  const [chars, setChars] = useState<CharacterWithVoice[]>(project.characters);

  useEffect(() => {
    setNarratorVoice(project.default_narrator_voice_id || narratorDefault?.id || '');
    setSpeed(project.default_speed ?? 1.0);
    setChars(project.characters);
  }, [project.project_id, project.default_narrator_voice_id, project.default_speed, project.characters, narratorDefault]);

  const saveDefaults = async () => {
    setSavingDefault(true);
    try {
      await api.projectUpdate(project.project_id, {
        default_narrator_voice_id: narratorVoice,
        default_speed: speed,
      });
      setSavedTip(true);
      setTimeout(() => setSavedTip(false), 1500);
      await onReload();
    } catch (e: any) {
      alert(`保存失败: ${e?.message || e}`);
    } finally {
      setSavingDefault(false);
    }
  };

  const onChangeCharVoice = async (charId: number, voiceId: string) => {
    setChars(prev =>
      prev.map(c => (c.id === charId ? { ...c, assigned_voice_id: voiceId } : c))
    );
    try {
      await api.projectUpdateCharVoice(project.project_id, charId, voiceId);
    } catch (e: any) {
      alert(`保存角色音色失败: ${e?.message || e}`);
      setChars(prev =>
        prev.map(c =>
          c.id === charId
            ? { ...c, assigned_voice_id: project.characters.find(x => x.id === charId)?.assigned_voice_id || null }
            : c
        )
      );
    }
  };

  return (
    <div className="space-y-4">
      <div className="glass-panel space-y-5 p-5 sm:p-6 relative overflow-hidden">
        <div className="glow-orb w-56 h-56 bg-brand-500/10" style={{ top: '-40px', right: '-40px' }} />
        <h3 className="font-semibold text-ink-800 flex items-center gap-2 relative">
          <div className="w-8 h-8 rounded-xl grid place-items-center bg-brand-500/20 text-brand-300">🎙</div>
          <div>
            <div>旁白音色 & 语速 <span className="text-xs text-ink-500 font-normal">（项目默认）</span></div>
          </div>
        </h3>
        <div className="relative">
          <div className="text-sm text-ink-700 mb-2">旁白音色</div>
          <VoicePicker
            voices={voices}
            value={narratorVoice}
            onChange={setNarratorVoice}
            onPreview={vid =>
              onPreviewVoice(
                vid,
                '这是一段旁白示例文本，用于试听所选音色的朗读效果。',
                speed
              )
            }
            isPlaying={playingKey === `voice_${narratorVoice}`}
            isLoading={loadingVoice === narratorVoice}
            playingVoiceId={playingKey?.startsWith('voice_') ? playingKey.slice(6) : null}
            loadingVoiceId={loadingVoice}
          />
        </div>

        <div className="relative">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-ink-700">语速</span>
            <span
              className="chip-soft font-mono tabular-nums"
              style={{ color: '#c4b5fd' }}
            >
              {speed.toFixed(1)}x
            </span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-xs text-ink-500">0.5x</span>
            <div className="flex-1 relative">
              <input
                type="range"
                min={0.5}
                max={2.0}
                step={0.1}
                value={speed}
                onChange={e => setSpeed(parseFloat(e.target.value))}
                className="w-full h-2 rounded-full appearance-none cursor-pointer"
                style={{
                  background: `linear-gradient(90deg, #8b5cf6 0%, #8b5cf6 ${((speed - 0.5) / 1.5) * 100}%, rgba(255,255,255,0.08) ${((speed - 0.5) / 1.5) * 100}%, rgba(255,255,255,0.08) 100%)`,
                }}
              />
            </div>
            <span className="text-xs text-ink-500">2.0x</span>
          </div>
        </div>

        <div className="flex items-center gap-3 relative">
          <button className="btn-primary" disabled={savingDefault} onClick={saveDefaults}>
            {savingDefault ? (
              <>
                <span className="inline-block w-4 h-4 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                保存中…
              </>
            ) : (
              <>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                保存为项目默认
              </>
            )}
          </button>
          {savedTip && (
            <span className="text-xs text-accent-lime flex items-center gap-1">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
              已保存
            </span>
          )}
        </div>
      </div>

      <div className="glass-panel space-y-4">
        <h3 className="font-semibold text-ink-800 flex items-center gap-2">
          <div className="w-8 h-8 rounded-xl grid place-items-center bg-accent-rose/20 text-accent-rose">🧑‍🤝‍🧑</div>
          角色音色
          <span className="chip-soft">{chars.length} 个角色</span>
        </h3>
        {chars.length === 0 ? (
          <div className="rounded-3xl border border-dashed border-white/[0.1] p-10 text-center">
            <div className="mx-auto mb-3 w-14 h-14 rounded-2xl grid place-items-center bg-white/[0.04] border border-white/[0.07] text-3xl">🎭</div>
            <div className="text-sm font-medium text-ink-700">还没有识别到角色</div>
            <div className="text-xs text-ink-500 mt-1">请先到「概览」触发识别，或直接导入章节系统会自动识别角色</div>
          </div>
        ) : (
          <div className="grid md:grid-cols-2 gap-3">
            {chars.map(c => {
              const vid = c.assigned_voice_id || '';
              const genderColor =
                c.gender === '男' ? '#3b82f6'
                : c.gender === '女' ? '#ec4899'
                : '#8b5cf6';
              return (
                <div
                  key={c.id}
                  className="rounded-2xl border border-white/[0.06] bg-white/[0.025] p-4 space-y-3
                    hover:border-brand-500/30 hover:bg-white/[0.05] transition-all animate-fade-in group"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-start gap-3 min-w-0">
                      <div
                        className="w-10 h-10 rounded-xl grid place-items-center shrink-0 text-white font-bold"
                        style={{
                          background: `linear-gradient(135deg, ${genderColor}, ${genderColor}aa)`,
                          boxShadow: `0 0 0 1px rgba(255,255,255,0.12) inset, 0 6px 12px -6px ${genderColor}66`,
                        }}
                      >
                        {c.name?.trim()?.[0] || '?'}
                      </div>
                      <div className="min-w-0">
                        <div className="font-semibold text-ink-800 truncate">{c.name}</div>
                        <div className="text-[11px] text-ink-500 mt-0.5">
                          {[c.gender, c.age, c.personality].filter(Boolean).join(' · ') || '—'}
                        </div>
                      </div>
                    </div>
                    <span
                      className="chip shrink-0 text-[11px]"
                      style={{
                        background: `${genderColor}22`,
                        color: `${genderColor}ee`,
                        border: `1px solid ${genderColor}44`,
                      }}
                    >
                      {c.gender || '—'}
                    </span>
                  </div>
                  <VoicePicker
                    voices={voices}
                    value={vid}
                    onChange={id => onChangeCharVoice(c.id, id)}
                    onPreview={id =>
                      onPreviewVoice(
                        id,
                        `你好，我是${c.name}，很高兴认识你。`,
                        speed
                      )
                    }
                    isPlaying={playingKey === `voice_${vid}`}
                    isLoading={loadingVoice === vid}
                    playingVoiceId={playingKey?.startsWith('voice_') ? playingKey.slice(6) : null}
                    loadingVoiceId={loadingVoice}
                    compact
                  />
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

// =================== Builds Tab ===================
function BuildsTab({
  projectId, project, builds, voices, narratorDefault, playingKey, onTogglePlay, onReload,
}: {
  projectId: string;
  project: ProjectDetailResp;
  builds: BuildListItem[];
  voices: Voice[];
  narratorDefault: Voice | undefined;
  playingKey: string | null;
  onTogglePlay: (key: string, url: string | null) => void;
  onReload: () => void;
}) {
  const [showCreate, setShowCreate] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const hasRunning = builds.some(
    b => b.status === 'synthesizing' || b.status === 'preparing'
  );

  useEffect(() => {
    if (!hasRunning) return;
    const timer = setInterval(() => {
      onReload();
    }, 2000);
    return () => clearInterval(timer);
  }, [hasRunning, onReload]);

  const onCreateSuccess = async () => {
    setShowCreate(false);
    await onReload();
  };

  return (
    <div className="space-y-4">
      <div className="glass-panel flex items-center justify-between gap-3 flex-wrap p-5 sm:p-6 relative overflow-hidden">
        <div className="glow-orb w-56 h-56 bg-brand-500/10" style={{ bottom: '-50px', right: '-40px' }} />
        <div className="flex items-start gap-3 min-w-0">
          <div className="w-10 h-10 rounded-xl grid place-items-center shrink-0 shadow-brand"
            style={{ backgroundImage: 'linear-gradient(135deg, #8b5cf6, #7c3aed)' }}
          >
            🏗
          </div>
          <div className="min-w-0">
            <h3 className="font-semibold text-ink-800">构建</h3>
            <p className="text-sm text-ink-500 mt-0.5 max-w-xl">
              每次构建都会按当前角色音色 + 旁白 + 语速合成所有章节 MP3，可单独下载或打包 ZIP。
            </p>
          </div>
        </div>
        <button
          className="btn-primary shrink-0 relative"
          onClick={() => setShowCreate(true)}
          disabled={project.chapter_count === 0}
          title={project.chapter_count === 0 ? '请先识别章节' : '启动一次构建'}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z"/></svg>
          开始生成
        </button>
      </div>

      {builds.length === 0 ? (
        <div className="glass-panel text-center py-16 text-ink-500 relative overflow-hidden">
          <div className="glow-orb w-56 h-56 bg-accent-teal/10" style={{ top: '-30px', left: '50%', transform: 'translateX(-50%)' }} />
          <div className="mx-auto mb-3 w-14 h-14 rounded-2xl grid place-items-center bg-white/[0.04] border border-white/[0.07] text-3xl relative">
            🏗
          </div>
          <div className="text-sm font-medium text-ink-700 relative">还没有构建记录</div>
          <div className="text-xs text-ink-500 mt-1 relative">
            配置好角色音色后，点击上方「开始生成」启动第一次构建
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          {builds.map(b => (
            <BuildRow
              key={b.build_id}
              projectId={projectId}
              item={b}
              expanded={expandedId === b.build_id}
              onToggleExpand={() =>
                setExpandedId(prev => (prev === b.build_id ? null : b.build_id))
              }
              playingKey={playingKey}
              onTogglePlay={onTogglePlay}
              onReload={onReload}
            />
          ))}
        </div>
      )}

      {showCreate && (
        <CreateBuildModal
          projectId={projectId}
          project={project}
          voices={voices}
          narratorDefault={narratorDefault}
          onClose={() => setShowCreate(false)}
          onCreated={onCreateSuccess}
        />
      )}
    </div>
  );
}

function BuildRow({
  projectId, item, expanded, onToggleExpand, playingKey, onTogglePlay, onReload,
}: {
  projectId: string;
  item: BuildListItem;
  expanded: boolean;
  onToggleExpand: () => void;
  playingKey: string | null;
  onTogglePlay: (key: string, url: string | null) => void;
  onReload: () => void;
}) {
  const [detail, setDetail] = useState<BuildDetailResp | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const pct =
    item.total_chapters > 0
      ? Math.round((item.completed_chapters / item.total_chapters) * 100)
      : 0;
  const isRunning = item.status === 'synthesizing' || item.status === 'preparing';

  const loadDetail = async () => {
    setLoadingDetail(true);
    try {
      const d = await api.buildGet(projectId, item.build_id);
      setDetail(d);
    } catch (e: any) {
      console.error('load build detail:', e);
    } finally {
      setLoadingDetail(false);
    }
  };

  useEffect(() => {
    if (expanded && !detail && !loadingDetail) {
      loadDetail();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded]);

  useEffect(() => {
    if (!expanded || !isRunning) return;
    const t = setInterval(loadDetail, 2000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded, isRunning]);

  const onDelete = async () => {
    try {
      await api.buildDelete(projectId, item.build_id);
      setConfirmDelete(false);
      await onReload();
    } catch (e: any) {
      alert(`删除失败: ${e?.message || e}`);
      setConfirmDelete(false);
    }
  };

  const pctBarBg =
    item.status === 'done'
      ? 'linear-gradient(90deg, #c6f44a 0%, #2dd4bf 100%)'
      : item.status === 'failed'
      ? 'linear-gradient(90deg, #ef4444, #dc2626)'
      : isRunning
      ? 'linear-gradient(90deg, #f59e0b, #fbbf24)'
      : 'linear-gradient(90deg, #8b5cf6, #6366f1)';

  return (
    <div className="glass-panel space-y-3 animate-fade-in p-5 relative overflow-hidden">
      {isRunning && (
        <div className="glow-orb w-40 h-40 bg-brand-500/10" style={{ top: '-30px', right: '-20px' }} />
      )}
      <div className="flex items-center gap-3 flex-wrap relative">
        <button
          className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-xl
            border border-white/[0.07] bg-white/[0.03] text-ink-600
            hover:border-brand-500/30 hover:bg-brand-500/10 hover:text-brand-300
            transition-colors shrink-0"
          onClick={onToggleExpand}
          title={expanded ? '收起章节列表' : '展开查看章节'}
        >
          {expanded ? '收起章节' : '展开章节'}
          <span className={`inline-block transition-transform duration-200 ${expanded ? 'rotate-180' : ''}`}>
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6"/></svg>
          </span>
        </button>
        <StatusBadge status={item.status} />
        <span className="text-sm text-ink-600 tabular-nums">
          {item.completed_chapters} / {item.total_chapters} 章 · {pct}%
        </span>
        {isRunning && item.started_at && (
          <span className="chip"
            style={{
              background: 'linear-gradient(135deg, rgba(245,158,11,0.22), rgba(251,191,36,0.12))',
              color: '#fde68a',
              border: '1px solid rgba(245,158,11,0.3)',
            }}
          >
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse mr-1" />
            {(() => {
              const elapsed = (Date.now() - new Date(item.started_at).getTime()) / 1000;
              const remaining = elapsed > 0 && pct > 0 ? (elapsed / pct) * (100 - pct) : 0;
              if (remaining > 0) {
                const m = Math.floor(remaining / 60);
                const s = Math.round(remaining % 60);
                return `预计剩余 ${m}分${s}秒`;
              }
              return '计算中…';
            })()}
          </span>
        )}
        <span className="text-xs text-ink-500 ml-auto tabular-nums shrink-0">
          {relativeTime(item.created_at)}
        </span>
        <button
          onClick={() => setConfirmDelete(true)}
          className="inline-flex items-center justify-center shrink-0 rounded-xl
            border border-white/[0.06] bg-white/[0.03] text-ink-500
            hover:border-red-500/40 hover:bg-red-500/15 hover:text-red-300
            transition-all duration-150"
          style={{ width: 34, height: 34 }}
          title="删除 build"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        </button>
      </div>

      <div className="progress-track h-2">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${pct}%`, backgroundImage: pctBarBg }}
        />
      </div>

      {expanded && (
        <div className="pt-1 mt-1 border-t border-white/[0.05]">
          {loadingDetail && !detail ? (
            <div className="text-center py-4 text-sm text-ink-500">
              <span className="inline-block w-4 h-4 border-2 border-brand-500 border-t-transparent rounded-full animate-spin mr-2 align-middle" />
              加载章节列表…
            </div>
          ) : detail ? (
            <BuildDetailContent
              detail={detail}
              projectId={projectId}
              playingKey={playingKey}
              onTogglePlay={onTogglePlay}
            />
          ) : (
            <div className="text-sm text-ink-500">加载失败</div>
          )}
        </div>
      )}

      {confirmDelete && (
        <div className="rounded-2xl border border-red-500/40 bg-red-500/10 p-3 flex items-center justify-between gap-3 flex-wrap">
          <span className="text-sm text-red-200">确认删除该构建？删除后所有音频产物不可恢复。</span>
          <div className="flex gap-2">
            <button className="btn-ghost !py-1.5 text-xs" onClick={() => setConfirmDelete(false)}>
              取消
            </button>
            <button className="btn-danger !py-1.5 !px-3 text-xs" onClick={onDelete}>
              确认删除
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function BuildDetailContent({
  detail, projectId, playingKey, onTogglePlay,
}: {
  detail: BuildDetailResp;
  projectId: string;
  playingKey: string | null;
  onTogglePlay: (key: string, url: string | null) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 flex-wrap text-xs text-ink-500">
        <span className="chip-soft">语速 {detail.speed?.toFixed(1) ?? '1.0'}x</span>
        <span className="chip-soft">总时长 {formatDuration(detail.total_duration_sec)}</span>
        <span className="chip-soft">总大小 {formatSize(detail.total_size_kb ? detail.total_size_kb * 1024 : null)}</span>
        {detail.zip_url && detail.status === 'done' && (
          <a
            className="btn-primary !py-1.5 !px-3 text-xs ml-auto"
            href={api.buildDownloadAll(projectId, detail.build_id)}
            download
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" className="mr-1"><rect x="2" y="7" width="13" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4.586a1 1 0 0 1 .707.293l5.414 5.414a1 1 0 0 1 .293.707V18a2 2 0 0 1-2 2"/></svg>
            下载全部 ZIP
          </a>
        )}
      </div>

      {detail.progress_msg && (
        <div className="text-xs text-ink-500 rounded-xl bg-white/[0.04] px-3 py-2 border border-white/[0.05]">
          {detail.progress_msg}
        </div>
      )}

      <div className="space-y-2 max-h-[500px] overflow-y-auto pr-1">
        {detail.artifacts.map(a => {
          const key = `art_${a.chapter_idx}`;
          const playing = playingKey === key;
          return (
            <div
              key={a.chapter_idx}
              className={`rounded-2xl p-3 transition-all duration-150 border
                ${playing
                  ? 'bg-brand-500/10 border-brand-500/40'
                  : 'bg-white/[0.02] border-white/[0.04] hover:bg-white/[0.045] hover:border-white/[0.10]'
                }`}
            >
              <div className="flex items-center gap-2 mb-2">
                <span
                  className="text-[11px] font-mono tabular-nums shrink-0 rounded-lg px-2 py-1"
                  style={{
                    background: 'rgba(255,255,255,0.04)',
                    color: playing ? '#c4b5fd' : 'rgba(255,255,255,0.45)',
                    border: '1px solid rgba(255,255,255,0.05)',
                  }}
                >
                  #{String(a.chapter_idx + 1).padStart(3, '0')}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-ink-800 truncate">{a.title || '(无标题)'}</div>
                </div>
                <BuildArtifactStatusIcon status={a.status} />
                {a.duration_ms != null && (
                  <span className="text-[11px] text-ink-500 shrink-0 tabular-nums w-12 text-right">
                    {formatMs(a.duration_ms)}
                  </span>
                )}
              </div>
              {a.status === 'failed' && a.error_msg && (
                <div className="text-[11px] text-red-300/90 truncate mb-2" title={a.error_msg}>
                  ❌ {a.error_msg.split('\n')[0]}
                </div>
              )}
              {a.status === 'done' && a.audio_url && (
                <WaveformPlayer
                  src={a.audio_url}
                  compact
                  onDownload={() => {
                    const a_ = document.createElement('a');
                    a_.href = api.buildChapterDownload(projectId, detail.build_id, a.chapter_idx);
                    a_.download = '';
                    a_.click();
                  }}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function BuildArtifactStatusIcon({ status }: { status: string }) {
  if (status === 'done')
    return (
      <span className="inline-flex w-5 h-5 rounded-full bg-accent-lime/20 text-accent-lime items-center justify-center text-[11px]">✓</span>
    );
  if (status === 'synthesizing')
    return (
      <span className="inline-block w-3.5 h-3.5 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" />
    );
  if (status === 'failed')
    return (
      <span className="inline-flex w-5 h-5 rounded-full bg-red-500/20 text-red-300 items-center justify-center text-[11px]">✗</span>
    );
  return (
    <span className="inline-flex w-5 h-5 rounded-full bg-white/[0.04] border border-white/[0.07] items-center justify-center text-[9px] text-ink-500">○</span>
  );
}

// =================== Create Build Modal ===================
function CreateBuildModal({
  projectId, project, voices, narratorDefault, onClose, onCreated,
}: {
  projectId: string;
  project: ProjectDetailResp;
  voices: Voice[];
  narratorDefault: Voice | undefined;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [narrator, setNarrator] = useState<string>(
    project.default_narrator_voice_id || narratorDefault?.id || ''
  );
  const [speed, setSpeed] = useState<number>(project.default_speed ?? 1.0);
  const [charVoices, setCharVoices] = useState<Record<string, string>>(() => {
    const m: Record<string, string> = {};
    project.characters.forEach(c => {
      if (c.assigned_voice_id) m[c.name] = c.assigned_voice_id;
    });
    return m;
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    setErr(null);
    if (!narrator) {
      setErr('请选择旁白音色');
      return;
    }
    setBusy(true);
    try {
      await api.buildCreate(projectId, {
        voice_assignments: charVoices,
        narrator_voice_id: narrator,
        speed,
      });
      onCreated();
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 animate-fade-in"
      onClick={onClose}
    >
      <div
        className="glass-panel max-w-2xl w-full max-h-[90vh] overflow-y-auto p-5 sm:p-6 animate-scale-in relative"
        onClick={e => e.stopPropagation()}
      >
        <div className="glow-orb w-60 h-60 bg-brand-500/15" style={{ top: '-40px', right: '-40px' }} />

        <div className="flex items-center justify-between mb-5 relative">
          <div className="flex items-start gap-3 min-w-0">
            <div className="w-10 h-10 rounded-xl grid place-items-center shrink-0 shadow-brand"
              style={{ backgroundImage: 'linear-gradient(135deg, #8b5cf6, #7c3aed)' }}
            >
              ▶
            </div>
            <div className="min-w-0">
              <h3 className="headline text-xl">启动新构建</h3>
              <p className="text-xs text-ink-500 mt-0.5">
                将合成 <b className="text-ink-800">{project.chapter_count}</b> 章 MP3
              </p>
            </div>
          </div>
          <button className="btn-ghost !px-2.5 !py-2 shrink-0" onClick={onClose}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
          </button>
        </div>

        <p className="text-sm text-ink-500 mb-5 relative">
          可在生成前做最后调整；保存到项目的默认配置不会被改动。
        </p>

        <div className="space-y-5 relative">
          <div className="space-y-2">
            <div className="text-sm text-ink-700 flex items-center gap-2">
              <span className="w-6 h-6 rounded-lg grid place-items-center bg-brand-500/20 text-brand-300 text-xs">1</span>
              旁白音色
            </div>
            <VoicePicker
              voices={voices}
              value={narrator}
              onChange={setNarrator}
              onPreview={() => {}}
            />
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between mb-1">
              <div className="text-sm text-ink-700 flex items-center gap-2">
                <span className="w-6 h-6 rounded-lg grid place-items-center bg-brand-500/20 text-brand-300 text-xs">2</span>
                语速
              </div>
              <span className="chip-soft font-mono tabular-nums" style={{ color: '#c4b5fd' }}>
                {speed.toFixed(1)}x
              </span>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-xs text-ink-500">0.5x</span>
              <div className="flex-1 relative">
                <input
                  type="range"
                  min={0.5}
                  max={2.0}
                  step={0.1}
                  value={speed}
                  onChange={e => setSpeed(parseFloat(e.target.value))}
                  className="w-full h-2 rounded-full appearance-none cursor-pointer"
                  style={{
                    background: `linear-gradient(90deg, #8b5cf6 0%, #8b5cf6 ${((speed - 0.5) / 1.5) * 100}%, rgba(255,255,255,0.08) ${((speed - 0.5) / 1.5) * 100}%, rgba(255,255,255,0.08) 100%)`,
                  }}
                />
              </div>
              <span className="text-xs text-ink-500">2.0x</span>
            </div>
          </div>

          {project.characters.length > 0 && (
            <div className="space-y-2.5">
              <div className="text-sm text-ink-700 flex items-center gap-2">
                <span className="w-6 h-6 rounded-lg grid place-items-center bg-brand-500/20 text-brand-300 text-xs">3</span>
                角色音色
                <span className="chip-soft ml-1">{project.characters.length} 个角色</span>
                <span className="ml-auto text-[11px] text-ink-500">已使用项目当前配置</span>
              </div>
              <div className="grid md:grid-cols-2 gap-2.5 max-h-[300px] overflow-y-auto pr-1 p-1">
                {project.characters.map(c => {
                  const genderColor =
                    c.gender === '男' ? '#3b82f6'
                    : c.gender === '女' ? '#ec4899'
                    : '#8b5cf6';
                  return (
                    <div
                      key={c.id}
                      className="rounded-2xl border border-white/[0.06] bg-white/[0.025] p-3 space-y-2 hover:border-white/[0.12] hover:bg-white/[0.05] transition-all"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2 min-w-0">
                          <div
                            className="w-7 h-7 rounded-lg grid place-items-center shrink-0 text-white text-xs font-bold"
                            style={{
                              background: `linear-gradient(135deg, ${genderColor}, ${genderColor}aa)`,
                            }}
                          >
                            {c.name?.trim()?.[0] || '?'}
                          </div>
                          <div className="min-w-0">
                            <span className="font-medium text-sm text-ink-800 truncate block">{c.name}</span>
                            <span className="text-[10px] text-ink-500">{c.gender || '—'}</span>
                          </div>
                        </div>
                      </div>
                      <select
                        className="w-full text-sm !py-1.5"
                        value={charVoices[c.name] || ''}
                        onChange={e =>
                          setCharVoices(prev => ({ ...prev, [c.name]: e.target.value }))
                        }
                      >
                        <option value="">未分配（使用旁白朗读）</option>
                        {voices.map(v => (
                          <option key={v.id} value={v.id}>
                            {v.name} ({v.gender})
                          </option>
                        ))}
                      </select>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {err && (
          <div className="mt-5 rounded-2xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
            {err}
          </div>
        )}

        <div className="flex justify-end gap-2 mt-6 relative">
          <button className="btn-ghost" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button className="btn-primary" onClick={submit} disabled={busy}>
            {busy ? (
              <>
                <span className="inline-block w-4 h-4 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                启动中…
              </>
            ) : (
              <>
                🚀 开始生成
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// =================== Settings Tab ===================
function SettingsTab({
  project, voices, onReload,
}: {
  project: ProjectDetailResp;
  voices: Voice[];
  onReload: () => void;
}) {
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description || '');
  const [tagsInput, setTagsInput] = useState((project.tags || []).join(', '));
  const [narratorVoice, setNarratorVoice] = useState(
    project.default_narrator_voice_id || ''
  );
  const [speed, setSpeed] = useState(project.default_speed ?? 1.0);

  const [saving, setSaving] = useState(false);
  const [savedTip, setSavedTip] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    setName(project.name);
    setDescription(project.description || '');
    setTagsInput((project.tags || []).join(', '));
    setNarratorVoice(project.default_narrator_voice_id || '');
    setSpeed(project.default_speed ?? 1.0);
  }, [project.project_id, project.name, project.description, project.tags, project.default_narrator_voice_id, project.default_speed]);

  const save = async () => {
    setErr(null);
    setSaving(true);
    try {
      const tags = tagsInput
        .split(/[,，]/)
        .map(s => s.trim())
        .filter(Boolean);
      await api.projectUpdate(project.project_id, {
        name: name.trim(),
        description,
        tags,
        default_narrator_voice_id: narratorVoice || null,
        default_speed: speed,
      });
      setSavedTip(true);
      setTimeout(() => setSavedTip(false), 1500);
      await onReload();
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setSaving(false);
    }
  };

  const doDelete = async () => {
    setDeleting(true);
    try {
      await api.projectDelete(project.project_id);
      window.location.hash = '#/projects';
    } catch (e: any) {
      setErr(`删除失败: ${e?.message || e}`);
      setConfirmDelete(false);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="glass-panel p-5 sm:p-6 space-y-5 relative overflow-hidden">
        <div className="glow-orb w-56 h-56 bg-accent-teal/10" style={{ bottom: '-50px', right: '-40px' }} />
        <h3 className="font-semibold text-ink-800 flex items-center gap-2 relative">
          <div className="w-8 h-8 rounded-xl grid place-items-center bg-accent-teal/20 text-accent-teal">
            ⚙️
          </div>
          项目设置
        </h3>

        <div className="space-y-2 relative">
          <label className="block text-sm text-ink-700">项目名</label>
          <input
            type="text"
            className="input"
            value={name}
            onChange={e => setName(e.target.value)}
            maxLength={80}
          />
        </div>

        <div className="space-y-2 relative">
          <label className="block text-sm text-ink-700">描述</label>
          <textarea
            className="textarea min-h-[90px]"
            value={description}
            onChange={e => setDescription(e.target.value)}
            placeholder="可选：填写项目描述、备注等"
            maxLength={500}
          />
        </div>

        <div className="space-y-2 relative">
          <label className="block text-sm text-ink-700">标签（逗号分隔）</label>
          <input
            type="text"
            className="input"
            value={tagsInput}
            onChange={e => setTagsInput(e.target.value)}
            placeholder="例如：科幻, 长篇, 三体"
          />
        </div>

        <div className="space-y-2 relative">
          <label className="block text-sm text-ink-700">默认旁白音色</label>
          <select
            className="w-full"
            value={narratorVoice}
            onChange={e => setNarratorVoice(e.target.value)}
          >
            <option value="">未设置</option>
            {voices.map(v => (
              <option key={v.id} value={v.id}>
                {v.name} ({v.gender})
              </option>
            ))}
          </select>
        </div>

        <div className="space-y-2 relative">
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm text-ink-700">默认语速</span>
            <span className="chip-soft font-mono tabular-nums" style={{ color: '#c4b5fd' }}>
              {speed.toFixed(1)}x
            </span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-xs text-ink-500">0.5x</span>
            <div className="flex-1 relative">
              <input
                type="range"
                min={0.5}
                max={2.0}
                step={0.1}
                value={speed}
                onChange={e => setSpeed(parseFloat(e.target.value))}
                className="w-full h-2 rounded-full appearance-none cursor-pointer"
                style={{
                  background: `linear-gradient(90deg, #8b5cf6 0%, #8b5cf6 ${((speed - 0.5) / 1.5) * 100}%, rgba(255,255,255,0.08) ${((speed - 0.5) / 1.5) * 100}%, rgba(255,255,255,0.08) 100%)`,
                }}
              />
            </div>
            <span className="text-xs text-ink-500">2.0x</span>
          </div>
        </div>

        {err && (
          <div className="rounded-2xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200 relative">
            {err}
          </div>
        )}

        <div className="flex items-center gap-3 relative">
          <button className="btn-primary" disabled={saving} onClick={save}>
            {saving ? (
              <>
                <span className="inline-block w-4 h-4 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                保存中…
              </>
            ) : (
              <>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/></svg>
                保存
              </>
            )}
          </button>
          {savedTip && (
            <span className="text-xs text-accent-lime flex items-center gap-1">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
              已保存
            </span>
          )}
        </div>
      </div>

      <div className="glass-panel !border-red-500/30 !bg-red-500/[0.04] space-y-3 relative overflow-hidden">
        <div className="glow-orb w-48 h-48 bg-red-500/10" style={{ bottom: '-40px', right: '-30px' }} />
        <h3 className="font-semibold text-red-300 flex items-center gap-2 relative">
          <span className="inline-flex w-7 h-7 rounded-xl items-center justify-center bg-red-500/20 text-red-300">⚠️</span>
          危险区
        </h3>
        <p className="text-sm text-ink-500 relative">
          删除项目会同时删除所有章节、角色音色配置与构建历史，操作不可恢复。
        </p>
        {!confirmDelete ? (
          <button
            className="btn-danger relative"
            onClick={() => setConfirmDelete(true)}
          >
            🗑 删除该项目
          </button>
        ) : (
          <div className="rounded-2xl border border-red-500/40 bg-red-500/10 p-3 space-y-3 relative">
            <div className="text-sm text-red-200">
              确认要删除项目「{project.book_title || project.name}」吗？
            </div>
            <div className="flex gap-2">
              <button
                className="btn-ghost"
                onClick={() => setConfirmDelete(false)}
                disabled={deleting}
              >
                取消
              </button>
              <button
                className="btn-danger"
                onClick={doDelete}
                disabled={deleting}
              >
                {deleting ? '删除中…' : '🗑 确认删除'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
