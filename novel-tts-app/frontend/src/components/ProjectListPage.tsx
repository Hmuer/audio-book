'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { api, PrepareProgress, ProjectListItem } from '@/lib/api';

// ===== 常量 =====
const POLL_INTERVAL_MS = 3000; // 列表页 3s 轮询（后台 prepare / synthesize 进行中，刷新/重开标签页仍能看到进度）

// 状态徽章：颜色映射
// draft=灰, imported=蓝, preparing=黄(脉冲), ready=青, synthesizing=橙(脉冲), done=绿, failed=红
const STATUS_STYLES: Record<string, string> = {
  draft: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
  imported: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  preparing: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
  ready: 'bg-cyan-500/20 text-cyan-300 border-cyan-500/30',
  synthesizing: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  done: 'bg-green-500/20 text-green-300 border-green-500/30',
  failed: 'bg-red-500/20 text-red-300 border-red-500/30',
};
const STATUS_PULSING: Record<string, boolean> = {
  preparing: true,
  synthesizing: true,
};
const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  imported: '已导入',
  preparing: '识别中',
  ready: '就绪',
  synthesizing: '合成中',
  done: '已完成',
  failed: '失败',
};

// ===== 工具 =====
function relativeTime(iso: string): string {
  try {
    const t = new Date(iso).getTime();
    const diff = Date.now() - t;
    if (diff < 60 * 1000) return '刚刚';
    if (diff < 60 * 60 * 1000) return `${Math.floor(diff / 60000)} 分钟前`;
    if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / 3600000)} 小时前`;
    if (diff < 7 * 24 * 60 * 60 * 1000) return `${Math.floor(diff / 86400000)} 天前`;
    return new Date(iso).toLocaleDateString('zh-CN');
  } catch {
    return iso;
  }
}

const STAGE_LABELS: Record<string, string> = {
  start: '准备中',
  split: '切章',
  characters: '角色识别',
  dedup: '角色去重',
  dialogues: '对白归属',
  voice_recs: '音色推荐',
  done: '完成',
};
function stageLabel(stage?: string | null): string {
  if (!stage) return '';
  return STAGE_LABELS[stage] ?? stage;
}

export interface PrepareProgressMetrics {
  charPct: number | null; // 0-100
  charText: string; // e.g. "角色识别 12 / 60"
  dialoguePct: number | null; // 0-100
  dialogueText: string; // e.g. "对白归属 8 / 72 批 · 100 / 1000 章"
  lineText: string; // 单行摘要，用于全局任务条
  charFailedN: number;
  dialogueFailedN: number;
}
export function computePrepareMetrics(prog: PrepareProgress | null | undefined, chapterCount: number): PrepareProgressMetrics {
  const empty: PrepareProgressMetrics = {
    charPct: null,
    charText: '',
    dialoguePct: null,
    dialogueText: '',
    lineText: '',
    charFailedN: 0,
    dialogueFailedN: 0,
  };
  if (!prog) return empty;
  // 角色切片进度
  let charPct: number | null = null;
  let charText = '';
  const charTotal = typeof prog.char_slice_total === 'number' ? prog.char_slice_total : 0;
  const charDone = typeof prog.char_slice_completed_n === 'number'
    ? prog.char_slice_completed_n
    : 0;
  if (charTotal > 0) {
    charPct = Math.min(100, Math.round((charDone / charTotal) * 100));
    charText = `角色识别 ${charDone}/${charTotal}`;
    if (prog.char_current_slice && typeof prog.char_current_slice.idx === 'number') {
      charText += `（当前 #${prog.char_current_slice.idx + 1}）`;
    }
  } else if (prog.stage === 'characters') {
    charText = '角色识别中…';
  }
  // 对白归属进度
  let dialoguePct: number | null = null;
  let dialogueText = '';
  const batchTotal = typeof prog.dialogue_total_batches === 'number' ? prog.dialogue_total_batches : 0;
  const batchDone = typeof prog.dialogue_completed_batches_count === 'number'
    ? prog.dialogue_completed_batches_count
    : 0;
  const chapTotal = typeof prog.dialogue_total_chapters === 'number'
    ? prog.dialogue_total_chapters
    : chapterCount;
  const chapDone = typeof prog.dialogue_completed_chapters_count === 'number'
    ? prog.dialogue_completed_chapters_count
    : (typeof prog.dialogue_completed_chapters_n === 'number' ? prog.dialogue_completed_chapters_n : 0);
  if (batchTotal > 0) {
    dialoguePct = Math.min(100, Math.round((batchDone / batchTotal) * 100));
    dialogueText = `对白归属 ${batchDone}/${batchTotal} 批`;
    if (chapTotal > 0) dialogueText += ` · ${chapDone}/${chapTotal} 章`;
  } else if (chapTotal > 0 && chapDone > 0) {
    dialoguePct = Math.min(100, Math.round((chapDone / chapTotal) * 100));
    dialogueText = `对白归属 ${chapDone}/${chapTotal} 章`;
  } else if (prog.stage === 'dialogues') {
    dialogueText = '对白归属中…';
  }
  const parts: string[] = [];
  if (stageLabel(prog.stage)) parts.push(stageLabel(prog.stage));
  if (charText) parts.push(charText);
  else if (prog.stage === 'dedup' && prog.dedup_done === undefined) parts.push('角色去重中…');
  if (dialogueText) parts.push(dialogueText);
  else if (prog.stage === 'voice_recs' && !prog.voice_recs_done) parts.push('音色推荐中…');
  const lineText = parts.join(' · ');

  const charFailedN = typeof prog.char_failed_slices_n === 'number' ? prog.char_failed_slices_n : 0;
  const dialogueFailedN = typeof prog.dialogue_failed_batches_n === 'number'
    ? prog.dialogue_failed_batches_n
    : 0;
  return { charPct, charText, dialoguePct, dialogueText, lineText, charFailedN, dialogueFailedN };
}

