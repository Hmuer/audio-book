// Thin fetch wrappers. In static export mode we call backend same origin (/api).

const BASE = '';

async function _fetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
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

export interface PrepareResp {
  job_id: string;
  polished_text: string;
  diff: any[];
  polish_warning: string | null;
  characters: Character[];
  dialogue_attributions: DialogueAttr[];
  chapters: ChapterMeta[];
  voice_recommendations: VoiceRec[];
}

export interface SynthSegment {
  idx: number;
  kind: 'title' | 'narrator' | 'dialogue' | 'silence';
  speaker: string | null;
  voice_id: string;
  text: string;
  audio_filename: string;
  audio_url: string;
  duration_ms: number;
  confidence: number | null;
}

export interface SynthResp {
  job_id: string;
  audio_filename: string;
  audio_url: string;
  duration_sec: number;
  segments: SynthSegment[];
}

// ---------- Book (整本小说) ----------

export interface BookChapterMeta {
  idx: number;
  title: string;
  text_len: number;
}

export interface BookPrepareResp {
  job_id: string;
  book_title: string | null;
  total_chapters: number;
  chapters: BookChapterMeta[];
  characters: Character[];
  voice_recommendations: VoiceRec[];
  polish_warning: string | null;
}

export interface BookChapterResult {
  chapter_idx: number;
  title: string;
  status: 'pending' | 'synthesizing' | 'done' | 'failed';
  audio_url: string | null;
  duration_ms: number | null;
  error_msg: string | null;
}

export interface BookStatusResp {
  job_id: string;
  book_status: 'prepared' | 'synthesizing' | 'done' | 'failed';
  total_chapters: number;
  completed_chapters: number;
  progress_msg: string | null;
  final_audio_url: string | null; // 整本书模式：逐章下载，此处通常为 null（保留兼容）
  final_duration_sec: number | null; // 所有章节累计时长
  zip_url: string | null; // done 后可整包下载 ZIP
  total_size_kb: number | null; // 所有章 MP3 合计大小
  chapters: BookChapterResult[];
}

export interface BookSynthResp {
  job_id: string;
  final_audio_filename: string;
  final_audio_url: string;
  duration_sec: number;
  chapters: BookChapterResult[];
}

export const api = {
  health: () => _fetch<{ status: string }>('/api/health'),
  voices: () =>
    _fetch<{ voices: Voice[]; count: number }>('/api/voices').then(r => r.voices),
  prepare: (text: string, enable_polish: boolean) =>
    _fetch<PrepareResp>('/api/chapter/prepare', {
      method: 'POST',
      body: JSON.stringify({ text, enable_polish }),
    }),
  synthesize: (args: {
    job_id: string;
    voice_assignments: Record<string, string>;
    narrator_voice_id: string;
    segment_overrides?: Record<number, string>;
    speed?: number;
  }) =>
    _fetch<SynthResp>('/api/chapter/synthesize', {
      method: 'POST',
      body: JSON.stringify(args),
    }),
  preview: (text: string, voice_id: string, speed?: number) =>
    _fetch<{ audio_url: string; duration_ms: number; audio_filename: string }>('/api/tts/preview', {
      method: 'POST',
      body: JSON.stringify({ text, voice_id, speed: speed ?? 1.0 }),
    }),
  // ---------- Book ----------
  bookUpload: (file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    // 注意：FormData 不能预设 Content-Type，需要单独 fetch
    return fetch(`${BASE}/api/book/upload`, { method: 'POST', body: fd }).then(async r => {
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg += `: ${j.detail}`; } catch {}
        throw new Error(msg);
      }
      return r.json() as Promise<{ file_id: string; filename: string; size: number }>;
    });
  },
  bookPrepare: (file_id: string, filename: string) =>
    _fetch<BookPrepareResp>('/api/book/prepare', {
      method: 'POST',
      body: JSON.stringify({ file_id, filename }),
    }),
  // 注：/book/synthesize 是 fire-and-forget，立即返回 BookStatusResp，前端随后轮询 /status
  bookSynthesize: (args: {
    job_id: string;
    voice_assignments: Record<string, string>;
    narrator_voice_id: string;
    speed?: number;
  }) =>
    _fetch<BookStatusResp>('/api/book/synthesize', {
      method: 'POST',
      body: JSON.stringify(args),
    }),
  bookStatus: (job_id: string) =>
    _fetch<BookStatusResp>(`/api/book/${job_id}/status`),
  bookDownloadAll: (job_id: string) =>
    `/api/book/${job_id}/download-all`,
  bookChapterDownload: (job_id: string, idx: number) =>
    `/api/book/${job_id}/chapters/${idx}/download`,

  // ---------- Project 制（项目工作台） ----------

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
  // 上传文件到项目（multipart/form-data，参考 bookUpload 实现）
  projectImport: (id: string, file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    // 注意：FormData 不能预设 Content-Type，需要单独 fetch
    return fetch(`${BASE}/api/projects/${id}/import`, {
      method: 'POST',
      body: fd,
    }).then(async r => {
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg += `: ${j.detail}`; } catch {}
        throw new Error(msg);
      }
      return r.json() as Promise<ProjectResp>;
    });
  },
  // 触发后端识别（章节/角色/对白归属）
  projectPrepare: (id: string) =>
    _fetch<ProjectPrepareResp>(`/api/projects/${id}/prepare`, { method: 'POST' }),
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
  buildDownloadAll: (projectId: string, buildId: string) =>
    `/api/projects/${projectId}/builds/${buildId}/download-all`,
  // 单章 MP3 下载 URL
  buildChapterDownload: (projectId: string, buildId: string, idx: number) =>
    `/api/projects/${projectId}/builds/${buildId}/chapters/${idx}/download`,
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
