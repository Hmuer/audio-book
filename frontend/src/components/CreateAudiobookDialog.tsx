'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, LibraryBook, LibraryInfo } from '@/lib/api';

interface Props {
  onClose: () => void;
  /** 创建完成回调，参数为新项目 ID */
  onCreated: (id: string) => void;
}

type Mode = 'library' | 'file' | 'text';

const PAGE_SIZE = 40;

export default function CreateAudiobookDialog({ onClose, onCreated }: Props) {
  const [mode, setMode] = useState<Mode>('library');
  const [name, setName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // 书库首次导入要先从对象存储下载原文，冷启动可能几十秒，这里显示耗时让用户知道没卡死
  const [elapsed, setElapsed] = useState(0);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // 标记当前 name 是否是"自动预填"的。true 时允许被新选择覆盖；
  // false（用户手动输入过）时不再自动覆盖。需要在用户每次输入 name 时手动 set 回 false。
  const nameAutoFilledRef = useRef(false);
  // 创建成功后导入失败时，重试应复用同一个空项目，避免列表里留下多个草稿。
  const createdProjectIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!busy) {
      setElapsed(0);
      return;
    }
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  // ---- 书库 ----
  const [libInfo, setLibInfo] = useState<LibraryInfo | null>(null);
  const [libErr, setLibErr] = useState<string | null>(null);
  const [libLoading, setLibLoading] = useState(false);
  const [category, setCategory] = useState('');
  const [keyword, setKeyword] = useState('');
  const [books, setBooks] = useState<LibraryBook[]>([]);
  const [booksTotal, setBooksTotal] = useState(0);
  const [booksLoading, setBooksLoading] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [selected, setSelected] = useState<LibraryBook | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const loadInfo = useCallback(async (force = false) => {
    setLibLoading(true);
    setLibErr(null);
    try {
      const info = await api.libraryInfo(force);
      setLibInfo(info);
    } catch (e: any) {
      setLibErr(String(e?.message || e));
    } finally {
      setLibLoading(false);
    }
  }, []);

  // 切到书库模式时按需加载书目概览（成功过就不再重复请求）
  useEffect(() => {
    if (mode === 'library' && !libInfo && !libLoading && !libErr) void loadInfo();
  }, [mode, libInfo, libLoading, libErr, loadInfo]);

  // 分类 / 关键字变化时拉书目；关键字做 250ms 防抖
  useEffect(() => {
    if (mode !== 'library' || !libInfo) return;
    let alive = true;
    const t = setTimeout(async () => {
      setBooksLoading(true);
      try {
        const res = await api.libraryBooks({
          category: category || undefined,
          q: keyword.trim() || undefined,
          limit: PAGE_SIZE,
        });
        if (!alive) return;
        setBooks(res.items);
        setBooksTotal(res.total);
      } catch (e: any) {
        if (alive) setLibErr(String(e?.message || e));
      } finally {
        if (alive) setBooksLoading(false);
      }
    }, keyword ? 250 : 0);
    return () => { alive = false; clearTimeout(t); };
  }, [mode, libInfo, category, keyword]);

  const pickBook = (b: LibraryBook) => {
    setSelected(b);
    setPickerOpen(false);
    if (nameAutoFilledRef.current || !name.trim()) {
      nameAutoFilledRef.current = true;
      setName(b.title);
    }
  };

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);

    if (!name.trim()) { setErr('请输入有声书名称'); return; }
    if (mode === 'library' && !selected) { setErr('请从书库中选择一本书'); return; }
    if (mode === 'file' && !file) { setErr('请选择要上传的文件'); return; }
    if (mode === 'text' && !text.trim()) { setErr('请粘贴小说文本'); return; }

    setBusy(true);
    try {
      // 1. 创建项目
      if (createdProjectIdRef.current) {
        await api.projectUpdate(createdProjectIdRef.current, { name: name.trim() });
      }
      const id = createdProjectIdRef.current ?? (await api.projectCreate(name.trim())).project_id;
      createdProjectIdRef.current = id;

      // 2. 导入内容
      if (mode === 'library' && selected) {
        await api.projectImportLibrary(id, selected.uid);
      } else if (mode === 'file' && file) {
        await api.projectImport(id, file);
      } else if (mode === 'text') {
        await api.projectImportText(id, text, `${name.trim()}.txt`);
      }

      onCreated(id);
    } catch (e: any) {
      const message = String(e?.message || e);
      setErr(createdProjectIdRef.current ? `${message}。项目已创建，请修改内容后重试导入。` : message);
    } finally {
      setBusy(false);
    }
  };

  // 选择文件时：
  // - 用户手动输入过的名字保留（不覆盖）
  // - 当前名字是"自动预填"的，选新文件时同步更新到新文件名
  // - 当前名字为空（无论 trimmed 还是 untrimmed）则用新文件名预填
  const pickFile = (f: File | null) => {
    setFile(f);
    if (f && (nameAutoFilledRef.current || !name.trim())) {
      nameAutoFilledRef.current = true;
      setName(nameFromFilename(f.name));
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) pickFile(f);
  };

  const totalBooks = libInfo?.totals?.novels ?? 0;
  const activeCategoryLabel = useMemo(() => {
    if (!category) return '全部分类';
    const hit = libInfo?.categories.find(c => c.name === category);
    return hit ? hit.name : category;
  }, [category, libInfo]);
  const canSubmit = !busy && (mode !== 'library' || !!selected);

  return (
    <div className="fixed inset-0 z-50 grid place-items-center p-4"
      style={{ background: 'rgba(0,0,0,0.72)' }}
      onClick={() => { if (!busy) onClose(); }}>
      <div
        className="bg-ink-100 border border-ink-300 w-full max-w-lg max-h-[88vh] overflow-hidden flex flex-col animate-scale-in shadow-[0_30px_80px_-20px_rgba(0,0,0,0.9)]"
        style={{ borderRadius: 'var(--radius-md)' }}
        onClick={e => e.stopPropagation()}
      >
        {/* 头部：GreenSpring简洁步骤条 · 无胶囊装饰 */}
        <div className="relative px-6 pt-5 pb-4"
          style={{
            borderBottom: '1px solid rgb(var(--ink-300))',
            background: 'rgb(var(--ink-50))',
          }}
        >
          {/* 顶部：标题行 */}
          <div className="flex items-center justify-between mb-3">
            <div className="text-[11px] font-medium" style={{ color: 'rgb(var(--ink-600))' }}>
              步骤 1 / 3
            </div>
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              aria-label="关闭"
              className="w-7 h-7 grid place-items-center transition-all text-ink-600 hover:text-ink-900 hover:bg-ink-200"
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
                    color: '#fff',
                  }}
                >{i + 1}</div>
                <div className="min-w-0">
                  <div className="text-[11.5px] font-medium leading-tight truncate"
                    style={{ color: i === 0 ? 'rgb(var(--ink-900))' : 'rgba(var(--ink-700), 0.55)' }}
                  >{t}</div>
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
                style={{ color: 'rgba(var(--ink-900), 0.8)' }}>有声书名称
              </label>
              <input
                type="text"
                value={name}
                onChange={e => {
                  nameAutoFilledRef.current = false;
                  setName(e.target.value);
                }}
                placeholder="例如：神秘峡谷"
                disabled={busy}
                autoFocus
                maxLength={80}
                className="input-base w-full"
              />
            </div>

            {/* 模式切换（Edtech segmented control · 冷灰） */}
            <div
              className="grid grid-cols-3 p-1"
              style={{
                borderRadius: 'var(--radius-sm)',
                background: 'rgb(var(--ink-0))',
                border: '1px solid rgb(var(--ink-300))',
              }}
            >
              {([
                { k: 'library', iconName: 'library', label: '从书库导入', desc: '选择书目' },
                { k: 'file',    iconName: 'upload',  label: '上传文件',   desc: 'TXT / EPUB' },
                { k: 'text',    iconName: 'pen',     label: '粘贴文本',   desc: '原文直贴' },
              ] as { k: Mode; iconName: 'library' | 'upload' | 'pen'; label: string; desc: string }[]).map(it => {
                const active = mode === it.k;
                return (
                  <button
                    key={it.k}
                    type="button"
                    onClick={() => setMode(it.k)}
                    disabled={busy}
                    className="relative flex flex-col items-start gap-1.5 px-2.5 py-2 text-left transition-all"
                    style={{
                      borderRadius: 'calc(calc(var(--radius-sm) - 2px))',
                      background: active ? 'rgb(var(--brand-600))' : 'transparent',
                      color: active ? '#fff' : 'rgba(var(--ink-700), 0.75)',
                      boxShadow: active ? '0 0 0 1px rgba(var(--brand-500), 0.4)' : undefined,
                    }}
                  >
                    <span className={`w-7 h-7 shrink-0 grid place-items-center rounded-[6px] ${
                      active ? 'bg-white/15' : 'bg-ink-200'
                    }`}>
                      <SegmentIcon name={it.iconName} />
                    </span>
                    <span className="min-w-0 w-full">
                      <span className="block text-[12.5px] font-semibold leading-tight truncate">{it.label}</span>
                      <span className={`block text-[10.5px] mt-0.5 truncate ${active ? 'text-ink-700' : 'text-ink-500'}`}>{it.desc}</span>
                    </span>
                  </button>
                );
              })}
            </div>

            {/* 内容区 */}
            {mode === 'library' ? (
              <div className="space-y-3">
                {libErr ? (
                  <div className="rounded-lg px-3 py-2 text-xs"
                    style={{
                      border: '1px solid rgba(var(--status-error-bg), 0.28)',
                      background: 'rgba(var(--status-error-bg), 0.06)',
                      color: 'rgb(var(--status-error-fg))',
                    }}
                  >
                    <div className="flex items-start gap-2">
                      <span className="flex-1 break-all">{libErr}</span>
                      <button
                        type="button"
                        onClick={() => { setLibErr(null); void loadInfo(true); }}
                        disabled={libLoading}
                        className="shrink-0 underline underline-offset-2 hover:opacity-80 disabled:opacity-50"
                      >
                        重试
                      </button>
                    </div>
                  </div>
                ) : null}

                {/* 分类 + 书目选择 */}
                <div className="grid grid-cols-[minmax(0,132px)_minmax(0,1fr)] gap-2">
                  <div>
                    <label className="block text-[11.5px] mb-1" style={{ color: 'rgb(var(--ink-500))' }}>
                      分类
                    </label>
                    <div className="relative">
                      <select
                        value={category}
                        onChange={e => { setCategory(e.target.value); setPickerOpen(false); }}
                        disabled={busy || !libInfo}
                        className="input-base w-full appearance-none pr-6 text-[12.5px] cursor-pointer"
                        title={totalBooks ? `书库共 ${totalBooks.toLocaleString()} 本` : undefined}
                      >
                        <option value="">全部分类</option>
                        {(libInfo?.categories ?? []).map(c => (
                          <option key={c.name} value={c.name} title={`${c.count.toLocaleString()} 本`}>
                            {c.name}
                          </option>
                        ))}
                      </select>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                        strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                        className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-ink-500">
                        <polyline points="6 9 12 15 18 9" />
                      </svg>
                    </div>
                  </div>

                  <div className="min-w-0">
                    <label className="block text-[11.5px] mb-1" style={{ color: 'rgb(var(--ink-500))' }}>
                      书籍
                    </label>
                    <button
                      type="button"
                      disabled={busy || !libInfo}
                      onClick={() => {
                        setPickerOpen(o => !o);
                        if (!pickerOpen) setTimeout(() => searchRef.current?.focus(), 30);
                      }}
                      className="input-base w-full flex items-center gap-2 text-left"
                      aria-expanded={pickerOpen}
                    >
                      <BookGlyph className="w-3.5 h-3.5 shrink-0 text-ink-500" />
                      <span className={`flex-1 truncate text-[12.5px] ${selected ? 'text-ink-900' : 'text-ink-500'}`}>
                        {selected ? selected.title : libLoading ? '加载书目…' : '选择书籍'}
                      </span>
                      {selected ? (
                        <span className="shrink-0 text-[10.5px] text-ink-500">
                          {selected.chapter_count ? `${selected.chapter_count} 章` : formatWords(selected.word_count)}
                        </span>
                      ) : null}
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                        strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                        className={`shrink-0 text-ink-500 transition-transform ${pickerOpen ? 'rotate-180' : ''}`}>
                        <polyline points="6 9 12 15 18 9" />
                      </svg>
                    </button>
                  </div>
                </div>

                {/* 展开的书目列表（在流内展开，避免弹层被滚动容器裁切） */}
                {pickerOpen && (
                  <div
                    className="overflow-hidden"
                    style={{
                      borderRadius: 'var(--radius-sm)',
                      border: '1px solid rgb(var(--ink-300))',
                      background: 'rgb(var(--ink-0))',
                    }}
                  >
                    <div className="flex items-center gap-2 px-2.5 py-2 border-b border-ink-300/60">
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                        strokeWidth="2" strokeLinecap="round" className="shrink-0 text-ink-500">
                        <circle cx="11" cy="11" r="7" /><line x1="20" y1="20" x2="16.65" y2="16.65" />
                      </svg>
                      <input
                        ref={searchRef}
                        value={keyword}
                        onChange={e => setKeyword(e.target.value)}
                        placeholder="搜索书名或作者…"
                        className="w-full bg-transparent text-[12.5px] outline-none placeholder:text-ink-500"
                      />
                      {keyword && (
                        <button type="button" onClick={() => setKeyword('')}
                          className="shrink-0 text-[11px] text-ink-500 hover:text-ink-700">
                          清除
                        </button>
                      )}
                    </div>
                    <div className="max-h-[188px] overflow-y-auto">
                      {booksLoading && books.length === 0 ? (
                        <div className="px-3 py-6 text-center text-[12px] text-ink-500">加载中…</div>
                      ) : books.length === 0 ? (
                        <div className="px-3 py-6 text-center text-[12px] text-ink-500">
                          {libLoading ? '加载书库…' : '没有匹配的书'}
                        </div>
                      ) : (
                        books.map(b => {
                          const active = selected?.uid === b.uid;
                          return (
                            <button
                              key={b.uid}
                              type="button"
                              onClick={() => pickBook(b)}
                              className={`w-full text-left px-3 py-2 transition-colors ${
                                active ? 'bg-brand-500/15' : 'hover:bg-ink-200'
                              }`}
                            >
                              <div className="flex items-center gap-2">
                                <span className={`flex-1 truncate text-[12.5px] ${active ? 'text-brand-400' : 'text-ink-900'}`}>
                                  {b.title}
                                </span>
                                <span className="shrink-0 text-[10.5px] text-ink-500">
                                  {b.chapter_count ? `${b.chapter_count} 章` : formatWords(b.word_count)}
                                </span>
                              </div>
                              <div className="mt-0.5 flex items-center gap-1.5 text-[10.5px] text-ink-500">
                                <span className="truncate">{b.author || '佚名'}</span>
                                <span>·</span>
                                <span className="shrink-0">{b.category}</span>
                              </div>
                            </button>
                          );
                        })
                      )}
                    </div>
                    <div className="px-3 py-1.5 text-[10.5px] text-ink-500 border-t border-ink-300/60 flex items-center justify-between">
                      <span>{activeCategoryLabel} · 共 {booksTotal.toLocaleString()} 本</span>
                      {booksTotal > books.length ? (
                        <span>输入关键字缩小范围</span>
                      ) : null}
                    </div>
                  </div>
                )}

                {/* 选中书目的信息卡 */}
                {selected ? (
                  <div
                    className="px-3 py-2.5"
                    style={{
                      borderRadius: 'var(--radius-sm)',
                      border: '1px solid rgba(var(--brand-500), 0.35)',
                      background: 'rgba(var(--brand-600), 0.08)',
                    }}
                  >
                    <div className="text-[13px] font-medium text-ink-900 truncate">{selected.title}</div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-ink-500">
                      <span>{selected.author || '佚名'}</span>
                      <span>·</span>
                      <span>{selected.category}</span>
                      {selected.status ? (<><span>·</span><span>{selected.status}</span></>) : null}
                      <span>·</span>
                      <span>{selected.chapter_count ? `${selected.chapter_count.toLocaleString()} 章` : '章节待识别'}</span>
                      <span>·</span>
                      <span>{formatWords(selected.word_count)}</span>
                      <span>·</span>
                      <span>{formatSize(selected.size_bytes)}</span>
                    </div>
                  </div>
                ) : (
                  <div className="text-[11px] text-ink-500">
                    选择书目后会自动填入有声书名称，导入后仍可修改。
                  </div>
                )}
              </div>
            ) : mode === 'file' ? (
              <div
                onDragOver={e => { e.preventDefault(); setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`cursor-pointer rounded-md border-2 border-dashed transition-all text-center py-9 px-4 ${
                  dragOver
                    ? 'border-brand-400 bg-brand-500/10'
                    : 'border-ink-300/70 hover:border-ink-400 hover:bg-ink-200'
                }`}
                style={{ borderRadius: 'var(--radius-md)' }}
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".txt,.md,.epub"
                  onChange={e => pickFile(e.target.files?.[0] ?? null)}
                  className="hidden"
                />
                {file ? (
                  <div>
                    <div className="mx-auto w-10 h-10 grid place-items-center mb-2"
                      style={{
                        background: 'rgba(var(--brand-600), 0.18)',
                        borderRadius: 'var(--radius-sm)',
                        color: 'rgb(var(--brand-400))',
                      }}
                    >
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/>
                      </svg>
                    </div>
                    <div className="text-sm text-white font-medium truncate max-w-[300px] mx-auto">{file.name}</div>
                    <div className="text-xs text-ink-500 mt-1">{(file.size / 1024).toFixed(1)} KB</div>
                    <button
                      type="button"
                      onClick={e => { e.stopPropagation(); setFile(null); if (fileInputRef.current) fileInputRef.current.value = ''; }}
                      className="mt-3 text-xs text-ink-500 hover:text-ink-600"
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
                    <div className="text-sm text-ink-600 font-medium">拖拽文件到这里，或点击选择</div>
                    <div className="text-xs text-ink-500 mt-2">支持 TXT、Markdown、EPUB 格式</div>
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
                <div className="mt-1.5 text-[11px] text-ink-500 text-right">
                  {text.length.toLocaleString()} 字符
                </div>
              </div>
            )}

            {err && (
              <div className="rounded-lg px-3 py-2 text-xs"
                style={{
                  border: '1px solid rgba(var(--status-error-bg), 0.28)',
                  background: 'rgba(var(--status-error-bg), 0.06)',
                  color: 'rgb(var(--status-error-fg))',
                }}
              >{err}</div>
            )}
          </div>

          {/* 底部按钮 */}
          <div className="px-6 py-4 border-t border-ink-300/60 flex items-center gap-2 justify-end">
            {busy && mode === 'library' && elapsed >= 3 && (
              <span className="mr-auto text-[11px] text-ink-500">
                正在从对象存储下载
                {selected ? `《${selected.title}》` : ''}
                ，首次导入可能较慢…已用 {elapsed}s
              </span>
            )}
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
              disabled={!canSubmit}
              className="btn-primary"
            >
              {busy
                ? (mode === 'library' ? `正在导入 ${elapsed}s…` : '处理中…')
                : createdProjectIdRef.current
                  ? '重试导入'
                  : mode === 'library' && selected
                    ? '导入并进入'
                    : '创建并进入'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function formatWords(n: number): string {
  if (!n) return '字数未知';
  if (n >= 10000) return `${(n / 10000).toFixed(n >= 100000 ? 0 : 1)} 万字`;
  return `${n.toLocaleString()} 字`;
}

function formatSize(bytes: number): string {
  if (!bytes) return '大小未知';
  const mb = bytes / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function BookGlyph({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 19.5A2.5 2.5 0 016.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" />
    </svg>
  );
}

function SegmentIcon({ name }: { name: 'library' | 'upload' | 'pen' }) {
  const common = {
    width: 14, height: 14, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.8,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  };
  if (name === 'library')
    return (<svg {...common}><path d="M4 4h5v16H4z"/><path d="M11 4h4v16h-4z"/><path d="M17.5 4.5l3 15.5-3.4.7L14 5.2z"/></svg>);
  if (name === 'upload')
    return (<svg {...common}><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>);
  return (<svg {...common}><path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/><path d="M2 2l7.586 7.586"/><circle cx="11" cy="11" r="2"/></svg>);
}

/**
 * 从文件名生成默认项目名：去掉最后一个后缀。
 * - "神秘峡谷.txt"        → "神秘峡谷"
 * - "alice.epub"          → "alice"
 * - "book.tar.gz"         → "book.tar"   （只剥最后一层，符合用户直觉）
 * - "no-extension"        → "no-extension"
 * - ".env"                → ".env"       （隐藏文件没有"名字"，整段保留）
 * - ""                    → ""
 */
function nameFromFilename(filename: string): string {
  if (!filename) return '';
  // 仅剥最后一个 .ext；找不到 "." 或以 "." 开头（隐藏文件）则原样返回
  const i = filename.lastIndexOf('.');
  if (i <= 0) return filename;
  return filename.slice(0, i);
}
