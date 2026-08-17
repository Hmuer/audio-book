'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ProjectDetailResp } from '@/lib/api';

// 4 步向导
type WizardStep = 1 | 2 | 3 | 4;

const STEP_LABELS = ['填写项目名', '导入小说', '识别中', '完成'];

// Step2 导入方式：文件上传 / 粘贴文本
type ImportMode = 'file' | 'text';

export default function ProjectWizard() {
  const [step, setStep] = useState<WizardStep>(1);
  const [err, setErr] = useState<string | null>(null);

  // Step 1
  const [name, setName] = useState<string>('我的有声书');

  // Step 2: 支持上传文件 + 粘贴文本两种模式
  const [importMode, setImportMode] = useState<ImportMode>('file');
  // 文件上传
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  // 粘贴文本
  const [pastedText, setPastedText] = useState<string>('');
  const [textFilenameHint, setTextFilenameHint] = useState<string>('');

  // Step 3 / 4 共享
  const [projectId, setProjectId] = useState<string | null>(null);
  const [prepareMsg, setPrepareMsg] = useState<string>('正在上传文件…');
  // 原 prepareResult 不再保存旧版 ProjectPrepareResp（接口已改成 202+后台任务），
  // 统一改成轮询拿到的 ProjectDetailResp（含 chapters/characters/prepare_progress）
  const [detail, setDetail] = useState<ProjectDetailResp | null>(null);
  // 防止用户离开/出错后轮询仍继续跑
  const pollAbortRef = useRef<{ aborted: boolean }>({ aborted: false });
  // 轮询计时器
  const pollTimerRef = useRef<number | null>(null);

  // ---------- Step 1 → 2 ----------
  const goStep2 = () => {
    if (!name.trim()) {
      setErr('请填写项目名');
      return;
    }
    setErr(null);
    setStep(2);
  };

  // ---------- 文件选择（点击 + 拖拽） ----------
  const onPickFile = (f: File | null) => {
    setErr(null);
    if (!f) return;
    if (f.size > 50 * 1024 * 1024) {
      setErr(`文件过大（${(f.size / 1024 / 1024).toFixed(1)}MB），上限 50MB`);
      return;
    }
    setFile(f);
  };

  // stage 中文名（给 Step3 文案更新用）
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

  // 停止轮询
  const stopPoll = useCallback(() => {
    pollAbortRef.current.aborted = true;
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  // 卸载时清理
  useEffect(() => () => stopPoll(), [stopPoll]);

  // ---------- Step 2 → 3：创建项目 + 导入（文件/文本二选一） + 触发 prepare（202） + 轮询 ----------
  const goStep3 = async () => {
    let valid = true;
    if (importMode === 'file' && !file) {
      setErr('请先选择 TXT 文件');
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
      // 1. 创建项目
      const proj = await api.projectCreate(name.trim());
      pid = proj.project_id;
      setProjectId(pid);

      // 2. 导入（文件 / 文本二选一）
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

      // 3. 触发 prepare（HTTP 202 Accepted，立即返回，真实执行在后台 asyncio.create_task）
      await api.projectPrepare(pid);

      // 4. 轮询 project 详情：status 变成 ready/failed，或 last_error 非空
      setPrepareMsg('后台任务已启动，首次识别可能需 1-10 分钟，请勿关闭页面…');

      const tick = async () => {
        if (pollAbortRef.current.aborted || !pid) return;
        try {
          const d = await api.projectGet(pid);
          setDetail(d);
          const prog = d.prepare_progress ?? null;
          // 失败：优先按 last_error
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
          // preparing / imported / draft：按 stage 更新文案
          setPrepareMsg(stageLabel(prog?.stage, prog));
        } catch (e: any) {
          // 轮询单次失败不直接退出，记日志；连续 15 次（~30s）都失败再报错
          // 这里简化：直接吞掉继续轮询
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
      // 出错回退到 step 2 让用户重试（已创建项目的话保留 projectId，可后续在列表里删）
      setStep(2);
    }
  };

  // ---------- Step 4：进入项目 ----------
  const enterProject = () => {
    if (projectId) {
      window.location.hash = `#/projects/${projectId}`;
    }
  };

  // ---------- 取消，返回列表 ----------
  const cancel = () => {
    window.location.hash = '#/projects';
  };

  return (
    <section className="space-y-6 max-w-3xl mx-auto">
      {/* 顶部进度条 */}
      <div className="card">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">✨ 新建项目向导</h2>
          <button className="btn-ghost" onClick={cancel}>取消</button>
        </div>
        <div className="flex items-center gap-2">
          {STEP_LABELS.map((label, i) => {
            const n = (i + 1) as WizardStep;
            const isCurrent = step === n;
            const isDone = step > n;
            return (
              <div key={n} className="flex items-center gap-2 flex-1">
                <div
                  className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm flex-1 ${
                    isCurrent
                      ? 'bg-brand-600 text-white'
                      : isDone
                      ? 'bg-white/10 text-white/80'
                      : 'bg-white/5 text-white/40'
                  }`}
                >
                  <span className="w-5 h-5 rounded-full grid place-items-center text-xs font-mono">
                    {isDone ? '✓' : n}
                  </span>
                  <span className="truncate">{label}</span>
                </div>
                {i < STEP_LABELS.length - 1 && (
                  <span className="w-3 h-px bg-white/20 shrink-0" />
                )}
              </div>
            );
          })}
        </div>
        {/* 进度条 */}
        <div className="mt-3 w-full h-1 bg-white/10 rounded-full overflow-hidden">
          <div
            className="h-full bg-gradient-to-r from-brand-500 to-blue-500 transition-all duration-300"
            style={{ width: `${(step / 4) * 100}%` }}
          />
        </div>
      </div>

      {err && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          {err}
        </div>
      )}

      {/* ============ Step 1: 项目名 ============ */}
      {step === 1 && (
        <div className="card space-y-4">
          <div>
            <label className="block text-sm text-white/70 mb-2">项目名称</label>
            <input
              type="text"
              className="input"
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="例如：三体 · 第一部"
              maxLength={80}
              autoFocus
            />
            <p className="text-xs text-white/40 mt-2">
              项目名仅作为本地标识，不影响最终生成的 MP3 文件名。
            </p>
          </div>
          <div className="flex justify-end gap-2">
            <button className="btn-ghost" onClick={cancel}>取消</button>
            <button className="btn-primary" onClick={goStep2}>下一步 →</button>
          </div>
        </div>
      )}

      {/* ============ Step 2: 导入小说（支持文件上传 + 粘贴文本） ============ */}
      {step === 2 && (
        <div className="card space-y-4">
          {/* 模式切换：文件 / 粘贴文本 */}
          <div className="flex gap-1 p-1 rounded-xl bg-white/5 border border-white/10 w-fit">
            <button
              className={`px-4 py-1.5 rounded-lg text-sm transition ${
                importMode === 'file'
                  ? 'bg-brand-600 text-white shadow-sm'
                  : 'text-white/60 hover:text-white/90'
              }`}
              onClick={() => { setImportMode('file'); setErr(null); }}
            >
              📁 上传 TXT 文件
            </button>
            <button
              className={`px-4 py-1.5 rounded-lg text-sm transition ${
                importMode === 'text'
                  ? 'bg-brand-600 text-white shadow-sm'
                  : 'text-white/60 hover:text-white/90'
              }`}
              onClick={() => { setImportMode('text'); setErr(null); }}
            >
              📝 粘贴文本内容
            </button>
          </div>

          {/* 文件上传模式 */}
          {importMode === 'file' && (
            <div>
              <label className="block text-sm text-white/70 mb-2">上传 TXT 小说文件</label>
              <label
                className={`block border-2 border-dashed rounded-xl p-10 text-center cursor-pointer transition ${
                  dragOver
                    ? 'border-brand-500 bg-brand-500/10'
                    : 'border-white/20 hover:border-brand-500/60 hover:bg-brand-500/5'
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
              {file && (
                <div className="mt-2 text-xs text-white/50">
                  文件名将自动用作书名，可在项目详情中修改。
                </div>
              )}
            </div>
          )}

          {/* 粘贴文本模式 */}
          {importMode === 'text' && (
            <div className="space-y-3">
              <div>
                <label className="block text-sm text-white/70 mb-2">书名（可选）</label>
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
                  <label className="block text-sm text-white/70">粘贴小说正文内容</label>
                  <span
                    className={`chip ${
                      pastedText.length > 50 * 1024 * 1024 / 3  // 约 50MB UTF-8 中文上限
                        ? 'bg-red-500/20 text-red-300'
                        : 'bg-white/10 text-white/60'
                    } text-xs`}
                  >
                    {pastedText.length} 字
                  </span>
                </div>
                <textarea
                  value={pastedText}
                  onChange={e => setPastedText(e.target.value)}
                  placeholder="在此粘贴整本小说正文（支持中文自动识别章节，推荐带「第一章」「第1章」等标题标记）…"
                  className="textarea min-h-[380px] font-mono text-sm leading-relaxed"
                />
                <p className="text-xs text-white/50 mt-2">
                  支持从浏览器/记事本/WPS 直接全选复制粘贴，内容会自动保存到项目中。
                </p>
              </div>
            </div>
          )}

          <div className="flex justify-between gap-2 pt-2">
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
        <div className="card text-center py-12 space-y-3">
          <div className="inline-block w-12 h-12 border-4 border-brand-500 border-t-transparent rounded-full animate-spin" />
          <div className="text-lg font-medium">{prepareMsg}</div>
          <div className="text-sm text-white/50">
            首次处理整本小说可能需要 1-5 分钟（取决于字数和章节数）<br />
            请勿关闭页面
          </div>
        </div>
      )}

      {/* ============ Step 4: 完成 ============ */}
      {step === 4 && detail && (
        <div className="card space-y-4">
          <div className="text-center py-4">
            <div className="text-5xl mb-3">🎉</div>
            <h3 className="text-lg font-semibold">识别完成</h3>
            <p className="text-sm text-white/60 mt-1">
              「{detail.book_title || name}」已就绪，可以进入项目配置音色并开始生成
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-xl bg-white/5 border border-white/10 p-4 text-center">
              <div className="text-2xl font-bold text-brand-300">
                {detail.chapter_count}
              </div>
              <div className="text-xs text-white/50 mt-1">章节数</div>
            </div>
            <div className="rounded-xl bg-white/5 border border-white/10 p-4 text-center">
              <div className="text-2xl font-bold text-brand-300">
                {detail.characters.length}
              </div>
              <div className="text-xs text-white/50 mt-1">角色数</div>
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-2">
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
