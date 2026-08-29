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
        {/* 头部：Edtech 简洁 · 冷灰 Eyebrow + 步骤条 */}
        <div className="relative px-6 pt-5 pb-4"
          style={{
            borderBottom: '1px solid rgb(var(--ink-300))',
            background: 'rgb(var(--ink-50))',
          }}
        >
          {/* 顶部 Eyebrow */}
          <div className="flex items-center justify-between mb-3">
            <div className="inline-flex items-center px-2 py-0.5"
              style={{
                borderRadius: 'var(--radius-xs)',
                background: 'rgb(var(--ink-200))',
                border: '1px solid rgb(var(--ink-300))',
              }}
            >
              <span className="text-[10.5px] uppercase tracking-[0.08em] font-medium"
                style={{ color: 'rgb(var(--ink-600))' }}
              >步骤 1 / 3</span>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="w-7 h-7 grid place-items-center transition-all text-white/55 hover:text-white hover:bg-white/[0.06]"
              style={{ borderRadius: 'var(--radius-xs)' }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>

          <h3 className="text-[17px] font-semibold text-white leading-tight">新建有声书</h3>
          <p className="text-[12.5px] mt-0.5" style={{ color: 'rgb(var(--ink-500))' }}>
            命名项目 · 导入文本 · AI 自动合成
          </p>

          {/* Stepper（水平 3 步条 · 冷灰） */}
          <div className="mt-4 grid grid-cols-3 gap-2">
            {['命名项目', '导入内容', 'AI 合成'].map((t, i) => (
              <div key={t} className="flex items-center gap-2">
                <div className="w-6 h-6 shrink-0 grid place-items-center text-[11px] font-semibold"
                  style={{
                    borderRadius: 'var(--radius-xs)',
                    background: i === 0
                      ? 'rgb(var(--brand-600))'
                      : 'rgb(var(--ink-300))',
                    boxShadow: i === 0 ? '0 0 0 3px rgb(var(--brand-500) / 0.18)' : undefined,
                    color: '#fff',
                  }}
                >{i + 1}</div>
                <div className="min-w-0">
                  <div className="text-[11.5px] font-medium leading-tight truncate"
                    style={{ color: i === 0 ? 'rgb(var(--ink-900))' : 'rgb(var(--ink-700) / 0.55)' }}
                  >{t}</div>
                  <div className="text-[10px] font-medium mt-0.5"
                    style={{ color: i === 0 ? 'rgb(var(--brand-400))' : 'rgb(var(--ink-700) / 0.35)' }}
                  >{i === 0 ? '当前' : '待办'}</div>
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

            {/* 模式切换（Edtech segmented control · 冷灰） */}
            <div
              className="grid grid-cols-2 p-1"
              style={{
                borderRadius: 'var(--radius-sm)',
                background: 'rgb(var(--ink-0))',
                border: '1px solid rgb(var(--ink-300))',
              }}
            >
              {([
                { k: 'file', iconName: 'upload', label: '上传文件', desc: 'TXT / EPUB' },
                { k: 'text', iconName: 'pen',    label: '粘贴文本', desc: '直接粘贴原文' },
              ] as { k: Mode; iconName: 'upload' | 'pen'; label: string; desc: string }[]).map(it => {
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
                      background: active ? 'rgb(var(--brand-600))' : 'transparent',
                      color: active ? '#fff' : 'rgb(var(--ink-700) / 0.75)',
                      boxShadow: active ? '0 0 0 1px rgb(var(--brand-500) / 0.4)' : undefined,
                    }}
                  >
                    <span className={`w-8 h-8 shrink-0 grid place-items-center rounded-[6px] ${
                      active ? 'bg-white/15' : 'bg-white/[0.04]'
                    }`}>
                      <SegmentIcon name={it.iconName} />
                    </span>
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
                className={`cursor-pointer rounded-md border-2 border-dashed transition-all text-center py-9 px-4 ${
                  dragOver
                    ? 'border-brand-400 bg-brand-500/10'
                    : 'border-white/10 hover:border-white/20 hover:bg-white/[0.02]'
                }`}
                style={{ borderRadius: 'var(--radius-md)' }}
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
                    <div className="mx-auto w-10 h-10 grid place-items-center mb-2"
                      style={{
                        background: 'rgb(var(--brand-600) / 0.18)',
                        borderRadius: 'var(--radius-sm)',
                        color: 'rgb(var(--brand-400))',
                      }}
                    >
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/>
                      </svg>
                    </div>
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
                    <div className="mx-auto w-12 h-12 grid place-items-center mb-3"
                      style={{
                        background: 'rgb(var(--ink-200))',
                        borderRadius: 'var(--radius-md)',
                        border: '1px solid rgb(var(--ink-300))',
                        color: 'rgb(var(--ink-600))',
                      }}
                    >
                      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
                      </svg>
                    </div>
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
          <div className="px-6 py-4 border-t border-ink-300/60 flex gap-2 justify-end">
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

function SegmentIcon({ name }: { name: 'upload' | 'pen' }) {
  const common = {
    width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.8,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  };
  if (name === 'upload')
    return (<svg {...common}><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>);
  return (<svg {...common}><path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/><path d="M2 2l7.586 7.586"/><circle cx="11" cy="11" r="2"/></svg>);
}
