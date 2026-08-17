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
  authChangePassword: (old_pw: string, new_pw: string) =>
    _fetch<{ ok: boolean }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: old_pw, new_password: new_pw }),
    }),
  authLogout: () =>
    _fetch<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  // ---------- Projects ----------
  projects: () =>
    _fetch<{ items: import('./types_gen').ProjectListItem[] }>('/api/projects'),
  projectCreate: (data: { name: string }) =>
    _fetch<import('./types_gen').ProjectResp>('/api/projects', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  project: (id: string) =>
    _fetch<import('./types_gen').ProjectDetailResp>(`/api/projects/${id}`),
  projectUpdate: (id: string, data: { name?: string; narrator_voice_id?: string }) =>
    _fetch<import('./types_gen').ProjectResp>(`/api/projects/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  projectDelete: (id: string) =>
    _fetch<{ ok: boolean }>(`/api/projects/${id}`, { method: 'DELETE' }),
  projectImportFile: (id: string, file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    return _fetch<import('./types_gen').ProjectResp>(`/api/projects/${id}/import`, {
      method: 'POST',
      body: fd,
    });
  },
  projectImportText: (id: string, data: { content: string; filename?: string }) =>
    _fetch<import('./types_gen').ProjectResp>(`/api/projects/${id}/import-text`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  projectPrepare: (id: string) =>
    _fetch<import('./types_gen').ProjectPrepareTriggerResp>(`/api/projects/${id}/prepare`, {
      method: 'POST',
    }),
  projectProgress: (id: string) =>
    _fetch<import('./types_gen').ProjectProgressResp>(`/api/projects/${id}/prepare-progress`),
  projectChapters: (id: string) =>
    _fetch<import('./types_gen').ChapterSummary[]>(`/api/projects/${id}/chapters`),
  projectCharacters: (id: string) =>
    _fetch<import('./types_gen').CharacterResp[]>(`/api/projects/${id}/characters`),
  projectCharacterVoice: (
    id: string,
    character_name: string,
    data: { voice_id: string },
  ) =>
    _fetch<import('./types_gen').CharacterResp>(
      `/api/projects/${id}/characters/${encodeURIComponent(character_name)}/voice`,
      { method: 'PATCH', body: JSON.stringify(data) },
    ),
  projectPreviewVoice: (id: string, data: { text: string; voice_id?: string }) =>
    _fetch<{ url: string }>(`/api/projects/${id}/preview`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  // ---------- Builds ----------
  builds: (projectId: string) =>
    _fetch<{ items: import('./types_gen').BuildSummary[] }>(`/api/projects/${projectId}/builds`),
  buildStart: (
    projectId: string,
    data: { narrator_voice_id?: string; speed?: number; source_build_id?: string },
  ) =>
    _fetch<import('./types_gen').BuildDetailResp>(`/api/projects/${projectId}/builds`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  build: (projectId: string, buildId: string) =>
    _fetch<import('./types_gen').BuildDetailResp>(`/api/projects/${projectId}/builds/${buildId}`),
  buildCancel: (projectId: string, buildId: string) =>
    _fetch<{ ok: boolean }>(`/api/projects/${projectId}/builds/${buildId}/cancel`, {
      method: 'POST',
    }),
  buildRetryFailed: (projectId: string, buildId: string) =>
    _fetch<{ ok: boolean }>(`/api/projects/${projectId}/builds/${buildId}/retry-failed`, {
      method: 'POST',
    }),
  buildDelete: (projectId: string, buildId: string) =>
    _fetch<{ ok: boolean }>(`/api/projects/${projectId}/builds/${buildId}`, {
      method: 'DELETE',
    }),
  // 整包 ZIP 下载 URL（附带 ?token=，因 <a href> 无法带 Authorization header）
  buildDownloadAll: (projectId: string, buildId: string) => {
    const tok = getToken();
    const base = `/api/projects/${projectId}/builds/${buildId}/download-all`;
    return tok ? `${base}?token=${encodeURIComponent(tok)}` : base;
  },
  // 单章 MP3 下载 URL（附带 ?token=）
  buildChapterDownload: (projectId: string, buildId: string, idx: number) => {
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
  status: string;
  book_title: string | null;
  total_chapters: number;
  created_at: string;
}
