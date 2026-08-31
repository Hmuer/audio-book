'use client';

import { useEffect, useMemo, useState } from 'react';
import { api, SettingItem } from '@/lib/api';

// ---------- 常量 ----------
type IconName = 'cpu' | 'clock' | 'gauge' | 'database' | 'scissors' | 'file-text' | 'lock' | 'cog' | 'box';
const GROUP_META: Record<string, { icon: IconName; desc: string }> = {
  '模型配置': { icon: 'cpu', desc: 'TTS / LLM 厂商 API 地址、密钥与模型选择' },
  '超时配置': { icon: 'clock', desc: '各环节请求超时与 Build 运行超时' },
  '限流配置': { icon: 'gauge', desc: '并发度、RPM 限流、批处理参数' },
  '缓存配置': { icon: 'database', desc: 'TTS 段缓存 LRU / 磁盘过期策略' },
  '章节切分': { icon: 'scissors', desc: '章节识别正则匹配（可在线增删改，保存后即时生效）' },
  '日志配置': { icon: 'file-text', desc: '日志级别与文件路径' },
  '认证配置': { icon: 'lock', desc: 'JWT 过期时间' },
  '系统': { icon: 'cog', desc: '运行环境与服务参数（只读）' },
};

/** 分组图标 · 16px 线性 SVG（与全站图标语言一致） */
function GroupIcon({ name, size = 16 }: { name: IconName; size?: number }) {
  const common = {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.9,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  };
  switch (name) {
    case 'cpu': return (<svg {...common}><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/></svg>);
    case 'clock': return (<svg {...common}><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>);
    case 'gauge': return (<svg {...common}><path d="M12 14l4-4"/><path d="M3.34 19a10 10 0 1117.32 0"/></svg>);
    case 'database': return (<svg {...common}><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>);
    case 'scissors': return (<svg {...common}><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/></svg>);
    case 'file-text': return (<svg {...common}><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>);
    case 'lock': return (<svg {...common}><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/></svg>);
    case 'cog': return (<svg {...common}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 01-4 0v-.1A1.7 1.7 0 009 19.4a1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 010-4h.1A1.7 1.7 0 004.6 9a1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 014 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 010 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg>);
    default: return (<svg {...common}><path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z"/></svg>);
  }
}

const SENSITIVE_KEYS = new Set(['TTS_API_KEY', 'LLM_API_KEY', 'JWT_SECRET']);

type DraftValue = string | string[];
type DraftMap = Record<string, DraftValue>;

// ---------- 小工具 ----------
function valToDraft(v: SettingItem['value']): DraftValue {
  if (Array.isArray(v)) return [...v];
  if (v === null || v === undefined) return '';
  return String(v);
}

function deepEqual(a: SettingItem['value'], b: DraftValue | undefined): boolean {
  if (Array.isArray(a)) {
    if (!Array.isArray(b)) return false;
    if (a.length !== b.length) return false;
    return a.every((x, i) => x === b[i]);
  }
  if (Array.isArray(b)) return false;
  return String(a ?? '') === String(b ?? '');
}

const EMPTY_LINE_MARKER = '__EMPTY_LINE__';

/** 校验正则语法（浏览器端），返回错误字符串或 null；空行返回 EMPTY_LINE_MARKER */
function regexValidate(pattern: string): string | null {
  if (!pattern.trim()) return EMPTY_LINE_MARKER;
  try {
    new RegExp(pattern, 'm');
    return null;
  } catch (e: any) {
    return e.message || 'regex syntax error';
  }
}

// ---------- 主组件 ----------
export default function SettingsPage() {
  const [items, setItems] = useState<SettingItem[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftMap>({});
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = async () => {
    try {
      const list = await api.settingsGet();
      setItems(list);
      const map: DraftMap = {};
      for (const it of list) map[it.key] = valToDraft(it.value);
      setDraft(map);
      setErr(null);
    } catch (e: any) {
      setErr(String(e?.message || e));
      setItems([]);
    }
  };

  useEffect(() => { load(); }, []);

  const grouped = useMemo(() => {
    if (!items) return {};
    const g: Record<string, SettingItem[]> = {};
    for (const it of items) (g[it.group] ??= []).push(it);
    return g;
  }, [items]);

  const groupOrder = ['模型配置', '超时配置', '限流配置', '缓存配置', '章节切分', '日志配置', '认证配置', '系统'];

  const dirtyKeys = useMemo(() => {
    if (!items) return [];
    return items
      .filter(it => !it.readonly && !deepEqual(it.value, draft[it.key]))
      .map(it => it.key);
  }, [items, draft]);

  const onSave = async () => {
    if (dirtyKeys.length === 0) return;
    setSaving(true);
    setSavedMsg(null);
    try {
      const updates: Record<string, string | number | boolean | string[]> = {};
      for (const key of dirtyKeys) {
        const item = items!.find(i => i.key === key)!;
        const raw = draft[key];
        if (item.type === 'int') {
          updates[key] = parseInt(String(raw ?? 0), 10) || 0;
        } else if (item.type === 'bool') {
          updates[key] = raw === 'true' || raw === '1';
        } else if (item.type === 'list[str]') {
          updates[key] = Array.isArray(raw)
            ? raw.map(s => s.trim()).filter(Boolean)
            : String(raw ?? '').split('\n').map(s => s.trim()).filter(Boolean);
        } else {
          updates[key] = String(raw ?? '');
        }
      }
      const res = await api.settingsUpdate(updates);
      const tail = [res.skipped.length ? `跳过: ${res.skipped.join(', ')}` : ''].filter(Boolean);
      setSavedMsg({
        ok: true,
        text: `已更新 ${res.updated.length} 项配置。${tail.join(' ')}\n${res.note}`,
      });
      await load();
    } catch (e: any) {
      setSavedMsg({ ok: false, text: String(e?.message || e) });
    } finally {
      setSaving(false);
      setTimeout(() => setSavedMsg(null), 8000);
    }
  };

  const onReset = () => {
    if (!items) return;
    const map: DraftMap = {};
    for (const it of items) map[it.key] = valToDraft(it.value);
    setDraft(map);
    setSavedMsg(null);
  };

  const loading = items === null;

  // ---- 渲染普通字段 ----
  const renderScalarField = (it: SettingItem) => {
    const val = (draft[it.key] ?? '') as string;
    const isSensitive = SENSITIVE_KEYS.has(it.key);

    if (it.readonly) {
      if (it.type === 'list[str]') {
        const arr = (draft[it.key] ?? []) as string[];
        return (
          <div className="px-3 py-2 rounded-lg bg-ink-200 border border-ink-300/70 text-xs text-ink-500 max-h-32 overflow-auto font-mono whitespace-pre-wrap break-all">
            {arr.length ? arr.join('\n') : '—'}
          </div>
        );
      }
      return (
        <div className="px-3 py-2 rounded-lg bg-ink-200 border border-ink-300/70 text-sm text-ink-500 truncate">
          {it.type === 'bool' ? (val === 'true' ? '是' : '否') : (val || '—')}
        </div>
      );
    }

    if (it.type === 'bool') {
      return (
        <button
          onClick={() => setDraft(d => ({ ...d, [it.key]: val === 'true' ? 'false' : 'true' }))}
          className={`relative w-11 h-6 rounded-full transition-colors ${
            val === 'true' ? 'bg-brand-500/80' : 'bg-white/10'
          }`}
        >
          <span
            className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
              val === 'true' ? 'translate-x-[22px]' : 'translate-x-0.5'
            }`}
          />
        </button>
      );
    }

    return (
      <input
        type={isSensitive ? 'password' : it.type === 'int' ? 'number' : 'text'}
        value={val}
        onChange={e => setDraft(d => ({ ...d, [it.key]: e.target.value }))}
        placeholder={it.label}
        className="input-base w-full"
      />
    );
  };

  // ---- 渲染多行 list 编辑器 ----
  const renderListEditor = (it: SettingItem) => {
    const arr = Array.isArray(draft[it.key]) ? (draft[it.key] as string[]) : [];
    if (it.readonly) return renderScalarField(it);

    const updateAll = (next: string[]) => {
      setDraft(d => ({ ...d, [it.key]: next }));
    };
    const updateOne = (idx: number, v: string) => {
      const next = [...arr];
      next[idx] = v;
      updateAll(next);
    };
    const removeOne = (idx: number) => {
      const next = arr.filter((_, i) => i !== idx);
      updateAll(next);
    };
    const addOne = () => updateAll([...arr, '']);

    const errors = arr.map(s => regexValidate(s));
    const errCount = errors.filter(e => e && e !== EMPTY_LINE_MARKER).length;
    const emptyCount = errors.filter(e => e === EMPTY_LINE_MARKER).length;

    return (
      <div className="space-y-2">
        {/* 顶部：统计 + 添加/按换行粘贴 按钮 */}
        <div className="flex items-center gap-2 flex-wrap">
          <div className="text-xs text-ink-500 tabular-nums">
            共 <span className="text-ink-600">{arr.length}</span> 条规则
            {errCount > 0 && <span className="text-rose-300 ml-2">· {errCount} 语法错误</span>}
            {emptyCount > 0 && <span className="text-amber-300 ml-2">· {emptyCount} 空行</span>}
          </div>
          <div className="flex-1" />
          <button
            type="button"
            onClick={() => {
              const text = window.prompt('粘贴所有正则（每条一行）：', arr.join('\n'));
              if (text !== null) {
                updateAll(text.split('\n').map(s => s.trim()));
              }
            }}
            className="btn-ghost !py-1 !px-2.5 text-xs"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
            批量编辑
          </button>
          <button type="button" onClick={addOne} className="btn-ghost !py-1 !px-2.5 text-xs">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            新增
          </button>
        </div>

        {/* 正则行列表 */}
        <div className="space-y-1.5 max-h-96 overflow-y-auto pr-1">
          {arr.length === 0 && (
            <div className="rounded-lg border border-dashed border-ink-300/70 py-6 text-center text-xs text-ink-500">
              暂无规则，点击「新增」开始添加
            </div>
          )}
          {arr.map((line, idx) => {
            const errorMsg = errors[idx];
            const bad = errorMsg && errorMsg !== EMPTY_LINE_MARKER;
            return (
              <div key={idx} className="flex items-start gap-2">
                <span className="mt-2 w-7 shrink-0 text-right text-[11px] text-ink-500 tabular-nums pt-0.5">
                  #{idx + 1}
                </span>
                <div className="flex-1 min-w-0">
                  <input
                    value={line}
                    onChange={e => updateOne(idx, e.target.value)}
                    placeholder={'^[ \\t]*第[ \\t]*(章|回|节)...'}
                    spellCheck={false}
                    className={`input-base w-full !font-mono !text-xs !py-2 !leading-5 ${
                      bad ? '!border-rose-500/50 focus:!border-rose-500'
                      : errorMsg === EMPTY_LINE_MARKER ? '!border-amber-500/30' : ''
                    }`}
                  />
                  {bad && (
                    <div className="mt-1 text-[11px] text-rose-300">
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                      {errorMsg}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => removeOne(idx)}
                  className="mt-1.5 w-7 h-7 grid place-items-center rounded-lg text-ink-500 hover:text-rose-300 hover:bg-rose-500/10 transition-colors shrink-0"
                  title="删除该条"
                >
                  ×
                </button>
              </div>
            );
          })}
        </div>

        {/* 提示 */}
        <div className="text-[11px] text-ink-500 leading-relaxed">
          提示：正则自动带 re.MULTILINE 标志；英文模式自动加 IGNORECASE；建议行首用 ^[ \t]* 锁定避免正文误命中。保存时后端会即时重新编译，语法错误的规则会被跳过。
        </div>
      </div>
    );
  };

  const renderField = (it: SettingItem) => {
    if (it.type === 'list[str]') return renderListEditor(it);
    return renderScalarField(it);
  };

  return (
    <section className="max-w-5xl mx-auto space-y-5">
      {/* 顶部 */}
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-[22px] font-semibold text-white leading-tight">设置</h2>
          <p className="mt-1 text-sm text-ink-500">
            管理模型厂商、限流、超时、切章正则等系统配置 · 修改即时生效
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn-ghost" onClick={onReset} disabled={loading || saving || dirtyKeys.length === 0}>
            重置
          </button>
          <button
            className="btn-primary"
            onClick={onSave}
            disabled={loading || saving || dirtyKeys.length === 0}
          >
            {saving ? '保存中…' : `保存${dirtyKeys.length > 0 ? ` (${dirtyKeys.length})` : ''}`}
          </button>
        </div>
      </div>

      {/* 提示信息 */}
      {savedMsg && (
        <div
          className={`rounded-md px-4 py-3 text-sm flex items-center gap-2 whitespace-pre-wrap ${
            savedMsg.ok
              ? 'border border-lime-500/30 bg-lime-500/10 text-lime-200'
              : 'border border-rose-500/30 bg-rose-500/10 text-rose-200'
          }`}
        >
          <span className="shrink-0">{savedMsg.ok ? (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          )}</span>
          <span className="min-w-0">{savedMsg.text}</span>
        </div>
      )}

      {err && (
        <div className="rounded-md px-4 py-3 text-sm border border-rose-500/30 bg-rose-500/10 text-rose-200">
          {err}
        </div>
      )}

      {/* 加载中 */}
      {loading && (
        <div className="rounded-lg border border-ink-300/70 bg-ink-200 text-center py-16">
          <div className="mx-auto w-9 h-9 rounded-full border-2 border-brand-500/30 border-t-brand-500 animate-spin mb-4" />
          <div className="text-sm text-ink-500">加载配置…</div>
        </div>
      )}

      {/* 分组卡片 */}
      {!loading && items && (
        <div className="space-y-4">
          {groupOrder.map(group => {
            const groupItems = grouped[group];
            if (!groupItems || groupItems.length === 0) return null;
            const meta = GROUP_META[group] ?? { icon: 'box' as IconName, desc: '' };
            return (
              <div key={group} className="rounded-lg border border-ink-300/70 bg-ink-200 overflow-hidden">
                {/* 分组头 */}
                <div className="px-5 py-3.5 border-b border-ink-300/70 flex items-center gap-3">
                  <span className="w-8 h-8 grid place-items-center text-brand-300 bg-brand-500/10 border border-brand-500/20 shrink-0" style={{ borderRadius: 'var(--radius-xs)' }}>
                    <GroupIcon name={meta.icon} />
                  </span>
                  <div>
                    <div className="text-sm font-semibold text-white">{group}</div>
                    <div className="text-xs text-ink-500">{meta.desc}</div>
                  </div>
                </div>
                {/* 字段 */}
                <div className="divide-y divide-white/[0.04]">
                  {groupItems.map(it => {
                    const isDirty = !it.readonly && !deepEqual(it.value, draft[it.key]);
                    const isList = it.type === 'list[str]';
                    return (
                      <div key={it.key} className={`px-5 ${isList ? 'py-4' : 'py-3'} flex gap-4 ${isList ? 'items-start' : 'items-center'}`}>
                        {/* 标签 */}
                        <div className="w-[180px] shrink-0 pt-0.5">
                          <div className="text-sm text-ink-700">{it.label}</div>
                          <div className="text-[11px] text-ink-500 font-mono mt-0.5">{it.key}</div>
                        </div>
                        {/* 输入 */}
                        <div className="flex-1 min-w-0">
                          {renderField(it)}
                        </div>
                        {/* 修改标记 */}
                        <div className="w-4 shrink-0 pt-1">
                          {isDirty && <span className="w-2 h-2 rounded-full bg-amber-400 inline-block" title="已修改" />}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* 底部说明 */}
      {!loading && items && (
        <div className="text-xs text-ink-500 text-center py-2 leading-relaxed">
          配置修改即时生效（内存级）。重启后端后恢复 .env 默认值，如需持久化请手动写入 .env 文件。
        </div>
      )}
    </section>
  );
}
