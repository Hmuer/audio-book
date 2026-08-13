'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  api,
  BookPrepareResp,
  BookStatusResp,
  Voice,
  VoiceRec,
} from '@/lib/api';
import VoicePicker from './VoicePicker';

type Phase = 'upload' | 'preparing' | 'config' | 'synthesizing' | 'done';

export default function BookFlow({ voices }: { voices: Voice[] }) {
  const [phase, setPhase] = useState<Phase>('upload');
  const [err, setErr] = useState<string | null>(null);

  // 上传
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [fileId, setFileId] = useState<string | null>(null);

  // prepare 结果
  const [prep, setPrep] = useState<BookPrepareResp | null>(null);
  const [preparingMsg, setPreparingMsg] = useState<string>('');

  // 音色配置
  const [narrator, setNarrator] = useState<string>('');
  const [charVoices, setCharVoices] = useState<Record<string, string>>({});
  const [speed, setSpeed] = useState<number>(1.0);

  // 合成进度
  const [synthJobId, setSynthJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<BookStatusResp | null>(null);

  // 试听播放管理
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playingKey, setPlayingKey] = useState<string | null>(null);
  const [loadingKey, setLoadingKey] = useState<string | null>(null);

  const narratorDefault = voices.find(v => v.id === 'male-qn-jingying') || voices[0];

  // ---------- 上传 ----------
  const onPickFile = (f: File | null) => {
    setErr(null);
    if (!f) return;
    if (!/\.(txt|text|md)$/i.test(f.name)) {
      // 不阻断，但提示
      console.warn('文件后缀不是 .txt/.md，仍尝试上传');
    }
    if (f.size > 50 * 1024 * 1024) {
      setErr(`文件过大（${(f.size / 1024 / 1024).toFixed(1)}MB），上限 50MB`);
      return;
    }
    setFile(f);
    setFileId(null);
  };

  const doUploadAndPrepare = async () => {
    if (!file) return;
    setErr(null);
    setUploading(true);
    setPhase('preparing');
    setPreparingMsg('正在上传文件…');
    try {
      const up = await api.bookUpload(file);
      setFileId(up.file_id);
      setPreparingMsg('上传完成，正在识别章节 + 角色 + 对白归属 + 推荐音色…');
      const r = await api.bookPrepare(up.file_id, up.filename);
      setPrep(r);
      // 初始化音色配置
      const initNarrator = narratorDefault?.id || '';
      setNarrator(initNarrator);
      const initV: Record<string, string> = {};
      r.characters.forEach(c => {
        const rec = r.voice_recommendations.find(
          x => x.character_name === c.name
        );
        if (rec && voices.some(v => v.id === rec.suggested_voice_id)) {
          initV[c.name] = rec.suggested_voice_id;
        } else {
          const fb = voices.find(v =>
            c.gender === '男' ? v.gender === '男声'
            : c.gender === '女' ? v.gender === '女声'
            : true
          );
          if (fb) initV[c.name] = fb.id;
        }
      });
      setCharVoices(initV);
      setPhase('config');
    } catch (e: any) {
      setErr(String(e?.message || e));
      setPhase('upload');
    } finally {
      setUploading(false);
    }
  };

  // ---------- 合成 ----------
  const startSynthesize = async () => {
    if (!prep) return;
    setErr(null);
    setPhase('synthesizing');
    setSynthJobId(prep.job_id);
    // 不阻塞轮询：先发起合成请求
    api
      .bookSynthesize({
        job_id: prep.job_id,
        voice_assignments: charVoices,
        narrator_voice_id: narrator,
        speed,
      })
      .then(r => {
        // 合成完成：刷新状态
        setStatus({
          job_id: r.job_id,
          book_status: 'done',
          total_chapters: r.chapters.length,
          completed_chapters: r.chapters.filter(c => c.status === 'done').length,
          progress_msg: null,
          final_audio_url: r.final_audio_url,
          final_duration_sec: r.duration_sec,
          chapters: r.chapters,
        });
        setPhase('done');
      })
      .catch(e => {
        setErr(`合成失败: ${e?.message || e}`);
        setPhase('config');
      });
  };

  // 合成期间轮询状态
  useEffect(() => {
    if (phase !== 'synthesizing' || !synthJobId) return;
    let stop = false;
    const tick = async () => {
      if (stop) return;
      try {
        const s = await api.bookStatus(synthJobId);
        if (!stop) setStatus(s);
        if (s.book_status === 'done' || s.book_status === 'failed') return;
      } catch (e) {
        console.warn('status 轮询失败', e);
      }
      setTimeout(tick, 2000);
    };
    tick();
    return () => { stop = true; };
  }, [phase, synthJobId]);

  // ---------- 试听 ----------
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

  const togglePreviewVoice = async (voiceId: string, text: string) => {
    const key = `voice_${voiceId}`;
    if (playingKey === key) { stopPlayback(); return; }
    stopPlayback();
    setLoadingKey(key);
    try {
      const r = await api.preview(text.slice(0, 80), voiceId, speed);
      playUrl(key, r.audio_url);
    } finally {
      setLoadingKey(prev => (prev === key ? null : prev));
    }
  };

  const togglePlayChapter = (chapterIdx: number, url: string | null) => {
    if (!url) return;
    const key = `ch_${chapterIdx}`;
    if (playingKey === key) { stopPlayback(); return; }
    stopPlayback();
    playUrl(key, url);
  };

  const togglePlayFinal = () => {
    if (!status?.final_audio_url) return;
    const key = 'final';
    if (playingKey === key) { stopPlayback(); return; }
    stopPlayback();
    playUrl(key, status.final_audio_url);
  };

  const voicesById = useMemo(() => {
    const m = new Map<string, Voice>();
    voices.forEach(v => m.set(v.id, v));
    return m;
  }, [voices]);

  const completedCount = status?.chapters.filter(c => c.status === 'done').length || 0;
  const totalCount = prep?.total_chapters || status?.total_chapters || 0;
  const progressPct = totalCount ? Math.round((completedCount / totalCount) * 100) : 0;

  return (
    <section className="space-y-6">
      <audio ref={audioRef} className="hidden" />

      {/* ============ Phase 1: 上传 ============ */}
      {phase === 'upload' && (
        <div className="card space-y-5 max-w-3xl mx-auto">
          <div>
            <h2 className="text-lg font-semibold">📚 整本小说转语音</h2>
            <p className="text-sm text-white/60 mt-1">
              上传一整本 TXT 小说，系统自动识别章节、角色、对白归属，串行合成所有章节并合并为一个 MP3。
            </p>
          </div>

          <label
            className="block border-2 border-dashed border-white/20 rounded-xl p-10 text-center cursor-pointer hover:border-brand-500/60 hover:bg-brand-500/5 transition"
            onDragOver={e => { e.preventDefault(); }}
            onDrop={e => {
              e.preventDefault();
              onPickFile(e.dataTransfer.files?.[0] || null);
            }}
          >
            <input
              type="file"
              accept=".txt,.text,.md"
              className="hidden"
              onChange={e => onPickFile(e.target.files?.[0] || null)}
            />
            {file ? (
              <div className="space-y-1">
                <div className="text-3xl">📄</div>
                <div className="font-medium">{file.name}</div>
                <div className="text-xs text-white/50">{(file.size / 1024).toFixed(1)} KB</div>
              </div>
            ) : (
              <div className="space-y-2 text-white/60">
                <div className="text-3xl">📁</div>
                <div>点击或拖拽 TXT 文件到这里</div>
                <div className="text-xs">支持 .txt / .md，最大 50MB</div>
              </div>
            )}
          </label>

          {err && (
            <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
              {err}
            </div>
          )}

          <div className="rounded-xl bg-white/5 border border-white/10 p-4 text-xs text-white/60 space-y-2">
            <div className="font-semibold text-white/80">📋 流程说明</div>
            <div>1. 上传 TXT → 后端保存并解析编码（utf-8/gbk/big5 等）</div>
            <div>2. 自动识别章节（正则「第X章」+ LLM 兜底切分）</div>
            <div>3. 全书角色识别 + 每章对白归属 + 音色推荐</div>
            <div>4. 配置全书统一的旁白音色 + 每个角色音色</div>
            <div>5. 按章串行合成，每章独立 MP3，最后合并整本</div>
            <div className="text-yellow-300/80">⚠ 整本合成耗时较长（每章约 1-3 分钟），请耐心等待</div>
          </div>

          <button
            className="btn-primary w-full"
            disabled={!file || uploading}
            onClick={doUploadAndPrepare}
          >
            {!file ? '请先选择文件' : uploading ? '处理中…' : '🚀 上传并开始 prepare'}
          </button>
        </div>
      )}

      {/* ============ Phase 2: preparing ============ */}
      {phase === 'preparing' && (
        <div className="card max-w-2xl mx-auto text-center py-12">
          <div className="inline-block w-12 h-12 border-4 border-brand-500 border-t-transparent rounded-full animate-spin mb-4" />
          <div className="text-lg font-medium">{preparingMsg}</div>
          <div className="text-sm text-white/50 mt-2">
            首次处理整本小说可能需要 1-5 分钟（取决于字数和章节数）
          </div>
          {err && (
            <div className="mt-6 rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
              {err}
            </div>
          )}
        </div>
      )}

      {/* ============ Phase 3: config ============ */}
      {phase === 'config' && prep && (
        <div className="space-y-6">
          <div className="card space-y-3">
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div>
                <h2 className="text-lg font-semibold">📋 prepare 完成</h2>
                <div className="text-sm text-white/60">
                  {prep.book_title || '整本小说'} · 共 {prep.total_chapters} 章 ·{' '}
                  {prep.characters.length} 个角色
                </div>
              </div>
              <button
                className="btn-ghost"
                onClick={() => {
                  setPrep(null);
                  setFile(null);
                  setFileId(null);
                  setPhase('upload');
                }}
              >
                ← 重新上传
              </button>
            </div>

            <details className="rounded-xl bg-white/5 border border-white/10 p-3">
              <summary className="cursor-pointer font-medium text-sm">
                章节列表（{prep.total_chapters} 章）
              </summary>
              <div className="mt-3 max-h-60 overflow-y-auto space-y-1 text-xs">
                {prep.chapters.map(c => (
                  <div key={c.idx} className="flex justify-between gap-3 py-1 border-b border-white/5">
                    <span className="text-white/70 truncate">
                      <span className="text-white/40 mr-2">#{c.idx + 1}</span>
                      {c.title || '(无标题)'}
                    </span>
                    <span className="text-white/40 shrink-0">{c.text_len} 字</span>
                  </div>
                ))}
              </div>
            </details>
          </div>

          <div className="card space-y-4">
            <h3 className="font-semibold">🎙️ 全书音色配置</h3>
            <div className="space-y-2">
              <div className="text-sm text-white/70">旁白音色（全书统一）</div>
              <VoicePicker
                voices={voices}
                value={narrator}
                onChange={setNarrator}
                onPreview={(id) => togglePreviewVoice(id, '这是一段旁白试听。林若雪站在街角，望着远方。')}
                isPlaying={playingKey === `voice_${narrator}`}
                isLoading={loadingKey === `voice_${narrator}`}
              />
            </div>

            <div className="flex items-center gap-3 flex-wrap">
              <span className="text-sm text-white/70">语速</span>
              <input
                type="range"
                min={0.5}
                max={2.0}
                step={0.1}
                value={speed}
                onChange={e => setSpeed(parseFloat(e.target.value))}
                className="flex-1 min-w-[200px]"
              />
              <span className="text-sm font-mono w-12 text-right">{speed.toFixed(1)}x</span>
            </div>

            {prep.characters.length > 0 && (
              <div className="space-y-3">
                <div className="text-sm text-white/70">角色音色</div>
                <div className="space-y-2">
                  {prep.characters.map(c => {
                    const rec = prep.voice_recommendations.find(r => r.character_name === c.name);
                    return (
                      <div key={c.name} className="rounded-xl bg-white/5 border border-white/10 p-3 space-y-2">
                        <div className="flex items-center justify-between gap-3 flex-wrap">
                          <div>
                            <span className="font-medium">{c.name}</span>
                            <span className="text-xs text-white/40 ml-2">
                              {c.gender} · {c.age}
                            </span>
                          </div>
                          {rec && (
                            <span className="text-xs text-white/40 max-w-[50%] truncate" title={rec.reason}>
                              💡 {rec.reason}
                            </span>
                          )}
                        </div>
                        <VoicePicker
                          voices={voices}
                          value={charVoices[c.name] || ''}
                          onChange={(id) => setCharVoices(prev => ({ ...prev, [c.name]: id }))}
                          onPreview={(id) => togglePreviewVoice(id, `你好，我是${c.name}，很高兴认识你。`)}
                          isPlaying={playingKey === `voice_${charVoices[c.name]}`}
                          isLoading={loadingKey === `voice_${charVoices[c.name]}`}
                        />
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>

          <div className="card space-y-3">
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div>
                <h3 className="font-semibold">▶️ 开始整本合成</h3>
                <div className="text-sm text-white/60">
                  将串行合成 {prep.total_chapters} 章，预计耗时{' '}
                  {Math.max(1, Math.round(prep.total_chapters * 1.5))} 分钟
                </div>
              </div>
              <button className="btn-primary" onClick={startSynthesize}>
                🎬 开始合成整本
              </button>
            </div>
            {err && (
              <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
                {err}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ============ Phase 4: synthesizing ============ */}
      {phase === 'synthesizing' && status && (
        <div className="space-y-6">
          <div className="card space-y-4">
            <div className="flex items-center gap-4">
              <div className="inline-block w-8 h-8 border-4 border-brand-500 border-t-transparent rounded-full animate-spin" />
              <div className="flex-1">
                <div className="font-medium">
                  {status.progress_msg || '正在合成…'}
                </div>
                <div className="text-sm text-white/60">
                  已完成 {completedCount} / {status.total_chapters} 章
                </div>
              </div>
              <div className="text-2xl font-mono">{progressPct}%</div>
            </div>
            <div className="w-full h-2 bg-white/10 rounded-full overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-brand-500 to-blue-500 transition-all duration-500"
                style={{ width: `${progressPct}%` }}
              />
            </div>
          </div>

          <div className="card space-y-2">
            <h3 className="font-semibold mb-3">📜 各章进度</h3>
            <div className="space-y-1 max-h-[400px] overflow-y-auto">
              {status.chapters.map(c => (
                <div
                  key={c.chapter_idx}
                  className="flex items-center justify-between gap-3 py-2 px-3 rounded-lg hover:bg-white/5"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <StatusIcon status={c.status} />
                    <span className="text-white/40 text-xs w-8 shrink-0">
                      #{c.chapter_idx + 1}
                    </span>
                    <span className="text-sm truncate">{c.title || '(无标题)'}</span>
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    {c.duration_ms && (
                      <span className="text-xs text-white/40">
                        {(c.duration_ms / 1000).toFixed(1)}s
                      </span>
                    )}
                    {c.status === 'done' && c.audio_url && (
                      <button
                        className="chip bg-white/10 hover:bg-white/20 text-xs"
                        onClick={() => togglePlayChapter(c.chapter_idx, c.audio_url)}
                      >
                        {playingKey === `ch_${c.chapter_idx}` ? '⏸' : '▶'} 试听
                      </button>
                    )}
                    {c.status === 'failed' && c.error_msg && (
                      <span className="text-xs text-red-300" title={c.error_msg}>
                        ❌ 失败
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ============ Phase 5: done ============ */}
      {phase === 'done' && status && (
        <div className="space-y-6">
          <div className="card border-brand-500/40 bg-brand-500/5 space-y-4 sticky top-4 z-10">
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div>
                <div className="text-lg font-semibold">🎉 整本合成完成</div>
                <div className="text-sm text-white/60">
                  共 {status.total_chapters} 章 · 总时长{' '}
                  {Math.floor((status.final_duration_sec || 0) / 60)}分
                  {Math.round((status.final_duration_sec || 0) % 60)}秒
                </div>
              </div>
              <div className="flex gap-2">
                <button
                  className="btn-ghost"
                  onClick={togglePlayFinal}
                >
                  {playingKey === 'final' ? '⏸ 暂停' : '▶ 播放整本'}
                </button>
                <a
                  className="btn-primary"
                  href={status.final_audio_url || '#'}
                  download={`${prep?.book_title || 'book'}_整本.mp3`}
                >
                  ⬇️ 下载整本 MP3
                </a>
              </div>
            </div>
            <audio controls src={status.final_audio_url || undefined} className="w-full" />
          </div>

          <div className="card space-y-2">
            <h3 className="font-semibold mb-3">📜 各章详情</h3>
            <div className="space-y-1 max-h-[500px] overflow-y-auto">
              {status.chapters.map(c => (
                <div
                  key={c.chapter_idx}
                  className="flex items-center justify-between gap-3 py-2 px-3 rounded-lg hover:bg-white/5"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <StatusIcon status={c.status} />
                    <span className="text-white/40 text-xs w-8 shrink-0">#{c.chapter_idx + 1}</span>
                    <span className="text-sm truncate">{c.title || '(无标题)'}</span>
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    {c.duration_ms && (
                      <span className="text-xs text-white/40">
                        {(c.duration_ms / 1000).toFixed(1)}s
                      </span>
                    )}
                    {c.status === 'done' && c.audio_url && (
                      <button
                        className="chip bg-white/10 hover:bg-white/20 text-xs"
                        onClick={() => togglePlayChapter(c.chapter_idx, c.audio_url)}
                      >
                        {playingKey === `ch_${c.chapter_idx}` ? '⏸' : '▶'} 试听
                      </button>
                    )}
                    {c.status === 'done' && c.audio_url && (
                      <a
                        className="chip bg-white/10 hover:bg-white/20 text-xs"
                        href={c.audio_url}
                        download={`${String(c.chapter_idx + 1).padStart(3, '0')}_${c.title}.mp3`}
                      >
                        ⬇
                      </a>
                    )}
                    {c.status === 'failed' && c.error_msg && (
                      <span className="text-xs text-red-300" title={c.error_msg}>
                        ❌ 失败
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <button
            className="btn-ghost"
            onClick={() => {
              setPrep(null);
              setFile(null);
              setFileId(null);
              setStatus(null);
              setPhase('upload');
            }}
          >
            ← 再来一本
          </button>
        </div>
      )}
    </section>
  );
}

function StatusIcon({ status }: { status: string }) {
  if (status === 'done') return <span className="text-green-400">✓</span>;
  if (status === 'synthesizing') return <span className="inline-block w-3 h-3 border-2 border-brand-400 border-t-transparent rounded-full animate-spin" />;
  if (status === 'failed') return <span className="text-red-400">✗</span>;
  return <span className="text-white/30">○</span>;
}
