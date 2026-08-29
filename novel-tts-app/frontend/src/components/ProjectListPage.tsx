'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { api, PrepareProgress, ProjectListItem } from '@/lib/api';
import CreateAudiobookDialog from './CreateAudiobookDialog';

// ===== 常量 =====
const POLL_INTERVAL_MS = 3000;

// 状态定义（Eleven 风格：柔和、克制，不是浓重的半透明块）
const STATUS_DEFS: Record<string, { label: string; bg: string; color: string; dot: string; pulse?: boolean }> = {
  draft:           { label: '草稿',      bg: 'rgba(255,255,255,0.04)', color: 'rgba(255,255,255,0.62)', dot: '#71717a' },
  imported:        { label: '已导入',    bg: 'rgba(59,130,246,0.10)',  color: '#93c5fd', dot: '#3b82f6' },
  preparing:       { label: '识别中',    bg: 'rgba(245,158,11,0.12)',  color: '#fcd34d', dot: '#f59e0b', pulse: true },
  ready:           { label: '就绪',      bg: 'rgba(45,212,191,0.10)',  color: '#5eead4', dot: '#2dd4bf' },
  synthesizing:    { label: '合成中',    bg: 'rgba(251,146,60,0.12)',  color: '#fdba74', dot: '#fb923c', pulse: true },
  done:            { label: '已完成',    bg: 'rgba(74,222,128,0.10)',  color: '#86efac', dot: '#4ade80' },
  success:         { label: '已完成',    bg: 'rgba(74,222,128,0.10)',  color: '#86efac', dot: '#4ade80' },
  partial_success: { label: '部分成功',  bg: 'rgba(250,204,21,0.10)',  color: '#fde68a', dot: '#facc15' },
  failed:          { label: '失败',      bg: 'rgba(251,113,133,0.10)', color: '#fda4af', dot: '#fb7185' },
  cancelled:       { label: '已取消',    bg: 'rgba(161,161,170,0.12)', color: '#d4d4d8', dot: '#a1a1aa' },
};

function relativeTime(iso: string): string {
  try {
    const t = new Date(iso).getTime();
    const diff = Date.now() - t;
    if (diff < 60 * 1000) return '刚刚';
    if (diff < 60 * 60 * 1000) return `${Math.floor(diff / 60000)} 分钟前`;
    if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / 3600000)} 小时前`;
    if (diff < 7 * 24 * 60 * 60 * 1000) return `${Math.floor(diff / 86400000)} 天前`;
    return new Date(iso).toLocaleDateString('zh-CN');
  } catch { return iso; }
}

const STAGE_LABELS: Record<string, string> = {
  start: '准备中', split: '切章', characters: '角色识别',
  dedup: '角色去重', dialogues: '对白归属', voice_recs: '音色推荐', done: '完成',
};
function stageLabel(stage?: string | null): string {
  if (!stage) return '';
  return STAGE_LABELS[stage] ?? stage;
}

export interface PrepareProgressMetrics {
  charPct: number | null;
  charText: string;
  dialoguePct: number | null;
  dialogueText: string;
  lineText: string;
  charFailedN: number;
  dialogueFailedN: number;
}
export function computePrepareMetrics(prog: PrepareProgress | null | undefined, chapterCount: number): PrepareProgressMetrics {
  const empty = { charPct: null, charText: '', dialoguePct: null, dialogueText: '', lineText: '', charFailedN: 0, dialogueFailedN: 0 };
  if (!prog) return empty;
  let charPct: number | null = null, charText = '';
  const charTotal = prog.char_slice_total ?? 0;
  const charDone = prog.char_slice_completed_n ?? 0;
  if (charTotal > 0) {
    charPct = Math.min(100, Math.round((charDone / charTotal) * 100));
    charText = `角色识别 ${charDone}/${charTotal}`;
    if (prog.char_current_slice && typeof prog.char_current_slice.idx === 'number') {
      charText += `（#${prog.char_current_slice.idx + 1}）`;
    }
  } else if (prog.stage === 'characters') charText = '角色识别中…';

  let dialoguePct: number | null = null, dialogueText = '';
  const batchTotal = prog.dialogue_total_batches ?? 0;
  const batchDone = prog.dialogue_completed_batches_count ?? 0;
  const chapTotal = prog.dialogue_total_chapters ?? chapterCount;
  const chapDone = prog.dialogue_completed_chapters_count ?? (prog.dialogue_completed_chapters_n ?? 0);
  if (batchTotal > 0) {
    dialoguePct = Math.min(100, Math.round((batchDone / batchTotal) * 100));
    dialogueText = `对白归属 ${batchDone}/${batchTotal} 批`;
    if (chapTotal > 0) dialogueText += ` · ${chapDone}/${chapTotal} 章`;
  } else if (chapTotal > 0 && chapDone > 0) {
    dialoguePct = Math.min(100, Math.round((chapDone / chapTotal) * 100));
    dialogueText = `对白归属 ${chapDone}/${chapTotal} 章`;
  } else if (prog.stage === 'dialogues') dialogueText = '对白归属中…';

  const parts: string[] = [];
  if (stageLabel(prog.stage)) parts.push(stageLabel(prog.stage));
  if (charText) parts.push(charText);
  else if (prog.stage === 'dedup' && prog.dedup_done === undefined) parts.push('角色去重中…');
  if (dialogueText) parts.push(dialogueText);
  else if (prog.stage === 'voice_recs' && !prog.voice_recs_done) parts.push('音色推荐中…');

  return {
    charPct, charText, dialoguePct, dialogueText,
    lineText: parts.join(' · '),
    charFailedN: prog.char_failed_slices_n ?? 0,
    dialogueFailedN: prog.dialogue_failed_batches_n ?? 0,
  };
}

