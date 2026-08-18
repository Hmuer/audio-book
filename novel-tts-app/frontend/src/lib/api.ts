// Thin fetch wrappers. In static export mode we call backend same origin (/api).

const BASE = '';
const TOKEN_KEY = 'novel_tts_token';
const TOKEN_EXP_KEY = 'novel_tts_token_exp';

// ---------- Token 管理 ----------

export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) return null;
  // 过期检查（前端兜底；后端 JWT exp 才是权威）
  const exp = localStorage.getItem(TOKEN_EXP_KEY);
  if (exp) {
    const expMs = parseInt(exp, 10);
    if (Date.now() > expMs) {
      // 已过期，清掉
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(TOKEN_EXP_KEY);
      return null;
    }
  }
  return token;
}

export function setToken(token: string, expiresAtIso: string): void {
  if (typeof window === 'undefined') return;
  localStorage.setItem(TOKEN_KEY, token);
  const expMs = new Date(expiresAtIso).getTime();
  if (!isNaN(expMs)) {
    localStorage.setItem(TOKEN_EXP_KEY, String(expMs));
  }
}

export function clearToken(): void {
  if (typeof window === 'undefined') return;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_EXP_KEY);
}

export function isLoggedIn(): boolean {
  return getToken() !== null;
}

// 401 时自动跳登录页
let _onAuthFail: (() => void) | null = null;
export function setOnAuthFail(cb: () => void): void {
  _onAuthFail = cb;
}

async function _fetch<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {};
  // FormData 上传时不显式设 Content-Type（浏览器自动 multipart boundary）
  const bodyIsFormData = init?.body instanceof FormData;
  if (!bodyIsFormData) {
    headers['Content-Type'] = 'application/json';
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...headers, ...(init?.headers as Record<string, string>) },
  });
  if (res.status === 401) {
    // token 失效/过期 → 清掉本地，触发登录跳转
    clearToken();
    if (_onAuthFail) _onAuthFail();
    throw new Error('登录已失效，请重新登录');
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j.detail) msg += `: ${j.detail}`;
    } catch {}
    throw new Error(msg);
  }
  return (await res.json()) as T;
}

// ---------- Auth 类型 ----------

export interface UserInfo {
  id: number;
  username: string;
  is_active: boolean;
  created_at: string | null;
}

export interface LoginResp {
  token: string;
  token_type: string;
  expires_at: string;
  user: UserInfo;
}

// ---------- 业务类型 ----------

export interface Voice {
  id: string;
  name: string;
  gender: string;
  description: string;
}

export interface Character {
  name: string;
  gender: string;
  age: string;
  personality: string;
}

export interface DialogueAttr {
  anchor: { text: string; start: number; end: number };
  speaker: string;
  confidence: number;
  text: string;
}

export interface ChapterMeta {
  idx: number;
  title: string;
  text: string;
}

export interface VoiceRec {
  character_name: string;
  suggested_voice_id: string;
  reason: string;
}