// ===== 基础组件 =====
export function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_STYLES[status] || 'bg-white/10 text-white/70 border-white/20';
  const label = STATUS_LABEL[status] || status;
  const pulse = STATUS_PULSING[status] ? 'animate-pulse' : '';
  return (
    <span className={`chip border ${cls} ${pulse}`}>
      {label}
    </span>
  );
}

/** 在列表卡片 / 全局任务条里通用的一段 prepare 进度展示（两条进度条 + 阶段） */
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
            <span className="chip border border-yellow-500/30 bg-yellow-500/10 text-yellow-300">
              阶段：{stageLabel(prog.stage)}
            </span>
          )}
          {prog.last_error && (
            <span className="chip border border-red-500/30 bg-red-500/10 text-red-300 max-w-full truncate" title={prog.last_error}>
              ❌ {prog.last_error}
            </span>
          )}
          {typeof prog.restart_count === 'number' && prog.restart_count > 0 && (
            <span className="chip border border-white/15 bg-white/5 text-white/70">
              ♻ 自动恢复 × {prog.restart_count}
            </span>
          )}
        </div>
      )}
      {m.charPct !== null && (
        <div>
          <div className="flex items-center justify-between text-white/60 mb-1">
            <span>{m.charText}</span>
            <span>{m.charPct}%</span>
          </div>
          <div className="h-1.5 w-full rounded-full bg-white/5 overflow-hidden">
            <div className="h-full bg-yellow-400 transition-all" style={{ width: `${m.charPct}%` }} />
          </div>
          {m.charFailedN > 0 && (
            <div className="mt-1 text-orange-300">⚠ 有 {m.charFailedN} 个切片失败，完成后可再次识别补跑</div>
          )}
        </div>
      )}
      {m.dialoguePct !== null && (
        <div>
          <div className="flex items-center justify-between text-white/60 mb-1">
            <span>{m.dialogueText}</span>
            <span>{m.dialoguePct}%</span>
          </div>
          <div className="h-1.5 w-full rounded-full bg-white/5 overflow-hidden">
            <div className="h-full bg-blue-400 transition-all" style={{ width: `${m.dialoguePct}%` }} />
          </div>
          {m.dialogueFailedN > 0 && (
            <div className="mt-1 text-orange-300">⚠ 有 {m.dialogueFailedN} 批对白失败，完成后可补跑</div>
          )}
        </div>
      )}
    </div>
  );
}

