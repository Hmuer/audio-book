# AI 有声书制作流程质量优化方案（完整版）

> 目标：**成品质量越高越好**。本文件为证据化诊断 + 可落地的改造设计。
> 覆盖：选择/导入 → 识别（润色/角色/对白）→ 合成 → 全部音频生成。
> 诊断依据：`~/.novel-tts/data/app.db`（真实项目）、`data/logs`、COS 产物、当前代码。

---

## 0. 结论速览

| # | 现象 | 根因（已定位，含证据） | 优先级 |
|---|---|---|---|
| 1 | 全书听感"很平" | 所有旁白/对白都以 `emotion="calm"` 合成；MiniMax 逐段情绪阶段被整体跳过、指令也不下发 | **P0** |
| 2 | **对白重复播放**（旁白读一遍 + 对白音色再读一遍） | 对白锚点 `anchor_start` 系统性不准；`_backfill_anchor_text` 只校验"在范围内"不校验"切片==原文"；`build.py:2293` 按错误 anchor_start 排序；`chapter.py` 据此切旁白 → 旁白切片把对白原文吞进去，随后对白段再读一遍 | **P0** |
| 3 | **对白漏识别**（原文有引号却没被当对白） | 分段完全依赖 LLM 找引号；确定性统计：79 章里 47 章「引号对数 − 识别对白数 ≥3」，如第 49 章 35 对引号只识别出 17 条 | **P0** |
| 4 | **作者拉票/注释/本章完被朗读** | 正文未做元文本清洗；元尊全文命中 `本章完`×22、`土豆(公众号)`×4、`月票`×3，另有第 0 章整章"土豆新书预览"广告 | **P0** |
| 5 | **《仙途遗祸》长书失败** | 1962 章/730 万字，prepare 在角色/对白阶段遇 LLM 402 配额耗尽 + 422 内容审核拒绝即中止；且**章节只在流程最后一步落库**，一失败 `chapter_count=0`，无法构建/续跑 | **P0** |
| 6 | 音色/响度/专名 | 旁白快照与项目设定不一致；大量「未知」性别角色一律兜底男声；整书 -22.3 LUFS 偏轻；无发音词典 | P1 |
| 7 | 产能 | MiniMax 默认 10 RPM，元尊约 2.2 万段构建约 3 天 | P2 |

---

## 1. 详细诊断（证据）

### 1.1 全书"平"（情绪缺失）
- 元尊 `aa6fefa5…`：567 章、1,674,165 字、构建 `4b31a8bc…` 成功、MiniMax `speech-2.8-hd`、总时长 89.8h。
- 代码：`project.py:2559` 在 `active_tts=="minimax"` 时**整段跳过**逐句语音指令；`build.py:2624` 固定 `emotion=seg_emo or "calm"`；`minimax/tts.py:262` 的 `instruction_text` 参数**被忽略**。
- 结果：打斗/质问/悲恸/日常同一平静腔调 → 这是"听感差"主因，不是音质码率（32kHz/128kbps 够用）。

### 1.2 对白重复（高优先，用户重点反馈）
- 复算 59 章：**19 章存在重复，共 127 句对白被"旁白+对白"读两遍**。
- 例：第 3 章 `徐林：「不过拳脚无眼，等会伤到了殿下，可不要怪罪于我。」`
  - LLM 给的 `anchor_start=4121`，实际原文位置 `find=4027`（偏差 ~94 字）。
  - 第 3 章 37 条对白的 `anchor_start` **非单调递增**（顺序错乱）。
- 链路：①`dialogue.py:_backfill_anchor_text` 仅判断 `0<=start<end<=len` 就采信（不验证 `chapter_text[start:end]==anchor.text`）→ 错误锚点入库；②`build.py:2293` `lst.sort(key=anchor_start)` 用错误锚点排序 → 顺序错乱；③`chapter.py` 顺次切 `[cursor, local_start)` 作旁白 → 落在错误位置的旁白切片把对白原文包含进来 → 对白段再读一次。

### 1.3 对白漏识别（用户重点反馈）
- 全文引号统计：`“”`≈25756、`『』`74、`「」`5、`"`4 —— 原文以标准弯引号为主。
- 确定性引号对 vs 已识别对白：79 章中 **47 章缺口≥3**；例第 49 章 35 对引号仅 17 条对白、第 56 章 42 对仅 31 条。
- 根因：**分段完全依赖 LLM**。LLM 会漏引号句、合并连续引号、或对非常规引号（`『』`/直引号/不配对）识别差；漏掉的引号句被当旁白平读。

