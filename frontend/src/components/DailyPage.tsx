'use client';

/**
 * AI 前沿日报（一级菜单 #/daily）
 *
 * - 无 date → 列表：期数列表 + 运行状态条 + 「立即生成」
 * - 有 date → 详情：音频试听 / 简介·脚本·选题三 Tab / 人工审核 / RSS 地址
 *
 * 日期一律按 `YYYY-MM-DD` 字符串在 URL 里传递，不做 new Date 时区换算
 * （避免 UTC 与 Asia/Shanghai 跨时区把「今天」算成相邻一天）。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  api,
  fetchMediaObjectUrl,
  DailyConfig,
  DailyIssue,
  DailyIssueDetail,
  DailyIssueStatus,
  DailyRunStatus,
  DailyTopic,
} from '@/lib/api';
import { formatDateTime, parseTime } from '@/lib/time';

// =====================================================================
// 常量 · 工具
// =====================================================================

const LIST_LIMIT = 30;
/** 运行中每 2s 轮询，空闲/结束后 5s 轮询（兼顾实时性与请求量） */
const POLL_RUNNING_MS = 2000;
const POLL_IDLE_MS = 5000;

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

type Tone = 'error' | 'warn' | 'ok' | 'info' | 'muted' | 'purple';

const TONES: Record<Tone, { bg: string; border: string; fg: string }> = {
  error:  { bg: 'rgba(var(--status-error-bg), 0.10)',  border: 'rgba(var(--status-error-bg), 0.32)',  fg: 'rgb(var(--status-error-fg))' },
  warn:   { bg: 'rgba(var(--status-warn-bg), 0.10)',   border: 'rgba(var(--status-warn-bg), 0.32)',   fg: 'rgb(var(--status-warn-fg))' },
  ok:     { bg: 'rgba(var(--status-success-bg), 0.10)', border: 'rgba(var(--status-success-bg), 0.32)', fg: 'rgb(var(--status-success-fg))' },
  info:   { bg: 'rgba(var(--status-info-bg), 0.10)',   border: 'rgba(var(--status-info-bg), 0.30)',   fg: 'rgb(var(--status-info-fg))' },
  muted:  { bg: 'rgba(var(--status-muted-bg), 0.10)',  border: 'rgba(var(--status-muted-bg), 0.30)',  fg: 'rgb(var(--status-muted-fg))' },
  purple: { bg: 'rgba(var(--status-synth-bg), 0.10)',  border: 'rgba(var(--status-synth-bg), 0.30)',  fg: 'rgb(var(--status-synth-fg))' },
};

/** 期数状态：draft=待审 / approved=已通过 / rejected=已退回 / failed=生成失败 */
const ISSUE_STATUS: Record<string, { label: string; tone: Tone }> = {
  draft:    { label: '待审',     tone: 'warn' },
  approved: { label: '已通过',   tone: 'ok' },
  rejected: { label: '已退回',   tone: 'purple' },
  failed:   { label: '生成失败', tone: 'error' },
};

const RUN_STATUS_LABEL: Record<DailyRunStatus['status'], string> = {
  idle: '空闲',
  running: '生成中',
  success: '生成完成',
  failed: '生成失败',
};

// 后端 stage 取值可能随实现调整：命中即翻译，未命中回退显示原始值，不隐藏信息。
const STAGE_LABELS: Record<string, string> = {
  queued: '排队中', start: '准备中', prepare: '准备中', preparing: '准备中',
  fetch: '抓取资讯', fetching: '抓取资讯', crawl: '抓取资讯', collect: '采集资讯', collecting: '采集资讯',
  cluster: '事件去重', clustering: '事件去重',
  filter: '筛选选题', dedup: '选题去重', rank: '选题打分', ranking: '选题打分', score: '选题打分', select: '选题确认',
  llm: 'LLM 撰写', write: '撰写稿件', writing: '撰写稿件', script: '生成脚本', shownotes: '生成简介',
  tts: '语音合成', synthesize: '语音合成', synthesizing: '语音合成', audio: '音频处理',
  store: '写入产物', package: '打包发布', publish: '发布', feed: '生成订阅源',
  done: '完成', success: '完成', failed: '失败', cancel: '已取消', cancelled: '已取消',
};

function stageLabel(stage?: string | null): string {
  if (!stage) return '';
  return STAGE_LABELS[stage] ?? stage;
}

function errorText(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (typeof e === 'string') return e;
  return '未知错误';
}

/** 2026-11-24 → 周二（按本地日解析，date 字符串本身就是用户视角的日期） */
function weekdayOf(date: string): string {
  if (!date) return '';
  const d = new Date(`${date}T00:00:00`);
  if (Number.isNaN(d.getTime())) return '';
  return WEEKDAYS[d.getDay()] ?? '';
}