// ===== 全局条（HomePage 里展示正在进行的识别/合成任务）=====
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
    <div className="sticky top-2 z-30 rounded-2xl border border-brand-500/30 bg-brand-500/10 backdrop-blur px-4 py-3 mb-4 shadow-lg shadow-brand-500/10">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3 min-w-0">
          <span className="inline-block w-3 h-3 rounded-full bg-brand-500 animate-pulse shrink-0" />
          <div className="min-w-0">
            <div className="font-semibold text-brand-100 truncate">{summary.join(' · ')}</div>
            <div className="text-xs text-brand-100/70 truncate">
              关闭标签页/刷新/重开浏览器后任务继续，卡片上每 3 秒自动刷新进度。点击右侧按钮跳转。
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {preparing.length > 0 && (
            <button
              className="btn-ghost text-xs py-1"
              onClick={() => {
                window.location.hash = `#/projects/${preparing[0].project_id}`;
              }}
              title={preparing.map(p => p.book_title || p.name).join(' · ')}
            >
              前往识别：{(preparing[0].book_title || preparing[0].name).slice(0, 14)}
              {preparing.length > 1 ? ` 等 ${preparing.length} 本` : ''}
            </button>
          )}
          {synthesizing.length > 0 && (
            <button
              className="btn-ghost text-xs py-1"
              onClick={() => {
                window.location.hash = `#/projects/${synthesizing[0].project_id}`;
              }}
            >
              前往合成：{(synthesizing[0].book_title || synthesizing[0].name).slice(0, 14)}
              {synthesizing.length > 1 ? ` 等 ${synthesizing.length} 本` : ''}
            </button>
          )}
        </div>
      </div>
      {/* 多项目运行时：显示一个紧凑进度预览条 */}
      {running.length > 0 && (
        <div className="mt-3 grid sm:grid-cols-2 gap-2">
          {running.slice(0, 4).map(p => (
            <button
              key={p.project_id}
              className="text-left card border-white/10 bg-white/5 hover:bg-white/10 transition !p-2 rounded-lg"
              onClick={() => {
                window.location.hash = `#/projects/${p.project_id}`;
              }}
            >
              <div className="flex items-center justify-between gap-2 mb-1">
                <div className="font-medium truncate text-sm">{p.book_title || p.name}</div>
                <StatusBadge status={p.status} />
              </div>
              {p.status === 'preparing' && (
                <PrepareProgressInline prog={p.prepare_progress} chapterCount={p.chapter_count} compact />
              )}
              {p.status === 'synthesizing' && (
                <div className="text-xs text-white/60">合成中：请点击进入详情查看进度</div>
              )}
            </button>
          ))}
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

  // 首次加载 + 3s 轮询：只要组件在页面上（用户停留在项目工作台 / 重开标签页后落到此页面），
  // 自动刷新列表，后台识别/合成进度无需用户手动点『刷新』。
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
  const hasRunning = useMemo(
    () => !!items && items.some(p => p.status === 'preparing' || p.status === 'synthesizing'),
    [items],
  );

  return (
    <section className="space-y-6">
      {/* 顶部操作栏 */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-lg font-semibold">📚 项目工作台</h2>
          <p className="text-sm text-white/60 mt-1">
            管理你的整本有声书项目 · 每个项目独立保存章节、角色音色与构建历史
            {hasRunning ? <> · <span className="text-yellow-300">后台任务进行中，每 3 秒自动刷新</span></> : null}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn-ghost" onClick={reload} title="立即刷新项目列表">
            ⟳ 刷新
          </button>
          <button
            className="btn-primary"
            onClick={() => {
              window.location.hash = '#/projects/new';
            }}
          >
            ＋ 新建项目
          </button>
        </div>
      </div>

      {err && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          {err}
        </div>
      )}

      {/* 全局 running 任务条（放在 section 内部，和 HomePage 顶部 sticky 那一份双保险） */}
      {!loading && items && <RunningTasksBar items={items} />}

      {/* 加载中 */}
      {loading && (
        <div className="card text-center py-12 text-white/60">
          <div className="inline-block w-8 h-8 border-4 border-brand-500 border-t-transparent rounded-full animate-spin mb-3" />
          <div>加载项目列表…</div>
        </div>
      )}

      {/* 空状态：即使列表为空（可能刚创建后还没 reload 出来？其实不会，API 一定返回），
           但保持与 hasRunning 语义一致：如果 items 存在且长度为 0 才显示空。 */}
      {!loading && items && items.length === 0 && (
        <div className="card text-center py-16 max-w-2xl mx-auto">
          <div className="text-6xl mb-4">📭</div>
          <h3 className="text-lg font-semibold mb-2">还没有项目</h3>
          <p className="text-sm text-white/60 mb-6">
            创建你的第一个有声书项目，上传 TXT 后系统会自动识别章节与角色，
            然后配置音色即可一键生成多章节 MP3。
          </p>
          <button
            className="btn-primary"
            onClick={() => {
              window.location.hash = '#/projects/new';
            }}
          >
            ＋ 创建第一个项目
          </button>
        </div>
      )}

      {/* 卡片网格 */}
      {!loading && items && items.length > 0 && (
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {items.map(p => {
            const isRunning = p.status === 'preparing' || p.status === 'synthesizing';
            return (
              <div
                key={p.project_id}
                className={`group card relative overflow-hidden cursor-pointer transition animate-fade-in
                  ${isRunning ? 'border-yellow-500/40 bg-yellow-500/5 hover:border-yellow-500/60 hover:bg-yellow-500/10' : 'hover:border-brand-500/50 hover:bg-brand-500/5'}`}
                onClick={() => {
                  window.location.hash = `#/projects/${p.project_id}`;
                }}
              >
                {/* 左侧色条 + running 时加流动高光 */}
                <div className="absolute left-0 top-0 bottom-0 w-1.5 overflow-hidden">
                  <div
                    className="w-full h-full"
                    style={{ backgroundColor: p.cover_color || '#a855f7' }}
                  />
                  {isRunning && (
                    <div className="absolute inset-0 animate-pulse bg-white/20" />
                  )}
                </div>

                <div className="pl-2 space-y-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="font-semibold truncate">{p.book_title || p.name}</div>
                      {p.book_title && p.book_title !== p.name && (
                        <div className="text-xs text-white/40 truncate mt-0.5">{p.name}</div>
                      )}
                    </div>
                    <StatusBadge status={p.status} />
                  </div>

                  <div className="flex items-center gap-3 text-xs text-white/60">
                    <span title="章节数">📜 {p.chapter_count} 章</span>
                    <span title="源文件">
                      {p.source_filename ? `📄 ${p.source_filename}` : '📄 未上传'}
                    </span>
                  </div>

                  {/* preparing/synthesizing 卡片：内嵌进度条 */}
                  {p.status === 'preparing' && (
                    <PrepareProgressInline prog={p.prepare_progress} chapterCount={p.chapter_count} />
                  )}
                  {p.status === 'synthesizing' && (
                    <div className="text-xs text-orange-200">
                      🔊 合成中…（点击进入详情查看章节级进度，可随时关闭此页面）
                    </div>
                  )}
                  {p.status === 'failed' && p.prepare_progress?.last_error && (
                    <div className="text-xs text-red-200 break-words whitespace-pre-wrap line-clamp-3">
                      ❌ {p.prepare_progress.last_error}
                    </div>
                  )}

                  <div className="flex items-center justify-between text-xs text-white/40">
                    <span>更新于 {relativeTime(p.updated_at)}</span>
                    {isRunning && (
                      <span className="inline-flex items-center gap-1">
                        <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                        自动刷新中
                      </span>
                    )}
                  </div>
                </div>

                {/* hover 删除按钮 */}
                <button
                  className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition chip bg-red-500/20 text-red-300 hover:bg-red-500/30 border border-red-500/30"
                  onClick={e => {
                    e.stopPropagation();
                    setConfirmDeleteId(p.project_id);
                  }}
                  title="删除项目"
                >
                  🗑 删除
                </button>

                {confirmDeleteId === p.project_id && (
                  <div
                    className="absolute inset-0 bg-gray-950/95 backdrop-blur-sm flex flex-col items-center justify-center text-center p-4 z-10"
                    onClick={e => e.stopPropagation()}
                  >
                    <div className="text-2xl mb-2">⚠️</div>
                    <div className="font-semibold mb-1">确认删除该项目？</div>
                    <div className="text-xs text-white/60 mb-4">
                      删除后无法恢复，所有章节、音色配置与构建历史都会丢失。
                    </div>
                    <div className="flex gap-2">
                      <button
                        className="btn-ghost"
                        onClick={e => {
                          e.stopPropagation();
                          setConfirmDeleteId(null);
                        }}
                      >
                        取消
                      </button>
                      <button
                        className="btn bg-red-600 hover:bg-red-500 text-white"
                        onClick={e => {
                          e.stopPropagation();
                          onDelete(p.project_id);
                        }}
                      >
                        🗑 确认删除
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