### 1.4 作者拉票/注释被朗读（用户重点反馈）
- 元尊全文命中：`本章完`×22（多为章末"（本章完）8)"等抓取噪声）、`土豆/公众号`×4、`月票`×3（"请投给元尊吧"）、第 0 章整章为新书预览广告。
- 这些都在 `project_chapters.text` 里，会原样朗读。

### 1.5 长书失败（《仙途遗祸》）
- `559dc960…`：21.79MB、7,304,968 字、1962 章；`status=failed`、`chapter_count=0`。
- `last_error=LLMContentRejectedError 422 output new_sensitive`；`prev_error=LLMQuotaExhausted 402`；预估 2782 次 LLM / 7.73h。
- `project.py` 章节落库在**第 7 步（2745–2817）**，位于 polish/角色/去重/对白/指令/资料/音色**全部之后**；角色/对白阶段对 422 无容错 → 一失败章节为空、项目卡死。

---

## 2. 改造设计（分模块）

### M1 对白分段重构 —— 彻底消除"重复"与"漏识别"（P0，核心）
**设计：把"分段"从 LLM 手里收回来，改成确定性引号解析；LLM 只负责"这句是谁说的"。**
1. 新增 `services/quote_segmenter.py`：
   - 支持 `“”`、`「」`、`『』`、直引号 `"`、以及常见不配对/嵌套的稳健配对；
   - 归一化非常规引号（`＂„‟`→`“”`），对不配对引号做启发式闭合；
   - 输出**有序、互不重叠的引号区间** `[(start,end,inner_text), ...]`；
   - 过滤明显非对白引号（如书名号内、纯标点）。
2. `chapter.py:_build_segments_for_chapter` 改为**单次前向扫描 + 区间合并**算法：
   - 以确定性引号区间为"对白区"，其余为"旁白区"；
   - 逐字符前向推进，**保证旁白区绝不包含任何对白区文本** → 结构上不可能重复；
   - 对白区用 `dlg.text`（按区间回填的对白文本）朗读，音色按 speaker 归属。
3. `dialogue.py` 的 LLM 结果仅用于**给引号区间打 speaker**：
   - 按"LLM anchor 与哪个确定性引号区间重叠"做匹配，不再依赖 LLM 的字符偏移；
   - 未匹配到 speaker 的引号区间：用旁白默认性别/上下文就近"XX 道/说"提示词推断；仍未知则用默认音色，**绝不回退成"旁白读对白"**。
4. 锚点规范化：新增 `normalize_dialogue_anchors(chapter_text, dialogues)`，按确定性区间重算 `anchor_start/end`；`build.py` 排序改用规范化后位置（或直接用引号区间顺序）。

> 收益：一次性同时消灭"重复播放"和"漏识别"两大类问题；也不再受 LLM 偏移不准影响。风险中等但可充分单测（见 §4）。

### M2 元文本清洗 —— 不读拉票/注释/广告（P0，低风险）
- 新增 `services/text_cleanup.py`，在**切章后、落库前**清洗每章正文：
  - 删除整行/整段明显元文本：`本章完/未完待续/（求月票/推荐票/求订阅/求收藏/打赏/加更/码字/PS/作者的话/公众号/微博/QQ群…）`；
  - 删除章末"（本章完）8)"这类抓取噪声；
  - 识别"新书预览/广告页"整章（如第 0 章）→ 标记跳过（不朗读，保留原文供核对）；
  - 保守策略：只删"整行命中"或"明确括号注释"，并输出**清洗报告**（删了哪些行、多少字），可一键关闭（`TEXT_CLEANUP_ENABLED`，默认开）。
- 接入点：`project.py` 导入/切章流程、`book_split` 之后。清洗只改用于合成的 `project_chapters.text`，不动用户上传原文件。

### M3 MiniMax 逐段情绪（P0）
- `ProjectDialogue` 新增列 `emotion VARCHAR(16) DEFAULT ''`（`models.py` + `session.py` 迁移字典），旧库自动 `ALTER TABLE`。
- 把 `voice_instruction` 阶段改造为**产出 emotion**（并保留豆包 instruction）：
  - LLM 输入：对白 + 紧邻描写 + 角色设定；
  - 输出：`{"chapter_idx","segment_index","emotion","instruction"}`；emotion ∈ `happy/sad/angry/fearful/surprised/disgusted/calm`；
  - MiniMax 激活时**不再跳过**该阶段（`project.py:2559` 去掉 minimax 短路）。
