'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { api, PrepareProgress, ProjectListItem } from '@/lib/api';

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
      className="sticky top-2 z-30 rounded-2xl backdrop-blur-xl px-5 py-4 mb-5 animate-fade-in"
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
              onClick={() => { window.location.hash = `#/projects/${preparing[0].project_id}`; }}
              title={preparing.map(p => p.book_title || p.name).join(' · ')}
            >
              查看识别 · {(preparing[0].book_title || preparing[0].name).slice(0, 12)}
              {preparing.length > 1 ? ` 等 ${preparing.length} 本` : ''}
            </button>
          )}
          {synthesizing.length > 0 && (
            <button
              className="btn-ghost !px-3 !py-1.5 text-xs"
              onClick={() => { window.location.hash = `#/projects/${synthesizing[0].project_id}`; }}
            >
              查看合成 · {(synthesizing[0].book_title || synthesizing[0].name).slice(0, 12)}
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
                onClick={() => { window.location.hash = `#/projects/${p.project_id}`; }}
                className="text-left surface !p-3 hover:!border-ink-400 transition"
              >
                <div className="flex items-center justify-between gap-2 mb-1.5">
                  <div className="font-medium truncate text-sm text-ink-800">
                    {p.book_title || p.name}
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
  const hasRunning = useMemo(
    () => !!items && items.some(p => p.status === 'preparing' || p.status === 'synthesizing'),
    [items],
  );

  return (
    <section className="space-y-6">
      {/* ===== 顶部操作栏：Eleven 风格 — 大标题 + 右侧操作区 ===== */}
      <div className="flex items-end justify-between gap-4 flex-wrap animate-fade-in">
        <div className="min-w-0">
          <div className="inline-flex items-center gap-2 chip-soft mb-3">
            <span className="badge-dot" style={{ background: '#8b5cf6' }} />
            工作台 · 有声书项目
          </div>
          <h2 className="headline text-[26px] sm:text-3xl leading-tight">
            你的有声书
          </h2>
          <p className="mt-2 text-sm text-ink-600">
            每个项目独立保存章节识别结果、角色音色与构建历史
            {hasRunning ? <> · <span style={{ color: '#fcd34d' }}>后台任务进行中</span></> : null}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button className="btn-ghost" onClick={reload} title="立即刷新项目列表">
            <span className="opacity-80">⟳</span> 刷新
          </button>
          <button
            className="btn-primary"
            onClick={() => { window.location.hash = '#/projects/new'; }}
          >
            <span className="text-base leading-none">＋</span> 新建项目
          </button>
        </div>
      </div>

      {err && (
        <div className="rounded-2xl px-4 py-3 text-sm"
          style={{
            border: '1px solid rgba(251,113,133,0.28)',
            background: 'rgba(251,113,133,0.06)',
            color: '#fecdd3',
          }}
        >{err}</div>
      )}

      {!loading && items && <RunningTasksBar items={items} />}

      {/* 加载中 */}
      {loading && (
        <div className="card text-center py-16">
          <div className="mx-auto w-9 h-9 rounded-full border-2 border-brand-500/30 border-t-brand-500 animate-spin mb-4" />
          <div className="text-sm text-ink-600">加载项目列表…</div>
        </div>
      )}

      {/* 空状态（Eleven 风格：插画式 icon + 明确 CTA） */}
      {!loading && items && items.length === 0 && (
        <div className="relative card text-center py-20 overflow-hidden">
          <div className="glow-orb w-[360px] h-[360px] bg-brand-500/20" style={{ left: '50%', top: '-80px', transform: 'translateX(-50%)' }} />
          <div className="relative mx-auto w-20 h-20 rounded-3xl grid place-items-center mb-6"
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
          <h3 className="headline text-xl mb-2">还没有项目</h3>
          <p className="text-sm text-ink-600 max-w-md mx-auto mb-7">
            上传一本 TXT 小说，系统会自动识别章节切分、角色名单与对白归属，
            为每个角色分配音色后即可一键生成整本多音色 MP3 有声书。
          </p>
          <button
            className="btn-primary !px-6 !py-2.5"
            onClick={() => { window.location.hash = '#/projects/new'; }}
          >
            ＋ 创建第一个项目
          </button>
        </div>
      )}

      {/* 卡片网格（Eleven 风格：浮岛 + 细条 + 左侧色条 2px + hover lift） */}
      {!loading && items && items.length > 0 && (
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-5">
          {items.map((p, i) => {
            const isRunning = p.status === 'preparing' || p.status === 'synthesizing';
            const def = STATUS_DEFS[p.status];
            return (
              <div
                key={p.project_id}
                className={`group card cursor-pointer animate-fade-in overflow-hidden !p-0`}
                style={{ animationDelay: `${i * 40}ms` }}
                onClick={() => { window.location.hash = `#/projects/${p.project_id}`; }}
              >
                {/* 细色条（Eleven 风格） */}
                <div
                  className="stripe"
                  style={{
                    backgroundColor: isRunning ? def.dot : (p.cover_color || '#8b5cf6'),
                    opacity: isRunning ? 1 : 0.85,
                  }}
                />
                {isRunning && (
                  <div className="absolute top-0 bottom-0 left-0 w-[2px] animate-pulse-soft bg-white/50" />
                )}

                <div className="pl-4 pr-5 py-5 h-full flex flex-col gap-3">
                  {/* 顶行：标题 + 状态 */}
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="font-semibold truncate text-ink-900 text-[15px] leading-snug">
                        {p.book_title || p.name}
                      </div>
                      {p.book_title && p.book_title !== p.name && (
                        <div className="text-[11px] text-ink-500 truncate mt-1">{p.name}</div>
                      )}
                    </div>
                    <StatusBadge status={p.status} />
                  </div>

                  {/* Meta 行 */}
                  <div className="flex items-center gap-3 text-[12px] text-ink-600">
                    <span>📜 {p.chapter_count} 章</span>
                    <span className="truncate">
                      {p.source_filename ? `📄 ${p.source_filename}` : '📄 未上传'}
                    </span>
                  </div>

                  {/* 进度 / 提示 */}
                  <div className="flex-1 min-h-[0]">
                    {p.status === 'preparing' && (
                      <PrepareProgressInline prog={p.prepare_progress} chapterCount={p.chapter_count} />
                    )}
                    {p.status === 'synthesizing' && (
                      <div className="text-xs" style={{ color: '#fdba74' }}>
                        🔊 合成中…点击卡片进入详情查看章节级进度
                      </div>
                    )}
                    {p.status === 'failed' && p.prepare_progress?.last_error && (
                      <div className="text-xs break-words whitespace-pre-wrap line-clamp-3"
                        style={{ color: '#fda4af' }}>
                        ❌ {p.prepare_progress.last_error}
                      </div>
                    )}
                  </div>

                  {/* 底行：时间 + 心跳 */}
                  <div className="flex items-center justify-between text-[11px] text-ink-500 pt-1">
                    <span>更新于 {relativeTime(p.updated_at)}</span>
                    {isRunning && (
                      <span className="inline-flex items-center gap-1.5">
                        <span className="badge-dot animate-pulse-soft" style={{ background: def.dot }} />
                        自动刷新中
                      </span>
                    )}
                  </div>
                </div>

                {/* hover 删除按钮（Eleven 风格：右上小圆点展开式） */}
                <button
                  className="absolute top-3 right-3 opacity-0 group-hover:opacity-100 transition chip"
                  style={{
                    background: 'rgba(251,113,133,0.08)',
                    border: '1px solid rgba(251,113,133,0.22)',
                    color: '#fda4af',
                  }}
                  onClick={e => { e.stopPropagation(); setConfirmDeleteId(p.project_id); }}
                  title="删除项目"
                >
                  🗑 删除
                </button>

                {/* 删除确认 overlay（Eleven 风格：半透明蒙层 + 居中卡片） */}
                {confirmDeleteId === p.project_id && (
                  <div
                    className="absolute inset-0 z-10 flex items-center justify-center p-5"
                    style={{ background: 'rgba(9,9,11,0.78)', backdropFilter: 'blur(6px)' }}
                    onClick={e => e.stopPropagation()}
                  >
                    <div className="surface p-5 max-w-xs w-full text-center animate-scale-in">
                      <div className="mx-auto w-11 h-11 rounded-2xl grid place-items-center mb-3"
                        style={{ background: 'rgba(251,113,133,0.12)' }}>
                        <span className="text-xl">⚠️</span>
                      </div>
                      <div className="font-semibold text-ink-900 mb-1.5">确认删除该项目？</div>
                      <div className="text-xs text-ink-600 mb-5 leading-relaxed">
                        删除后无法恢复，所有章节、角色识别、音色配置与构建历史都会丢失。
                      </div>
                      <div className="flex gap-2">
                        <button
                          className="btn-ghost flex-1"
                          onClick={e => { e.stopPropagation(); setConfirmDeleteId(null); }}
                        >
                          取消
                        </button>
                        <button
                          className="btn-danger flex-1"
                          onClick={e => { e.stopPropagation(); onDelete(p.project_id); }}
                        >
                          🗑 确认
                        </button>
                      </div>
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
