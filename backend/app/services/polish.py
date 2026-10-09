import hashlib
import json
import logging
from pathlib import Path

from pydantic import BaseModel

from ..ai.factory import get_llm

logger = logging.getLogger(__name__)


FEW_SHOT = r"""
你是一名专业的中文小说文本校对编辑。你的任务是：
1. 修正原文中的错别字、漏字、多字、标点误用（明显输入错误类）
2. 不要改写原意、不要润色文笔、不要增删情节
3. 不要擅自更改引号风格（『』、「」、"" 都是合法的）、不要省略号乱删
4. 修正后自我评估：本次改动是否"合理且必要"，如果你只是改了文风、改了引号风格等则必须 is_reasonable=false
5. 只输出**一个** JSON 对象（以 { 开始、以 } 结束），且只包含 polished_text / is_reasonable / reason 三个字段。
   不要输出数组，不要只输出某个字段的内容，不要输出任何解释文字或 markdown 代码块。

【示例 1】
输入："林若雪走在回家的路上，心理想着明天的考试。"
输出：
{
  "polished_text": "林若雪走在回家的路上，心里想着明天的考试。",
  "is_reasonable": true,
  "reason": "「心理」应为「心里」，属于常见错别字，修正合理。"
}

【示例 2】
输入："他推开门，走进了教师。"
输出：
{
  "polished_text": "他推开门，走进了教室。",
  "is_reasonable": true,
  "reason": "「教师」与上下文「走进」搭配不当，应为「教室」。"
}

【示例 3】
输入："他沉默了许久，终于开口说道：『我……我不知道。』"
如果你的改动只是把『』换成「」，那么 is_reasonable 必须是 false。
输出：
{
  "polished_text": "他沉默了许久，终于开口说道：『我……我不知道。』",
  "is_reasonable": true,
  "reason": "原文无语病，无需修改。"
}

【示例 4】
输入："夕阳西下，断肠人在天涯。"
输出：
{
  "polished_text": "夕阳西下，断肠人在天涯。",
  "is_reasonable": true,
  "reason": "原文无语病，无需修改。"
}
"""


class PolishResult(BaseModel):
    """润色结果（rewrite 模式）。

    字段设计说明：早期版本还有 diff: list[DiffItem]，但下游从未消费它；
    它既是 required、形状又是数组，模型偶尔会把「这个数组」当成整个答案返回
    （顶层回数组 → Pydantic 校验失败 → 白跑一次重试、白烧 token）。故移除。
    reason 保留用于排查（is_reasonable=false 时说明理由），给默认值以降低校验失败面。
    """
    polished_text: str
    is_reasonable: bool
    reason: str = ""


async def polish_with_llm(raw_text: str) -> PolishResult:
    """
    调 LLM 做错别字纠错 + 自我评估。
    is_reasonable=false 时调用方应回退 raw_text。
    """
    prompt = (
        FEW_SHOT
        + "\n\n【现在处理以下文本】\n---RAW TEXT START---\n"
        + raw_text
        + "\n---RAW TEXT END---\n\n请严格输出 JSON，不要任何解释文字。"
    )
    llm = get_llm()
    result = await llm.chat_structured(
        prompt=prompt,
        output_schema=PolishResult,
        temperature=0.1,
        max_tokens=16000,
    )
    from .usage import track_llm
    track_llm(calls=1, chars=len(prompt), detail="polish")
    return result


# =====================================================================
# H-18 diff 模式：LLM 只输出「锚点 → 替换」修改清单，本地应用
# =====================================================================
# 背景（383 章实测）：rewrite 模式要求 LLM 把整章 ~3000 字重新誊写一遍，
# 串行 4h11m（H-17 并发化后 ~1h）；更糟的是誊写时 LLM 顺手改写干净句子，
# 触发 is_reasonable 自评 / 长度比校验拒收 —— 383 章里 51 章被拒（13%），
# ~33 分钟 LLM 时间白烧、13% 的章最终没润上。
# diff 模式下 LLM 只输出错字对（一章通常几条，输出 token ≈ 重写的 1/10~1/30），
# 且「誊写漂移」从根上消失：干净句子根本不会被重新生成。
# 旧 PolishResult 保留为 POLISH_MODE=rewrite 回退通道。