export const api = {
  health: () => _fetch<{ status: string }>('/api/health'),
  voices: () =>
    _fetch<{ voices: Voice[]; count: number }>('/api/voices').then(r => r.voices),
  // ---------- Auth ----------
  authLogin: (username: string, password: string) =>
    _fetch<LoginResp>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  authMe: () => _fetch<UserInfo>('/api/auth/me'),
  authLogout: () =>
    _fetch<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  authChangePassword: (oldPassword: string, newPassword: string) =>
    _fetch<{ ok: boolean }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  // ---------- 业务：TTS 音色试听 ----------
  preview: (text: string, voice_id: string, speed?: number) =>
    _fetch<{ audio_url: string; duration_ms: number; audio_filename: string }>('/api/tts/preview', {
      method: 'POST',
      body: JSON.stringify({ text, voice_id, speed: speed ?? 1.0 }),
    }),

  // ---------- Project 制（项目工作台：唯一入口） ----------

  // 创建项目
  projectCreate: (name: string) =>
    _fetch<ProjectResp>('/api/projects', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  // 项目列表
  projectList: () => _fetch<ProjectListItem[]>('/api/projects'),
  // 项目详情
  projectGet: (id: string) => _fetch<ProjectDetailResp>(`/api/projects/${id}`),
  // 修改项目元信息
  projectUpdate: (id: string, patch: Record<string, any>) =>
    _fetch<ProjectResp>(`/api/projects/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
  // 删除项目
  projectDelete: (id: string) =>
    _fetch<{ ok: boolean }>(`/api/projects/${id}`, { method: 'DELETE' }),
  // 上传 TXT 文件到项目（multipart/form-data，FormData 由浏览器设 Content-Type）
  projectImport: (id: string, file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    const token = getToken();
    return fetch(`${BASE}/api/projects/${id}/import`, {
      method: 'POST',
      body: fd,
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    }).then(async r => {
      if (r.status === 401) {
        clearToken();
        if (_onAuthFail) _onAuthFail();
        throw new Error('登录已失效，请重新登录');
      }
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg += `: ${j.detail}`; } catch {}
        throw new Error(msg);
      }
      return r.json() as Promise<ProjectResp>;
    });
  },
  // 粘贴文本到项目（直接 POST JSON，浏览器粘贴场景）
  projectImportText: (id: string, text: string, filenameHint = 'pasted_text.txt') =>
    _fetch<ProjectResp>(`/api/projects/${id}/import-text`, {
      method: 'POST',
      body: JSON.stringify({ text, filename_hint: filenameHint }),
    }),
  // 触发后端识别（章节/角色/对白归属）：202 Accepted，后台异步执行
  projectPrepare: (id: string) =>
    _fetch<ProjectPrepareTriggerResp>(`/api/projects/${id}/prepare`, { method: 'POST' }),
  // 拉取章节列表
  projectChapters: (id: string) =>
    _fetch<ChapterSummary[]>(`/api/projects/${id}/chapters`),
  // 拉取角色列表
  projectCharacters: (id: string) =>
    _fetch<CharacterWithVoice[]>(`/api/projects/${id}/characters`),
  // 修改角色音色
  projectUpdateCharVoice: (projectId: string, charId: number, voiceId: string) =>
    _fetch<CharacterResp>(`/api/projects/${projectId}/characters/${charId}`, {
      method: 'PATCH',
      body: JSON.stringify({ voice_id: voiceId }),
    }),
  // 创建 build 任务
  buildCreate: (
    projectId: string,
    args: {
      voice_assignments: Record<string, string>;
      narrator_voice_id: string;
      speed?: number;
    }
  ) =>
    _fetch<BuildResp>(`/api/projects/${projectId}/builds`, {
      method: 'POST',
      body: JSON.stringify(args),
    }),
  // build 列表
  buildList: (projectId: string) =>
    _fetch<BuildListItem[]>(`/api/projects/${projectId}/builds`),
  // build 详情
  buildGet: (projectId: string, buildId: string) =>
    _fetch<BuildDetailResp>(`/api/projects/${projectId}/builds/${buildId}`),
  // build 轮询状态
  buildStatus: (projectId: string, buildId: string) =>
    _fetch<BuildStatusResp>(`/api/projects/${projectId}/builds/${buildId}/status`),
  // 删除 build
  buildDelete: (projectId: string, buildId: string) =>
    _fetch<{ ok: boolean }>(`/api/projects/${projectId}/builds/${buildId}`, {
      method: 'DELETE',
    }),
  // 整包 ZIP 下载 URL
  buildDownloadAll: (projectId: string, buildId: string) => {
    const tok = getToken();
    const base = `/api/projects/${projectId}/builds/${buildId}/download-all`;
    return tok ? `${base}?token=${encodeURIComponent(tok)}` : base;
  },
  // 单章 MP3 下载 URL
  buildChapterDownload: (projectId: string, buildId: string, idx: number) => {
    const tok = getToken();
    const base = `/api/projects/${projectId}/builds/${buildId}/chapters/${idx}/download`;
    return tok ? `${base}?token=${encodeURIComponent(tok)}` : base;
  },
  // 单章 MP3 音频 URL（用于 <audio src> 试听，需要 token 认证）
  buildChapterAudioUrl: (projectId: string, buildId: string, idx: number) => {
    const tok = getToken();
    const base = `/api/projects/${projectId}/builds/${buildId}/chapters/${idx}/download`;
    return tok ? `${base}?token=${encodeURIComponent(tok)}` : base;
  },
};

// ---------- Project 制类型定义 ----------

// 创建/更新项目后返回的精简结构
export interface ProjectResp {
  project_id: string;
  name: string;
  book_title: string | null;
  status: string;
  source_filename: string | null;
  chapter_count: number;
  cover_color: string;
  created_at: string;
  updated_at: string;
}

// 项目列表项
export interface ProjectListItem {
  project_id: string;
  name: string;
  book_title: string | null;
  status: string;
  source_filename: string | null;
  chapter_count: number;
  cover_color: string;
  created_at: string;
  updated_at: string;
  /** prepare 阶段当前 stage（列表页快速显示）。也会同步出现在 prepare_progress.stage。 */
  prepare_stage?: string | null;
  /** prepare 进度白名单：刷新/重开标签页后列表直接显示进度条 & 阶段，无需进详情。 */
  prepare_progress?: PrepareProgress | null;
}

// 项目详情
export interface ProjectDetailResp {
  project_id: string;
  name: string;
  book_title: string | null;
  status: string;
  source_filename: string | null;
  source_file_size: number | null;
  chapter_count: number;
  cover_color: string;
  description: string | null;
  tags: string[] | null;
  default_narrator_voice_id: string | null;
  default_speed: number | null;
  created_at: string;
  updated_at: string;
  chapters: ChapterSummary[];
  characters: CharacterWithVoice[];
  last_build: BuildBrief | null;
  // prepare 阶段进度（stage / last_error / 各子阶段计数），失败时带具体错误
  prepare_progress: PrepareProgress | null;
}

// prepare 阶段进度（从 DB progress_json 透传，字段名与后端 progress_json 白名单对齐）
export interface PrepareProgress {
  version?: number;
  stage?: string; // start / split / characters / dedup / dialogues / voice_recs / done
  started_at?: string;
  updated_at?: string;
  // 失败时：具体错误类型 + 消息 + 时间
  last_error?: string;
  last_error_at?: string;
  last_error_type?: string;
  prev_error?: { at?: string; msg?: string };
  /** 服务重启/看门狗自动恢复次数（>0 时前端显示"♻ 自动恢复 × N"） */
  restart_count?: number;
  // 角色识别进度
  char_slice_total?: number;
  char_slice_completed_n?: number;
  char_current_slice?: { idx: number; slice_len?: number } | null;
  char_failed_slices?: Record<string, { slice_idx: number; slice_len?: number; retries?: number; last_err?: string }>;
  char_failed_slices_n?: number;
  char_full_text_len?: number;
  dedup_done?: boolean;
  // 对白归属进度
  dialogue_total_batches?: number;
  dialogue_completed_batches_count?: number;
  dialogue_failed_batch_count?: number;
  dialogue_total_chapters?: number;
  dialogue_completed_chapters_count?: number;
  dialogue_completed_chapters_n?: number;
  dialogue_failed_batches?: Record<string, any>;
  dialogue_failed_batches_n?: number;
  dialogue_total_dialogues?: number;
  // 音色推荐进度
  voice_recs_done?: boolean;
  voice_recs_count?: number;
}

// prepare 触发立即返回（HTTP 202 Accepted）
export interface ProjectPrepareTriggerResp {
  project_id: string;
  status: string;
  message: string;
  prepare_progress?: PrepareProgress | null;
}

// 章节摘要
export interface ChapterSummary {
  idx: number;
  title: string;
  text_len: number;
}

// 角色（含已分配音色）
export interface CharacterWithVoice {
  id: number;
  name: string;
  gender: string;
  age: string;
  personality: string;
  canonical_name: string | null;
  assigned_voice_id: string | null;
}

// 最近一次 build 的摘要
export interface BuildBrief {
  build_id: string;
  status: string;
  completed_chapters: number;
  total_chapters: number;
  created_at: string;
}

// prepare 接口返回
export interface ProjectPrepareResp {
  project_id: string;
  book_title: string | null;
  total_chapters: number;
  chapters: ChapterSummary[];
  characters: any[];
  voice_recommendations: any[];
}

// 修改角色音色后返回
export interface CharacterResp {
  id: number;
  name: string;
  gender: string;
  age: string;
  personality: string;
  canonical_name: string | null;
  assigned_voice_id: string | null;
}

// build 列表项
export interface BuildListItem {
  build_id: string;
  status: string;
  total_chapters: number;
  completed_chapters: number;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

// build 详情
export interface BuildDetailResp {
  build_id: string;
  project_id: string;
  status: string;
  progress_msg: string | null;
  total_chapters: number;
  completed_chapters: number;
  narrator_voice_id: string | null;
  speed: number | null;
  zip_url: string | null;
  total_size_kb: number | null;
  total_duration_sec: number | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  artifacts: BuildArtifactResp[];
}

// build 单章产物
export interface BuildArtifactResp {
  chapter_idx: number;
  title: string;
  status: string;
  audio_url: string | null;
  duration_ms: number | null;
  error_msg: string | null;
}

// build 状态轮询响应
export interface BuildStatusResp {
  build_id: string;
  status: string;
  progress_msg: string | null;
  completed_chapters: number;
  total_chapters: number;
  artifacts: BuildArtifactResp[];
}

// 创建 build 后返回
export interface BuildResp {
  build_id: string;
  project_id: string;
  status: string;
  created_at: string;
}
