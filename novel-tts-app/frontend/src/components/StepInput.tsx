'use client';

import { useState } from 'react';
import { api, PrepareResp, Voice } from '@/lib/api';

export default function StepInput({
  voices,
  busy,
  setBusy,
  onDone,
}: {
  voices: Voice[];
  busy: boolean;
  setBusy: (b: boolean) => void;
  onDone: (r: PrepareResp) => void;
}) {
  const [text, setText] = useState<string>(DEMO_TEXT);
  const [enablePolish, setEnablePolish] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    setErr(null);
    if (!text.trim()) return setErr('文本不能为空');
    if (text.length > 50000) return setErr('文本不能超过 50000 字');
    setBusy(true);
    try {
      const r = await api.prepare(text, enablePolish);
      onDone(r);
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="grid lg:grid-cols-5 gap-6">
      <div className="lg:col-span-3 card space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Step 1 · 粘贴小说正文</h2>
          <span
            className={`chip ${
              text.length > 50000 ? 'bg-red-500/20 text-red-300' : 'bg-white/10'
            }`}
          >
            {text.length} / 50000
          </span>
        </div>
        <textarea
          value={text}
          onChange={e => setText(e.target.value)}
          placeholder="粘贴小说正文（支持 50000 字以内）"
          className="textarea min-h-[380px] font-mono text-sm leading-relaxed"
          maxLength={50000}
        />

        <div className="flex flex-wrap items-center justify-between gap-4 pt-2">
          <label className="flex items-center gap-2 select-none cursor-pointer">
            <Switch checked={enablePolish} onChange={setEnablePolish} />
            <span className="text-sm">启用 LLM 错别字优化（并自我评估，过度修改自动回退）</span>
          </label>
          <button
            className="btn-primary"
            onClick={submit}
            disabled={busy || !text.trim()}
          >
            {busy ? 'LLM 正在分析（约 30-60s）…' : '🔍 准备章节 →'}
          </button>
        </div>

        {err && (
          <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
            {err}
          </div>
        )}
      </div>

      <div className="lg:col-span-2 card space-y-4">
        <h3 className="font-semibold">📋 准备步骤说明</h3>
        <ol className="list-decimal list-inside space-y-2 text-sm text-white/70">
          <li>LLM 校对错别字并<b className="text-white">自我评估</b>改动是否合理（不合理时回退原文）</li>
          <li>LLM 提取<b>角色</b>（姓名/性别/年龄/性格），并判断重名</li>
          <li>LLM 为每段对白标记说话人 + 置信度</li>
          <li>LLM 按语义<b>分章</b>（而非字数硬切），并生成章节标题</li>
          <li>LLM 为每个角色推荐 56 种中文音色</li>
        </ol>
        <div className="rounded-xl bg-white/5 border border-white/10 p-4 text-xs text-white/60 space-y-2">
          <div className="font-semibold text-white/80">💡 小提示</div>
          <div>当前音色库：<b className="text-white">{voices.length}</b> 种中文音色</div>
          <div>合成：每段对白独立 TTS → 插入静音 → MP3 拼接</div>
          <div>章节标题后自动插入 1.5s 静音（无需 LLM）</div>
        </div>
        <button
          className="btn-outline w-full"
          onClick={() => setText(DEMO_TEXT)}
          disabled={busy}
        >
          📝 填入示例文本
        </button>
      </div>
    </section>
  );
}

function Switch({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (b: boolean) => void;
}) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition ${
        checked ? 'bg-brand-600' : 'bg-white/15'
      }`}
    >
      <span
        className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow transition ${
          checked ? 'translate-x-5' : 'translate-x-0'
        }`}
      />
    </button>
  );
}

const DEMO_TEXT = `林若雪今年十七岁，是个内向的高二女生。她总是低着头走路，长长的刘海遮住眼睛，仿佛把自己关在一个透明的玻璃罩里。

同桌李明却恰恰相反。他性格开朗，爱笑，篮球打得好，是全班的阳光担当。
「喂，林若雪，」李明凑过来戳戳她的胳膊肘，「周末一起去图书馆吧？」

林若雪心里咯噔一下，脸一下子红了。「我……我不去了。」她小声说。

「真可惜。」李明耸耸肩，转回头去和后排的王胖子打闹。王大爷是街角卖冰棍的老人，认识林若雪很多年了。每次她路过，都会多给她舀一勺红豆。

这天下雨，林若雪没带伞。她站在教学楼下发呆，一把蓝色的伞突然从旁边递过来。
「给你。」李明笑着说，「我家近，跑两步就到。」
没等她拒绝，他已经冲进了雨里。

林若雪握着那把伞，心里某个地方软了一下。街角的王大爷看到这一幕，嘿嘿一笑：「年轻真好哦。」
`;