DIFF_FEW_SHOT = r"""
你是一名专业的中文小说文本校对编辑。找出下面文本中的错别字、漏字、多字、标点误用，输出一份最小化的修改清单。

修改规则（非常重要）：
1. 只修正明显的输入错误类问题：错别字、同音字混淆、漏字、多字、标点误用。
2. 不要改写原意、不要润色文笔、不要增删情节、不要合并拆分段落。
3. 不要更改引号风格（『』、「」、"" 都是合法的），不要动省略号。
4. 每处修改输出一条：anchor 必须是原文中**逐字连续出现**的片段（把出错的那半句话原样抄下来，通常 10~30 字，在原文中能唯一定位），replacement 是修正后的同一片段。
5. 没有任何错误时输出 {"data": []}。
6. 只输出一个 JSON 对象（以 { 开始、以 } 结束），格式为 {"data": [{"anchor": "...", "replacement": "..."}]}，不要输出任何解释文字或 markdown 代码块。

【示例 1】
输入："林若雪走在回家的路上，心理想着明天的考试。"
输出：{"data": [{"anchor": "心理想着明天的考试", "replacement": "心里想着明天的考试"}]}

【示例 2】
输入："他推开门，走进了教师。"
输出：{"data": [{"anchor": "走进了教师。", "replacement": "走进了教室。"}]}

【示例 3】
输入："夕阳西下，断肠人在天涯。"
输出：{"data": []}
"""


class PolishDiffItem(BaseModel):
    """单处修改：anchor 为原文中的连续片段，replacement 为修正后的同片段。"""
    anchor: str
    replacement: str


class PolishDiffResult(BaseModel):
    """diff 模式润色结果：修改清单，空列表 = 原文无误。

    data 是唯一字段且必填（required）——模型漏给会直接校验失败触发重试，
    而不是静默当成「原文无误」处理（那会把有错的章错误标记为已认证干净）。
    """
    data: list[PolishDiffItem]


async def polish_diff_with_llm(raw_text: str) -> PolishDiffResult:
    """diff 模式：让 LLM 只输出错字修改清单（H-18，默认模式）。"""
    prompt = (
        DIFF_FEW_SHOT
        + "\n\n【现在处理以下文本】\n---RAW TEXT START---\n"
        + raw_text
        + "\n---RAW TEXT END---\n\n请严格输出 JSON，不要任何解释文字。"
    )
    llm = get_llm()
    result = await llm.chat_structured(
        prompt=prompt,
        output_schema=PolishDiffResult,
        temperature=0.1,
        max_tokens=8000,
    )
    from .usage import track_llm
    track_llm(calls=1, chars=len(prompt), detail="polish")
    return result


# ---- 本地 diff 应用引擎 ----

# 单条 diff 的长度比防护：错字修正的 replacement 与 anchor 应同量级。
# 上界拦「把短锚点扩写成整句」的越权改写，下界拦「整段替换成一个字」的删减；
# 两端各留 5 倍余量，只拦明显异常（漏字补全 anchor=5→replacement=6 之类完全合法）。
_DIFF_ITEM_RATIO_RANGE = (0.2, 5.0)
# 整章应用后的总长度比防护（与 rewrite 模式 0.5~1.5 的口径一致）
_CHAPTER_RATIO_RANGE = (0.5, 1.5)

# 归一化匹配时统一引号风格（LLM 偶尔把原文的『』抄成「」）
_OPEN_QUOTES = "『「“‘"
_CLOSE_QUOTES = "』」”’"


def _normalize_for_match(s: str) -> tuple[str, list[int]]:
    """压缩全部空白 + 统一引号风格，返回 (归一化串, 原始下标映射)。

    归一化只删/换不合并 —— 每个保留字符对应原文唯一位置，
    找到归一化锚点后可经 idx_map 精确回溯到原文区间。
    """
    out_chars: list[str] = []
    idx_map: list[int] = []
    for i, ch in enumerate(s):
        if ch.isspace():
            continue
        if ch in _OPEN_QUOTES:
            ch = "「"
        elif ch in _CLOSE_QUOTES:
            ch = "」"
        out_chars.append(ch)
        idx_map.append(i)
    return "".join(out_chars), idx_map


def _find_anchor_span(text: str, anchor: str) -> tuple[int, int] | None:
    """在 text 中定位 anchor，返回原文区间 [start, end)；找不到返回 None。

    两级匹配：先精确 find；失败再归一化匹配（容忍空白/引号风格的誊写差异）。
    """
    pos = text.find(anchor)
    if pos >= 0:
        return pos, pos + len(anchor)
    t_norm, t_map = _normalize_for_match(text)
    a_norm, _ = _normalize_for_match(anchor)
    if not a_norm:
        return None
    j = t_norm.find(a_norm)
    if j >= 0 and j + len(a_norm) <= len(t_map):
        # 归一化命中的原始区间可能比 len(anchor) 长（区间内空白被压缩），
        # 必须经 idx_map 回溯，否则会替换出残缺片段。
        return t_map[j], t_map[j + len(a_norm) - 1] + 1
    return None