- 旁白：构建弹窗可选旁白情绪；默认 `calm`，可按章给基调（后续增强）。
- `build.py`：MiniMax 用 `emotion=seg.emotion or "calm"`；豆包仍走 `context_texts`。
- 缓存键已含 emotion/instruction，切换后自动失效重合成。

### M4 长书健壮性（P0）
1. **章节尽早落库**：切章完成后立即 `save_chapters` + `p.chapter_count`（状态保持 `preparing`），后续失败仍可构建已识别部分/断点续跑；最终 step7 再覆盖为 ready。
2. **阶段级内容审核容错**：角色/去重/对白阶段捕获 `LLMContentRejectedError` → 该批降级（角色走名单档案、对白该章留空待补），记录 warning，**不中止整体**。
3. **角色规模治理**：长书默认走「提及名单→档案」路径（已有 `build_profiles_from_mentions_with_llm`）；只对出现频次 Top-N 角色做两两去重；N 超阈值时分桶，避免 N² 爆炸。
4. **分卷生成**：提供"按卷 / 每 N 章"拆分子构建（可选，先提示用户长书建议分卷），把单次规模压到可控、失败面缩小。
5. 失败状态改为 `partial_success + 可重试`，前端展示"缺哪些章可补跑"。

### M5 音色/响度/文本（P1）
- 角色性别推断增强（`未知` 时综合称呼/上下文，默认跟旁白性别而非一律男声）。
- 构建弹窗校验：旁白音色必须与项目设定一致（不一致给黄色提示）。
- 整书统一响度归一（目标 -16～-18 LUFS / TTS 合成后用 ffmpeg loudnorm，逐章同参数避免跳变）。
- 发音词典自动生成（人名/专名，`pronunciation.py` 已有应用逻辑，补自动生成）。

### M6 产能（P2）
- MiniMax 充值后 `MINIMAX_TTS_RPM_LIMIT` 10→20（约减半构建时间）。
- 章节级试听/质检看板（响度、静音比例、失败段）。

---

## 3. 路线图（建议实施顺序）

| 阶段 | 内容 | 优先级 | 风险 | 预估 |
|---|---|---|---|---|
| S1 | M1 对白分段重构 + M2 文本清洗 | P0 | 中 | 高收益 |
| S2 | M3 逐段情绪（MiniMax） | P0 | 中 | 高收益 |
| S3 | M4 长书健壮性 | P0 | 中高 | 解决仙途 |
| S4 | M5 音色/响度/发音 | P1 | 低 | 打磨 |
| S5 | M6 产能/看板 | P2 | 低 | 长期 |

---

## 4. 测试与验证

- 新增单测：
  - `test_quote_segmenter.py`：多引号形态、不配对、嵌套、重复短句 → 区间不重叠且覆盖全部引号；
  - `test_no_dialogue_in_narrator.py`：对元尊真实章节样本，断言任何旁白段不包含对白原文（回归"重复"）；
  - `test_text_cleanup.py`：拉票/本章完/预览页样例被删、正常叙事不被误删；
  - `test_emotion_synthesis.py`：MiniMax 段 emotion 透传；豆包 instruction 不变；
  - `test_long_book_persist.py`：模拟后续阶段失败，章节仍落库、可 retry。
- 真机验证：按用户选择**只跑 1–2 章样章**（元尊第 1、3 章），对比优化前后：重复率、漏识别数、听感。
- 全量 `pytest backend/tests -q` 回归（当前基线约 507 passed）。
- 不改线上、不部署；只改仓库 + 跑测试 + 提交推送 main（遵循 `AGENTS.md`）。

---

## 5. 待确认

- 实施范围：是否按 S1→S3 全部实现（推荐），还是先只做 S1（对白重复+漏识别+清洗，用户最痛）。
- 样章：跑元尊第 1、3 章对比（已确认只跑 1–2 章）。

---

## 6. 实施状态（本轮已落地并测试）

