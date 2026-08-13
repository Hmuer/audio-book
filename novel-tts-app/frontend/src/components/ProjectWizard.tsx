'use client';

import { useState } from 'react';
import { api, ProjectPrepareResp } from '@/lib/api';

// 4 步向导
type WizardStep = 1 | 2 | 3 | 4;

const STEP_LABELS = ['填写项目名', '上传 TXT', '识别中', '完成'];

export default function ProjectWizard() {
  const [step, setStep] = useState<WizardStep>(1);
  const [err, setErr] = useState<string | null>(null);

  // Step 1
  const [name, setName] = useState<string>('我的有声书');

  // Step 2
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);

  // Step 3 / 4 共享
  const [projectId, setProjectId] = useState<string | null>(null);
  const [prepareMsg, setPrepareMsg] = useState<string>('正在上传文件…');
  const [prepareResult, setPrepareResult] = useState<ProjectPrepareResp | null>(null);

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

  // ---------- Step 2 → 3：创建项目 + 上传 + prepare ----------
  const goStep3 = async () => {
    if (!file) {
      setErr('请先选择 TXT 文件');
      return;
    }
    setErr(null);
    setStep(3);
    setPrepareMsg('正在创建项目…');
    try {
      // 1. 创建项目
      const proj = await api.projectCreate(name.trim());
      setProjectId(proj.project_id);
      setPrepareMsg('项目已创建，正在上传文件…');

      // 2. 上传文件
      await api.projectImport(proj.project_id, file);
      setPrepareMsg('上传完成，正在识别章节与角色（可能需 1-5 分钟）…');

      // 3. 触发 prepare
      const r = await api.projectPrepare(proj.project_id);
      setPrepareResult(r);
      setStep(4);
    } catch (e: any) {
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

      {/* ============ Step 2: 上传文件 ============ */}
      {step === 2 && (
        <div className="card space-y-4">
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
          </div>
          <div className="flex justify-between gap-2">
            <button className="btn-ghost" onClick={() => setStep(1)}>← 上一步</button>
            <button
              className="btn-primary"
              disabled={!file}
              onClick={goStep3}
            >
              {file ? '🚀 上传并识别 →' : '请先选择文件'}
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
      {step === 4 && prepareResult && (
        <div className="card space-y-4">
          <div className="text-center py-4">
            <div className="text-5xl mb-3">🎉</div>
            <h3 className="text-lg font-semibold">识别完成</h3>
            <p className="text-sm text-white/60 mt-1">
              「{prepareResult.book_title || name}」已就绪，可以进入项目配置音色并开始生成
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-xl bg-white/5 border border-white/10 p-4 text-center">
              <div className="text-2xl font-bold text-brand-300">
                {prepareResult.total_chapters}
              </div>
              <div className="text-xs text-white/50 mt-1">章节数</div>
            </div>
            <div className="rounded-xl bg-white/5 border border-white/10 p-4 text-center">
              <div className="text-2xl font-bold text-brand-300">
                {prepareResult.characters.length}
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
