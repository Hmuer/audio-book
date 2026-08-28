'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ProjectDetailResp } from '@/lib/api';

// 4 步向导
type WizardStep = 1 | 2 | 3 | 4;

const STEP_LABELS = ['填写项目名', '导入小说', '识别中', '完成'];

// Step2 导入方式：文件上传 / 粘贴文本
type ImportMode = 'file' | 'text';

/**
 * ElevenLabs 风格新建项目向导
 *  - 顶部步骤：step-dot + 连接线 + 当前渐变光晕
 *  - 卡片：玻璃态 + 角标光晕（左上紫 / 右下青柠）
 *  - 文件拖入区：虚线圆角 + 大图标 + 激活紫色发光
 *  - 进度条：Eleven 三色渐变（紫→蓝→青）
 */

export default function ProjectWizard() {
  const [step, setStep] = useState<WizardStep>(1);
  const [err, setErr] = useState<string | null>(null);

  // Step 1
  const [name, setName] = useState<string>('我的有声书');

  // Step 2
  const [importMode, setImportMode] = useState<ImportMode>('file');
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [pastedText, setPastedText] = useState<string>('');
  const [textFilenameHint, setTextFilenameHint] = useState<string>('');

  // Step 3 / 4 共享
  const [projectId, setProjectId] = useState<string | null>(null);
  const [prepareMsg, setPrepareMsg] = useState<string>('正在上传文件…');
  const [detail, setDetail] = useState<ProjectDetailResp | null>(null);
  const pollAbortRef = useRef<{ aborted: boolean }>({ aborted: false });
  const pollTimerRef = useRef<number | null>(null);

  const goStep2 = () => {
    if (!name.trim()) {
      setErr('请填写项目名');
      return;
    }
    setErr(null);
    setStep(2);
  };

  const onPickFile = (f: File | null) => {
    setErr(null);
    if (!f) return;
    if (f.size > 50 * 1024 * 1024) {
      setErr(`文件过大（${(f.size / 1024 / 1024).toFixed(1)}MB），上限 50MB`);
      return;
    }
    setFile(f);
  };

  const stageLabel = (stg?: string | null, prog?: ProjectDetailResp['prepare_progress']) => {
    if (!stg) return '正在启动识别任务…';
    switch (stg) {
      case 'start': return '识别任务已启动，读取源文件…';
      case 'split': return '切章中（按第X章/第X回等正则拆分）…';
      case 'characters': {
        const t = prog?.char_slice_total ?? 0;
        const n = prog?.char_slice_completed_n ?? 0;
        return t ? `角色识别中：切片 ${n}/${t}（可能需要几分钟）…` : '角色识别中…';
      }
      case 'dedup': return '角色去重与别名合并中…';
      case 'dialogues': {
        const t = prog?.dialogue_total_batches ?? 0;
        const n = prog?.dialogue_completed_batches_count ?? 0;
        return t ? `对白归属批处理：批次 ${n}/${t}…` : '对白归属识别中…';
      }
      case 'voice_recs': return '根据角色自动推荐音色中…';
      case 'done': return '识别完成，保存结果中…';
      default: return `识别中（stage: ${stg}）…`;
    }
  };

  const stopPoll = useCallback(() => {
    pollAbortRef.current.aborted = true;
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  useEffect(() => () => stopPoll(), [stopPoll]);

  const goStep3 = async () => {
    let valid = true;
    if (importMode === 'file' && !file) {
      setErr('请先选择 .txt / .md / .epub 文件');
      valid = false;
    }
    if (importMode === 'text' && !pastedText.trim()) {
      setErr('请先粘贴小说正文内容');
      valid = false;
    }
    if (!valid) return;
    setErr(null);
    setDetail(null);
    stopPoll();
    pollAbortRef.current = { aborted: false };
    setStep(3);
    setPrepareMsg('正在创建项目…');

    let pid: string | null = null;
    try {
      const proj = await api.projectCreate(name.trim());
      pid = proj.project_id;
      setProjectId(pid);

      if (importMode === 'file' && file) {
        setPrepareMsg('项目已创建，正在上传文件…');
        await api.projectImport(pid, file);
      } else if (importMode === 'text') {
        setPrepareMsg('项目已创建，正在保存粘贴内容…');
        await api.projectImportText(
          pid,
          pastedText,
          textFilenameHint.trim() || 'pasted_text.txt',
        );
      }

      setPrepareMsg('导入完成，正在启动后台识别…');
      await api.projectPrepare(pid);
      setPrepareMsg('后台任务已启动，首次识别可能需 1-10 分钟，请勿关闭页面…');

      const tick = async () => {
        if (pollAbortRef.current.aborted || !pid) return;
        try {
          const d = await api.projectGet(pid);
          setDetail(d);
          const prog = d.prepare_progress ?? null;
          if (prog?.last_error) {
            stopPoll();
            const t = prog.last_error_type ? `[${prog.last_error_type}] ` : '';
            const at = prog.last_error_at ? `（${prog.last_error_at}）` : '';
            setErr(`识别失败${at}: ${t}${prog.last_error}`);
            setStep(2);
            return;
          }
          if (d.status === 'failed') {
            stopPoll();
            setErr('识别失败（项目状态置为 failed，请在列表中删除或重试）');
            setStep(2);
            return;
          }
          if (d.status === 'ready') {
            stopPoll();
            setPrepareMsg('识别完成 ✓');
            setStep(4);
            return;
          }
          setPrepareMsg(stageLabel(prog?.stage, prog));
        } catch (e: any) {
          console.warn('[wizard] poll projectGet fail', e);
        }
        if (!pollAbortRef.current.aborted) {
          pollTimerRef.current = window.setTimeout(tick, 2000);
        }
      };

      pollTimerRef.current = window.setTimeout(tick, 800);
    } catch (e: any) {
      stopPoll();
      setErr(String(e?.message || e));
      setStep(2);
    }
  };

  const enterProject = () => {
    if (projectId) {
      window.location.hash = `#/projects/${projectId}`;
    }
  };

  const cancel = () => {
    window.location.hash = '#/projects';
  };

  return (
    <section className="space-y-6 max-w-3xl mx-auto animate-fade-in">
      {/* 顶部步骤条 — Eleven 风格：step-dot + 状态色 + 连线 */}
      <div className="glass-panel p-5 sm:p-6 relative overflow-hidden">
        <div className="glow-orb w-48 h-48 bg-brand-500/15" style={{ top: '-30px', left: '-30px' }} />
        <div className="glow-orb w-56 h-56 bg-accent-lime/5" style={{ bottom: '-40px', right: '-40px' }} />

        <div className="flex items-center justify-between mb-5 relative">
          <div>
            <h2 className="headline text-xl sm:text-2xl">新建有声书项目</h2>
            <p className="text-[13px] text-ink-600 mt-1">
              四步即可把小说转换为可听的 MP3 有声书
            </p>
          </div>
          <button className="btn-ghost !px-3 !py-1.5 text-sm" onClick={cancel}>取消</button>
        </div>

        {/* Step dots */}
        <div className="flex items-center w-full relative">
          {STEP_LABELS.map((label, i) => {
            const n = (i + 1) as WizardStep;
            const isCurrent = step === n;
            const isDone = step > n;
            return (
              <div key={n} className="flex items-center flex-1 last:flex-none">
                <div className="flex items-center gap-2 sm:gap-3 min-w-0">
                  <div
                    className={`step-dot shrink-0 ${isDone ? 'done' : ''} ${isCurrent ? 'active' : ''}`}
                  >
                    {isDone ? (
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M20 6 9 17l-5-5" />
                      </svg>
                    ) : n}
                  </div>
                  <div className="min-w-0 hidden sm:block">
                    <div className={`text-[13px] font-medium leading-tight ${isCurrent ? 'text-ink-900' : isDone ? 'text-ink-700' : 'text-ink-600'}`}>
                      {label}
                    </div>
                    <div className={`text-[11px] mt-0.5 ${isCurrent ? 'text-brand-300' : 'text-ink-500/80'}`}>
                      Step {n} / 4
                    </div>
                  </div>
                </div>
                {i < STEP_LABELS.length - 1 && (
                  <div className="flex-1 mx-2 sm:mx-4 h-px rounded-full overflow-hidden bg-ink-300/70">
                    <div
                      className="h-full transition-all duration-500"
                      style={{
                        width: step > n ? '100%' : '0%',
                        backgroundImage: 'linear-gradient(90deg, #8b5cf6, #2dd4bf)',
                      }}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {err && (
        <div className="rounded-2xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200 backdrop-blur-sm shadow-el animate-shake">
          {err}
        </div>
      )}

      {/* ============ Step 1: 项目名 ============ */}
      {step === 1 && (
        <div className="glass-panel p-5 sm:p-6 space-y-5 animate-slide-in-right">
          <div>
            <div className="flex items-center gap-2 mb-2">
              <span className="chip-soft !py-0.5 !px-2">📚 基本信息</span>
              <span className="text-xs text-ink-500">给你的有声书一个名字</span>
            </div>
            <label className="block text-sm text-ink-700 mb-2">项目名称</label>
            <input
              type="text"
              className="input !py-3 !text-[15px]"
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="例如：三体 · 第一部"
              maxLength={80}
              autoFocus
            />
            <p className="text-[11px] text-ink-500 mt-2">
              项目名仅作为本地标识，不影响最终生成的 MP3 文件名。
            </p>
          </div>
          <div className="flex justify-end gap-2 pt-1">
            <button className="btn-ghost" onClick={cancel}>取消</button>
            <button className="btn-primary" onClick={goStep2}>下一步 →</button>
          </div>
        </div>
      )}

      {/* ============ Step 2: 导入小说 ============ */}
      {step === 2 && (
        <div className="glass-panel p-5 sm:p-6 space-y-5 animate-slide-in-right relative overflow-hidden">
          <div className="glow-orb w-48 h-48 bg-accent-teal/10" style={{ bottom: '-40px', right: '-30px' }} />

          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="chip-soft !py-0.5 !px-2">📥 导入小说</span>
            </div>

            {/* 模式切换：pill-tab */}
            <div className="inline-flex p-1 rounded-2xl bg-white/[0.04] border border-white/[0.06]">
              <button
                className={`px-4 py-2 rounded-xl text-sm transition-all duration-200 flex items-center gap-2 ${
                  importMode === 'file'
                    ? 'bg-brand-500 text-white shadow-brand'
                    : 'text-ink-600 hover:text-ink-800'
                }`}
                onClick={() => { setImportMode('file'); setErr(null); }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <path d="M14 2v6h6" />
                </svg>
                上传文件
              </button>
              <button
                className={`px-4 py-2 rounded-xl text-sm transition-all duration-200 flex items-center gap-2 ${
                  importMode === 'text'
                    ? 'bg-brand-500 text-white shadow-brand'
                    : 'text-ink-600 hover:text-ink-800'
                }`}
                onClick={() => { setImportMode('text'); setErr(null); }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 20h9" />
                  <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
                </svg>
                粘贴文本内容
              </button>
            </div>
          </div>

          {importMode === 'file' && (
            <div>
              <label className="block text-sm text-ink-700 mb-2">上传小说文件（TXT / MD / EPUB）</label>
              <label
                className={`relative block rounded-3xl border-2 border-dashed p-8 sm:p-12 text-center cursor-pointer transition-all duration-200 overflow-hidden ${
                  dragOver
                    ? 'border-brand-500/80 bg-brand-500/10 shadow-brand'
                    : 'border-ink-300/80 hover:border-brand-500/60 hover:bg-brand-500/[0.04]'
                }`}
                onDragOver={e => {
                  e.preventDefault();
                  setDragOver(true);
                }}
                onDragLeave={() => setDragOver(false)}
                onDrop={e => {
                  e.preventDefault();
                  setDragOver(false);
                  onPickFile(e.dataTransfer.files?.[0] || null);
                }}
              >
                {dragOver && (
                  <div className="absolute inset-0 bg-brand-500/5 pointer-events-none animate-pulse-soft" />
                )}
                <input
                  type="file"
                  accept=".txt,.text,.md,.epub"
                  className="hidden"
                  onChange={e => onPickFile(e.target.files?.[0] || null)}
                />
                {file ? (
                  <div className="space-y-2 relative">
                    <div className="mx-auto w-14 h-14 rounded-2xl grid place-items-center bg-brand-500/15 border border-brand-500/30">
                      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#c4b5fd" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                        <path d="M14 2v6h6" />
                      </svg>
                    </div>
                    <div className="text-base font-semibold text-ink-800">{file.name}</div>
                    <div className="text-xs text-ink-500">{(file.size / 1024).toFixed(1)} KB · 已就绪</div>
                  </div>
                ) : (
                  <div className="space-y-3 relative">
                    <div className="mx-auto w-16 h-16 rounded-2xl grid place-items-center bg-white/[0.03] border border-white/[0.07]">
                      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="text-ink-600">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                        <polyline points="17 8 12 3 7 8" />
                        <line x1="12" y1="3" x2="12" y2="15" />
                      </svg>
                    </div>
                    <div className="text-ink-700 text-[15px] font-medium">
                      点击或拖拽 TXT / Markdown / EPUB 文件到这里
                    </div>
                    <div className="text-xs text-ink-500">
                      推荐 UTF-8 编码，最大 50MB
                    </div>
                  </div>
                )}
              </label>
              {file && (
                <div className="mt-2 text-xs text-ink-500">
                  文件名将自动用作书名，可在项目详情中修改。
                </div>
              )}
            </div>
          )}

          {importMode === 'text' && (
            <div className="space-y-4">
              <div>
                <label className="block text-sm text-ink-700 mb-2">书名（可选）</label>
                <input
                  type="text"
                  className="input"
                  value={textFilenameHint}
                  onChange={e => setTextFilenameHint(e.target.value)}
                  placeholder="例如：三体 · 第一部（留空则显示为 粘贴文本）"
                  maxLength={100}
                />
              </div>
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="block text-sm text-ink-700">粘贴小说正文内容</label>
                  <span
                    className={`chip ${
                      pastedText.length > 50 * 1024 * 1024 / 3
                        ? 'bg-red-500/20 text-red-300'
                        : 'bg-white/[0.05] text-ink-600'
                    } text-[11px] border border-white/[0.06]`}
                  >
                    {pastedText.length.toLocaleString()} 字
                  </span>
                </div>
                <textarea
                  value={pastedText}
                  onChange={e => setPastedText(e.target.value)}
                  placeholder="在此粘贴整本小说正文（支持中文自动识别章节，推荐带「第一章」「第1章」等标题标记）…"
                  className="textarea min-h-[380px] font-mono text-[13px] leading-relaxed"
                />
                <p className="text-[11px] text-ink-500 mt-2">
                  支持从浏览器/记事本/WPS 直接全选复制粘贴，内容会自动保存到项目中。
                </p>
              </div>
            </div>
          )}

          <div className="flex justify-between gap-2 pt-1">
            <button className="btn-ghost" onClick={() => setStep(1)}>← 上一步</button>
            <button
              className="btn-primary"
              disabled={
                (importMode === 'file' && !file) ||
                (importMode === 'text' && !pastedText.trim())
              }
              onClick={goStep3}
            >
              {(importMode === 'file' && file) || (importMode === 'text' && pastedText.trim())
                ? '🚀 导入并识别 →'
                : importMode === 'file'
                  ? '请先选择文件'
                  : '请先粘贴内容'
              }
            </button>
          </div>
        </div>
      )}

      {/* ============ Step 3: 识别中 ============ */}
      {step === 3 && (
        <div className="glass-panel text-center py-14 sm:py-16 space-y-5 relative overflow-hidden">
          <div className="glow-orb w-64 h-64 bg-brand-500/20" style={{ top: '-40px', left: '50%', transform: 'translateX(-50%)' }} />
          <div className="relative mx-auto w-16 h-16 grid place-items-center">
            <span className="absolute inset-0 rounded-full animate-ping bg-brand-500/25" style={{ animationDuration: '2.2s' }} />
            <div className="relative w-14 h-14 grid place-items-center rounded-full shadow-brand"
              style={{
                backgroundImage: 'linear-gradient(180deg, rgba(255,255,255,0.14) 0%, rgba(255,255,255,0) 50%), linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)',
              }}
            >
              <span className="inline-block w-6 h-6 border-[3px] border-white/40 border-t-white rounded-full animate-spin" />
            </div>
          </div>
          <div className="relative text-lg sm:text-xl font-semibold text-ink-800">{prepareMsg}</div>
          <div className="relative text-sm text-ink-500 max-w-md mx-auto">
            首次处理整本小说可能需要 1-10 分钟（取决于字数和章节数）<br />
            请勿关闭页面
          </div>
          {/* 进度骨架条 */}
          <div className="relative max-w-sm mx-auto mt-3">
            <div className="progress-track"><div className="progress-fill animate-shimmer" style={{ width: '60%' }} /></div>
          </div>
        </div>
      )}

      {/* ============ Step 4: 完成 ============ */}
      {step === 4 && detail && (
        <div className="glass-panel p-5 sm:p-6 space-y-5 animate-slide-in-right relative overflow-hidden">
          <div className="glow-orb w-60 h-60 bg-accent-lime/10" style={{ bottom: '-50px', right: '-40px' }} />

          <div className="text-center py-4 relative">
            <div className="mx-auto mb-4 w-20 h-20 rounded-full grid place-items-center animate-scale-in"
              style={{
                background: 'linear-gradient(135deg, #c6f44a 0%, #2dd4bf 100%)',
                boxShadow: '0 16px 40px -10px rgba(45,212,191,0.45)',
              }}
            >
              <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#0f0f12" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                <path d="M20 6 9 17l-5-5" />
              </svg>
            </div>
            <h3 className="headline text-2xl">识别完成</h3>
            <p className="text-sm text-ink-600 mt-2 max-w-md mx-auto">
              「{detail.book_title || name}」已就绪，可以进入项目配置音色并开始生成
            </p>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 relative">
            {[
              { label: '章节数', value: detail.chapter_count, color: '#8b5cf6', icon: '📜' },
              { label: '角色数', value: detail.characters.length, color: '#ec4899', icon: '🧑' },
              { label: '字数（约）', value: detail.source_file_size ? `${Math.round(detail.source_file_size / 2 / 1000)}K` : '—', color: '#0ea5e9', icon: '✍️' },
              { label: '识别耗时', value: detail.updated_at ? '就绪' : '—', color: '#c6f44a', icon: '⚡' },
            ].map((it, i) => (
              <div key={i} className="rounded-2xl p-4 border border-white/[0.06] bg-white/[0.025] hover:bg-white/[0.05] transition-colors">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs text-ink-500">{it.label}</span>
                  <span className="text-base opacity-70">{it.icon}</span>
                </div>
                <div
                  className="text-2xl font-bold font-display tracking-tight"
                  style={{
                    background: `linear-gradient(135deg, ${it.color} 0%, ${it.color}aa 100%)`,
                    WebkitBackgroundClip: 'text',
                    WebkitTextFillColor: 'transparent',
                  }}
                >
                  {it.value}
                </div>
              </div>
            ))}
          </div>

          <div className="flex justify-end gap-2 pt-2 relative">
            <button className="btn-ghost" onClick={cancel}>
              返回项目列表
            </button>
            <button className="btn-primary" onClick={enterProject}>
              进入项目 →
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