def apply_polish_diffs(text: str, result: PolishDiffResult) -> tuple[str, str]:
    """应用 diff 清单，返回 (new_text, outcome)。

    outcome ∈ {"clean", "changed", "rejected"}：
    - clean：清单为空 / 全是 no-op（LLM 认证原文无误）
    - changed：≥1 条修改成功应用
    - rejected：LLM 给出了真实修改意图（anchor≠replacement 的条目）但一条都没
      应用上（锚点全部无法定位或未通过防护）—— 保留原文，且不写 checkpoint，
      重跑 prepare 时会补跑该章。

    防护设计：单条 anchor 失配只丢那一条（missed 计数语义，不影响其余条目），
    单条长度比异常只丢那一条；只有「全军覆没」或「整章总长度比异常」才拒绝
    整章 —— 不存在 rewrite 模式「一次誊写漂移废掉整章」的放大效应。
    """
    items = result.data or []
    if not items:
        return text, "clean"

    cur = text
    applied = 0
    # 区分「no-op（LLM 表达无需修改）」与「真实修改意图」：全是 no-op → clean，
    # 有真实意图但全军覆没 → rejected。不区分的话 anchor==replacement 会被误判
    # 成 rejected，把「LLM 认证无误」错当失败处理。
    noop_n = 0
    attempted_n = 0
    for it in items:
        anchor = (it.anchor or "").strip()
        replacement = (it.replacement or "").strip()
        if not anchor or anchor == replacement:
            noop_n += 1
            continue
        attempted_n += 1
        lo, hi = _DIFF_ITEM_RATIO_RANGE
        ratio = len(replacement) / max(len(anchor), 1)
        if not (lo <= ratio <= hi):
            continue
        span = _find_anchor_span(cur, anchor)
        if span is None:
            continue
        s, e = span
        cur = cur[:s] + replacement + cur[e:]
        applied += 1

    if applied == 0:
        if attempted_n == 0:
            return text, "clean"
        return text, "rejected"
    lo, hi = _CHAPTER_RATIO_RANGE
    if not (lo <= len(cur) / max(len(text), 1) <= hi):
        return text, "rejected"
    if cur == text:
        return text, "clean"
    return cur, "changed"


# =====================================================================
# 润色 checkpoint（sidecar）：v2 带章节指纹，prepare 成功后保留
# =====================================================================
# 语义（H-18）：
# - 文件：data/polish_<pid>.json，格式 {"_version": 2, "_fingerprint": ...,
#   "chapters": {idx_str: 润色后全文}}。changed 与 clean（LLM 认证无误）都记录；
#   rejected / failed 不记录 —— 重跑 prepare 时只有这些章会补跑 LLM，
#   这就是「上次没润上的章」的补录路径（383 章实测 59 章未润上）。
# - 指纹 = 切分后各章文本的 sha256：源文件被替换 / 切分参数（CHAPTER_SPLIT_PATTERNS
#   等）变化 → 指纹失配 → 整个 checkpoint 作废重跑，绝不跨内容串档。
# - prepare 成功后**不再删除**（旧代码删，导致 rejected 章的补录只能全量重跑）；
#   项目删除时由 delete_project 清理。
# - v1 兼容：无 _fingerprint 的裸 {idx: text} 只存在于旧版中断现场，按原样接受。

def polish_sidecar_fingerprint(chapter_texts: list[str]) -> str:
    """以「切分后的章节文本序列」做 checkpoint 有效性指纹。"""
    h = hashlib.sha256()
    h.update(str(len(chapter_texts)).encode("utf-8"))
    for t in chapter_texts:
        h.update(b"\x00")
        h.update((t or "").encode("utf-8"))
    return h.hexdigest()


def load_polish_sidecar(path: Path, fingerprint: str) -> dict[str, str]:
    """加载 sidecar，返回 {idx_str: 润色后文本}。指纹失配 / 文件损坏 → 空表。"""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    if "_fingerprint" in obj:
        if obj.get("_fingerprint") != fingerprint:
            logger.info(
                "润色 checkpoint 指纹失配（源文件或切分结果已变化），作废重跑: "
                f"{path.name}"
            )
            return {}
        chapters = obj.get("chapters")
    else:
        # v1（旧格式）：只可能是历史中断现场，无法校验指纹，按原样接受
        chapters = obj
    if not isinstance(chapters, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in chapters.items()
        if isinstance(v, str) and v
    }


def save_polish_sidecar(path: Path, fingerprint: str, chapters: dict[str, str]) -> None:
    """sidecar 落盘（OSError 不抛出 —— checkpoint 写失败绝不阻塞润色流程）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "_version": 2,
                    "_fingerprint": fingerprint,
                    "chapters": chapters,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(f"润色 checkpoint 落盘失败（不影响流程）: {path.name} -> {e}")
