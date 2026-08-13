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
import { StatusBadge } from './ProjectListPage';

type Tab = 'overview' | 'chapters' | 'voices' | 'builds' | 'settings';

const TAB_LABELS: { key: Tab; label: string; icon: string }[] = [
  { key: 'overview', label: 'Overview', icon: '📋' },
  { key: 'chapters', label: 'Chapters', icon: '📜' },
  { key: 'voices', label: 'Voices', icon: '🎙' },
  { key: 'builds', label: 'Builds', icon: '🏗' },
  { key: 'settings', label: 'Settings', icon: '⚙️' },
];

// 格式化文件大小
function formatSize(bytes: number | null | undefined): string {
  if (bytes == null) return '—';
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

// 格式化时长（秒）
function formatDuration(sec: number | null | undefined): string {
  if (sec == null) return '—';
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}分${s}秒`;
}

// 格式化毫秒时长
function formatMs(ms: number | null | undefined): string {
  if (ms == null) return '';
  if (ms >= 60000) return `${(ms / 60000).toFixed(1)}m`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// 相对时间
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

  // 试听播放管理（统一一个隐藏 audio 元素）
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playingKey, setPlayingKey] = useState<string | null>(null);
  const [loadingVoice, setLoadingVoice] = useState<string | null>(null);

  const narratorDefault = useMemo(
    () => voices.find(v => v.id === 'male-qn-jingying') || voices[0],
    [voices]
  );

  // 拉取项目详情 + builds 列表
  const reload = async () => {
    setErr(null);
    try {
      const [detail, bl] = await Promise.all([
        api.projectGet(projectId),
        api.buildList(projectId),
      ]);
      setProject(detail);
      // 时间倒序
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

  // ---------- 试听播放管理 ----------
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

  // 音色试听：调 preview 合成短样例
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
      <div className="card text-center py-12 text-white/60">
        <div className="inline-block w-8 h-8 border-4 border-brand-500 border-t-transparent rounded-full animate-spin mb-3" />
        <div>加载项目详情…</div>
      </div>
    );
  }

  return (
    <section className="space-y-6">
      <audio ref={audioRef} className="hidden" />

      {err && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          {err}
          <button className="btn-ghost ml-3 py-0.5 px-2 text-xs" onClick={reload}>
            重试
          </button>
        </div>
      )}

      {/* ============ Header ============ */}
      <div className="card relative overflow-hidden">
        <div
          className="absolute left-0 top-0 bottom-0 w-2"
          style={{ backgroundColor: project!.cover_color || '#a855f7' }}
        />
        <div className="pl-3 flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3 min-w-0 flex-1">
            <button className="btn-ghost shrink-0" onClick={goBack} title="返回项目列表">
              ←
            </button>
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <h2 className="text-lg font-semibold truncate">
                  {project!.book_title || project!.name}
                </h2>
                <StatusBadge status={project!.status} />
              </div>
              <div className="text-xs text-white/50 mt-0.5">
                {project!.name}
                {project!.source_filename && ` · ${project!.source_filename}`}
                {project!.chapter_count > 0 && ` · ${project!.chapter_count} 章`}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* ============ Tab 切换条 ============ */}
      <div className="card !p-2">
        <div className="flex items-center gap-1 overflow-x-auto">
          {TAB_LABELS.map(t => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`px-4 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition ${
                tab === t.key
                  ? 'bg-brand-600 text-white'
                  : 'text-white/60 hover:bg-white/5 hover:text-white'
              }`}
            >
              {t.icon} {t.label}
            </button>
          ))}
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
  // 如果项目还没 prepare（status 为 draft 或 imported），显示引导
  const needsPrepare =
    project.status === 'draft' || (project.status === 'imported' && project.chapter_count === 0);

  const handlePrepare = async () => {
    try {
      await api.projectPrepare(project.project_id);
      await onReload();
    } catch (e: any) {
      alert(`prepare 失败: ${e?.message || e}`);
    }
  };

  return (
    <div className="space-y-4">
      {/* 引导：还没 prepare */}
      {needsPrepare && (
        <div className="card border-brand-500/40 bg-brand-500/5">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div>
              <div className="font-semibold mb-1">🚀 还没识别章节与角色</div>
              <div className="text-sm text-white/60">
                {project.source_filename
                  ? `已上传文件「${project.source_filename}」，点击右侧按钮开始识别章节、角色与对白归属。`
                  : '请先到 Voices / Settings 上传源文件，然后开始识别。'}
              </div>
            </div>
            {project.source_filename && (
              <button className="btn-primary" onClick={handlePrepare}>
                🚀 开始识别
              </button>
            )}
          </div>
        </div>
      )}

      {/* 概览卡片 */}
      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <OverviewStat label="章节数" value={String(project.chapter_count)} icon="📜" />
        <OverviewStat label="角色数" value={String(project.characters.length)} icon="🧑" />
        <OverviewStat
          label="文件大小"
          value={formatSize(project.source_file_size)}
          icon="📄"
        />
        <OverviewStat
          label="创建时间"
          value={new Date(project.created_at).toLocaleDateString('zh-CN')}
          icon="🗓"
        />
      </div>

      {/* 描述与标签 */}
      {(project.description || (project.tags && project.tags.length > 0)) && (
        <div className="card space-y-3">
          {project.description && (
            <div>
              <div className="text-xs text-white/40 mb-1">描述</div>
              <div className="text-sm">{project.description}</div>
            </div>
          )}
          {project.tags && project.tags.length > 0 && (
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs text-white/40">标签</span>
              {project.tags.map((t, i) => (
                <span key={i} className="chip bg-white/10">#{t}</span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 最近 build 摘要 */}
      <div className="card space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">最近一次构建</h3>
          <button className="btn-ghost text-xs py-1" onClick={() => onTab('builds')}>
            查看全部 →
          </button>
        </div>
        {project.last_build ? (
          <LastBuildSummary
            build={project.last_build}
            onGotoBuilds={() => onTab('builds')}
          />
        ) : (
          <div className="text-sm text-white/50 py-3">
            还没有构建记录，到 Builds tab 启动第一次生成。
          </div>
        )}
      </div>

      {/* 快捷入口 */}
      <div className="grid sm:grid-cols-3 gap-3">
        <button
          className="card text-left hover:border-brand-500/40 transition cursor-pointer"
          onClick={() => onTab('chapters')}
        >
          <div className="text-sm font-semibold">📜 查看章节</div>
          <div className="text-xs text-white/50 mt-1">
            共 {project.chapter_count} 章 · 试听下载
          </div>
        </button>
        <button
          className="card text-left hover:border-brand-500/40 transition cursor-pointer"
          onClick={() => onTab('voices')}
        >
          <div className="text-sm font-semibold">🎙 配置音色</div>
          <div className="text-xs text-white/50 mt-1">
            {project.characters.length} 个角色 · 旁白与语速
          </div>
        </button>
        <button
          className="card text-left hover:border-brand-500/40 transition cursor-pointer"
          onClick={() => onTab('settings')}
        >
          <div className="text-sm font-semibold">⚙️ 项目设置</div>
          <div className="text-xs text-white/50 mt-1">
            名称 · 描述 · 标签 · 默认配置
          </div>
        </button>
      </div>
    </div>
  );
}

function OverviewStat({
  label,
  value,
  icon,
}: {
  label: string;
  value: string;
  icon: string;
}) {
  return (
    <div className="card">
      <div className="flex items-center justify-between">
        <span className="text-xs text-white/50">{label}</span>
        <span className="text-lg opacity-70">{icon}</span>
      </div>
      <div className="text-2xl font-bold mt-2 truncate">{value}</div>
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
    <div className="space-y-2">
      <div className="flex items-center gap-3 flex-wrap">
        <StatusBadge status={build.status} />
        <span className="text-sm text-white/60">
          {build.completed_chapters} / {build.total_chapters} 章 · {pct}%
        </span>
        <span className="text-xs text-white/40 ml-auto">
          {relativeTime(build.created_at)}
        </span>
      </div>
      <div className="w-full h-1.5 bg-white/10 rounded-full overflow-hidden">
        <div
          className="h-full bg-gradient-to-r from-brand-500 to-blue-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      <button className="btn-ghost text-xs py-1 mt-1" onClick={onGotoBuilds}>
        查看 Builds →
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
  // 如果有 done 的 last build，显示该 build 的章节音频
  const lastBuild = project.last_build;
  const hasAudio = lastBuild && lastBuild.status === 'done';

  if (chapters.length === 0) {
    return (
      <div className="card text-center py-12 text-white/60">
        <div className="text-4xl mb-3">📭</div>
        <div>还没有章节，请先到 Overview 触发识别</div>
      </div>
    );
  }

  return (
    <div className="card space-y-2">
      <div className="flex items-center justify-between mb-2">
        <h3 className="font-semibold">章节列表（{chapters.length}）</h3>
        {hasAudio && (
          <span className="text-xs text-white/50">
            ▶ 试听来自最近一次完成的 build
          </span>
        )}
      </div>
      <div className="space-y-1 max-h-[640px] overflow-y-auto pr-1">
        {chapters.map(c => {
          const key = `ch_${c.idx}`;
          const playing = playingKey === key;
          return (
            <div
              key={c.idx}
              className={`flex items-center gap-3 py-2 px-3 rounded-lg ${
                playing ? 'bg-brand-500/10 ring-1 ring-brand-500/40' : 'hover:bg-white/5'
              }`}
            >
              <span className="text-white/40 text-xs w-10 shrink-0 font-mono">
                #{c.idx + 1}
              </span>
              <div className="flex-1 min-w-0">
                <div className="text-sm truncate">{c.title || '(无标题)'}</div>
              </div>
              <span className="text-xs text-white/40 shrink-0">{c.text_len} 字</span>
              {hasAudio && (
                <div className="flex items-center gap-2 shrink-0">
                  <a
                    className="chip bg-white/10 hover:bg-white/20 text-xs"
                    href={api.buildChapterDownload(
                      project.project_id,
                      lastBuild!.build_id,
                      c.idx
                    )}
                    download
                    title="下载该章 MP3"
                  >
                    ⬇
                  </a>
                </div>
              )}
            </div>
          );
        })}
      </div>
      {!hasAudio && (
        <div className="text-xs text-white/40 pt-3 border-t border-white/5">
          完成一次 build 后，此页面将显示每章的试听与下载按钮。
        </div>
      )}
    </div>
  );
}

// =================== Voices Tab ===================
function VoicesTab({
  project,
  voices,
  narratorDefault,
  playingKey,
  loadingVoice,
  onPreviewVoice,
  onReload,
}: {
  project: ProjectDetailResp;
  voices: Voice[];
  narratorDefault: Voice | undefined;
  playingKey: string | null;
  loadingVoice: string | null;
  onPreviewVoice: (voiceId: string, text: string, speed: number) => void;
  onReload: () => void;
}) {
  // 旁白音色 + 语速（项目默认配置）
  const [narratorVoice, setNarratorVoice] = useState<string>(
    project.default_narrator_voice_id || narratorDefault?.id || ''
  );
  const [speed, setSpeed] = useState<number>(project.default_speed ?? 1.0);
  const [savingDefault, setSavingDefault] = useState(false);
  const [savedTip, setSavedTip] = useState(false);

  // 角色音色本地缓存，避免每次 PATCH 都重新拉详情
  const [chars, setChars] = useState<CharacterWithVoice[]>(project.characters);

  // 项目切换时同步本地 state
  useEffect(() => {
    setNarratorVoice(project.default_narrator_voice_id || narratorDefault?.id || '');
    setSpeed(project.default_speed ?? 1.0);
    setChars(project.characters);
  }, [project.project_id, project.default_narrator_voice_id, project.default_speed, project.characters, narratorDefault]);

  // 保存项目默认配置（旁白 + 语速）
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

  // 修改角色音色并立即 PATCH
  const onChangeCharVoice = async (charId: number, voiceId: string) => {
    // 乐观更新
    setChars(prev =>
      prev.map(c => (c.id === charId ? { ...c, assigned_voice_id: voiceId } : c))
    );
    try {
      await api.projectUpdateCharVoice(project.project_id, charId, voiceId);
    } catch (e: any) {
      alert(`保存角色音色失败: ${e?.message || e}`);
      // 失败回滚
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
      {/* 旁白 + 语速 */}
      <div className="card space-y-4">
        <h3 className="font-semibold">🎙 旁白音色 & 语速（项目默认）</h3>
        <div className="space-y-2">
          <div className="text-sm text-white/70">旁白音色</div>
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

        <div className="flex items-center gap-3 flex-wrap">
          <span className="text-sm text-white/70 w-12">语速</span>
          <input
            type="range"
            min={0.5}
            max={2.0}
            step={0.1}
            value={speed}
            onChange={e => setSpeed(parseFloat(e.target.value))}
            className="flex-1 min-w-[200px] accent-brand-500 cursor-pointer"
          />
          <span className="text-sm font-mono w-12 text-right">{speed.toFixed(1)}x</span>
        </div>

        <div className="flex items-center gap-3">
          <button
            className="btn-primary"
            disabled={savingDefault}
            onClick={saveDefaults}
          >
            {savingDefault ? '保存中…' : '💾 保存为项目默认'}
          </button>
          {savedTip && <span className="text-xs text-green-300">✓ 已保存</span>}
        </div>
      </div>

      {/* 角色列表 */}
      <div className="card space-y-3">
        <h3 className="font-semibold">🧑‍🤝‍🧑 角色音色（{chars.length}）</h3>
        {chars.length === 0 ? (
          <div className="text-sm text-white/50 py-4">
            还没有识别到角色，请先到 Overview 触发识别。
          </div>
        ) : (
          <div className="grid md:grid-cols-2 gap-3">
            {chars.map(c => {
              const vid = c.assigned_voice_id || '';
              return (
                <div
                  key={c.id}
                  className="rounded-xl border border-white/10 bg-white/5 p-4 space-y-3"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="font-semibold truncate">{c.name}</div>
                      <div className="text-xs text-white/50 mt-0.5">
                        {[c.gender, c.age, c.personality].filter(Boolean).join(' · ')}
                      </div>
                    </div>
                    <span
                      className={`chip shrink-0 ${
                        c.gender === '男'
                          ? 'bg-blue-500/20 text-blue-300'
                          : c.gender === '女'
                          ? 'bg-pink-500/20 text-pink-300'
                          : 'bg-white/10 text-white/70'
                      }`}
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
  projectId,
  project,
  builds,
  voices,
  narratorDefault,
  playingKey,
  onTogglePlay,
  onReload,
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
  // 创建 build 的弹窗状态
  const [showCreate, setShowCreate] = useState(false);
  // 当前展开查看详情的 build id
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // 是否有正在运行的 build（用于触发轮询）
  const hasRunning = builds.some(
    b => b.status === 'synthesizing' || b.status === 'preparing'
  );

  // 自动轮询：当存在 running build 时每 2s 重新拉取列表 + 项目
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
      {/* 顶部操作 */}
      <div className="card flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h3 className="font-semibold">🏗 Builds（{builds.length}）</h3>
          <p className="text-sm text-white/60 mt-1">
            每次 build 都会按当前角色音色 + 旁白 + 语速合成所有章节 MP3，可单独下载或打包 ZIP。
          </p>
        </div>
        <button
          className="btn-primary"
          onClick={() => setShowCreate(true)}
          disabled={project.chapter_count === 0}
          title={project.chapter_count === 0 ? '请先识别章节' : '启动一次构建'}
        >
          ▶ 开始生成
        </button>
      </div>

      {builds.length === 0 ? (
        <div className="card text-center py-12 text-white/60">
          <div className="text-4xl mb-3">🏗</div>
          <div>还没有构建记录</div>
          <div className="text-xs mt-1">
            配置好角色音色后，点击上方「▶ 开始生成」启动第一次构建
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

      {/* 创建 build 弹窗 */}
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

// 单行 build（可展开查看章节列表）
function BuildRow({
  projectId,
  item,
  expanded,
  onToggleExpand,
  playingKey,
  onTogglePlay,
  onReload,
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

  // 展开时拉详情
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

  // 如果 build 正在运行且已展开，定期刷新详情
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

  return (
    <div className="card space-y-3">
      <div className="flex items-center gap-3 flex-wrap">
        <button
          className="btn-ghost text-xs py-1 px-2 shrink-0"
          onClick={onToggleExpand}
          title={expanded ? '收起' : '展开查看章节'}
        >
          {expanded ? '▼' : '▶'}
        </button>
        <StatusBadge status={item.status} />
        <span className="text-sm text-white/70">
          {item.completed_chapters} / {item.total_chapters} 章 · {pct}%
        </span>
        <span className="text-xs text-white/40 ml-auto">
          创建于 {relativeTime(item.created_at)}
        </span>
        <button
          className="chip bg-red-500/20 text-red-300 hover:bg-red-500/30 border border-red-500/30 text-xs"
          onClick={() => setConfirmDelete(true)}
          title="删除 build"
        >
          🗑
        </button>
      </div>

      {/* 进度条 */}
      <div className="w-full h-1.5 bg-white/10 rounded-full overflow-hidden">
        <div
          className={`h-full transition-all duration-500 ${
            isRunning
              ? 'bg-gradient-to-r from-orange-500 to-yellow-500'
              : item.status === 'done'
              ? 'bg-gradient-to-r from-green-500 to-emerald-500'
              : item.status === 'failed'
              ? 'bg-red-500'
              : 'bg-gradient-to-r from-brand-500 to-blue-500'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>

      {/* 展开内容 */}
      {expanded && (
        <div className="pt-2 border-t border-white/5">
          {loadingDetail && !detail ? (
            <div className="text-center py-4 text-sm text-white/50">
              <div className="inline-block w-5 h-5 border-2 border-brand-500 border-t-transparent rounded-full animate-spin mr-2" />
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
            <div className="text-sm text-white/50">加载失败</div>
          )}
        </div>
      )}

      {/* 删除确认 */}
      {confirmDelete && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 p-3 flex items-center justify-between gap-3">
          <span className="text-sm text-red-200">确认删除该 build？</span>
          <div className="flex gap-2">
            <button className="btn-ghost text-xs py-1" onClick={() => setConfirmDelete(false)}>
              取消
            </button>
            <button className="btn bg-red-600 hover:bg-red-500 text-white text-xs py-1" onClick={onDelete}>
              确认删除
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// build 展开后的章节列表 + 下载入口
function BuildDetailContent({
  detail,
  projectId,
  playingKey,
  onTogglePlay,
}: {
  detail: BuildDetailResp;
  projectId: string;
  playingKey: string | null;
  onTogglePlay: (key: string, url: string | null) => void;
}) {
  return (
    <div className="space-y-3">
      {/* 概要 + 整包下载 */}
      <div className="flex items-center gap-3 flex-wrap text-xs text-white/60">
        <span>语速 {detail.speed?.toFixed(1) ?? '1.0'}x</span>
        <span>总时长 {formatDuration(detail.total_duration_sec)}</span>
        <span>总大小 {formatSize(detail.total_size_kb ? detail.total_size_kb * 1024 : null)}</span>
        {detail.zip_url && detail.status === 'done' && (
          <a
            className="btn-primary text-xs py-1 ml-auto"
            href={api.buildDownloadAll(projectId, detail.build_id)}
            download
          >
            📦 下载全部 ZIP
          </a>
        )}
      </div>

      {detail.progress_msg && (
        <div className="text-xs text-white/50 rounded-lg bg-white/5 px-3 py-2">
          {detail.progress_msg}
        </div>
      )}

      {/* 章节产物列表 */}
      <div className="space-y-1 max-h-[400px] overflow-y-auto pr-1">
        {detail.artifacts.map(a => {
          const key = `art_${a.chapter_idx}`;
          const playing = playingKey === key;
          return (
            <div
              key={a.chapter_idx}
              className={`flex items-center gap-3 py-2 px-3 rounded-lg ${
                playing ? 'bg-brand-500/10 ring-1 ring-brand-500/40' : 'hover:bg-white/5'
              }`}
            >
              <span className="text-white/40 text-xs w-10 shrink-0 font-mono">
                #{a.chapter_idx + 1}
              </span>
              <div className="flex-1 min-w-0">
                <div className="text-sm truncate">{a.title || '(无标题)'}</div>
                {a.status === 'failed' && a.error_msg && (
                  <div className="text-[11px] text-red-300/90 truncate" title={a.error_msg}>
                    ❌ {a.error_msg.split('\n')[0]}
                  </div>
                )}
              </div>
              <BuildArtifactStatusIcon status={a.status} />
              {a.duration_ms != null && (
                <span className="text-xs text-white/40 shrink-0 w-12 text-right">
                  {formatMs(a.duration_ms)}
                </span>
              )}
              {a.status === 'done' && a.audio_url && (
                <>
                  <button
                    className={`chip text-xs ${
                      playing
                        ? 'bg-brand-500 text-white'
                        : 'bg-white/10 hover:bg-white/20'
                    }`}
                    onClick={() => onTogglePlay(key, a.audio_url)}
                  >
                    {playing ? '⏸' : '▶'}
                  </button>
                  <a
                    className="chip bg-white/10 hover:bg-white/20 text-xs"
                    href={api.buildChapterDownload(projectId, detail.build_id, a.chapter_idx)}
                    download
                    title="下载该章 MP3"
                  >
                    ⬇
                  </a>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function BuildArtifactStatusIcon({ status }: { status: string }) {
  if (status === 'done') return <span className="text-green-400 text-xs">✓</span>;
  if (status === 'synthesizing')
    return (
      <span className="inline-block w-3 h-3 border-2 border-orange-400 border-t-transparent rounded-full animate-spin" />
    );
  if (status === 'failed') return <span className="text-red-400 text-xs">✗</span>;
  return <span className="text-white/30 text-xs">○</span>;
}

// 创建 build 弹窗
function CreateBuildModal({
  projectId,
  project,
  voices,
  narratorDefault,
  onClose,
  onCreated,
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
  // 角色音色：以项目当前分配为准
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
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="card max-w-2xl w-full max-h-[90vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">▶ 启动新 build</h3>
          <button className="btn-ghost" onClick={onClose}>✕</button>
        </div>

        <div className="space-y-4">
          <p className="text-sm text-white/60">
            将按以下配置合成 <b className="text-white">{project.chapter_count}</b> 章 MP3。
            可在生成前做最后调整；保存到项目的默认配置不会被改动。
          </p>

          {/* 旁白 */}
          <div className="space-y-2">
            <div className="text-sm text-white/70">旁白音色</div>
            <VoicePicker
              voices={voices}
              value={narrator}
              onChange={setNarrator}
              onPreview={() => {}}
            />
          </div>

          {/* 语速 */}
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-sm text-white/70 w-12">语速</span>
            <input
              type="range"
              min={0.5}
              max={2.0}
              step={0.1}
              value={speed}
              onChange={e => setSpeed(parseFloat(e.target.value))}
              className="flex-1 min-w-[200px] accent-brand-500 cursor-pointer"
            />
            <span className="text-sm font-mono w-12 text-right">{speed.toFixed(1)}x</span>
          </div>

          {/* 角色音色 */}
          {project.characters.length > 0 && (
            <div className="space-y-2">
              <div className="text-sm text-white/70">
                角色音色（{project.characters.length}）· 已使用项目当前配置
              </div>
              <div className="grid md:grid-cols-2 gap-2 max-h-[280px] overflow-y-auto pr-1">
                {project.characters.map(c => (
                  <div
                    key={c.id}
                    className="rounded-lg border border-white/10 bg-white/5 p-3 space-y-2"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-sm truncate">{c.name}</span>
                      <span className="text-xs text-white/40">{c.gender}</span>
                    </div>
                    <select
                      className="w-full text-sm"
                      value={charVoices[c.name] || ''}
                      onChange={e =>
                        setCharVoices(prev => ({ ...prev, [c.name]: e.target.value }))
                      }
                    >
                      <option value="">未分配</option>
                      {voices.map(v => (
                        <option key={v.id} value={v.id}>
                          {v.name} ({v.gender})
                        </option>
                      ))}
                    </select>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {err && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
            {err}
          </div>
        )}

        <div className="flex justify-end gap-2 mt-5">
          <button className="btn-ghost" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button className="btn-primary" onClick={submit} disabled={busy}>
            {busy ? '启动中…' : '🚀 开始生成'}
          </button>
        </div>
      </div>
    </div>
  );
}

// =================== Settings Tab ===================
function SettingsTab({
  project,
  voices,
  onReload,
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

  // 删除项目二次确认
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // 项目切换时同步
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
      <div className="card space-y-4">
        <h3 className="font-semibold">⚙️ 项目设置</h3>

        <div className="space-y-2">
          <label className="block text-sm text-white/70">项目名</label>
          <input
            type="text"
            className="input"
            value={name}
            onChange={e => setName(e.target.value)}
            maxLength={80}
          />
        </div>

        <div className="space-y-2">
          <label className="block text-sm text-white/70">描述</label>
          <textarea
            className="textarea min-h-[80px]"
            value={description}
            onChange={e => setDescription(e.target.value)}
            placeholder="可选：填写项目描述、备注等"
            maxLength={500}
          />
        </div>

        <div className="space-y-2">
          <label className="block text-sm text-white/70">标签（逗号分隔）</label>
          <input
            type="text"
            className="input"
            value={tagsInput}
            onChange={e => setTagsInput(e.target.value)}
            placeholder="例如：科幻, 长篇, 三体"
          />
        </div>

        <div className="space-y-2">
          <label className="block text-sm text-white/70">默认旁白音色</label>
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

        <div className="flex items-center gap-3 flex-wrap">
          <span className="text-sm text-white/70 w-20">默认语速</span>
          <input
            type="range"
            min={0.5}
            max={2.0}
            step={0.1}
            value={speed}
            onChange={e => setSpeed(parseFloat(e.target.value))}
            className="flex-1 min-w-[200px] accent-brand-500 cursor-pointer"
          />
          <span className="text-sm font-mono w-12 text-right">{speed.toFixed(1)}x</span>
        </div>

        {err && (
          <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
            {err}
          </div>
        )}

        <div className="flex items-center gap-3 pt-2">
          <button className="btn-primary" disabled={saving} onClick={save}>
            {saving ? '保存中…' : '💾 保存'}
          </button>
          {savedTip && <span className="text-xs text-green-300">✓ 已保存</span>}
        </div>
      </div>

      {/* 危险区：删除项目 */}
      <div className="card border-red-500/30 bg-red-500/5 space-y-3">
        <h3 className="font-semibold text-red-300">⚠️ 危险区</h3>
        <p className="text-sm text-white/60">
          删除项目会同时删除所有章节、角色音色配置与 build 历史，操作不可恢复。
        </p>
        {!confirmDelete ? (
          <button
            className="btn bg-red-600 hover:bg-red-500 text-white"
            onClick={() => setConfirmDelete(true)}
          >
            🗑 删除该项目
          </button>
        ) : (
          <div className="rounded-xl border border-red-500/40 bg-red-500/10 p-3 space-y-3">
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
                className="btn bg-red-600 hover:bg-red-500 text-white"
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
