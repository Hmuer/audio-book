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
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="bg-ink-50 border border-ink-300/70 rounded-lg w-full max-w-lg max-h-[85vh] overflow-hidden flex flex-col animate-scale-in"
        onClick={e => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="px-6 pt-5 pb-4 border-b border-white/[0.06]">
          <h3 className="text-lg font-semibold text-white">创建有声书</h3>
          <p className="text-xs text-white/40 mt-1">上传 TXT/EPUB 文件或直接粘贴文本</p>
        </div>

        <form onSubmit={onSubmit} className="flex-1 overflow-y-auto">
          <div className="px-6 py-5 space-y-5">
            {/* 项目名 */}
            <div>
              <label className="block text-sm text-white/70 mb-1.5">有声书名称</label>
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

            {/* 模式切换 */}
            <div className="flex rounded-lg p-1 bg-white/[0.04] border border-white/[0.06]">
              <button
                type="button"
                onClick={() => setMode('file')}
                disabled={busy}
                className={`flex-1 py-1.5 text-xs font-medium rounded-md transition-all ${
                  mode === 'file' ? 'bg-white/[0.08] text-white shadow-sm' : 'text-white/50 hover:text-white/70'
                }`}
              >
                📁 上传文件
              </button>
              <button
                type="button"
                onClick={() => setMode('text')}
                disabled={busy}
                className={`flex-1 py-1.5 text-xs font-medium rounded-md transition-all ${
                  mode === 'text' ? 'bg-white/[0.08] text-white shadow-sm' : 'text-white/50 hover:text-white/70'
                }`}
              >
                ✏️ 粘贴文本
              </button>
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
