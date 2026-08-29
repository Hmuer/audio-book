'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { api, PrepareProgress, ProjectListItem } from '@/lib/api';
import { parseTime, relativeTime } from '@/lib/time';
import CreateAudiobookDialog from './CreateAudiobookDialog';

// ===== 常量 =====
const POLL_INTERVAL_MS = 3000;

// 状态定义（Eleven 风格：柔和、克制，不是浓重的半透明块）
// 颜色全部引用 status CSS 变量，深浅主题自动切换
const STATUS_DEFS: Record<string, { label: string; bg: string; color: string; dot: string; pulse?: boolean }> = {
  draft:           { label: '草稿',      bg: 'rgb(var(--status-muted-bg) / 0.12)',    color: 'rgb(var(--status-muted-fg))',    dot: 'rgb(var(--status-muted-dot))' },
  imported:        { label: '已导入',    bg: 'rgb(var(--status-info-bg) / 0.12)',     color: 'rgb(var(--status-info-fg))',     dot: 'rgb(var(--status-info-dot))' },
  preparing:       { label: '识别中',    bg: 'rgb(var(--status-warn-bg) / 0.14)',     color: 'rgb(var(--status-warn-fg))',     dot: 'rgb(var(--status-warn-dot))', pulse: true },
  ready:           { label: '就绪',      bg: 'rgb(var(--status-ready-bg) / 0.12)',    color: 'rgb(var(--status-ready-fg))',    dot: 'rgb(var(--status-ready-dot))' },
  synthesizing:    { label: '合成中',    bg: 'rgb(var(--status-synth-bg) / 0.14)',    color: 'rgb(var(--status-synth-fg))',    dot: 'rgb(var(--status-synth-dot))', pulse: true },
  done:            { label: '已完成',    bg: 'rgb(var(--status-success-bg) / 0.12)',  color: 'rgb(var(--status-success-fg))',  dot: 'rgb(var(--status-success-dot))' },
  success:         { label: '已完成',    bg: 'rgb(var(--status-success-bg) / 0.12)',  color: 'rgb(var(--status-success-fg))',  dot: 'rgb(var(--status-success-dot))' },
  partial_success: { label: '部分成功',  bg: 'rgb(var(--status-partial-bg) / 0.12)',  color: 'rgb(var(--status-partial-fg))',  dot: 'rgb(var(--status-partial-dot))' },
  failed:          { label: '失败',      bg: 'rgb(var(--status-error-bg) / 0.12)',    color: 'rgb(var(--status-error-fg))',    dot: 'rgb(var(--status-error-dot))' },
  cancelled:       { label: '已取消',    bg: 'rgb(var(--status-muted-bg) / 0.12)',    color: 'rgb(var(--status-muted-fg))',    dot: 'rgb(var(--status-muted-dot))' },
};

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
              background: 'rgb(var(--status-error-bg) / 0.08)',
              color: 'rgb(var(--status-error-fg))',
              border: '1px solid rgb(var(--status-error-bg) / 0.22)',
            }}>
              {prog.last_error}
            </span>
          )}
          {typeof prog.restart_count === 'number' && prog.restart_count > 0 && (
            <span className="chip-soft">
              自动恢复 × {prog.restart_count}
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
            <div className="mt-1" style={{ color: 'rgb(var(--status-warn-fg))' }}>有 {m.charFailedN} 个切片失败，完成后可补跑</div>
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
            <div className="mt-1" style={{ color: 'rgb(var(--status-warn-fg))' }}>有 {m.dialogueFailedN} 批对白失败，完成后可补跑</div>
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
  if (preparing.length) summary.push(`${preparing.length} 个识别任务进行中`);
  if (synthesizing.length) summary.push(`${synthesizing.length} 个合成任务进行中`);
  return (
    <div
      className="sticky top-2 z-30 rounded-lg px-5 py-4 mb-5 animate-fade-in"
      style={{
        border: '1px solid rgb(var(--ink-300))',
        background: 'rgb(var(--ink-100))',
      }}
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3 min-w-0">
          <span className="badge-dot animate-pulse-soft" style={{ background: 'rgb(var(--brand-500))', transform: 'scale(1.4)' }} />
          <div className="min-w-0">
            <div className="font-semibold truncate text-brand-300">
              {summary.join(' · ')}
            </div>
            <div className="text-xs truncate mt-0.5 text-brand-300/70">
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

// ===== 空状态 · Edtech Stepper Row =====
function StepperRow({
  step, title, desc, iconName, status,
}: {
  step: number;
  title: string;
  desc: string;
  iconName: 'upload' | 'assign' | 'render';
  status: 'done' | 'current' | 'pending';
}) {
  const palette =
    status === 'done'
      ? { ring: 'rgb(var(--ink-200))',          fg: 'rgb(var(--ink-700))',        text: 'rgb(var(--ink-800))',     muted: 'rgb(var(--ink-700) / 0.65)' }
      : status === 'current'
      ? { ring: 'rgb(var(--brand-600))',        fg: '#fff',                        text: 'rgb(var(--ink-900))',     muted: 'rgb(var(--ink-700) / 0.65)' }
      : { ring: 'rgb(var(--ink-200))',          fg: 'rgb(var(--ink-500))',         text: 'rgb(var(--ink-700) / 0.55)', muted: 'rgb(var(--ink-700) / 0.4)' };

  return (
    <li className="relative grid grid-cols-[48px_minmax(0,1fr)] gap-3 items-start">
      {/* 竖线：除最后一个外，向下延伸 100% */}
      {step < 3 && (
        <span
          className="absolute left-[23px] top-[40px] bottom-[-16px] w-px"
          style={{ background: 'rgb(var(--ink-300) / 0.9)' }}
        />
      )}
      {/* 左侧：实心方 + SVG 图标（Edtech 工具感） */}
      <div className="relative flex flex-col items-center">
        <div
          className="w-11 h-11 shrink-0 grid place-items-center shadow-[0_6px_16px_-8px_rgba(0,0,0,0.55)]"
          style={{
            borderRadius: 'var(--radius-sm)',
            background: palette.ring,
            boxShadow: status === 'current'
              ? `0 0 0 3px rgb(var(--brand-500) / 0.18), 0 6px 16px -8px rgb(var(--brand-700) / 0.85)`
              : undefined,
            color: palette.fg,
            opacity: status === 'pending' ? 0.9 : 1,
          }}
        >
          <StepperIcon name={iconName} />
        </div>
        {/* Step 编号（简单 · 无装饰） */}
        <span
          className="mt-1.5 text-[10px] font-medium px-1.5 py-0.5"
          style={{
            borderRadius: 4,
            background: status === 'pending'
              ? 'rgb(var(--ink-300) / 0.45)'
              : 'rgb(var(--brand-500) / 0.14)',
            color: status === 'pending' ? 'rgb(var(--ink-700) / 0.7)' : 'rgb(var(--brand-300))',
          }}
        >
          0{step}
        </span>
      </div>
      {/* 右侧：标题 + 描述 */}
      <div className="pt-1">
        <div className="flex items-center gap-2 mb-0.5">
          <h4 className="text-[14px] font-semibold leading-tight" style={{ color: palette.text }}>
            {title}
          </h4>
          {status === 'current' && (
            <span className="text-[10px] font-medium px-1.5 py-0.5"
              style={{
                borderRadius: 'var(--radius-xs)',
                background: 'rgb(var(--ink-300) / 0.65)',
                color: 'rgb(var(--ink-700))',
                border: '1px solid rgb(var(--ink-400) / 0.65)',
              }}
            >进行中</span>
          )}
          {status === 'done' && (
            <span className="text-[10px] font-medium px-1.5 py-0.5"
              style={{
                borderRadius: 'var(--radius-xs)',
                background: 'rgb(var(--ink-300) / 0.55)',
                color: 'rgb(var(--ink-600))',
              }}
            >已就绪</span>
          )}
        </div>
        <p className="text-[13px] leading-5" style={{ color: palette.muted }}>{desc}</p>
      </div>
    </li>
  );
}

function StepperIcon({ name }: { name: 'upload' | 'assign' | 'render' }) {
  const common = {
    width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.8,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  };
  switch (name) {
    case 'upload':
      return (<svg {...common}><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>);
    case 'assign':
      return (<svg {...common}><path d="M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>);
    case 'render':
      return (<svg {...common}><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>);
  }
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
      // 同步给顶栏
      try {
        window.dispatchEvent(new CustomEvent('app:projects-refreshed', { detail: list }));
      } catch {}
    } catch (e: any) {
      setErr(String(e?.message || e));
      setItems([]);
    }
  };

  useEffect(() => {
    reload();
    pollTimerRef.current = setInterval(reload, POLL_INTERVAL_MS);
    // 接收顶栏"新建有声书"CTA
    const onOpen = () => setShowCreateDialog(true);
    window.addEventListener('app:open-create-dialog', onOpen);
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
      window.removeEventListener('app:open-create-dialog', onOpen);
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
          {/* Edtech 工具化：仅当页标题，不做英文 eyebrow 胶囊 */}
          <h2 className="headline-lg text-[22px] sm:text-[24px]">我的有声书</h2>
          <p className="mt-1 text-sm" style={{ color: 'rgb(var(--ink-500))' }}>
            上传小说原文 · AI 识别角色 · 一键合成多角色有声书
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
            border: '1px solid rgb(var(--status-error-bg) / 0.28)',
            background: 'rgb(var(--status-error-bg) / 0.06)',
            color: 'rgb(var(--status-error-fg))',
          }}
        >{err}</div>
      )}

      {/* 加载中 */}
      {loading && (
        <div className="rounded-lg border border-ink-300/70 bg-ink-200 text-center py-16">
          <div className="mx-auto w-9 h-9 rounded-full border-2 border-brand-500/30 border-t-brand-500 animate-spin mb-4" />
          <div className="text-sm text-white/50">加载项目列表…</div>
        </div>
      )}

      {/* 空状态：Edtech 三步引导 · 工具化无装饰 */}
      {!loading && items && items.length === 0 && (
        <div className="card p-7 lg:p-8 overflow-hidden">
          <div className="relative grid gap-7 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)] items-start">
            {/* 左：Copy */}
            <div className="text-left">
              {/* Eyebrow （冷灰胶囊 · 无琥珀圆点） */}
              <div className="inline-flex items-center px-2 py-0.5 mb-3"
                style={{
                  borderRadius: 'var(--radius-xs)',
                  background: 'rgb(var(--ink-200))',
                  border: '1px solid rgb(var(--ink-300))',
                }}
              >
                <span className="text-[10.5px] uppercase tracking-[0.08em] font-medium"
                  style={{ color: 'rgb(var(--ink-600))' }}
                >快速开始 · 三步流程</span>
              </div>
              <h3 className="text-[22px] lg:text-[24px] font-semibold text-white leading-[1.22] tracking-tight mb-2">
                创建你的第一个有声书
              </h3>
              <p className="text-[13.5px] leading-6" style={{ color: 'rgb(var(--ink-500))' }}>
                上传 TXT 或 EPUB，AI 自动识别章节、角色与对白；匹配音色后一键合成多角色 MP3。
              </p>
              <div className="mt-5 flex flex-wrap items-center gap-2.5">
                <button
                  className="btn-primary !h-10 !px-4.5 text-[13.5px] font-semibold"
                  onClick={() => setShowCreateDialog(true)}
                >
                  新建有声书
                </button>
                <a href="#/audiobooks/voices"
                  className="btn-ghost !h-10 !px-4 text-[13.5px]"
                >
                  浏览音色库
                </a>
              </div>
            </div>

            {/* 右：Step Stepper（冷灰 · 信息密度优先） */}
            <ol className="relative space-y-4">
              <StepperRow
                step={1}
                title="导入小说原文"
                desc="支持 TXT / EPUB 文件，拖入即上传。"
                iconName="upload"
                status="current"
              />
              <StepperRow
                step={2}
                title="匹配角色音色"
                desc="自动提取角色列表，支持手工分配。"
                iconName="assign"
                status="pending"
              />
              <StepperRow
                step={3}
                title="合成并下载 MP3"
                desc="章节并发合成，随时试听与导出。"
                iconName="render"
                status="pending"
              />
            </ol>
          </div>
        </div>
      )}

      {/* 表格视图 */}
      {!loading && items && items.length > 0 && (
        <div className="card overflow-hidden !p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] text-white/40 uppercase tracking-[0.08em] border-b border-ink-300/60">
                <th className="px-5 py-3 font-medium">有声书</th>
                <th className="px-4 py-3 font-medium w-[100px]">状态</th>
                <th className="px-4 py-3 font-medium w-[200px]">进度</th>
                <th className="px-4 py-3 font-medium w-[80px] text-right tabular-nums">章节</th>
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
                    className="border-b border-ink-300/40 hover:bg-ink-200 transition-colors cursor-pointer stagger-item last:border-b-0"
                    style={{ ['--i' as any]: i }}
                    onClick={() => { window.location.hash = detailHref; }}
                  >
                    {/* 名称 */}
                    <td className="px-5 py-4 min-w-0">
                      <div className="flex items-center gap-3">
                        <div
                          className="w-9 h-9 grid place-items-center shrink-0"
                          style={{
                            borderRadius: 'var(--radius-sm)',
                            background: `linear-gradient(135deg, ${p.cover_color || 'rgb(var(--brand-500))'}33, ${p.cover_color || 'rgb(var(--brand-500))'}11)`,
                            border: `1px solid ${p.cover_color || 'rgb(var(--brand-500))'}33`,
                          }}
                        >
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="rgb(var(--ink-800))" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M4 4h10a4 4 0 014 4v12H8a4 4 0 01-4-4V4z"/><path d="M4 16a4 4 0 014-4h10"/>
                          </svg>
                        </div>
                        <div className="min-w-0">
                            <div className="font-medium text-white truncate">
                              {p.name}
                            </div>
                            {((p.book_title && p.book_title !== p.name) || p.source_filename) && (
                              <div className="text-[11px] text-white/40 truncate mt-0.5 flex items-center gap-3 flex-wrap">
                                {p.book_title && p.book_title !== p.name && (
                                  <span>{p.book_title}</span>
                                )}
                                {p.source_filename && (
                                  <span style={{ color: 'rgb(var(--ink-500))' }}>{p.source_filename}</span>
                                )}
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
                      {p.status === 'preparing' && p.prepare_progress?.stage && (
                        <span className="chip-soft text-[11px]">
                          阶段 · {stageLabel(p.prepare_progress.stage)}
                        </span>
                      )}
                      {p.status === 'synthesizing' && (
                        <div className="text-xs" style={{ color: 'rgb(var(--status-synth-fg))' }}>
                          合成中…
                        </div>
                      )}
                      {p.status === 'importing' && (
                        <div className="text-xs" style={{ color: 'rgb(var(--status-info-fg))' }}>
                          导入中…
                        </div>
                      )}
                      {p.status === 'failed' && p.prepare_progress?.last_error && (
                        <div className="text-xs truncate" style={{ color: 'rgb(var(--status-error-fg))' }} title={p.prepare_progress.last_error}>
                          {p.prepare_progress.last_error.slice(0, 40)}
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
                        <span className="ml-1 inline-flex items-center gap-1 text-[10px]" style={{ color: 'rgb(var(--ink-500))' }}>
                          <span className="badge-dot animate-pulse-soft !w-1 !h-1" style={{ background: 'rgb(var(--ink-500))' }} />
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
                          className="btn-ghost !px-2.5 !py-1 text-xs"
                          style={{ color: 'rgb(var(--status-error-fg) / 0.85)' }}
                          onClick={() => setConfirmDeleteId(p.project_id)}
                          title="删除"
                        >
                          删除
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
          className="fixed inset-0 z-50 grid place-items-center"
          style={{ background: 'rgba(0,0,0,0.72)' }}
          onClick={() => setConfirmDeleteId(null)}
        >
          <div
            className="bg-ink-50 border border-ink-300/70 rounded-lg p-6 w-full max-w-sm mx-4 text-center animate-scale-in"
            onClick={e => e.stopPropagation()}
          >
            <div className="mx-auto w-11 h-11 rounded-lg grid place-items-center mb-3"
              style={{ background: 'rgb(var(--status-error-bg) / 0.12)' }}>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="rgb(var(--status-error-fg))" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
                <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>
                <line x1="12" y1="9" x2="12" y2="13"/>
                <line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
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
                  background: 'rgb(var(--status-error-bg) / 0.15)',
                  color: 'rgb(var(--status-error-fg))',
                  border: '1px solid rgb(var(--status-error-bg) / 0.3)',
                }}
                onClick={() => onDelete(confirmDeleteId)}
              >
                确认删除
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
