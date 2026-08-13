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
};
