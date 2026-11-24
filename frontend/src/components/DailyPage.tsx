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
import { createPortal } from 'react-dom';
import {
  api,
  fetchMediaObjectUrl,
  DailyConfig,
  DailyIssue,
  DailyIssueDetail,
  DailyIssueStatus,
  DailyRunStatus,
  DailySource,
  DailySourceInput,
  DailyTopic,
  DailyVoiceOption,
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

// ---------- 生成设置：音色分组 / 信源校验 ----------

type VoiceGroup = 'male' | 'female' | 'other';

const VOICE_GROUP_LABELS: Record<VoiceGroup, string> = {
  male: '男声',
  female: '女声',
  other: '其他',
};

function voiceGroup(gender?: string | null): VoiceGroup {
  // 后端音色表 gender 多为 male/female/neutral；兼容中文取值
  const g = String(gender ?? '').trim().toLowerCase();
  if (g === 'male' || g === 'm' || g === '男' || g === '男声') return 'male';
  if (g === 'female' || g === 'f' || g === '女' || g === '女声') return 'female';
  return 'other';
}

function voiceSceneText(v: DailyVoiceOption): string {
  const scenes = v.scene;
  if (Array.isArray(scenes)) return scenes.filter(Boolean).join('/');
  if (typeof scenes === 'string') return scenes;
  return '';
}

/** 下拉选项文案：name · provider · 场景 */
function voiceOptionLabel(v: DailyVoiceOption): string {
  const parts = [v.name || v.id];
  if (v.provider) parts.push(v.provider);
  const scene = voiceSceneText(v);
  if (scene) parts.push(scene);
  return parts.join(' · ');
}

/** 日报音色试听文案：短句即可（TTS 按字符计费，控制在 40 字内）。 */
const VOICE_PREVIEW_TEXT = '欢迎收听 AI 前沿日报，今天我们来聊聊人工智能领域最值得关注的进展。';

/**
 * 可试听的音色列表：每一行都带试听按钮，支持搜索/直接选中。
 *
 * 试听走平台既有的 /api/tts/preview（真实 TTS）→ /media 需要鉴权头 →
 * fetchMediaObjectUrl 转 Blob 再播放；同一时间只播一个，切换/卸载时释放。
 */
function VoiceAuditionList({
  voices,
  value,
  onChange,
  disabled = false,
}: {
  voices: DailyVoiceOption[];
  value: string;
  onChange: (voiceId: string) => void;
  disabled?: boolean;
}) {
  const [query, setQuery] = useState('');
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const objUrlRef = useRef<string | null>(null);

  const releaseUrl = () => {
    if (objUrlRef.current) {
      URL.revokeObjectURL(objUrlRef.current);
      objUrlRef.current = null;
    }
  };
  const stop = () => {
    const a = audioRef.current;
    if (a) {
      a.onended = null;
      a.onerror = null;
      try { a.pause(); } catch {}
    }
    releaseUrl();
    setPlayingId(null);
  };
  useEffect(() => () => {
    const a = audioRef.current;
    if (a) { try { a.pause(); } catch {} }
    releaseUrl();
  }, []);

  const filtered = useMemo(() => {
    const k = query.trim().toLowerCase();
    if (!k) return voices;
    return voices.filter((v) =>
      [v.name, v.id, v.provider, v.gender, voiceSceneText(v)].join(' ').toLowerCase().includes(k),
    );
  }, [voices, query]);

  const audition = async (voice: DailyVoiceOption) => {
    if (playingId === voice.id) {
      stop();
      return;
    }
    stop();
    setError(null);
    setLoadingId(voice.id);
    let url: string | null = null;
    try {
      const r = await api.preview(VOICE_PREVIEW_TEXT, voice.id, 1.0);
      url = await fetchMediaObjectUrl(r.audio_url);
      if (!audioRef.current) {
        URL.revokeObjectURL(url);
        return;
      }
      objUrlRef.current = url;
      audioRef.current.src = url;
      audioRef.current.onended = () => setPlayingId(null);
      audioRef.current.onerror = () => {
        setError(`「${voice.name}」试听播放失败`);
        setPlayingId(null);
      };
      await audioRef.current.play();
      setPlayingId(voice.id);
    } catch (e) {
      if (url) URL.revokeObjectURL(url);
      setError(`「${voice.name}」试听失败：${errorText(e)}`);
    } finally {
      setLoadingId(null);
    }
  };

  return (
    <div className="space-y-2">
      <input
        className="input !text-xs w-full"
        placeholder={`搜索音色（共 ${voices.length} 个，全部可试听）`}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        disabled={disabled}
        aria-label="搜索音色"
      />
      {error && (
        <div className="text-[11px]" style={{ color: 'rgb(var(--status-error-fg))' }}>
          {error}
        </div>
      )}
      <div
        className="border border-ink-300/70 overflow-y-auto"
        style={{ borderRadius: 'var(--radius-sm)', maxHeight: 264 }}
      >
        <button
          type="button"
          onClick={() => onChange('')}
          disabled={disabled}
          className={`w-full text-left px-2.5 py-2 text-[12px] transition-colors ${
            value === '' ? 'text-brand-500 bg-brand-600/10' : 'text-ink-700 hover:bg-ink-200'
          }`}
        >
          使用默认音色（自动挑选「新闻/播报」类）
        </button>
        {filtered.map((v) => {
          const selected = value === v.id;
          const playing = playingId === v.id;
          const loading = loadingId === v.id;
          return (
            <div
              key={v.id}
              className={`flex items-center gap-2 px-2.5 py-1.5 border-t border-ink-300/40 ${
                selected ? 'bg-brand-600/10' : ''
              }`}
            >
              <button
                type="button"
                onClick={() => onChange(v.id)}
                disabled={disabled}
                className="flex-1 min-w-0 text-left text-[12px]"
              >
                <span className={selected ? 'text-brand-500' : 'text-ink-800'}>{v.name}</span>
                <span className="ml-1.5 text-[10px] text-ink-500">
                  {VOICE_GROUP_LABELS[voiceGroup(v.gender)]}
                  {v.provider ? ` · ${v.provider}` : ''}
                </span>
                {voiceSceneText(v) && (
                  <span className="ml-1.5 text-[10px] text-ink-500">· {voiceSceneText(v)}</span>
                )}
              </button>
              <button
                type="button"
                onClick={() => void audition(v)}
                disabled={disabled || loading}
                title={playing ? '停止试听' : '试听'}
                aria-label={playing ? `停止试听 ${v.name}` : `试听 ${v.name}`}
                className="shrink-0 grid place-items-center w-6 h-6 border border-ink-300/70 text-ink-600 hover:text-brand-500 hover:border-brand-500 transition-colors disabled:opacity-40"
                style={{ borderRadius: 'var(--radius-xs)' }}
              >
                {loading ? (
                  <span className="inline-block w-3 h-3 border-2 border-ink-400/70 border-t-brand-500 rounded-full animate-spin" />
                ) : playing ? (
                  <StopGlyph size={10} />
                ) : (
                  <PlayGlyph size={10} />
                )}
              </button>
            </div>
          );
        })}
        {filtered.length === 0 && (
          <div className="px-3 py-4 text-center text-[11px] text-ink-500">没有匹配的音色</div>
        )}
      </div>
      <audio ref={audioRef} className="hidden" preload="auto" />
    </div>
  );
}

/** 添加自定义信源：url 必须是 http(s)，name 缺省用域名兜底 */
function parseSourceUrl(raw: string): { url: string; name: string } | { error: string } {
  const url = raw.trim();
  if (!url) return { error: '请填写信源地址' };
  if (!/^https?:\/\//i.test(url)) return { error: '信源地址必须以 http:// 或 https:// 开头' };
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return { error: '信源地址格式不正确' };
  }
  if (!parsed.hostname) return { error: '信源地址缺少域名' };
  return { url, name: parsed.hostname.replace(/^www\./i, '') };
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

  // 生成设置（音色 / 信源）
  const [settingsOpen, setSettingsOpen] = useState(false);

  // 熔断恢复
  const [circuitBusy, setCircuitBusy] = useState(false);
  const [circuitError, setCircuitError] = useState<string | null>(null);
  const [circuitNotice, setCircuitNotice] = useState<string | null>(null);

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
      // 409 有两种：已有任务在跑 / 连续失败已熔断 —— 按 detail 文案区分提示
      if (/熔断|circuit/i.test(msg)) {
        setRunError('生成已熔断，请先点「重置熔断」再试');
      } else if (/HTTP\s*409|运行中|生成中|running/i.test(msg)) {
        setRunError('正在生成中，请稍候再试');
      } else {
        setRunError(msg);
      }
    } finally {
      setRunBusy(false);
    }
  };

  // 重置熔断：确认信源/模型/音色后再恢复生成
  const resetCircuit = async () => {
    setCircuitBusy(true);
    setCircuitError(null);
    setCircuitNotice(null);
    try {
      const r = await api.dailyResetCircuit();
      setRun(r.run);
      prevRunStatusRef.current = r.run.status;
      runningRef.current = r.run.status === 'running';
      setStatusError(null);
      setCircuitNotice('熔断已重置，可重新触发生成');
      void refreshList(true);
      void loadConfig(); // config.circuit 同步刷新，避免兜底值让熔断条残留
    } catch (e) {
      setCircuitError(errorText(e));
    } finally {
      setCircuitBusy(false);
    }
  };

  const isRunning = run?.status === 'running';
  const enabledSources = config ? config.sources.filter((s) => s.enabled).length : 0;
  // 熔断：run/status 为主，config.circuit 兜底（状态接口失败时仍能提示）
  const circuitOpen = !!run?.circuit_open || !!config?.circuit?.open;
  const circuitFailures = run?.consecutive_failures ?? config?.circuit?.failures ?? 0;

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
              {config && (
                <span className="chip-soft">音色 {config.voice_name || '默认'}</span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              className="btn-ghost"
              onClick={() => setSettingsOpen(true)}
              title="选择播报音色、管理信源"
            >
              <SettingsGlyph size={14} />
              生成设置
            </button>
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

      {/* ============ 熔断条（连续失败自动停更） ============ */}
      {circuitOpen && (
        <div
          className="flex flex-wrap items-start gap-3 px-4 py-3"
          style={{
            borderRadius: 'var(--radius-sm)',
            background: 'rgba(var(--status-error-bg), 0.12)',
            border: '1px solid rgba(var(--status-error-bg), 0.42)',
          }}
        >
          <span className="mt-[7px] badge-dot shrink-0" style={{ backgroundColor: 'rgb(var(--status-error-fg))' }} />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium" style={{ color: 'rgb(var(--status-error-fg))' }}>
              生成熔断已触发
            </div>
            <div className="text-[11px] text-ink-500 mt-0.5 leading-5">
              连续 {circuitFailures} 期生成失败，已自动停更以避免持续消耗。
              请检查信源 / 模型 / 音色后重置熔断再试。
            </div>
          </div>
          <button
            type="button"
            className="btn-ghost shrink-0"
            disabled={circuitBusy}
            onClick={() => void resetCircuit()}
            style={{ color: 'rgb(var(--status-error-fg))' }}
          >
            {circuitBusy ? '重置中…' : '重置熔断'}
          </button>
        </div>
      )}
      {circuitError && <Notice tone="error">重置熔断失败：{circuitError}</Notice>}
      {circuitNotice && <Notice tone="ok">{circuitNotice}</Notice>}

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

      {/* ============ 生成设置 Modal ============ */}
      {settingsOpen && (
        <DailySettingsModal
          initialConfig={config}
          onClose={() => setSettingsOpen(false)}
          onSaved={(c) => {
            setConfig(c);
            setConfigError(null);
            setSettingsOpen(false);
          }}
        />
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
        {typeof run.consecutive_failures === 'number' && run.consecutive_failures > 0 && (
          <span style={run.consecutive_failures >= 2 ? { color: 'rgb(var(--status-error-fg))' } : undefined}>
            连续失败 <span className="tabular-nums">{run.consecutive_failures}</span> 次
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
// 生成设置 Modal（播报音色 / 信源）
// =====================================================================

type EditableSource = { name: string; url: string; enabled: boolean; custom: boolean };

function toEditableSources(list: DailySource[] | undefined): EditableSource[] {
  return (list ?? []).map((s) => ({
    name: s.name || s.url,
    url: s.url,
    enabled: !!s.enabled,
    custom: !!s.custom,
  }));
}

function DailySettingsModal({
  initialConfig,
  onClose,
  onSaved,
}: {
  initialConfig: DailyConfig | null;
  onClose: () => void;
  onSaved: (config: DailyConfig) => void;
}) {
  const [config, setConfig] = useState<DailyConfig | null>(initialConfig);
  const [loading, setLoading] = useState(!initialConfig);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [voiceId, setVoiceId] = useState(initialConfig?.voice_id ?? '');
  // 只有用户动过音色下拉才提交 voice_id：否则「只改信源」会把自动挑选的音色钉成偏好
  const [voiceTouched, setVoiceTouched] = useState(false);
  const [sources, setSources] = useState<EditableSource[]>(
    toEditableSources(initialConfig?.sources),
  );

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [newUrl, setNewUrl] = useState('');
  const [newName, setNewName] = useState('');
  const [addError, setAddError] = useState<string | null>(null);

  const applyConfig = useCallback((c: DailyConfig) => {
    setConfig(c);
    setVoiceId(c.voice_id ?? '');
    setVoiceTouched(false);
    setSources(toEditableSources(c.sources));
  }, []);

  // 打开时重新拉一次 config：拿最新音色表与信源（父级 config 可能已过期）
  const reload = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const c = await api.dailyConfig();
      applyConfig(c);
    } catch (e) {
      setLoadError(errorText(e));
    } finally {
      setLoading(false);
    }
  }, [applyConfig]);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Esc 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !saving) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, saving]);

  // 打开期间锁住背景滚动
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  const voices = config?.voices ?? [];

  const enabledCount = sources.filter((s) => s.enabled).length;
  const canSave = !!config && !loading && !saving && enabledCount >= 1;

  const toggleSource = (url: string) => {
    setSources((prev) => prev.map((s) => (s.url === url ? { ...s, enabled: !s.enabled } : s)));
  };

  const removeSource = (url: string) => {
    setSources((prev) => prev.filter((s) => s.url !== url));
    setAddError(null);
  };

  const addSource = () => {
    setAddError(null);
    const parsed = parseSourceUrl(newUrl);
    if ('error' in parsed) {
      setAddError(parsed.error);
      return;
    }
    if (sources.some((s) => s.url === parsed.url)) {
      setAddError('该信源已在列表中');
      return;
    }
    if (sources.length >= 20) {
      setAddError('信源最多 20 个');
      return;
    }
    const name = (newName.trim() || parsed.name).slice(0, 40);
    setSources((prev) => [...prev, { name, url: parsed.url, enabled: true, custom: true }]);
    setNewUrl('');
    setNewName('');
  };

  const save = async () => {
    if (!canSave) return;
    setSaving(true);
    setSaveError(null);
    try {
      const patch: { voice_id?: string | null; sources?: DailySourceInput[] } = {
        sources: sources.map((s) => ({
          name: s.name || s.url,
          url: s.url,
          enabled: s.enabled,
        })),
      };
      if (voiceTouched) {
        // 空值 = 交回服务端自动挑选
        patch.voice_id = voiceId || null;
      }
      const updated = await api.dailyConfigUpdate(patch);
      onSaved(updated);
    } catch (e) {
      // 400 时 _fetch 已把后端 detail 拼进 message
      setSaveError(errorText(e));
    } finally {
      setSaving(false);
    }
  };

  if (typeof document === 'undefined') return null;

  // 用 portal 挂到 body：外层 <main> 的 animate-fade-in 带 transform，
  // 会让 position: fixed 相对 main 定位，弹窗顶部会被顶出可视区。
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={() => {
        if (!saving) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="生成设置"
        className="bg-ink-50 border border-ink-300/70 w-full max-w-[600px] max-h-[88vh] flex flex-col"
        style={{
          borderRadius: 'var(--radius-lg)',
          boxShadow: '0 24px 64px -20px rgba(0, 0, 0, 0.8)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-center gap-2 px-5 pt-4 pb-3 border-b border-ink-300/60 shrink-0">
          <span className="text-ink-500">
            <SettingsGlyph size={16} />
          </span>
          <h3 className="text-base font-semibold text-ink-900">生成设置</h3>
          <button
            type="button"
            className="ml-auto text-ink-500 hover:text-ink-800 transition-colors disabled:opacity-40"
            onClick={onClose}
            disabled={saving}
            aria-label="关闭"
          >
            <CloseGlyph size={16} />
          </button>
        </div>

        {/* 内容 */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
          {loading && !config && (
            <div className="flex items-center justify-center gap-2 py-10 text-xs text-ink-500">
              <span className="inline-block w-3.5 h-3.5 border-2 border-ink-400/70 border-t-brand-500 rounded-full animate-spin" />
              配置加载中…
            </div>
          )}

          {!loading && !config && (
            <Notice tone="error">
              <span className="flex-1">配置加载失败：{loadError || '未知错误'}</span>
              <button
                type="button"
                className="btn-ghost !py-1 !px-3 text-xs self-center"
                onClick={() => void reload()}
              >
                重试
              </button>
            </Notice>
          )}

          {config && (
            <>
              {/* 播报音色 */}
              <section>
                <div className="flex flex-wrap items-center gap-2 mb-2">
                  <h4 className="text-sm font-medium text-ink-800">播报音色</h4>
                  <span className="text-[11px] text-ink-500">
                    当前：{config.voice_name || '默认音色'}
                  </span>
                  {loading && <span className="text-[11px] text-ink-500">（刷新中…）</span>}
                </div>

                {voices.length === 0 ? (
                  <div
                    className="px-3 py-2.5 text-[11px] text-ink-500 border border-dashed border-ink-300"
                    style={{ borderRadius: 'var(--radius-sm)' }}
                  >
                    当前 TTS 厂商没有可用音色，保存将只更新信源。
                  </div>
                ) : (
                  <VoiceAuditionList
                    voices={voices}
                    value={voiceId}
                    onChange={(id) => {
                      setVoiceId(id);
                      setVoiceTouched(true);
                    }}
                    disabled={saving}
                  />
                )}
              </section>

              {/* 信源管理 */}
              <section>
                <div className="flex flex-wrap items-center gap-2 mb-2">
                  <h4 className="text-sm font-medium text-ink-800">信源</h4>
                  <span className="text-[11px] text-ink-500">
                    已启用 {enabledCount}/{sources.length}
                  </span>
                </div>

                <div
                  className="border border-ink-300/70 overflow-hidden"
                  style={{ borderRadius: 'var(--radius-sm)' }}
                >
                  {sources.length === 0 && (
                    <div className="px-3 py-4 text-center text-[11px] text-ink-500">
                      暂无信源，请在下方添加
                    </div>
                  )}
                  {sources.map((src) => (
                    <div
                      key={src.url}
                      className="flex items-center gap-2.5 px-3 py-2 border-t border-ink-300/60 first:border-t-0"
                    >
                      <label className="flex items-center gap-2.5 min-w-0 flex-1 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={src.enabled}
                          disabled={saving}
                          onChange={() => toggleSource(src.url)}
                          className="shrink-0 accent-brand-500"
                        />
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center gap-1.5">
                            <span
                              className={`text-[13px] truncate ${
                                src.enabled ? 'text-ink-800' : 'text-ink-500 line-through'
                              }`}
                            >
                              {src.name}
                            </span>
                            {src.custom && <span className="chip-soft !text-[10px] shrink-0">自定义</span>}
                          </span>
                          <span className="block text-[11px] text-ink-500 truncate font-mono" title={src.url}>
                            {src.url}
                          </span>
                        </span>
                      </label>
                      {src.custom && (
                        <button
                          type="button"
                          className="shrink-0 text-ink-500 hover:text-red-300 transition-colors disabled:opacity-40"
                          disabled={saving}
                          onClick={() => removeSource(src.url)}
                          aria-label={`删除信源 ${src.name}`}
                        >
                          <TrashGlyph size={14} />
                        </button>
                      )}
                    </div>
                  ))}
                </div>

                {/* 添加自定义信源 */}
                <div className="mt-2.5 flex flex-wrap items-start gap-2">
                  <input
                    className="input flex-1 min-w-[180px] !text-xs"
                    placeholder="信源 RSS 地址（http(s)://）"
                    value={newUrl}
                    disabled={saving}
                    onChange={(e) => {
                      setNewUrl(e.target.value);
                      setAddError(null);
                    }}
                  />
                  <input
                    className="input w-[150px] !text-xs"
                    placeholder="名称（可选）"
                    value={newName}
                    maxLength={40}
                    disabled={saving}
                    onChange={(e) => setNewName(e.target.value)}
                  />
                  <button type="button" className="btn-ghost shrink-0" disabled={saving} onClick={addSource}>
                    <PlusGlyph size={14} />
                    添加
                  </button>
                </div>
                {addError && (
                  <div className="mt-1.5 text-[11px]" style={{ color: 'rgb(var(--status-error-fg))' }}>
                    {addError}
                  </div>
                )}
              </section>
            </>
          )}

          {saveError && <Notice tone="error">保存失败：{saveError}</Notice>}
          {loadError && config && (
            <Notice tone="warn">配置刷新失败：{loadError}（可继续使用当前值保存）</Notice>
          )}
        </div>

        {/* 底部 */}
        <div className="flex items-center gap-2 px-5 py-3.5 border-t border-ink-300/60 shrink-0">
          {enabledCount < 1 && <span className="text-[11px] text-ink-500">至少启用 1 个信源</span>}
          <div className="ml-auto flex items-center gap-2">
            <button type="button" className="btn-ghost" onClick={onClose} disabled={saving}>
              取消
            </button>
            <button type="button" className="btn-primary" onClick={() => void save()} disabled={!canSave}>
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
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
  const [audioError, setAudioError] = useState<string | null>(null);
  const [audioReloadKey, setAudioReloadKey] = useState(0);
  // 音色选择（详情页直接可改，对下一期生效）
  const [voiceDraft, setVoiceDraft] = useState('');
  const [voiceSaving, setVoiceSaving] = useState(false);
  const [voiceMsg, setVoiceMsg] = useState<string | null>(null);
  const [regenBusy, setRegenBusy] = useState(false);

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
      setVoiceDraft((prev) => (prev === '' ? (c.voice_id ?? '') : prev));
      setConfigError(null);
    } catch (e) {
      setConfigError(errorText(e));
    }
  }, []);

  useEffect(() => {
    void loadDetail();
    void loadConfig();
  }, [loadDetail, loadConfig]);

  // 音频是「生成」阶段一次性合成好的成品（audio.mp3），这里只是播放：
  // 直链 + Range 流式，点开即播；不需要先下载成 Blob（9MB 会显得像"正在生成"）。
  const hasAudio = !!detail?.issue.has_audio;
  const audioSrc = useMemo(() => {
    if (!hasAudio) return '';
    const base = api.dailyAudioUrl(date);
    return `${base}${base.includes('?') ? '&' : '?'}v=${audioReloadKey}`;
  }, [date, hasAudio, audioReloadKey]);

  // 复制成功提示的定时器清理
  useEffect(() => () => {
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
  }, []);

  const issue = detail?.issue ?? null;
  const topics: DailyTopic[] = detail?.meta?.topics ?? [];

  // feed_url 可能是相对路径（同源）→ 拼成完整地址方便直接贴到播客客户端
  const fullFeedUrl = useMemo(() => {
    const raw = detail?.feed_url || config?.feed_url || '';
    if (!raw) return '';
    if (/^https?:\/\//i.test(raw)) return raw;
    if (typeof window !== 'undefined' && raw.startsWith('/')) return `${window.location.origin}${raw}`;
    return raw;
  }, [detail?.feed_url, config?.feed_url]);

  const saveVoice = async () => {
    setVoiceSaving(true);
    setVoiceMsg(null);
    try {
      const c = await api.dailyConfigUpdate({ voice_id: voiceDraft || '' });
      setConfig(c);
      setVoiceMsg('音色已保存，对下一期生效；本期音频不会重新合成。');
    } catch (e) {
      setVoiceMsg(`保存失败：${errorText(e)}`);
    } finally {
      setVoiceSaving(false);
    }
  };

  const regenerate = async () => {
    if (!window.confirm('重新生成本期会覆盖当前音频与文稿，耗时约 2 分钟并消耗 TTS 额度，确定继续？')) {
      return;
    }
    setRegenBusy(true);
    setVoiceMsg(null);
    try {
      await api.dailyRun(date);
      setVoiceMsg('已开始重新生成，可回到列表页查看进度；完成后刷新本页即可试听新版本。');
    } catch (e) {
      setVoiceMsg(`重新生成失败：${errorText(e)}`);
    } finally {
      setRegenBusy(false);
    }
  };

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
              {(detail.meta?.voice?.name || config?.voice_name) && (
                <span>
                  音色{' '}
                  <span className="text-ink-700">
                    {detail.meta?.voice?.name || config?.voice_name}
                  </span>
                </span>
              )}
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

        {audioError && (
          <Notice tone="error">
            <span className="flex-1">音频加载失败：{audioError}</span>
            <button
              type="button"
              className="btn-ghost !py-1 !px-3 text-xs self-center"
              onClick={() => {
                setAudioError(null);
                setAudioReloadKey((k) => k + 1);
              }}
            >
              重试
            </button>
          </Notice>
        )}

        {hasAudio && (
          // eslint-disable-next-line jsx-a11y/media-has-caption
          <audio
            key={audioReloadKey}
            controls
            preload="metadata"
            src={audioSrc}
            className="w-full"
            onError={() => setAudioError('播放失败（可点重试）')}
            onLoadedMetadata={() => setAudioError(null)}
          />
        )}

        {!hasAudio && (
          <div
            className="py-7 grid place-items-center text-xs text-ink-500 border border-dashed border-ink-300"
            style={{ borderRadius: 'var(--radius-sm)' }}
          >
            本期无音频
          </div>
        )}

        <div className="mt-2 text-[11px] text-ink-500">
          音频在「生成」时已一次性合成并保存，试听不会触发 TTS 合成。
        </div>
      </div>

      {/* ============ 播报音色（对下一期生效） ============ */}
      <div className="glass-panel !p-4 sm:!p-5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-ink-800">播报音色</span>
          <span className="text-[11px] text-ink-500">
            本期：{detail.meta?.voice?.name || '未知'}
            {config?.voice_name ? ` · 默认：${config.voice_name}` : ''}
          </span>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <div className="flex-1 min-w-[280px]">
            <VoiceAuditionList
              voices={config?.voices ?? []}
              value={voiceDraft}
              onChange={(id) => {
                setVoiceDraft(id);
                setVoiceMsg(null);
              }}
              disabled={voiceSaving || regenBusy || !config}
            />
          </div>
          <button
            type="button"
            className="btn-primary !py-1.5 !px-3 text-xs"
            onClick={() => void saveVoice()}
            disabled={voiceSaving || !config}
          >
            {voiceSaving ? '保存中…' : '保存音色'}
          </button>
          <button
            type="button"
            className="btn-ghost !py-1.5 !px-3 text-xs"
            onClick={() => void regenerate()}
            disabled={regenBusy}
          >
            {regenBusy ? '提交中…' : '用当前设置重生成本期'}
          </button>
        </div>
        {configError && (
          <div className="mt-2 text-[11px] text-ink-500">音色列表加载失败：{configError}（仍可用默认音色）</div>
        )}
        {voiceMsg && <div className="mt-2 text-[12px] text-ink-600">{voiceMsg}</div>}
        <div className="mt-2 text-[11px] text-ink-500">
          音色改动对<span className="text-ink-700">下一期</span>生效；想立刻听到新音色，点「用当前设置重生成本期」。
        </div>
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
            <MarkdownLite text={detail.shownotes_md} empty="本期暂无节目简介" />
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

/** 极简 markdown 渲染（标题/列表/引用/链接），不引入 markdown 依赖。 */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const re = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let idx = 0;
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    nodes.push(
      <a
        key={`${keyPrefix}-l${idx++}`}
        href={match[2]}
        target="_blank"
        rel="noreferrer"
        className="text-brand-500 hover:underline break-all"
      >
        {match[1]}
      </a>,
    );
    last = match.index + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function MarkdownLite({ text, empty }: { text: string; empty: string }) {
  if (!text || !text.trim()) {
    return <div className="text-sm text-ink-500">{empty}</div>;
  }
  const lines = text.split(/\r?\n/);
  return (
    <div className="text-[13px] leading-7 text-ink-700 max-h-[60vh] overflow-y-auto pr-1 space-y-1">
      {lines.map((raw, i) => {
        const line = raw.trimEnd();
        if (!line.trim()) return <div key={i} className="h-2" />;
        if (line.startsWith('### ')) {
          return (
            <div key={i} className="text-[13px] font-semibold text-ink-800 pt-1">
              {renderInline(line.slice(4), `h3-${i}`)}
            </div>
          );
        }
        if (line.startsWith('## ')) {
          return (
            <div key={i} className="text-sm font-semibold text-ink-900 pt-2">
              {renderInline(line.slice(3), `h2-${i}`)}
            </div>
          );
        }
        if (line.startsWith('# ')) {
          return (
            <div key={i} className="text-base font-semibold text-ink-900">
              {renderInline(line.slice(2), `h1-${i}`)}
            </div>
          );
        }
        if (line.startsWith('> ')) {
          return (
            <div key={i} className="border-l-2 border-ink-300 pl-2 text-[12px] text-ink-500">
              {renderInline(line.slice(2), `q-${i}`)}
            </div>
          );
        }
        if (/^[-*]\s+/.test(line)) {
          return (
            <div key={i} className="flex gap-2">
              <span className="text-ink-400 shrink-0">·</span>
              <span>{renderInline(line.replace(/^[-*]\s+/, ''), `b-${i}`)}</span>
            </div>
          );
        }
        return <div key={i}>{renderInline(line, `p-${i}`)}</div>;
      })}
    </div>
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

function PlayGlyph({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

function StopGlyph({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="1.5" />
    </svg>
  );
}

function SettingsGlyph({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 01-4 0v-.1A1.7 1.7 0 009 19.4a1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 010-4h.1A1.7 1.7 0 004.6 9a1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 014 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 010 4h-.1a1.7 1.7 0 00-1.5 1z" />
    </svg>
  );
}

function CloseGlyph({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 6L6 18" />
      <path d="M6 6l12 12" />
    </svg>
  );
}

function TrashGlyph({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 6h18" />
      <path d="M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2" />
      <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
    </svg>
  );
}

function PlusGlyph({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 5v14" />
      <path d="M5 12h14" />
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