// ===== 通用 StatusBadge（Eleven 风格：小圆点 + 软 chip） =====
export function StatusBadge({ status }: { status: string }) {
  const def = STATUS_DEFS[status] ?? STATUS_DEFS.draft;
  return (
    <span
      className={`chip ${def.pulse ? 'animate-pulse-soft' : ''}`}
      style={{ background: def.bg, color: def.color }}
      title={def.label}
    >
      <span
        className="badge-dot"
        style={{ backgroundColor: def.dot }}
      />
      {def.label}
    </span>
  );
}

// 进度展示
export function PrepareProgressInline({
  prog,
  chapterCount,
  compact = false,
}: {
  prog: PrepareProgress | null | undefined;
  chapterCount: number;
  compact?: boolean;
}) {
  const m = computePrepareMetrics(prog, chapterCount);
  if (!prog) return null;
  return (
    <div className={`space-y-2 ${compact ? 'text-[11px]' : 'text-xs'}`}>
      {(prog.stage || prog.last_error) && (
        <div className="flex items-center gap-2 flex-wrap">
          {prog.stage && (
            <span className="chip-soft">
              阶段 · {stageLabel(prog.stage)}
            </span>
          )}
          {prog.last_error && (
            <span className="chip" style={{
              background: 'rgba(251,113,133,0.08)',
              color: '#fda4af',
              border: '1px solid rgba(251,113,133,0.22)',
            }}>
              ❌ {prog.last_error}
            </span>
          )}
          {typeof prog.restart_count === 'number' && prog.restart_count > 0 && (
            <span className="chip-soft">
              ♻ 自动恢复 × {prog.restart_count}
            </span>
          )}
        </div>
      )}
      {m.charPct !== null && (
        <div>
          <div className="flex items-center justify-between text-ink-600 mb-1">
            <span>{m.charText}</span>
            <span className="tabular-nums text-ink-700">{m.charPct}%</span>
          </div>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${m.charPct}%` }} />
          </div>
          {m.charFailedN > 0 && (
            <div className="mt-1" style={{ color: '#fdba74' }}>⚠ 有 {m.charFailedN} 个切片失败，完成后可补跑</div>
          )}
        </div>
      )}
      {m.dialoguePct !== null && (
        <div>
          <div className="flex items-center justify-between text-ink-600 mb-1">
            <span>{m.dialogueText}</span>
            <span className="tabular-nums text-ink-700">{m.dialoguePct}%</span>
          </div>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${m.dialoguePct}%` }} />
          </div>
          {m.dialogueFailedN > 0 && (
            <div className="mt-1" style={{ color: '#fdba74' }}>⚠ 有 {m.dialogueFailedN} 批对白失败，完成后可补跑</div>
          )}
        </div>
      )}
    </div>
  );
}

