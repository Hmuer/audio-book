'use client';

import { useEffect, useMemo, useState } from 'react';
import { api, SettingItem } from '@/lib/api';

// 分组顺序 & 图标
const GROUP_META: Record<string, { icon: string; desc: string }> = {
  '模型配置': { icon: '🤖', desc: 'TTS / LLM 厂商 API 地址、密钥与模型选择' },
  '超时配置': { icon: '⏱️', desc: '各环节请求超时与 Build 运行超时' },
  '限流配置': { icon: '🚦', desc: '并发度、RPM 限流、批处理参数' },
  '缓存配置': { icon: '💾', desc: 'TTS 段缓存 LRU / 磁盘过期策略' },
  '章节切分': { icon: '📑', desc: '章节识别正则匹配与硬切兜底' },
  '日志配置': { icon: '📝', desc: '日志级别与文件路径' },
  '认证配置': { icon: '🔐', desc: 'JWT 过期时间' },
  '系统': { icon: '⚙️', desc: '运行环境与服务参数（只读）' },
};

// 敏感字段：显示为密码框
const SENSITIVE_KEYS = new Set(['TTS_API_KEY', 'LLM_API_KEY', 'JWT_SECRET']);

export default function SettingsPage() {
  const [items, setItems] = useState<SettingItem[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = async () => {
    try {
      const list = await api.settingsGet();
      setItems(list);
      const map: Record<string, string> = {};
      for (const it of list) {
        map[it.key] = String(it.value ?? '');
      }
      setDraft(map);
      setErr(null);
    } catch (e: any) {
      setErr(String(e?.message || e));
      setItems([]);
    }
  };

  useEffect(() => { load(); }, []);

  // 按分组归类
  const grouped = useMemo(() => {
    if (!items) return {};
    const g: Record<string, SettingItem[]> = {};
    for (const it of items) {
      (g[it.group] ??= []).push(it);
    }
    return g;
  }, [items]);

  const groupOrder = ['模型配置', '超时配置', '限流配置', '缓存配置', '章节切分', '日志配置', '认证配置', '系统'];

  // 检查是否有修改
  const dirtyKeys = useMemo(() => {
    if (!items) return [];
    return items
      .filter(it => !it.readonly && String(it.value ?? '') !== (draft[it.key] ?? ''))
      .map(it => it.key);
  }, [items, draft]);

  const onSave = async () => {
    if (dirtyKeys.length === 0) return;
    setSaving(true);
    setSavedMsg(null);
    try {
      const updates: Record<string, string | number | boolean> = {};
      for (const key of dirtyKeys) {
        const item = items!.find(i => i.key === key)!;
        const raw = draft[key];
        if (item.type === 'int') {
          updates[key] = parseInt(raw, 10) || 0;
        } else if (item.type === 'bool') {
          updates[key] = raw === 'true' || raw === '1';
        } else {
          updates[key] = raw;
        }
      }
      const res = await api.settingsUpdate(updates);
      setSavedMsg({
        ok: true,
        text: `已更新 ${res.updated.length} 项配置。${res.skipped.length ? `跳过: ${res.skipped.join(', ')}` : ''}`,
      });
      await load();
    } catch (e: any) {
      setSavedMsg({ ok: false, text: String(e?.message || e) });
    } finally {
      setSaving(false);
      setTimeout(() => setSavedMsg(null), 6000);
    }
  };

  const onReset = () => {
    if (!items) return;
    const map: Record<string, string> = {};
    for (const it of items) map[it.key] = String(it.value ?? '');
    setDraft(map);
    setSavedMsg(null);
  };

  const loading = items === null;

  const renderField = (it: SettingItem) => {
    const val = draft[it.key] ?? '';
    const isSensitive = SENSITIVE_KEYS.has(it.key);

    if (it.readonly) {
      return (
        <div className="px-3 py-2 rounded-lg bg-white/[0.02] border border-white/[0.06] text-sm text-white/50 truncate">
          {it.type === 'bool' ? (val === 'true' ? '✅ 是' : '❌ 否') : (val || '—')}
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

  return (
    <section className="p-6 max-w-4xl mx-auto space-y-5">
      {/* 顶部 */}
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-[22px] font-semibold text-white leading-tight">设置</h2>
          <p className="mt-1 text-sm text-white/50">
            管理模型厂商、限流、超时等系统配置 · 修改即时生效
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

      {/* 提示 */}
      {savedMsg && (
        <div
          className={`rounded-xl px-4 py-3 text-sm flex items-center gap-2 ${
            savedMsg.ok
              ? 'border border-lime-500/30 bg-lime-500/10 text-lime-200'
              : 'border border-rose-500/30 bg-rose-500/10 text-rose-200'
          }`}
        >
          {savedMsg.ok ? '✓' : '❌'} {savedMsg.text}
        </div>
      )}

      {err && (
        <div className="rounded-xl px-4 py-3 text-sm border border-rose-500/30 bg-rose-500/10 text-rose-200">
          {err}
        </div>
      )}

      {/* 加载中 */}
      {loading && (
        <div className="rounded-2xl border border-white/[0.06] bg-white/[0.02] text-center py-16">
          <div className="mx-auto w-9 h-9 rounded-full border-2 border-brand-500/30 border-t-brand-500 animate-spin mb-4" />
          <div className="text-sm text-white/50">加载配置…</div>
        </div>
      )}

      {/* 分组卡片 */}
      {!loading && items && (
        <div className="space-y-4">
          {groupOrder.map(group => {
            const groupItems = grouped[group];
            if (!groupItems || groupItems.length === 0) return null;
            const meta = GROUP_META[group] ?? { icon: '📦', desc: '' };
            return (
              <div key={group} className="rounded-2xl border border-white/[0.06] bg-white/[0.02] overflow-hidden">
                {/* 分组头 */}
                <div className="px-5 py-3.5 border-b border-white/[0.06] flex items-center gap-2.5">
                  <span className="text-lg">{meta.icon}</span>
                  <div>
                    <div className="text-sm font-semibold text-white">{group}</div>
                    <div className="text-[11px] text-white/40">{meta.desc}</div>
                  </div>
                </div>
                {/* 字段 */}
                <div className="divide-y divide-white/[0.04]">
                  {groupItems.map(it => {
                    const isDirty = !it.readonly && String(it.value ?? '') !== (draft[it.key] ?? '');
                    return (
                      <div key={it.key} className="px-5 py-3 flex items-center gap-4">
                        {/* 标签 */}
                        <div className="w-[180px] shrink-0">
                          <div className="text-sm text-white/80">{it.label}</div>
                          <div className="text-[10px] text-white/30 font-mono mt-0.5">{it.key}</div>
                        </div>
                        {/* 输入 */}
                        <div className="flex-1 min-w-0">
                          {renderField(it)}
                        </div>
                        {/* 修改标记 */}
                        <div className="w-4 shrink-0">
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
        <div className="text-[11px] text-white/30 text-center py-2">
          配置修改即时生效（内存级）。重启后端后恢复 .env 默认值，如需持久化请手动写入 .env 文件。
        </div>
      )}
    </section>
  );
}
