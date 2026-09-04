'use client';

/**
 * 多厂商模型配置编辑器（Cherry Studio 风格）
 *
 * 每个厂商是一张可折叠卡片：
 *  - 厂商名 + 启用开关
 *  - API Key（一个厂商一份；TTS 与 LLM 共用）
 *  - Base URL（厂商域名）
 *  - 模型列表（可添加 / 删除 / 编辑 label + id + kind）
 *
 * 卡片外独立"激活模型"区域：
 *  - TTS 激活：选择一个 provider + 该 provider 下的某个 tts 模型
 *  - LLM 激活：选择一个 provider + 该 provider 下的某个 llm 模型
 *
 * 设计要点：
 *  - 单 API key 设计：一个厂商只需一个 key（用户已确认 TTS/LLM 共用）
 *  - 模型不区分 pro/fast
 *  - 增删厂商、修改模型即时反映到「激活模型」下拉
 */

import { useEffect, useMemo, useState } from 'react';
import { api, ProviderConfig, ProviderModel, ProvidersConfig, ActiveModel } from '@/lib/api';

type IconName = 'plus' | 'trash' | 'check';

function Icon({ name, size = 14 }: { name: IconName; size?: number }) {
  const c = {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.9,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  };
  switch (name) {
    case 'plus':
      return (
        <svg {...c}>
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
      );
    case 'trash':
      return (
        <svg {...c}>
          <polyline points="3 6 5 6 21 6" />
          <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" />
          <path d="M10 11v6M14 11v6" />
          <path d="M9 6V4a1 1 0 011-1h4a1 1 0 011 1v2" />
        </svg>
      );
    case 'check':
      return (
        <svg {...c}>
          <polyline points="20 6 9 17 4 12" />
        </svg>
      );
  }
}

/**
 * 显眼的开关：左侧状态徽标 + 大号拨片
 * - 启用：品牌色背景 + 右侧"已启用"文字 + 拨片右移
 * - 停用：灰底 + 右侧"已停用"文字 + 拨片左移
 * 用 button 而不是 checkbox，键盘 / 屏幕阅读器友好；
 * 视觉上比"两个小圆点"明显得多。
 */