// ===== 全局运行中任务条 =====
export function RunningTasksBar({ items }: { items: ProjectListItem[] }) {
  const running = useMemo(
    () => items.filter(p => p.status === 'preparing' || p.status === 'synthesizing'),
    [items],
  );
  if (running.length === 0) return null;
  const preparing = running.filter(p => p.status === 'preparing');
  const synthesizing = running.filter(p => p.status === 'synthesizing');
  const summary: string[] = [];
  if (preparing.length) summary.push(`🔍 ${preparing.length} 个识别任务进行中`);
  if (synthesizing.length) summary.push(`🔊 ${synthesizing.length} 个合成任务进行中`);
  return (
    <div
      className="sticky top-2 z-30 rounded-lg backdrop-blur-xl px-5 py-4 mb-5 animate-fade-in"
      style={{
        border: '1px solid rgba(139,92,246,0.22)',
        background:
          'linear-gradient(180deg, rgba(139,92,246,0.10) 0%, rgba(139,92,246,0.04) 100%)',
        boxShadow: '0 0 0 1px rgba(139,92,246,0.10), 0 12px 32px -8px rgba(139,92,246,0.25)',
      }}
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3 min-w-0">
          <span className="badge-dot animate-pulse-soft" style={{ background: '#8b5cf6', transform: 'scale(1.4)' }} />
          <div className="min-w-0">
            <div className="font-semibold truncate" style={{ color: '#ede9fe' }}>
              {summary.join(' · ')}
            </div>
            <div className="text-xs truncate mt-0.5" style={{ color: 'rgba(237,233,254,0.65)' }}>
              后台继续运行 · 关闭此页面不影响 · 进度每 3 秒自动刷新
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {preparing.length > 0 && (
            <button
              className="btn-ghost !px-3 !py-1.5 text-xs"
              onClick={() => { window.location.hash = `#/audiobooks/${preparing[0].project_id}`; }}
              title={preparing.map(p => p.name).join(' · ')}
            >
              查看识别 · {preparing[0].name.slice(0, 12)}
              {preparing.length > 1 ? ` 等 ${preparing.length} 本` : ''}
            </button>
          )}
          {synthesizing.length > 0 && (
            <button
              className="btn-ghost !px-3 !py-1.5 text-xs"
              onClick={() => { window.location.hash = `#/audiobooks/${synthesizing[0].project_id}`; }}
            >
              查看合成 · {synthesizing[0].name.slice(0, 12)}
              {synthesizing.length > 1 ? ` 等 ${synthesizing.length} 本` : ''}
            </button>
          )}
        </div>
      </div>
      {running.length > 0 && (
        <div className="mt-4 grid sm:grid-cols-2 gap-2.5">
          {running.slice(0, 4).map(p => {
            const def = STATUS_DEFS[p.status];
            return (
              <button
                key={p.project_id}
                onClick={() => { window.location.hash = `#/audiobooks/${p.project_id}`; }}
                className="text-left surface !p-3 hover:!border-ink-400 transition"
              >
                <div className="flex items-start justify-between gap-2 mb-1.5">
                  <div className="min-w-0 flex-1">
                    <div className="font-medium truncate text-sm text-ink-800">
                      {p.name}
                    </div>
                    {p.book_title && p.book_title !== p.name && (
                      <div className="text-[11px] text-white/40 truncate mt-0.5">
                        📚 {p.book_title}
                      </div>
                    )}
                  </div>
                  <StatusBadge status={p.status} />
                </div>
                {p.status === 'preparing' && (
                  <PrepareProgressInline prog={p.prepare_progress} chapterCount={p.chapter_count} compact />
                )}
                {p.status === 'synthesizing' && (
                  <div className="text-xs text-ink-600">合成中 · 点击进入详情查看章节进度</div>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ===== 主页面 =====
export default function ProjectListPage() {
  const [items, setItems] = useState<ProjectListItem[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const reload = async () => {
    try {
      const list = await api.projectList();
      setItems(list);
      setErr(null);
    } catch (e: any) {
      setErr(String(e?.message || e));
      setItems([]);
    }
  };

  useEffect(() => {
    reload();
    pollTimerRef.current = setInterval(reload, POLL_INTERVAL_MS);
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    };
  }, []);

  const onDelete = async (id: string) => {
    try {
      await api.projectDelete(id);
      setConfirmDeleteId(null);
      await reload();
    } catch (e: any) {
      setErr(`删除失败: ${e?.message || e}`);
      setConfirmDeleteId(null);
    }
  };

  const loading = items === null;

  return (
    <section className="space-y-5">
      {/* ===== 顶部操作栏 ===== */}
      <div className="flex items-end justify-between gap-4 flex-wrap animate-fade-in">
        <div className="min-w-0">
          <h2 className="text-[22px] font-semibold text-white leading-tight">
            我的有声书
          </h2>
          <p className="mt-1 text-sm text-white/50">
            管理你的有声书项目
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button className="btn-ghost" onClick={reload} title="立即刷新">
            <span className="opacity-80">⟳</span> 刷新
          </button>
          <button className="btn-primary" onClick={() => setShowCreateDialog(true)}>
            <span className="text-base leading-none">＋</span> 新建有声书
          </button>
        </div>
      </div>

      {err && (
        <div className="rounded-md px-4 py-3 text-sm"
          style={{
            border: '1px solid rgba(251,113,133,0.28)',
            background: 'rgba(251,113,133,0.06)',
            color: '#fecdd3',
          }}
        >{err}</div>
      )}

      {/* 加载中 */}
      {loading && (
        <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] text-center py-16">
          <div className="mx-auto w-9 h-9 rounded-full border-2 border-brand-500/30 border-t-brand-500 animate-spin mb-4" />
          <div className="text-sm text-white/50">加载项目列表…</div>
        </div>
      )}

      {/* 空状态 */}
      {!loading && items && items.length === 0 && (
        <div className="relative rounded-lg border border-white/[0.06] bg-white/[0.02] text-center py-20 overflow-hidden">
          <div className="glow-orb w-[320px] h-[320px] bg-brand-500/15" style={{ left: '50%', top: '-80px', transform: 'translateX(-50%)' }} />
          <div className="relative mx-auto w-20 h-20 rounded-lg grid place-items-center mb-6"
            style={{
              background: 'linear-gradient(135deg, rgba(139,92,246,0.18), rgba(45,212,191,0.10))',
              border: '1px solid rgba(255,255,255,0.08)',
            }}
          >
            <div className="flex items-end gap-[3px] h-8">
              <span className="w-1.5 rounded-full bg-brand-400/70" style={{ height: '40%' }} />
              <span className="w-1.5 rounded-full bg-brand-400/80" style={{ height: '70%' }} />
              <span className="w-1.5 rounded-full bg-brand-400" style={{ height: '100%' }} />
              <span className="w-1.5 rounded-full bg-brand-400/85" style={{ height: '60%' }} />
              <span className="w-1.5 rounded-full bg-brand-400/65" style={{ height: '45%' }} />
            </div>
          </div>
          <h3 className="text-lg font-semibold mb-2">还没有有声书</h3>
          <p className="text-sm text-white/50 max-w-md mx-auto mb-6">
            上传 TXT/EPUB 小说，自动识别章节切分、角色名单与对白归属，
            为每个角色分配音色后即可一键生成多音色 MP3 有声书。
          </p>
          <button className="btn-primary !px-6 !py-2.5" onClick={() => setShowCreateDialog(true)}>
            ＋ 创建第一个有声书
          </button>
        </div>
      )}

      {/* 表格视图 */}
      {!loading && items && items.length > 0 && (
        <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] text-white/40 uppercase tracking-wider border-b border-white/[0.06]">
                <th className="px-5 py-3 font-medium">有声书</th>
                <th className="px-4 py-3 font-medium w-[100px]">状态</th>
                <th className="px-4 py-3 font-medium w-[200px]">进度</th>
                <th className="px-4 py-3 font-medium w-[80px] text-right">章节</th>
                <th className="px-4 py-3 font-medium w-[140px]">更新时间</th>
                <th className="px-5 py-3 font-medium w-[140px] text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p, i) => {
                const isRunning = ['preparing', 'synthesizing', 'importing'].includes(p.status);
                const detailHref = `#/audiobooks/${p.project_id}`;
                return (
                  <tr
                    key={p.project_id}
                    className="border-b border-white/[0.04] hover:bg-white/[0.03] transition-colors cursor-pointer animate-fade-in"
                    style={{ animationDelay: `${i * 25}ms` }}
                    onClick={() => { window.location.hash = detailHref; }}
                  >
                    {/* 名称 */}
                    <td className="px-5 py-4 min-w-0">
                      <div className="flex items-center gap-3">
                        <div
                          className="w-9 h-9 rounded-lg grid place-items-center text-base shrink-0"
                          style={{
                            background: `linear-gradient(135deg, ${p.cover_color || '#8b5cf6'}33, ${p.cover_color || '#8b5cf6'}11)`,
                            border: `1px solid ${p.cover_color || '#8b5cf6'}33`,
                          }}
                        >
                          <span>📖</span>
                        </div>
                        <div className="min-w-0">
                          <div className="font-medium text-white truncate">
                            {p.name}
                          </div>
                          {p.book_title && p.book_title !== p.name && (
                            <div className="text-[11px] text-white/40 truncate mt-0.5">
                              📚 {p.book_title}
                            </div>
                          )}
                          {p.source_filename && (
                            <div className="text-[11px] text-white/40 truncate mt-0.5">
                              📄 {p.source_filename}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>

                    {/* 状态 */}
                    <td className="px-4 py-4">
                      <StatusBadge status={p.status} />
                    </td>

                    {/* 进度 */}
                    <td className="px-4 py-4">
                      {p.status === 'preparing' && (
                        <div onClick={e => e.stopPropagation()}>
                          <PrepareProgressInline prog={p.prepare_progress} chapterCount={p.chapter_count} compact />
                        </div>
                      )}
                      {p.status === 'synthesizing' && (
                        <div className="text-xs text-orange-300">
                          🔊 合成中…
                        </div>
                      )}
                      {p.status === 'importing' && (
                        <div className="text-xs text-blue-300">
                          📥 导入中…
                        </div>
                      )}
                      {p.status === 'failed' && p.prepare_progress?.last_error && (
                        <div className="text-xs text-rose-300 truncate" title={p.prepare_progress.last_error}>
                          ❌ {p.prepare_progress.last_error.slice(0, 40)}
                        </div>
                      )}
                      {['ready', 'done', 'success', 'partial_success'].includes(p.status) && (
                        <div className="text-xs text-white/40">—</div>
                      )}
                    </td>

                    {/* 章节数 */}
                    <td className="px-4 py-4 text-right tabular-nums text-white/70">
                      {p.chapter_count}
                    </td>

                    {/* 更新时间 */}
                    <td className="px-4 py-4 text-xs text-white/40">
                      {relativeTime(p.updated_at)}
                      {isRunning && (
                        <span className="ml-1 inline-flex items-center gap-1 text-[10px] text-amber-300/70">
                          <span className="badge-dot animate-pulse-soft !w-1 !h-1" style={{ background: '#f59e0b' }} />
                          自动刷新
                        </span>
                      )}
                    </td>

                    {/* 操作 */}
                    <td className="px-5 py-4 text-right">
                      <div className="inline-flex items-center gap-1" onClick={e => e.stopPropagation()}>
                        <a href={detailHref} className="btn-ghost !px-2.5 !py-1 text-xs" title="进入工作台">
                          详情
                        </a>
                        {['ready', 'done', 'success', 'partial_success'].includes(p.status) && (
                          <a href={detailHref} className="btn-ghost !px-2.5 !py-1 text-xs" title="构建有声书">
                            构建
                          </a>
                        )}
                        {p.status === 'failed' && (
                          <a href={detailHref} className="btn-ghost !px-2.5 !py-1 text-xs" title="重试">
                            重试
                          </a>
                        )}
                        <button
                          className="btn-ghost !px-2.5 !py-1 text-xs hover:text-rose-300"
                          style={{ color: 'rgba(251,113,133,0.7)' }}
                          onClick={() => setConfirmDeleteId(p.project_id)}
                          title="删除"
                        >
                          🗑
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* 删除确认 Modal */}
      {confirmDeleteId && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/60 backdrop-blur-sm"
          onClick={() => setConfirmDeleteId(null)}
        >
          <div
            className="bg-zinc-900 border border-white/10 rounded-lg p-6 w-full max-w-sm mx-4 text-center animate-scale-in"
            onClick={e => e.stopPropagation()}
          >
            <div className="mx-auto w-11 h-11 rounded-lg grid place-items-center mb-3"
              style={{ background: 'rgba(251,113,133,0.12)' }}>
              <span className="text-xl">⚠️</span>
            </div>
            <div className="font-semibold text-white mb-1.5">确认删除？</div>
            <div className="text-xs text-white/50 mb-5 leading-relaxed">
              删除后无法恢复，所有章节、角色识别、音色配置与构建历史都会丢失。
            </div>
            <div className="flex gap-2">
              <button className="btn-ghost flex-1" onClick={() => setConfirmDeleteId(null)}>
                取消
              </button>
              <button
                className="flex-1 rounded-lg text-sm font-medium transition-all"
                style={{
                  background: 'rgba(251,113,133,0.15)',
                  color: '#fda4af',
                  border: '1px solid rgba(251,113,133,0.3)',
                }}
                onClick={() => onDelete(confirmDeleteId)}
              >
                🗑 确认删除
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 创建有声书弹窗 */}
      {showCreateDialog && (
        <CreateAudiobookDialog
          onClose={() => setShowCreateDialog(false)}
          onCreated={(id) => {
            setShowCreateDialog(false);
            window.location.hash = `#/audiobooks/${id}`;
          }}
        />
      )}
    </section>
  );
}
// cache-bust at 1787940057
