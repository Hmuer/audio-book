'use client';

import { useState, useRef } from 'react';
import { api } from '@/lib/api';

interface Props {
  onClose: () => void;
  /** 创建完成回调，参数为新项目 ID */
  onCreated: (id: string) => void;
}

type Mode = 'file' | 'text';

export default function CreateAudiobookDialog({ onClose, onCreated }: Props) {
  const [mode, setMode] = useState<Mode>('file');
  const [name, setName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);

    if (!name.trim()) { setErr('请输入有声书名称'); return; }
    if (mode === 'file' && !file) { setErr('请选择要上传的文件'); return; }
    if (mode === 'text' && !text.trim()) { setErr('请粘贴小说文本'); return; }

    setBusy(true);
    try {
      // 1. 创建项目
      const project = await api.projectCreate(name.trim());
      const id = project.project_id;

      // 2. 导入内容
      if (mode === 'file' && file) {
        await api.projectImport(id, file);
      } else if (mode === 'text') {
        await api.projectImportText(id, text, `${name.trim()}.txt`);
      }

      onCreated(id);
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) setFile(f);
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center p-4"
      style={{ background: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}>
      <div
        className="bg-ink-100 border border-ink-300 w-full max-w-lg max-h-[88vh] overflow-hidden flex flex-col animate-scale-in"
        style={{ borderRadius: 'var(--radius-lg)' }}
        onClick={e => e.stopPropagation()}
      >
        {/* 头部：Edtech gradient eyebrow + 步骤 */}
        <div className="relative px-6 pt-5 pb-4 overflow-hidden"
          style={{
            borderBottom: '1px solid rgb(var(--ink-300)))',
            backgroundImage:
              'linear-gradient(180deg, rgb(var(--brand-600)0.14), rgb(var(--brand-600)0) 70%)',
          }}
        >
          {/* 顶部胶囊 Eyebrow */}
          <div className="flex items-center justify-between mb-3">
            <div className="inline-flex items-center gap-2 px-2 py-0.5"
              style={{
                borderRadius: 999,
                background: 'rgb(var(--brand-600)0.10)',
                border: '1px solid rgb(var(--brand-500)0.25)',
              }}
            >
              <span className="inline-block w-1.5 h-1.5 rounded-full animate-pulse"
                style={{ background: 'rgb(var(--accent-amber))' }} />
              <span className="text-[10px] uppercase tracking-[0.16em] font-semibold"
                style={{ color: 'rgb(var(--brand-400))' }}
              >CREATE · 01 / 03</span>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="w-7 h-7 grid place-items-center transition-all text-white/55 hover:text-white hover:bg-white/[0.06]"
              style={{ borderRadius: 'var(--radius-xs)' }}
            >✕</button>
          </div>

          <h3 className="text-[18px] font-semibold text-white leading-tight">创建你的第一部有声书</h3>
          <p className="text-[12.5px] mt-1" style={{ color: 'rgb(var(--ink-700)0.65)' }}>
            命名 → 导入文本 → 开始 AI 合成，仅需三步
          </p>

          {/* Stepper（水平 3 步条） */}
          <div className="mt-4 grid grid-cols-3 gap-2">
            {['命名项目', '导入内容', 'AI 合成'].map((t, i) => (
              <div key={t} className="flex items-center gap-2">
                <div className="w-6 h-6 shrink-0 grid place-items-center text-[11px] font-semibold text-white"
                  style={{
                    borderRadius: 999,
                    background: i === 0
                      ? 'rgb(var(--brand-600)))'
                      : 'rgb(var(--ink-300)))',
                    boxShadow: i === 0 ? '0 0 0 3px rgb(var(--brand-500)0.18))' : undefined,
                  }}
                >{i + 1}</div>
                <div className="min-w-0">
                  <div className="text-[11.5px] font-medium leading-tight truncate"
                    style={{ color: i === 0 ? 'rgb(var(--ink-900)))' : 'rgb(var(--ink-700)0.55))' }}
                  >{t}</div>
                  <div className="text-[9.5px] uppercase tracking-[0.12em] mt-0.5"
                    style={{ color: i === 0 ? 'rgb(var(--accent-amber)))' : 'rgb(var(--ink-700)0.35))' }}
                  >{i === 0 ? 'NOW' : 'NEXT'}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <form onSubmit={onSubmit} className="flex-1 overflow-y-auto">
          <div className="px-6 py-5 space-y-5">
            {/* 项目名 */}
            <div>
              <label className="block text-[13px] font-medium mb-1.5"
                style={{ color: 'rgb(var(--ink-900)0.8))' }}>有声书名称
              </label>
              <input
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="例如：神秘峡谷"
                disabled={busy}
                autoFocus
                maxLength={80}
                className="input-base w-full"
              />
            </div>

            {/* 模式切换（Edtech segmented control） */}
            <div
              className="grid grid-cols-2 p-1"
              style={{
                borderRadius: 'var(--radius-sm)',
                background: 'rgb(var(--ink-0)))',
                border: '1px solid rgb(var(--ink-300)))',
              }}
            >
              {([
                { k: 'file', icon: '📁', label: '上传文件', desc: 'TXT / EPUB' },
                { k: 'text', icon: '✏️', label: '粘贴文本', desc: '直接粘贴原文' },
              ] as { k: Mode; icon: string; label: string; desc: string }[]).map(it => {
                const active = mode === it.k;
                return (
                  <button
                    key={it.k}
                    type="button"
                    onClick={() => setMode(it.k)}
                    disabled={busy}
                    className="relative flex items-center gap-2.5 px-3 py-2.5 text-left transition-all"
                    style={{
                      borderRadius: 'calc(calc(var(--radius-sm) - 2px))',
                      background: active ? 'rgb(var(--brand-600)))' : 'transparent',
                      color: active ? '#fff' : 'rgb(var(--ink-700)0.75))',
                      boxShadow: active ? '0 0 0 1px rgb(var(--brand-500)0.4))' : undefined,
                    }}
                  >
                    <span className={`w-8 h-8 shrink-0 grid place-items-center text-[15px] rounded-[6px] ${
                      active ? 'bg-white/15' : 'bg-white/[0.04]'
                    }`}>{it.icon}</span>
                    <span className="min-w-0">
                      <span className="block text-[13px] font-semibold leading-tight">{it.label}</span>
                      <span className={`block text-[11px] mt-0.5 ${active ? 'text-white/75' : 'text-white/35'}`}>{it.desc}</span>
                    </span>
                  </button>
                );
              })}
            </div>

            {/* 内容区 */}
            {mode === 'file' ? (
              <div
                onDragOver={e => { e.preventDefault(); setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`cursor-pointer rounded-md border-2 border-dashed transition-all text-center py-10 px-4 ${
                  dragOver
                    ? 'border-brand-400 bg-brand-500/10'
                    : 'border-white/10 hover:border-white/20 hover:bg-white/[0.02]'
                }`}
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".txt,.md,.epub"
                  onChange={e => setFile(e.target.files?.[0] ?? null)}
                  className="hidden"
                />
                {file ? (
                  <div>
                    <div className="text-2xl mb-2">📄</div>
                    <div className="text-sm text-white font-medium truncate max-w-[300px] mx-auto">{file.name}</div>
                    <div className="text-xs text-white/40 mt-1">{(file.size / 1024).toFixed(1)} KB</div>
                    <button
                      type="button"
                      onClick={e => { e.stopPropagation(); setFile(null); }}
                      className="mt-3 text-xs text-white/40 hover:text-white/70"
                    >
                      重新选择
                    </button>
                  </div>
                ) : (
                  <div>
                    <div className="text-3xl mb-3">📖</div>
                    <div className="text-sm text-white/70 font-medium">拖拽文件到这里，或点击选择</div>
                    <div className="text-xs text-white/40 mt-2">支持 TXT、Markdown、EPUB 格式</div>
                  </div>
                )}
              </div>
            ) : (
              <div>
                <textarea
                  value={text}
                  onChange={e => setText(e.target.value)}
                  disabled={busy}
                  placeholder="直接粘贴小说文本…"
                  rows={10}
                  className="input-base w-full resize-none font-mono text-[13px] leading-relaxed"
                />
                <div className="mt-1.5 text-[11px] text-white/40 text-right">
                  {text.length.toLocaleString()} 字符
                </div>
              </div>
            )}

            {err && (
              <div className="rounded-lg px-3 py-2 text-xs"
                style={{
                  border: '1px solid rgb(var(--status-error-bg) / 0.28)',
                  background: 'rgb(var(--status-error-bg) / 0.06)',
                  color: 'rgb(var(--status-error-fg))',
                }}
              >{err}</div>
            )}
          </div>

          {/* 底部按钮 */}
          <div className="px-6 py-4 border-t border-white/[0.06] flex gap-2 justify-end">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="btn-ghost"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={busy}
              className="btn-primary"
            >
              {busy ? '创建中…' : '创建并进入'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