/** 毫秒时长 → mm:ss（保留两位，>59min 时分钟继续累加） */
function fmtDuration(ms: number | null | undefined): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) return '—';
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/** 简短时间（列表里省空间）：11/24 22:05 */
function fmtShortDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = parseTime(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** progress 可能是 0–1 也可能是 0–100，统一归一到 0–100 */
function progressPct(p: number | null | undefined): number {
  if (typeof p !== 'number' || !Number.isFinite(p)) return 0;
  const v = p <= 1 ? p * 100 : p;
  return Math.max(0, Math.min(100, Math.round(v)));
}

function reasonsText(t: DailyTopic): string {
  const list = Array.isArray(t.reasons) ? t.reasons.filter(Boolean) : [];
  return list.length ? list.join('；') : '—';
}

// =====================================================================
// 入口：按有无 date 分流（两个子组件各自持有 hooks，避免条件 hooks）
// =====================================================================

export default function DailyPage({ date }: { date?: string }) {
  if (date) return <DailyDetail date={date} />;
  return <DailyList />;
}

// =====================================================================
// 列表页
// =====================================================================

function DailyList() {
  const [issues, setIssues] = useState<DailyIssue[]>([]);
  const [total, setTotal] = useState(0);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [config, setConfig] = useState<DailyConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);

  const [run, setRun] = useState<DailyRunStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [runBusy, setRunBusy] = useState(false);
  const [runNotice, setRunNotice] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  // silent=true：后台刷新（轮询/生成后），不闪骨架
  const refreshList = useCallback(async (silent = false) => {
    if (!silent) setListLoading(true);
    try {
      const r = await api.dailyIssues(LIST_LIMIT);
      const items = Array.isArray(r.items) ? r.items : [];
      setIssues(items);
      setTotal(typeof r.total === 'number' ? r.total : items.length);
      setListError(null);
    } catch (e) {
      setListError(errorText(e));
    } finally {
      if (!silent) setListLoading(false);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const c = await api.dailyConfig();
      setConfig(c);
      setConfigError(null);
    } catch (e) {
      setConfigError(errorText(e));
    }
  }, []);

  // 首次加载
  useEffect(() => {
    void refreshList();
    void loadConfig();
  }, [refreshList, loadConfig]);

  // 运行状态轮询：运行中 2s，其它 5s；running → 结束的瞬间静默刷新列表
  const prevRunStatusRef = useRef<DailyRunStatus['status'] | null>(null);
  const runningRef = useRef(false);

  useEffect(() => {
    let alive = true;
    let timer: number | null = null;

    const tick = async () => {
      try {
        const r = await api.dailyRunStatus();
        if (!alive) return;
        const next = r.run;
        setRun(next);
        setStatusError(null);
        const prev = prevRunStatusRef.current;
        prevRunStatusRef.current = next.status;
        runningRef.current = next.status === 'running';
        // 一次运行刚结束（成功或失败）→ 让列表出现新一期 / 失败态
        if (prev === 'running' && next.status !== 'running') {
          void refreshList(true);
        }
      } catch (e) {
        if (alive) setStatusError(errorText(e));
      } finally {
        if (alive) {
          timer = window.setTimeout(tick, runningRef.current ? POLL_RUNNING_MS : POLL_IDLE_MS);
        }
      }
    };

    void tick();
    return () => {
      alive = false;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [refreshList]);

  // 立即生成
  const triggerRun = async () => {
    setRunBusy(true);
    setRunError(null);
    setRunNotice(null);
    try {
      await api.dailyRun();
      setRunNotice('已触发生成任务，正在生成中…');
      // 立刻补一次状态，让状态条尽早出现；失败也不影响轮询接管
      try {
        const r = await api.dailyRunStatus();
        setRun(r.run);
        prevRunStatusRef.current = r.run.status;
        runningRef.current = r.run.status === 'running';
        setStatusError(null);
      } catch {
        /* 轮询会继续覆盖 */
      }
      void refreshList(true);
    } catch (e) {
      const msg = errorText(e);
      // 后端「已有运行中任务」→ 409；_fetch 抛出的 message 形如 "HTTP 409: ..."
      if (/HTTP\s*409|409/.test(msg) || /运行中|生成中|running/i.test(msg)) {
        setRunError('正在生成中，请稍候再试');
      } else {
        setRunError(msg);
      }
    } finally {
      setRunBusy(false);
    }
  };

  const isRunning = run?.status === 'running';
  const enabledSources = config ? config.sources.filter((s) => s.enabled).length : 0;

  return (
    <section className="animate-fade-in space-y-5">
      {/* ============ 页头 ============ */}
      <div className="glass-panel p-5 sm:p-6 relative overflow-hidden">
        <div className="flex flex-wrap items-start gap-4">
          <div className="w-11 h-11 grid place-items-center shrink-0 bg-brand-600 text-white" style={{ borderRadius: 'var(--radius-md)' }}>
            <NewsGlyph size={20} />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="headline text-xl">AI 前沿日报</h1>
            <p className="text-xs text-ink-500 mt-1">
              {config?.show?.description || '每日聚合 AI 前沿资讯，自动生成可收听的日报节目。'}
            </p>
            <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
              <span className="chip-soft">共 {total} 期</span>
              {config && <span className="chip-soft">目标时长 {config.target_minutes} 分钟</span>}
              {config && (
                <span className="chip-soft">
                  订阅源 {enabledSources}/{config.sources.length} 已启用
                </span>
              )}
              {config && (
                <span className="chip-soft" style={{ color: config.llm_enabled ? 'rgb(var(--status-success-fg))' : 'rgb(var(--ink-500))' }}>
                  {config.llm_enabled ? 'LLM 已启用' : 'LLM 未启用'}
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              className="btn-ghost"
              onClick={() => void refreshList()}
              disabled={listLoading}
            >
              <RefreshGlyph size={14} />
              刷新
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void triggerRun()}
              disabled={runBusy || isRunning}
            >
              <SparkGlyph size={14} />
              {isRunning ? '生成中…' : runBusy ? '提交中…' : '立即生成'}
            </button>
          </div>
        </div>

        {configError && (
          <div className="mt-3">
            <Notice tone="warn">日报配置读取失败：{configError}</Notice>
          </div>
        )}
      </div>

      {/* ============ 生成提示 ============ */}
      {runNotice && <Notice tone="ok">{runNotice}</Notice>}
      {runError && <Notice tone="error">{runError}</Notice>}

      {/* ============ 运行状态条 ============ */}
      <RunStatusBar run={run} error={statusError} />

      {/* ============ 错误 / 骨架 / 列表 ============ */}
      {listLoading && <ListSkeleton />}

      {!listLoading && listError && issues.length === 0 && (
        <Notice tone="error" >
          <span className="flex-1">日报列表加载失败：{listError}</span>
          <button type="button" className="btn-ghost !py-1 !px-3 text-xs self-center" onClick={() => void refreshList()}>
            重试
          </button>
        </Notice>
      )}

      {!listLoading && listError && issues.length > 0 && (
        <Notice tone="warn">列表刷新失败：{listError}</Notice>
      )}

      {!listLoading && !listError && issues.length === 0 && (
        <div className="glass-panel py-16 text-center">
          <div
            className="mx-auto w-14 h-14 grid place-items-center mb-4 text-ink-500"
            style={{ background: 'rgb(var(--ink-200))', borderRadius: 'var(--radius-md)', border: '1px solid rgb(var(--ink-300))' }}
          >
            <NewsGlyph size={26} />
          </div>
          <p className="text-sm text-ink-700">还没有日报，点右上角「立即生成」</p>
          <p className="text-[11px] text-ink-500 mt-1">生成完成后会自动出现在这里，可试听、审核并发布。</p>
        </div>
      )}

      {!listLoading && issues.length > 0 && (
        <div className="glass-panel !p-0 overflow-hidden">
          {/* 表头（窄屏隐藏，行内自带标签） */}
          <div className="hidden sm:grid grid-cols-[110px_minmax(0,1fr)_88px_76px_64px_130px] items-center gap-3 px-4 py-2.5 text-[11px] text-ink-500">
            <span>日期</span>
            <span>标题</span>
            <span>状态</span>
            <span className="text-right">时长</span>
            <span className="text-right">条目</span>
            <span className="text-right">生成时间</span>
          </div>
          {issues.map((it) => (
            <IssueRow key={it.date} issue={it} />
          ))}
        </div>
      )}
    </section>
  );
}

// =====================================================================
// 列表子组件
// =====================================================================

function IssueRow({ issue }: { issue: DailyIssue }) {
  return (
    <a
      href={`#/daily/${encodeURIComponent(issue.date)}`}
      className="group grid grid-cols-[88px_minmax(0,1fr)_auto] sm:grid-cols-[110px_minmax(0,1fr)_88px_76px_64px_130px] items-center gap-x-3 gap-y-1 px-4 py-3 border-t border-ink-300/60 hover:bg-ink-200 transition-colors"
    >
      {/* 日期 + 星期 */}
      <div className="flex items-baseline gap-1.5 tabular-nums">
        <span className="font-mono text-[12px] text-ink-800">{issue.date}</span>
        <span className="text-[11px] text-ink-500">{weekdayOf(issue.date)}</span>
      </div>

      {/* 标题（+失败原因） */}
      <div className="min-w-0">
        <div className="text-sm font-medium text-ink-900 truncate group-hover:text-white transition-colors">
          {issue.title || '未命名日报'}
        </div>
        {issue.error && (
          <div className="text-[11px] text-ink-500 truncate" title={issue.error}>
            {issue.error}
          </div>
        )}
      </div>

      {/* 状态 */}
      <div className="justify-self-start">
        <StatusChip status={issue.status} />
      </div>

      {/* 时长 */}
      <div className="hidden sm:block text-right text-xs text-ink-600 tabular-nums">
        {fmtDuration(issue.duration_ms)}
      </div>

      {/* 条目数 */}
      <div className="hidden sm:block text-right text-xs text-ink-600 tabular-nums">
        {issue.item_count} 条
      </div>

      {/* 生成时间 */}
      <div
        className="hidden sm:block text-right text-[11px] text-ink-500 tabular-nums"
        title={issue.created_at ? formatDateTime(issue.created_at) : undefined}
      >
        {fmtShortDateTime(issue.created_at)}
      </div>
    </a>
  );
}

function RunStatusBar({ run, error }: { run: DailyRunStatus | null; error: string | null }) {
  if (!run && !error) return null;

  if (!run) {
    return <Notice tone="warn">运行状态读取失败：{error}（列表仍可正常使用）</Notice>;
  }

  const running = run.status === 'running';
  const pct = progressPct(run.progress);
  const tone: Tone =
    run.status === 'failed' ? 'error'
      : run.status === 'success' ? 'ok'
        : running ? 'info'
          : 'muted';
  const t = TONES[tone];
  const lastLog = run.log_tail && run.log_tail.length ? run.log_tail[run.log_tail.length - 1] : '';
  const headline = running
    ? (stageLabel(run.stage) || '正在生成…')
    : run.status === 'success'
      ? '上一次生成已完成'
      : run.status === 'failed'
        ? '上一次生成失败'
        : '当前空闲，可点右上角「立即生成」';
  const stats: Record<string, any> = run.stats ?? {};
  const numStat = (v: unknown): number | null =>
    typeof v === 'number' && Number.isFinite(v) ? v : null;
  const statCandidates = numStat(stats.items) ?? numStat(stats.candidates);
  const statKept = numStat(stats.kept);

  return (
    <div className="glass-panel !p-4 sm:!p-5 space-y-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="chip" style={{ background: t.bg, color: t.fg, border: `1px solid ${t.border}` }}>
          <span className={`badge-dot ${running ? 'animate-pulse-soft' : ''}`} style={{ backgroundColor: t.fg }} />
          {RUN_STATUS_LABEL[run.status] ?? run.status}
        </span>
        <span className="text-sm font-medium text-ink-800">{headline}</span>
        <span className="ml-auto text-[11px] text-ink-500 tabular-nums">{pct}%</span>
      </div>

      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${pct}%` }} />
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-500">
        {run.date && (
          <span>
            日期 <span className="font-mono text-ink-700 tabular-nums">{run.date}</span>
          </span>
        )}
        {run.started_at && (
          <span>
            开始 <span className="text-ink-700 tabular-nums">{fmtShortDateTime(run.started_at)}</span>
          </span>
        )}
        {run.finished_at && (
          <span>
            结束 <span className="text-ink-700 tabular-nums">{fmtShortDateTime(run.finished_at)}</span>
          </span>
        )}
        {statCandidates !== null && (
          <span>
            候选 <span className="text-ink-700 tabular-nums">{statCandidates}</span>
          </span>
        )}
        {statKept !== null && (
          <span>
            收录 <span className="text-ink-700 tabular-nums">{statKept}</span>
          </span>
        )}
        {running && <span className="text-ink-500">每 {POLL_RUNNING_MS / 1000}s 自动刷新状态</span>}
      </div>

      {lastLog && (
        <div className="font-mono text-[11px] text-ink-500 truncate" title={run.log_tail.join('\n')}>
          {lastLog}
        </div>
      )}

      {run.status === 'failed' && run.error && <Notice tone="error">生成失败：{run.error}</Notice>}
      {run.status === 'success' && (
        <Notice tone="ok">
          生成完成{run.finished_at ? ` · ${fmtShortDateTime(run.finished_at)}` : ''}
        </Notice>
      )}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="glass-panel !p-0 overflow-hidden">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-4 border-t border-ink-300/60 first:border-t-0">
          <div className="skeleton h-3.5 w-24 shrink-0" />
          <div className="skeleton h-3.5 flex-1 max-w-[320px]" />
          <div className="skeleton h-5 w-14 shrink-0" />
          <div className="skeleton h-3.5 w-16 shrink-0 ml-auto" />
        </div>
      ))}
    </div>
  );
}

// =====================================================================
// 详情页
// =====================================================================

type DetailTab = 'shownotes' | 'script' | 'topics';

function DailyDetail({ date }: { date: string }) {
  const [detail, setDetail] = useState<DailyIssueDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<DetailTab>('shownotes');

  const [config, setConfig] = useState<DailyConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);

  // 审核
  const [note, setNote] = useState('');
  const noteTouchedRef = useRef(false);
  const [reviewing, setReviewing] = useState(false);
  const [reviewMsg, setReviewMsg] = useState<string | null>(null);
  const [reviewErr, setReviewErr] = useState<string | null>(null);

  // 音频
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [audioLoading, setAudioLoading] = useState(false);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [audioReloadKey, setAudioReloadKey] = useState(0);

  // RSS 复制
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<number | null>(null);

  const loadDetail = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const d = await api.dailyIssue(date);
      setDetail(d);
      setError(null);
      // 备注只在用户未手动编辑时用后端值预填（避免覆盖正在输入的内容）
      if (!noteTouchedRef.current) setNote(d.issue.review_note ?? '');
    } catch (e) {
      setError(errorText(e));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [date]);

  const loadConfig = useCallback(async () => {
    try {
      const c = await api.dailyConfig();
      setConfig(c);
      setConfigError(null);
    } catch (e) {
      setConfigError(errorText(e));
    }
  }, []);

  useEffect(() => {
    void loadDetail();
    void loadConfig();
  }, [loadDetail, loadConfig]);

  // 音频：/api/daily/issues/<date>/audio 需要 JWT 头，<audio src> 带不上 →
  // 先取字节转 Blob URL；卸载/换期时 revoke，避免内存泄漏。
  const hasAudio = !!detail?.issue.has_audio;
  useEffect(() => {
    setAudioUrl(null);
    setAudioError(null);
    if (!hasAudio) {
      setAudioLoading(false);
      return;
    }
    let alive = true;
    let objUrl: string | null = null;
    setAudioLoading(true);
    fetchMediaObjectUrl(`/api/daily/issues/${encodeURIComponent(date)}/audio`)
      .then((u) => {
        if (alive) {
          objUrl = u;
          setAudioUrl(u);
        } else {
          URL.revokeObjectURL(u);
        }
      })
      .catch((e) => {
        if (alive) setAudioError(errorText(e));
      })
      .finally(() => {
        if (alive) setAudioLoading(false);
      });
    return () => {
      alive = false;
      if (objUrl) URL.revokeObjectURL(objUrl);
    };
  }, [date, hasAudio, audioReloadKey]);

  // 复制成功提示的定时器清理
  useEffect(() => () => {
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
  }, []);

  const issue = detail?.issue ?? null;
  const topics: DailyTopic[] = detail?.meta?.topics ?? [];
  // 详情刚回来、音频 effect 尚未跑完的一帧里避免音频区空白
  const audioWaiting = audioLoading || (!!issue?.has_audio && !audioUrl && !audioError);

  // feed_url 可能是相对路径（同源）→ 拼成完整地址方便直接贴到播客客户端
  const fullFeedUrl = useMemo(() => {
    const raw = detail?.feed_url || config?.feed_url || '';
    if (!raw) return '';
    if (/^https?:\/\//i.test(raw)) return raw;
    if (typeof window !== 'undefined' && raw.startsWith('/')) return `${window.location.origin}${raw}`;
    return raw;
  }, [detail?.feed_url, config?.feed_url]);

  const submitReview = async (decision: 'approve' | 'reject') => {
    if (!issue) return;
    setReviewing(true);
    setReviewMsg(null);
    setReviewErr(null);
    try {
      const r = await api.dailyReview(date, decision, note.trim() || undefined);
      setDetail((prev) => (prev ? { ...prev, issue: r.issue } : prev));
      setReviewMsg(decision === 'approve' ? '审核结果已保存：通过，可发布' : '审核结果已保存：已退回');
    } catch (e) {
      setReviewErr(errorText(e));
    } finally {
      setReviewing(false);
    }
  };

  const copyFeed = async () => {
    if (!fullFeedUrl) return;
    try {
      await navigator.clipboard.writeText(fullFeedUrl);
      setCopied(true);
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = window.setTimeout(() => setCopied(false), 1800);
    } catch {
      // 剪贴板不可用（非 https / 权限拒绝）→ 静默失败，输入框仍可手动全选复制
    }
  };

  // ---------- 加载态 ----------
  if (loading) {
    return (
      <section className="animate-fade-in space-y-5">
        <BackLink />
        <div className="glass-panel p-5 sm:p-6 space-y-3">
          <div className="skeleton h-3.5 w-40" />
          <div className="skeleton h-6 w-2/3" />
          <div className="skeleton h-3 w-1/3" />
        </div>
        <div className="glass-panel p-5 space-y-3">
          <div className="skeleton h-10 w-full" />
          <div className="skeleton h-3 w-1/2" />
        </div>
      </section>
    );
  }

  // ---------- 错误态 ----------
  if (!detail || !issue) {
    return (
      <section className="animate-fade-in space-y-5">
        <BackLink />
        <Notice tone="error">
          <span className="flex-1">日报详情加载失败：{error || '未获取到数据'}</span>
          <button type="button" className="btn-ghost !py-1 !px-3 text-xs self-center" onClick={() => void loadDetail()}>
            重试
          </button>
        </Notice>
      </section>
    );
  }

  return (
    <section className="animate-fade-in space-y-5">
      <BackLink />

      {error && <Notice tone="warn">详情刷新失败：{error}</Notice>}

      {/* ============ 头部信息 ============ */}
      <div className="glass-panel p-5 sm:p-6">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[13px] text-ink-500 tabular-nums">{issue.date}</span>
              <span className="text-[11px] text-ink-500">{weekdayOf(issue.date)}</span>
              <StatusChip status={issue.status} />
            </div>
            <h1 className="headline text-xl mt-2 break-words">{issue.title || '未命名日报'}</h1>
            <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-500">
              <span>
                时长 <span className="text-ink-700 tabular-nums">{fmtDuration(issue.duration_ms)}</span>
              </span>
              <span>
                条目 <span className="text-ink-700 tabular-nums">{issue.item_count}</span>
              </span>
              <span title={issue.created_at ? formatDateTime(issue.created_at) : undefined}>
                生成 <span className="text-ink-700 tabular-nums">{fmtShortDateTime(issue.created_at)}</span>
              </span>
              {issue.reviewed_at && (
                <span title={formatDateTime(issue.reviewed_at)}>
                  审核 <span className="text-ink-700 tabular-nums">{fmtShortDateTime(issue.reviewed_at)}</span>
                </span>
              )}
            </div>
            {issue.review_note && (
              <div className="mt-2 text-[11px] text-ink-500">
                审核备注：<span className="text-ink-700">{issue.review_note}</span>
              </div>
            )}
          </div>
        </div>

        {issue.error && (
          <div className="mt-3">
            <Notice tone="error">生成失败：{issue.error}</Notice>
          </div>
        )}
      </div>

      {/* ============ 音频试听 ============ */}
      <div className="glass-panel !p-4 sm:!p-5">
        <div className="flex items-center gap-2 mb-3">
          <span className="text-ink-500">
            <VolumeGlyph size={15} />
          </span>
          <span className="text-sm font-semibold text-ink-800">音频试听</span>
          {issue.has_audio && (
            <span className="ml-auto text-[11px] text-ink-500 tabular-nums">{fmtDuration(issue.duration_ms)}</span>
          )}
        </div>

        {audioWaiting && (
          <div className="flex items-center gap-2 py-2 text-xs text-ink-500">
            <span className="inline-block w-3.5 h-3.5 border-2 border-ink-400/70 border-t-brand-500 rounded-full animate-spin" />
            音频加载中…
          </div>
        )}

        {!audioWaiting && audioError && (
          <Notice tone="error">
            <span className="flex-1">音频加载失败：{audioError}</span>
            <button
              type="button"
              className="btn-ghost !py-1 !px-3 text-xs self-center"
              onClick={() => setAudioReloadKey((k) => k + 1)}
            >
              重试
            </button>
          </Notice>
        )}

        {!audioWaiting && !audioError && audioUrl && (
          // eslint-disable-next-line jsx-a11y/media-has-caption
          <audio controls preload="metadata" src={audioUrl} className="w-full" />
        )}

        {!audioWaiting && !audioError && !audioUrl && !issue.has_audio && (
          <div
            className="py-7 grid place-items-center text-xs text-ink-500 border border-dashed border-ink-300"
            style={{ borderRadius: 'var(--radius-sm)' }}
          >
            本期无音频
          </div>
        )}
      </div>

      {/* ============ Tabs：简介 / 脚本 / 选题 ============ */}
      <div className="space-y-4">
        <nav className="glass-panel !p-1.5 overflow-x-auto">
          <div className="flex items-center gap-1 py-0.5 min-w-0">
            {([
              { key: 'shownotes', label: '节目简介' },
              { key: 'script', label: '口播脚本' },
              { key: 'topics', label: `选题明细${topics.length ? `（${topics.length}）` : ''}` },
            ] as { key: DetailTab; label: string }[]).map((t) => {
              const active = tab === t.key;
              return (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setTab(t.key)}
                  className={`px-3.5 py-2 text-sm font-medium whitespace-nowrap transition-all duration-200 border ${
                    active
                      ? 'text-white bg-brand-600 border-brand-700'
                      : 'text-ink-600 hover:text-ink-800 hover:bg-ink-200 border-transparent'
                  }`}
                  style={{ borderRadius: 'var(--radius-sm)' }}
                >
                  {t.label}
                </button>
              );
            })}
          </div>
        </nav>

        <div className="glass-panel !p-4 sm:!p-5">
          {tab === 'shownotes' && (
            <PlainTextBlock text={detail.shownotes_md} empty="本期暂无节目简介" />
          )}
          {tab === 'script' && (
            <PlainTextBlock text={detail.script_md} empty="本期暂无口播脚本" />
          )}
          {tab === 'topics' && <TopicsTable topics={topics} />}
        </div>
      </div>

      {/* ============ 人工审核 ============ */}
      <div className="glass-panel !p-4 sm:!p-5 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-ink-800">人工审核</span>
          <StatusChip status={issue.status} />
          {issue.reviewed_at && (
            <span className="ml-auto text-[11px] text-ink-500 tabular-nums">
              审核于 {fmtShortDateTime(issue.reviewed_at)}
            </span>
          )}
        </div>

        {issue.status === 'approved' && <Notice tone="ok">已通过，可发布。</Notice>}
        {issue.status === 'rejected' && (
          <Notice tone="warn">
            已退回{issue.review_note ? `：${issue.review_note}` : '，需重新生成该期。'}
          </Notice>
        )}
        {issue.status === 'draft' && (
          <div className="text-[11px] text-ink-500">
            试听并确认内容无误后，可「通过」或「退回」。退回后需重新生成该期。
          </div>
        )}
        {issue.status === 'failed' && (
          <Notice tone="error">本期生成失败，建议重新生成后再审核。</Notice>
        )}

        <label className="block">
          <span className="text-[11px] text-ink-500">审核备注（可选）</span>
          <textarea
            className="textarea mt-1.5"
            rows={3}
            maxLength={500}
            value={note}
            onChange={(e) => {
              noteTouchedRef.current = true;
              setNote(e.target.value);
            }}
            placeholder="退回时建议写明原因，便于重新生成时参考"
          />
        </label>

        <div className="flex items-center gap-2">
          <button
            type="button"
            className="btn-primary"
            disabled={reviewing}
            onClick={() => void submitReview('approve')}
          >
            {reviewing ? '提交中…' : '通过'}
          </button>
          <button
            type="button"
            className="btn-ghost"
            disabled={reviewing}
            style={{ color: 'rgb(var(--status-error-fg))' }}
            onClick={() => void submitReview('reject')}
          >
            退回
          </button>
          <span className="ml-auto text-[11px] text-ink-500 tabular-nums">{note.length}/500</span>
        </div>

        {reviewMsg && (
          <div className="text-[11px]" style={{ color: 'rgb(var(--status-success-fg))' }}>
            {reviewMsg}
          </div>
        )}
        {reviewErr && <Notice tone="error">审核提交失败：{reviewErr}</Notice>}
      </div>

      {/* ============ RSS 订阅 ============ */}
      <div className="glass-panel !p-4 sm:!p-5 space-y-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-ink-800">RSS 订阅</span>
          {config?.feed_key_set && (
            <span className="chip-soft !text-[10px]">需访问密钥</span>
          )}
          {config && !config.feed_ready && (
            <span className="chip-soft !text-[10px]">尚未就绪</span>
          )}
        </div>

        {fullFeedUrl ? (
          <>
            <div className="flex items-stretch gap-2">
              <input
                readOnly
                value={fullFeedUrl}
                onFocus={(e) => e.currentTarget.select()}
                className="input flex-1 font-mono !text-xs"
                aria-label="RSS 订阅地址"
              />
              <button type="button" className="btn-ghost shrink-0" onClick={() => void copyFeed()}>
                <CopyGlyph size={14} />
                {copied ? '已复制' : '复制'}
              </button>
            </div>
            {config?.feed_key_set ? (
              <div className="text-[11px] text-ink-500 leading-5">
                该订阅地址需要在末尾追加 <code className="font-mono text-ink-700">?key=</code>
                （具体密钥请在服务端配置中查看，前端不展示）。
              </div>
            ) : config && !config.feed_ready ? (
              <div className="text-[11px] text-ink-500 leading-5">
                服务端尚未配置 RSS 访问密钥，订阅源暂未开放；配置后该地址即可使用。
              </div>
            ) : (
              <div className="text-[11px] text-ink-500 leading-5">
                可把该地址直接添加到播客客户端订阅。
              </div>
            )}
          </>
        ) : (
          <div className="text-[11px] text-ink-500">暂无订阅地址。</div>
        )}

        {configError && <Notice tone="warn">日报配置读取失败：{configError}</Notice>}
        {config && !config.feed_ready && (
          <Notice tone="warn">RSS 订阅源尚未开放，配置访问密钥后此地址才可用。</Notice>
        )}
      </div>
    </section>
  );
}

// =====================================================================
// 详情页子组件
// =====================================================================

function BackLink() {
  return (
    <a
      href="#/daily"
      className="inline-flex items-center gap-1.5 text-xs text-ink-500 hover:text-ink-800 transition-colors"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M19 12H5" />
        <path d="M12 19l-7-7 7-7" />
      </svg>
      返回日报列表
    </a>
  );
}

/** 简介 / 脚本以纯文本呈现（whitespace-pre-wrap 保留换行与 URL 可读性） */
function PlainTextBlock({ text, empty }: { text: string; empty: string }) {
  if (!text || !text.trim()) {
    return <div className="py-12 text-center text-xs text-ink-500">{empty}</div>;
  }
  return (
    <div className="whitespace-pre-wrap break-words text-[13px] leading-7 text-ink-700 max-h-[60vh] overflow-y-auto pr-1">
      {text}
    </div>
  );
}

function TopicsTable({ topics }: { topics: DailyTopic[] }) {
  if (!topics.length) {
    return <div className="py-12 text-center text-xs text-ink-500">本期暂无选题明细</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm min-w-[680px]">
        <thead>
          <tr className="text-left text-[11px] text-ink-500">
            <th className="font-medium px-2 py-2 w-[42%]">标题</th>
            <th className="font-medium px-2 py-2 w-[92px]">来源</th>
            <th className="font-medium px-2 py-2 w-[64px]">得分</th>
            <th className="font-medium px-2 py-2">理由</th>
          </tr>
        </thead>
        <tbody>
          {topics.map((t, i) => (
            <tr key={`${t.url || t.title}-${i}`} className="border-t border-ink-300/60 align-top">
              <td className="px-2 py-2.5">
                {t.url ? (
                  <a
                    href={t.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-brand-300 hover:text-brand-200 hover:underline break-words"
                    title={t.url}
                  >
                    {t.title || t.url}
                  </a>
                ) : (
                  <span className="text-ink-800 break-words">{t.title || '—'}</span>
                )}
                {t.summary && (
                  <div className="mt-0.5 text-[11px] text-ink-500 leading-5 line-clamp-2">{t.summary}</div>
                )}
              </td>
              <td className="px-2 py-2.5">
                <span className="chip-soft !text-[10px]">{t.source || '—'}</span>
              </td>
              <td className="px-2 py-2.5 text-xs text-ink-800 tabular-nums">
                {typeof t.score === 'number' && Number.isFinite(t.score) ? t.score.toFixed(2) : '—'}
              </td>
              <td className="px-2 py-2.5 text-[12px] text-ink-600 leading-5">{reasonsText(t)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatusChip({ status }: { status: DailyIssueStatus | string }) {
  const def = ISSUE_STATUS[status] ?? { label: status || '未知', tone: 'info' as Tone };
  const t = TONES[def.tone];
  return (
    <span
      className="chip"
      style={{ background: t.bg, color: t.fg, border: `1px solid ${t.border}` }}
    >
      <span className="badge-dot" style={{ backgroundColor: t.fg }} />
      {def.label}
    </span>
  );
}

function Notice({
  tone,
  children,
  className = '',
}: {
  tone: Tone;
  children: ReactNode;
  className?: string;
}) {
  const t = TONES[tone];
  return (
    <div
      className={`flex items-start gap-2 px-3 py-2 text-xs leading-5 ${className}`}
      style={{
        borderRadius: 'var(--radius-sm)',
        background: t.bg,
        border: `1px solid ${t.border}`,
        color: t.fg,
      }}
    >
      {children}
    </div>
  );
}

// =====================================================================
// 内联 SVG（线性、无 emoji）
// =====================================================================

function NewsGlyph({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 22h16a2 2 0 002-2V4a2 2 0 00-2-2H8a2 2 0 00-2 2v16a2 2 0 01-2 2zm0 0a2 2 0 01-2-2v-9c0-1.1.9-2 2-2h2" />
      <path d="M18 14h-8" />
      <path d="M15 18h-5" />
      <path d="M10 6h8v4h-8V6z" />
    </svg>
  );
}

function RefreshGlyph({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 12a9 9 0 0115-6.7L21 8" />
      <path d="M21 3v5h-5" />
      <path d="M21 12a9 9 0 01-15 6.7L3 16" />
      <path d="M3 21v-5h5" />
    </svg>
  );
}

function SparkGlyph({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3v3" />
      <path d="M12 18v3" />
      <path d="M3 12h3" />
      <path d="M18 12h3" />
      <path d="M5.6 5.6l2.1 2.1" />
      <path d="M16.3 16.3l2.1 2.1" />
      <path d="M5.6 18.4l2.1-2.1" />
      <path d="M16.3 7.7l2.1-2.1" />
    </svg>
  );
}

function VolumeGlyph({ size = 15 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11 5L6 9H3v6h3l5 4V5z" />
      <path d="M15.54 8.46a5 5 0 010 7.07" />
      <path d="M19.07 4.93a10 10 0 010 14.14" />
    </svg>
  );
}

function CopyGlyph({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" />
      <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
    </svg>
  );
}