| 模块 | 状态 | 落地位置 |
|---|---|---|
| M1 确定性对白分段（去重复/防漏识别） | ✅ 已实现 | `services/quote_segmenter.py`（新）、`services/dialogue.py:complete_dialogues_with_quotes`、`services/project.py`（对白批结果按引号区间补全）、`services/chapter.py`（区间并集切分，旁白结构上不含对白） |
| M2 元文本清洗 | ✅ 已实现 | `services/text_cleanup.py`（新）、`services/project.py`（切章后清洗 + 跳过非正文页） |
| M3 MiniMax 逐段情绪 | ✅ 已实现 | `db/models.py` + `db/session.py`（新增 `ProjectDialogue.emotion` 列，旧库自动迁移）、`services/dialogue_style.py`（新，LLM 生成情绪）、`services/project.py`（minimax 不再跳过语气阶段）、`services/chapter.py` + `services/build.py`（逐段透传/纳入 digest） |
| M4 长书健壮性（核心） | ✅ 已实现 | `services/project.py`（角色去重遇 422/异常降级不中断，解决《仙途遗祸》致命失败）、`services/dialogue.py`（空结果改为引号补全而非整批抛错） |
| M5 音色/响度/发音 | ✅ 音色兜底 + 响度归一已实现；发音沿用人工规则 | `services/build.py`（`_guess_gender_from_name` + 未知性别按分类/名字兜底；`_normalize_chapter_loudness` 章节 loudnorm -16 LUFS）、`core/config.py`（`AUDIO_LOUDNESS_*`）。发音规则 `ProjectPronunciationRule` 仍手工维护、构建时应用 |
| M-QA 章级质检 | ✅ 已实现 | `services/chapter_qa.py`（新）、`services/build.py:_h5_quality_summary`（逐章 reasons：时长异常/漏识别/未知/低置信） |
| 提前落盘章节 | ✅ 已实现 | `services/project.py` 切章+清洗后立即 `save_chapters` |
| M6 产能 | ⏳ 未实现 | 后续迭代 |

**验证**
- 全量 `pytest backend/tests -q`：**590 passed**。
- 新增测试：`test_quote_segmenter.py`(15)、`test_text_cleanup.py`(13)、`test_quality_fixes.py`(5)。
- 真实《元尊》前 120 章回归：见 `docs/quality-sample-diff.md`（旧逻辑 113/120 章存在「旁白吞对白」，新逻辑 0）。

**注意**：新代码需用户执行 `/opt/audio-book/reinstall.sh` 重启后生效（遵循 AGENTS.md 不代部署）；真实音频样章需在新代码运行后再生成（详见样章对比文档）。

---

## 7. 分阶段架构改造（Phase，按用户批准推进）

**Phase 1（已实现并部署）**
- 提前落盘章节（切章+清洗后立即写 `project_chapters`/`chapter_count`），长书失败不再「0 章」。
- 章级质检 M-QA（`services/chapter_qa.py`）：时长/字数比、引号句漏识别、未知/低置信说话人；`quality_report.suspicious` 带 `reasons`。
- 失败收尾：仅当「已落盘且有 `project_dialogues` 行」才置 `partial_success`，避免产出整本旁白平读的坏成品（真正的「按已完成章构建」在 Phase 2 落地）。

**Phase 2（已落地）**
- 章节级增量落盘：对白阶段每批处理完立即写该批章节的 `project_dialogues`（`_replace_chapter_dialogues`），长书中途失败时已完成批次的对白已入库，可断点续跑/部分构建。
- 部分构建：`start_build` 在 `project.status=partial_success` 且有 `dialogue_completed_chapters` 时，**只构建已识别章节**（`_select_build_chapters`），未识别章不会被合成成整本旁白平读。
- 情绪/指令逐章写回：情绪/指令阶段生成后立即按章 UPDATE 已落盘对白行的 emotion/instruction，后续阶段失败时部分构建也能带情绪（而非回落 calm）。

**Phase 3（后端已落地，UI 待接）**
- `plan_volumes(total, max_per_volume)`：把超长书按每卷 N 章（默认 `BUILD_VOLUME_MAX_CHAPTERS=200` 常量兜底）切卷。
- `start_build(..., chapter_range=(start,end))`：只构建指定章区间（分卷构建）；与 partial_success 的已识别章集合可叠加。
- 逐章试听：构建页已支持单章播放/下载（既有能力）。
- 待接：前端"按卷构建/可疑章重跑"按钮（后端 `retry-failed` 已支持 `chapters` 指定章）。