function ToggleSwitch({
  enabled, onChange, disabled,
}: {
  enabled: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      onClick={() => !disabled && onChange(!enabled)}
      disabled={disabled}
      title={enabled ? '点击停用此厂商' : '点击启用此厂商'}
      className={`group inline-flex items-center gap-2 rounded-full border pl-1 pr-3 h-7 transition-all shrink-0 select-none
        ${enabled
            ? 'bg-brand-500/20 border-brand-500/50 hover:bg-brand-500/30'
            : 'bg-white/[0.04] border-white/[0.08] hover:border-white/[0.2] hover:bg-white/[0.07]'}
        ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
      `}
    >
      {/* 拨片 */}
      <span
        className={`relative inline-block w-9 h-4 rounded-full transition-colors ${
          enabled ? 'bg-brand-500' : 'bg-white/[0.15]'
        }`}
      >
        <span
          className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-transform ${
            enabled ? 'translate-x-[22px]' : 'translate-x-0.5'
          }`}
        />
      </span>
      {/* 文字徽标 */}
      <span
        className={`text-[11px] font-semibold tracking-wide ${
          enabled ? 'text-brand-200' : 'text-ink-500'
        }`}
      >
        {enabled ? '已启用' : '已停用'}
      </span>
    </button>
  );
}

function MicIcon({ size = 12 }: { size?: number }) {
  const c = {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.9,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  };
  return (
    <svg {...c}>
      <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z" />
      <path d="M19 10v2a7 7 0 01-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="23" />
      <line x1="8" y1="23" x2="16" y2="23" />
    </svg>
  );
}

function BrainIcon({ size = 12 }: { size?: number }) {
  const c = {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.9,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  };
  return (
    <svg {...c}>
      <path d="M9.5 2A2.5 2.5 0 0112 4.5v15a2.5 2.5 0 01-4.96.44 2.5 2.5 0 01-2.96-3.08 3 3 0 01-.34-5.58 2.5 2.5 0 01.66-4.92 2.5 2.5 0 014.6-2.36z" />
      <path d="M14.5 2A2.5 2.5 0 0012 4.5v15a2.5 2.5 0 004.96.44 2.5 2.5 0 002.96-3.08 3 3 0 00.34-5.58 2.5 2.5 0 00-.66-4.92 2.5 2.5 0 00-4.6-2.36z" />
    </svg>
  );
}

function genId(prefix: string) {
  return `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
}

function makeEmptyProvider(label?: string): ProviderConfig {
  return {
    id: genId('p'),
    label: label ?? '新厂商',
    enabled: false,
    api_key: '',
    base_url: '',
    models: [],
  };
}

function getModelsOfKind(p: ProviderConfig, kind: 'tts' | 'llm'): ProviderModel[] {
  return p.models.filter(m => m.kind === kind);
}

// =====================================================================
// 主组件
// =====================================================================
export default function ProviderModelsEditor() {
  const [cfg, setCfg] = useState<ProvidersConfig | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [showKey, setShowKey] = useState<Record<string, boolean>>({});
  // [本地编辑草稿，与远端分离；保存时整体替换
  const [draft, setDraft] = useState<ProvidersConfig | null>(null);

  const load = async () => {
    try {
      const data = await api.providersGet();
      setCfg(data);
      setDraft(data);
      setErr(null);
    } catch (e: any) {
      setErr(String(e?.message || e));
    }
  };

  useEffect(() => { load(); }, []);

  // 比较 draft 与远端（cfg），判定是否脏
  const isDirty = useMemo(() => {
    if (!cfg || !draft) return false;
    return JSON.stringify(cfg.providers) !== JSON.stringify(draft.providers)
      || JSON.stringify(cfg.active) !== JSON.stringify(draft.active);
  }, [cfg, draft]);

  if (!draft) {
    return (
      <div className="rounded-lg border border-ink-300/70 bg-ink-200 text-center py-12">
        <div className="mx-auto w-8 h-8 rounded-full border-2 border-brand-500/30 border-t-brand-500 animate-spin mb-3" />
        <div className="text-sm text-ink-500">加载厂商配置…</div>
        {err && <div className="mt-2 text-xs text-rose-300">{err}</div>}
      </div>
    );
  }

  // ---------- 厂商操作 ----------
  const updateProvider = (idx: number, patch: Partial<ProviderConfig>) => {
    const next = [...draft.providers];
    next[idx] = { ...next[idx], ...patch };
    setDraft({ ...draft, providers: next });
  };

  const addProvider = () => {
    const np = makeEmptyProvider('新厂商');
    setDraft({ ...draft, providers: [...draft.providers, np] });
  };

  const removeProvider = (idx: number) => {
    const removed = draft.providers[idx];
    const next = draft.providers.filter((_, i) => i !== idx);
    // 同步清理 active（如果指向被删厂商）
    const newActive = { ...draft.active };
    if (newActive.tts.provider_id === removed.id) {
      newActive.tts = { provider_id: null, model_id: null };
    }
    if (newActive.llm.provider_id === removed.id) {
      newActive.llm = { provider_id: null, model_id: null };
    }
    setDraft({ ...draft, providers: next, active: newActive });
  };

  // ---------- 模型操作 ----------
  const updateModel = (pIdx: number, globalMIdx: number, patch: Partial<ProviderModel>) => {
    const next = [...draft.providers];
    const p = { ...next[pIdx] };
    const models = [...p.models];
    models[globalMIdx] = { ...models[globalMIdx], ...patch };
    p.models = models;
    next[pIdx] = p;

    // 改 kind 时清理 active
    const newActive = { ...draft.active };
    if (patch.kind) {
      const newKind = patch.kind;
      const m = models[globalMIdx];
      if (newActive.tts.provider_id === p.id && newActive.tts.model_id === m.id && m.kind !== 'tts') {
        newActive.tts = { provider_id: null, model_id: null };
      }
      if (newActive.llm.provider_id === p.id && newActive.llm.model_id === m.id && m.kind !== 'llm') {
        newActive.llm = { provider_id: null, model_id: null };
      }
    }
    setDraft({ ...draft, providers: next, active: newActive });
  };

  const addModel = (pIdx: number, kind: 'tts' | 'llm') => {
    const next = [...draft.providers];
    const p = { ...next[pIdx] };
    const newModel: ProviderModel = {
      id: '',
      label: kind === 'tts' ? '新语音模型' : '新语言模型',
      kind,
    };
    p.models = [...p.models, newModel];
    next[pIdx] = p;
    setDraft({ ...draft, providers: next });
  };

  const removeModel = (pIdx: number, globalMIdx: number) => {
    const next = [...draft.providers];
    const p = { ...next[pIdx] };
    const removed = p.models[globalMIdx];
    p.models = p.models.filter((_, i) => i !== globalMIdx);
    next[pIdx] = p;
    // 同步清理 active
    const newActive = { ...draft.active };
    if (newActive.tts.provider_id === p.id && newActive.tts.model_id === removed.id) {
      newActive.tts = { provider_id: null, model_id: null };
    }
    if (newActive.llm.provider_id === p.id && newActive.llm.model_id === removed.id) {
      newActive.llm = { provider_id: null, model_id: null };
    }
    setDraft({ ...draft, providers: next, active: newActive });
  };

  // ---------- active 操作 ----------
  const setActive = (kind: 'tts' | 'llm', provider_id: string | null, model_id: string | null) => {
    setDraft({
      ...draft,
      active: { ...draft.active, [kind]: { provider_id, model_id } },
    });
  };

  // ---------- 保存 ----------
  const onSave = async () => {
    if (!draft) return;
    setSaving(true);
    setMsg(null);
    try {
      // 校验：激活项必须能找到对应模型且 kind 匹配
      for (const kind of ['tts', 'llm'] as const) {
        const a = draft.active[kind];
        if (!a?.provider_id || !a?.model_id) continue;
        const p = draft.providers.find(x => x.id === a.provider_id);
        if (!p) throw new Error(`active.${kind}.provider_id 找不到厂商 ${a.provider_id}`);
        const m = p.models.find(x => x.id === a.model_id);
        if (!m) throw new Error(`active.${kind}.model_id 找不到模型 ${a.model_id}`);
        if (m.kind !== kind) throw new Error(`active.${kind} 指向的模型 kind=${m.kind}，与角色不匹配`);
      }
      const res = await api.providersUpdate(draft);
      setMsg({ ok: true, text: `已保存 ${res.providers.length} 个厂商配置。${res.note ?? ''}` });
      await load();
    } catch (e: any) {
      setMsg({ ok: false, text: String(e?.message || e) });
    } finally {
      setSaving(false);
      setTimeout(() => setMsg(null), 6000);
    }
  };

  const onReset = () => {
    if (cfg) setDraft(cfg);
    setMsg(null);
  };

  // 计算可激活的厂商（已启用 + 含对应 kind 模型）
  const ttsProviders = draft.providers.filter(p => p.enabled && getModelsOfKind(p, 'tts').length > 0);
  const llmProviders = draft.providers.filter(p => p.enabled && getModelsOfKind(p, 'llm').length > 0);

  return (
    <div className="space-y-5">
      {/* 头部 */}
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-[22px] font-semibold text-white leading-tight">模型厂商</h2>
          <p className="mt1 text-sm text-ink-500">
            配置多家 AI 厂商的 API Key 与模型 · 选择激活的 TTS / LLM 模型
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn-ghost" onClick={onReset} disabled={saving || !isDirty}>重置</button>
          <button className="btn-primary" onClick={onSave} disabled={saving || !isDirty}>
            {saving ? '保存中…' : `保存${isDirty ? ' *' : ''}`}
          </button>
        </div>
      </div>

      {/* 提示 */}
      {msg && (
        <div className={`rounded-md px-4 py-3 text-sm flex items-center gap-2 whitespace-pre-wrap ${
          msg.ok
            ? 'border border-lime-500/30 bg-lime-500/10 text-lime-200'
            : 'border border-rose-500/30 bg-rose-500/10 text-rose-200'
        }`}>
          <span className="shrink-0"><Icon name={msg.ok ? 'check' : 'plus'} /></span>
          <span>{msg.text}</span>
        </div>
      )}

      {/* 激活模型区 */}
      <div className="rounded-lg border border-ink-300/70 bg-ink-200 overflow-hidden">
        <div className="px-4 py-3 border-b border-ink-300/70 flex items-center gap-3">
          <span className="w-7 h-7 grid place-items-center text-brand-300 bg-brand-500/10 border border-brand-500/20 shrink-0" style={{ borderRadius: 'var(--radius-xs)' }}>
            <Icon name="check" size={14} />
          </span>
          <div>
            <div className="text-sm font-semibold text-white">当前激活</div>
            <div className="text-xs text-ink-500">选择项目实际使用的 TTS / LLM 模型（仅启用且含对应类型模型的厂商可选）</div>
          </div>
        </div>
        <div className="p-4 grid gap-4 md:grid-cols-2">
          <ActiveSelector
            kind="tts"
            icon={<MicIcon />}
            label="TTS 模型"
            providers={ttsProviders}
            value={draft.active.tts}
            onChange={(pid, mid) => setActive('tts', pid, mid)}
          />
          <ActiveSelector
            kind="llm"
            icon={<BrainIcon />}
            label="LLM 模型"
            providers={llmProviders}
            value={draft.active.llm}
            onChange={(pid, mid) => setActive('llm', pid, mid)}
          />
        </div>
      </div>

      {/* 厂商卡片列表 */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h3 className="text-sm font-semibold text-white">厂商列表</h3>
            <div className="flex items-center gap-1.5">
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-brand-500/15 text-brand-200 border border-brand-500/30">
                <span className="w-1.5 h-1.5 rounded-full bg-brand-400" />
                {draft.providers.filter(p => p.enabled).length} 已启用
              </span>
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-white/[0.04] text-ink-500 border border-white/[0.08]">
                {draft.providers.filter(p => !p.enabled).length} 已停用
              </span>
            </div>
          </div>
          <button onClick={addProvider} className="btn-ghost !py-1 !px-3 text-xs">
            <Icon name="plus" size={13} />
            新增厂商
          </button>
        </div>
        {draft.providers.length === 0 ? (
          <div className="rounded-lg border border-dashed border-ink-300/70 py-10 text-center text-xs text-ink-500">
            暂无厂商，点击「新增厂商」开始添加
          </div>
        ) : (
          draft.providers.map((p, i) => (
            <ProviderCard
              key={p.id}
              p={p}
              pIdx={i}
              keyShown={!!showKey[p.id]}
              onToggleKey={() => setShowKey(s => ({ ...s, [p.id]: !s[p.id] }))}
              onUpdateProvider={updateProvider}
              onRemove={removeProvider}
              onUpdateModel={updateModel}
              onAddModel={addModel}
              onRemoveModel={removeModel}
            />
          ))
        )}
      </div>

      <div className="text-[11px] text-ink-500 leading-relaxed">
        保存后即时生效（修改 <span className="font-mono">PROVIDERS_CONFIG</span> 与 <span className="font-mono">ACTIVE_*</span> 字段），
        重启后端后恢复 .env 默认值或上次保存的结构。
      </div>
    </div>
  );
}

// =====================================================================
// 子组件：单厂商卡片
// =====================================================================
function ProviderCard({
  p, pIdx, keyShown, onToggleKey, onUpdateProvider, onRemove,
  onUpdateModel, onAddModel, onRemoveModel,
}: {
  p: ProviderConfig;
  pIdx: number;
  keyShown: boolean;
  onToggleKey: () => void;
  onUpdateProvider: (idx: number, patch: Partial<ProviderConfig>) => void;
  onRemove: (idx: number) => void;
  onUpdateModel: (pIdx: number, globalMIdx: number, patch: Partial<ProviderModel>) => void;
  onAddModel: (pIdx: number, kind: 'tts' | 'llm') => void;
  onRemoveModel: (pIdx: number, globalMIdx: number) => void;
}) {
  const ttsModels = getModelsOfKind(p, 'tts');
  const llmModels = getModelsOfKind(p, 'llm');

  // 后端返回的 api_key 是脱敏占位 "***LAST4" 或完整字符串。
  // - 用户没改 → 直接当作占位符提交（后端会保留原 key）
  // - 用户改了 → 当作新 key 提交（覆盖）
  // 输入框本身显示占位符，避免展示完整 key；
  // 单独用一个 boolean keyDirty 标识用户输入了内容。
  const isPlaceholder = typeof p.api_key === 'string' && p.api_key.startsWith('***');
  const apiKeyDisplay = isPlaceholder ? p.api_key : (p.api_key || '');

  return (
    <div
      className={`relative rounded-lg border overflow-hidden transition-colors ${
        p.enabled
          ? 'border-brand-500/40 bg-brand-500/[0.03]'
          : 'border-ink-300/70 bg-ink-200 opacity-90'
      }`}
    >
      {/* 卡片左侧启用状态条（颜色条） */}
      <div
        aria-hidden
        className={`absolute left-0 top-0 bottom-0 w-1 transition-colors ${
          p.enabled ? 'bg-brand-500' : 'bg-white/[0.06]'
        }`}
      />
      {/* 卡片头 */}
      <div className={`pl-5 pr-4 py-3 flex items-center gap-3 border-b border-white/[0.04] transition-colors ${
        p.enabled ? '' : 'bg-white/[0.015]'
      }`}>
        <div className="flex items-center gap-2 flex-1 min-w-0">
          <input
            value={p.label}
            onChange={e => onUpdateProvider(pIdx, { label: e.target.value })}
            className={`bg-transparent text-sm font-semibold outline-none border-b border-transparent focus:border-brand-500/50 min-w-0 flex-1 ${
              p.enabled ? 'text-white' : 'text-ink-500'
            }`}
            placeholder="厂商名（如 MiniMax、火山引擎）"
          />
          <input
            value={p.id}
            onChange={e => onUpdateProvider(pIdx, {
              id: e.target.value.replace(/[^a-z0-9_-]/gi, '_').toLowerCase(),
            })}
            className="bg-transparent text-[11px] text-ink-500 font-mono outline-none border-b border-transparent focus:border-brand-500/50 w-32"
            placeholder="id"
            spellCheck={false}
          />
        </div>
        {/* 启用开关（高对比度 + 文字徽标） */}
        <ToggleSwitch
          enabled={p.enabled}
          onChange={next => onUpdateProvider(pIdx, { enabled: next })}
        />
        <button
          onClick={() => onRemove(pIdx)}
          className="w-7 h-7 grid place-items-center rounded-md text-ink-500 hover:text-rose-300 hover:bg-rose-500/10 transition-colors shrink-0"
          title="删除该厂商"
        >
          <Icon name="trash" size={13} />
        </button>
      </div>

      {/* 凭据 + Base URL */}
      <div className="p-4 grid gap-3 md:grid-cols-2">
        <div>
          <div className="text-[11px] text-ink-500 mb-1 flex items-center gap-2">
            <span>API Key（TTS 与 LLM 共用）</span>
            {isPlaceholder && (
              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-brand-500/10 text-brand-300 border border-brand-500/20">
                <span className="w-1 h-1 rounded-full bg-brand-400" />
                已配置（{apiKeyDisplay}）
              </span>
            )}
          </div>
          <div className="relative">
            <input
              type={keyShown ? 'text' : 'password'}
              value={p.api_key}
              onChange={e => {
                // 第一次输入时如果还是占位符，则清空让用户重新填入完整 key
                const v = e.target.value;
                if (isPlaceholder && v === p.api_key) return;
                // 占位符状态下用户改了内容：替换为空（避免把占位符当完整 key 提交）
                const next = isPlaceholder && v.startsWith('***') ? '' : v;
                onUpdateProvider(pIdx, { api_key: next });
              }}
              placeholder={isPlaceholder ? '点击「重写」按钮以填入新的 API Key' : 'sk-...'}
              className="input-base w-full !pr-24 font-mono !text-xs"
            />
            <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-1">
              {isPlaceholder && (
                <button
                  type="button"
                  onClick={() => onUpdateProvider(pIdx, { api_key: '' })}
                  className="text-[11px] text-ink-500 hover:text-brand-300 px-1.5 py-0.5"
                  title="清空当前 key，填入新的"
                >
                  重写
                </button>
              )}
              <button
                onClick={onToggleKey}
                className="text-[11px] text-ink-500 hover:text-ink-700 px-1.5 py-0.5"
              >
                {keyShown ? '隐藏' : '显示'}
              </button>
            </div>
          </div>
          {isPlaceholder && p.api_key === '' && (
            <div className="text-[10px] text-amber-300 mt-1">
              已清空，保存后该厂商的 API Key 会被移除
            </div>
          )}
        </div>
        <div>
          <div className="text-[11px] text-ink-500 mb-1">Base URL</div>
          <input
            type="text"
            value={p.base_url}
            onChange={e => onUpdateProvider(pIdx, { base_url: e.target.value })}
            placeholder="https://api.example.com/v1"
            className="input-base w-full font-mono !text-xs"
          />
        </div>
      </div>

      {/* 模型列表 */}
      <div className="px-4 pb-4 space-y-3">
        <ModelSection
          title="语音模型（TTS）"
          icon={<MicIcon />}
          accent="text-brand-300 bg-brand-500/10 border-brand-500/20"
          models={ttsModels}
          onAdd={() => onAddModel(pIdx, 'tts')}
          onUpdate={(globalMIdx, patch) => onUpdateModel(pIdx, globalMIdx, patch)}
          onRemove={(mIdxInSection) => {
            // 找到全局索引
            const m = ttsModels[mIdxInSection];
            const gIdx = p.models.indexOf(m);
            onRemoveModel(pIdx, gIdx);
          }}
          allModels={p.models}
        />
        <ModelSection
          title="语言模型（LLM）"
          icon={<BrainIcon />}
          accent="text-amber-300 bg-amber-500/10 border-amber-500/20"
          models={llmModels}
          onAdd={() => onAddModel(pIdx, 'llm')}
          onUpdate={(globalMIdx, patch) => onUpdateModel(pIdx, globalMIdx, patch)}
          onRemove={(mIdxInSection) => {
            const m = llmModels[mIdxInSection];
            const gIdx = p.models.indexOf(m);
            onRemoveModel(pIdx, gIdx);
          }}
          allModels={p.models}
        />
      </div>
    </div>
  );
}

// =====================================================================
// 子组件：激活选择器
// =====================================================================
function ActiveSelector({
  kind, icon, label, providers, value, onChange,
}: {
  kind: 'tts' | 'llm';
  icon: React.ReactNode;
  label: string;
  providers: ProviderConfig[];
  value: ActiveModel;
  onChange: (provider_id: string | null, model_id: string | null) => void;
}) {
  const pid = value?.provider_id ?? null;
  const mid = value?.model_id ?? null;
  const curProv = providers.find(p => p.id === pid) ?? null;
  const curModels = curProv ? curProv.models.filter(m => m.kind === kind) : [];
  const curModel = curModels.find(m => m.id === mid) ?? null;

  return (
    <div className="rounded-md border border-ink-300/70 p-3 bg-ink-100">
      <div className="flex items-center gap-2 text-[11px] text-ink-500 mb-2">
        <span className="w-5 h-5 grid place-items-center rounded bg-white/[0.04] border border-white/[0.04]">{icon}</span>
        {label}
      </div>
      <div className="grid grid-cols-[1fr_1.4fr] gap-2">
        <select
          value={pid ?? ''}
          onChange={e => {
            const npid = e.target.value || null;
            onChange(npid, null);
          }}
          className="input-base !py-1.5 !text-xs"
        >
          <option value="">（未选择）</option>
          {providers.map(p => (
            <option key={p.id} value={p.id}>{p.label}</option>
          ))}
        </select>
        <select
          value={mid ?? ''}
          onChange={e => onChange(pid, e.target.value || null)}
          disabled={!pid}
          className="input-base !py-1.5 !text-xs disabled:opacity-40"
        >
          <option value="">（未选择）</option>
          {curModels.map(m => (
            <option key={m.id} value={m.id}>{m.label} ({m.id})</option>
          ))}
        </select>
      </div>
      {curProv && curModel && (
        <div className="mt-2 text-[11px] text-ink-500 truncate">
          当前：<span className="text-ink-700">{curProv.label}</span> / <span className="text-ink-700 font-mono">{curModel.id}</span>
        </div>
      )}
      {providers.length === 0 && (
        <div className="mt-2 text-[11px] text-amber-300">
          暂无可用 {kind.toUpperCase()} 厂商：请在下方启用至少一个厂商并添加 {kind} 模型
        </div>
      )}
    </div>
  );
}

// =====================================================================
// 子组件：模型列表段
// =====================================================================
function ModelSection({
  title, icon, accent, models, onAdd, onUpdate, onRemove, allModels,
}: {
  title: string;
  icon: React.ReactNode;
  accent: string;
  models: ProviderModel[];
  onAdd: () => void;
  onUpdate: (globalIdx: number, patch: Partial<ProviderModel>) => void;
  onRemove: (idxInThisList: number) => void;
  allModels: ProviderModel[];
}) {
  return (
    <div className="rounded-md border border-ink-300/70 overflow-hidden">
      <div className="px-3 py-1.5 bg-white/[0.02] border-b border-ink-300/70 flex items-center gap-2">
        <span className={`w-5 h-5 grid place-items-center rounded border ${accent}`}>{icon}</span>
        <span className="text-[11px] font-semibold text-white">{title}</span>
        <span className="text-[11px] text-ink-500">· {models.length} 个</span>
        <div className="flex-1" />
        <button onClick={onAdd} className="text-[11px] text-ink-500 hover:text-brand-300 inline-flex items-center gap-1">
          <Icon name="plus" size={11} /> 添加
        </button>
      </div>
      <div className="divide-y divide-white/[0.04]">
        {models.length === 0 && (
          <div className="px-3 py-2 text-[11px] text-ink-500">暂无，点击右上「添加」</div>
        )}
        {models.map((m, idx) => {
          const globalIdx = allModels.indexOf(m);
          return (
            <div key={`${m.id}_${idx}_${globalIdx}`} className="px-3 py-2 grid grid-cols-[1fr_1fr_auto] gap-2 items-center">
              <input
                value={m.label}
                onChange={e => onUpdate(globalIdx, { label: e.target.value })}
                placeholder="模型显示名"
                className="input-base !py-1 !text-xs"
              />
              <input
                value={m.id}
                onChange={e => onUpdate(globalIdx, { id: e.target.value })}
                placeholder="model-id（如 speech-01、M3）"
                className="input-base !py-1 !text-xs font-mono"
                spellCheck={false}
              />
              <button
                onClick={() => onRemove(idx)}
                className="w-6 h-6 grid place-items-center text-ink-500 hover:text-rose-300"
                title="删除"
              >
                ×
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}