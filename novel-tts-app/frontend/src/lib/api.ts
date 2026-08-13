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
};
