# Checklist · 豆包 TTS 2.0 升级 + GLM-5.3 修复清单

> **目的**：本轮"重新审计 → 修 bug → 升级到 v3"的长期跟踪清单，每完成一项把 `[ ]` 改成 `[x]` 并在右侧 commit 链接列写上 commit id（或 PR 链接）。
>
> **关联文档**：
> - 笔记 `.trae/notes/doubao-voice-apis.md`（API 速查 / 字段名 / 鉴权 / 错误码）
> - 决策记录：本轮选择 **"继续用 TTS 2.0（选 A）"**，不接播客 API，删除/废弃 multicast 自造端点
>
> **优先级图例**：
> - 🔴 P0 = 必修（不修就跑不通）
> - 🟡 P1 = 推荐（P0 跑通后自然衔接）
> - 🟢 P2 = 增值（后续按需）
>
> **测试文件约定**：新增测试一律 `backend/tests/test_<主题>_red.py`（沿用现有 RED 命名约定）。
>
> **完成约定**：勾选 `[x]` 时必须满足
> 1. 代码已合并（或本地 commit）
> 2. 对应测试通过
> 3. 在"完成日期"列填入日期

---

## 🔴 P0 — 正确性（先修，否则用户跑不通）

### P0-1 修复 `_http_post_bytes`：解析 v1 响应 JSON 业务码 + base64 解码
- [x] 完成
- 位置：[tts.py:749-763](file:///workspace/backend/app/ai/providers/doubao/tts.py#L749-L763)
- 关键改动：
  - 返回 `(bytes, raw_response)` 或抛带 `code`/`message` 的异常
  - 调用点 [tts.py:636-638](file:///workspace/backend/app/ai/providers/doubao/tts.py#L636-L638) 改成"解析 JSON → 检查 `code==3000` → `base64.b64decode(data)`"
  - 重试判定从 HTTP status 改成业务码
- 测试：`backend/tests/test_doubao_response_parsing_red.py`（新）
- 完成日期：2026-09-09
- Commit：983f0e0

---

### P0-2 全量重建豆包音色表 + 情感/时间戳/语言元数据
- [x] 完成
- 位置：[tts.py:182-1172](file:///workspace/backend/app/ai/providers/doubao/tts.py#L182-L1172)（`_BUILTIN_VOICES` 重写）
- 关键改动：
  - 严格以官方 [97465](https://docs.volcengine.com/docs/6561/97465?lang=zh)（小模型）+ [1257544](https://docs.volcengine.com/docs/6561/1257544?lang=zh)（2.0 大模型）为准
  - 共收录 **187 条**官方真实音色：小模型 92 条（BVxxx_streaming）+ 大模型 95 条（zh_xxx_uranus_bigtts）
  - 每条新增元数据字段：`languages / supports_emotion / supports_subtitle / supports_language / free / model`
  - `model` 字段区分 `seed-tts-1.0`（小模型）/ `seed-tts-2.0`（大模型），供后续 P1-1 v3 协议路由用
  - `free=True` 严格按火山 FAQ「21 款免费音色」白名单标，杜绝误标
  - **删除所有自创 id**：`BV030_stream` ~ `BV613_stream` 全段、`zh_female_xxx` / `zh_male_xxx` 整组历史 zh_* 命名空间全部清空（grep 确认官方表里不存在）
  - 拼写修正：原 `BVxxx_stream` → 官方 `BVxxx_streaming`（带 ing 后缀）
  - 保留 `voices_doubao.json` 自定义覆盖机制
- 测试：`backend/tests/test_doubao_voices_red.py`（新，11 个场景）+ `backend/tests/test_doubao_task3_red.py`（sample_ids 更新）
- 完成日期：2026-09-09
- Commit：a1b576a（音色表重建）+ 35bb9ea（测试更新）

---

### P0-3 时长估算改用 MP3 帧头解析
- [x] 完成
- 位置：[tts.py:31-37](file:///workspace/backend/app/ai/providers/doubao/tts.py#L31-L37)（`_estimate_mp3_duration_ms`）
- 关键改动：
  - 用 mutagen 或自写 MPEG frame header 解析（查 bitrate/sample_rate 表算帧时长）
  - 接受精度 ±50ms
- 测试：加进 `test_doubao_response_parsing_red.py`
- 完成日期：2026-09-09
- Commit：983f0e0

---

### P0-4 重写 icl.py 对接官方 `/api/v3/tts/voice_clone` + `/api/v3/tts/get_voice`
- [ ] 完成
- 位置：[icl.py:29-30](file:///workspace/backend/app/ai/providers/doubao/icl.py#L29-L30) + [icl.py:74-85](file:///workspace/backend/app/ai/providers/doubao/icl.py#L74-L85)
- 关键改动：
  - 删除自造端点 `/api/v1/voice_clone/create` + `/query`
  - `create_training` body 改为 `{speaker_id, audio:{data,format}, text, language, extra_params:{demo_text, ...}}`
  - `query_training` 改调 `/api/v3/tts/get_voice`，按 `status` 字段（0/1/2/3/4）翻译
  - 删除 `data.task_id` 假设
  - 同步更新轮询 worker（`backend/app/services/icl.py`）
- 测试：扩 `backend/tests/test_doubao_task5_red.py`
- 完成日期：
- Commit：

---

### P0-5 删除/废弃 multicast.py 自造端点
- [ ] 完成
- 位置：[multicast.py:38-40](file:///workspace/backend/app/ai/providers/doubao/multicast.py#L38-L40) + [multicast.py:99-135](file:///workspace/backend/app/ai/providers/doubao/multicast.py#L99-L135)
- 关键改动：
  - 方案 1（推荐）：删除 `DoubaoMulticastProvider`，`mode=multicast` 退化为"全部旁白音色 + 关对白归属"
  - 方案 2（保守）：保留但 `build_payload` 抛 `NotImplementedError("multicast 已废弃")`
  - `routes.py` 的 `StartBuildRequest.mode` 文档说明 `multicast` 已废弃
- 测试：扩 `backend/tests/test_doubao_task8_red.py`
- 完成日期：
- Commit：

---

## 🟡 P1 — 体验提升（P0 跑通后自然衔接）

### P1-1 切到 v3 单向流式 HTTP（解锁 req_params / context_texts / enable_subtitle / loudness_rate）
- [ ] 完成
- 位置：[tts.py:550-630](file:///workspace/backend/app/ai/providers/doubao/tts.py#L550-L630)（`_build_request_body` 重写）+ [tts.py:454](file:///workspace/backend/app/ai/providers/doubao/tts.py#L454)（`DEFAULT_ENDPOINT` 改 `/api/v3/tts/unidirectional`）
- 关键改动：
  - 新增 `DoubaoTTSProviderV3` 类（保留 v1 兜底）
  - 请求体改为 v3 嵌套结构 `{req_params:{text,speaker,audio_params:{...}}}`
  - 鉴权切到 `X-Api-Key` + `X-Api-Resource-Id: seed-tts-2.0`
  - 启用 `disable_markdown_filter=true`、`enable_subtitle=true`
  - ⚠️ 用真实 Key 各打一发确认 emotion/emotion_scale 字段名后再正式落地（GLM 警告）
- 测试：`backend/tests/test_doubao_v3_protocol_red.py`（新）
- 完成日期：
- Commit：

---

### P1-2 情感合成落地（音色元数据 + 对白归属情绪 + 只对支持情感的音色下发）
- [ ] 完成
- 位置：chapter.py / dialogue.py / tts.py
- 关键改动：
  - `ProjectCharacter` 加 `emotion` 字段（默认空 = 用音色默认）
  - `Dialogue` 模型加 `emotion` 字段（LLM 对白归属时输出）
  - tts.py 渲染时，若 `emotion` 非空且**当前 voice_type 支持**（查 P0-2 元数据）→ 下发；否则降级
  - payload 里 emotion 字段**只塞一处**（GLM 指出当前 tts.py:588-604 三处冗余是 bug）
- 测试：扩 `backend/tests/test_doubao_authorization_red.py`
- 完成日期：
- Commit：

---

### P1-3 Build 流水线产出 SRT 字幕
- [ ] 完成
- 位置：build.py + 新文件 `backend/app/services/srt.py`
- 关键改动：
  - 段合成时若 `enable_subtitle=true`，存 `word[]` 到 artifact 表
  - build 结束时按章节合并 `chapter_NN.srt` 到 `data/audio/<book>/<build>/`
  - 段间停顿用段间隔补（差值填充 silence）
  - 前端 ProjectDetailPage 加"下载字幕"按钮
- 测试：`backend/tests/test_srt_export_red.py`（新）
- 完成日期：
- Commit：

---

### P1-4 响度/采样率在合成期统一
- [ ] 完成
- 位置：tts.py `_build_request_body`
- 关键改动：
  - audio_params 默认 `sample_rate=24000, speech_rate=0, loudness_rate=0`
  - 配置项 `settings.DOUBAO_AUDIO_SAMPLE_RATE` / `DOUBAO_AUDIO_LOUDNESS_RATE`
  - m4b.py 后处理 loudnorm 保留作为兜底
- 测试：扩 `test_settings_persist_red.py`
- 完成日期：
- Commit：

---

### P1-5 `instruction_text` 真正下发到 v3
- [ ] 完成
- 位置：tts.py `_build_request_body`
- 关键改动：
  - 把 `instruction_text` 塞到 `req_params.context_texts=[instruction_text]`
  - 复刻音色下忽略 `instruction_text` 并打 warning 日志（文档冲突约束）
- 测试：扩 `test_doubao_v3_protocol_red.py`
- 完成日期：
- Commit：

---

### P1-6 `X-Tt-Logid` 全链路透传
- [ ] 完成
- 位置：tts.py / icl.py / multicast.py 错误处理路径 + routes.py
- 关键改动：
  - 响应 header `X-Tt-Logid` 永远记到日志
  - 异常对象带 `logid` 属性
  - routes.py 5xx 响应附 `logid` 字段
- 测试：`backend/tests/test_logid_propagation_red.py`（新）
- 完成日期：
- Commit：

---

### P1-7 段缓存 key 加 `req_params.model` + `context_texts_hash`
- [ ] 完成
- 位置：build.py 段缓存 key 计算
- 关键改动：
  - 缓存 key = sha256(f"{model}|{speaker}|{text}|{context_texts_str}|{sample_rate}")
  - 避免切 model / 切 instruction_text 后命中旧缓存
- 测试：扩 `test_settings_persist_red.py` 或新文件
- 完成日期：
- Commit：

---

## 🟢 P2 — 后续增量（打磨完核心再做）

### P2-1 音色列表动态化（ListSpeakers 接口 + 启动同步缓存）
- [ ] 完成
- 关键改动：启动时调官方 ListSpeakers → 写 `data/voices_doubao_remote.json` → 合并本地内置 + 远程 + 自定义
- 测试：扩 `test_doubao_voices_red.py`
- 完成日期：
- Commit：

---

### P2-2 长文本异步接口（submit/query）
- [ ] 完成
- 适用：纯旁白章节绕过 60s 超时
- 测试：新文件
- 完成日期：
- Commit：

---

### P2-3 Seed-Audio 1.0 → 章节预告片
- [ ] 完成
- 关键改动：完全独立于 Build，作为"Build 完正片后的可选增值"
- 测试：新文件
- 完成日期：
- Commit：

---

### P2-4 ASR 反向识别（用户上传 mp3 → 文本 → 校对 → 重导出）
- [ ] 完成
- 适用：新场景"二次创作"
- 测试：新文件
- 完成日期：
- Commit：

---

### P2-5 小模型免费音色库独立试听 Tab
- [ ] 完成
- 关键改动：前端音色库加 Tab；后端独立 `DoubaoSmallTTSProvider`，复用 v3 单向流式 HTTP
- 测试：新文件
- 完成日期：
- Commit：

---

## 📊 进度看板

| 类别 | 总数 | 已完成 | 进度 |
|---|---|---|---|
| 🔴 P0 | 5 | 3 | ▰▰▰▱▱ 60% |
| 🟡 P1 | 7 | 0 | ▱▱▱▱▱▱▱ 0% |
| 🟢 P2 | 5 | 0 | ▱▱▱▱▱ 0% |
| **合计** | **17** | **3** | **18%** |

> 更新方式：完成时把 `0` 改成实际数字、进度条同步。也可以用 `grep -c '\[x\]' checklist-tts-v3-migration.md` 一键统计。

---

## 📅 推荐执行节奏

```
Week 1: P0-1 + P0-3（同一根因，一起做）
Week 2: P0-2
Week 3: P0-4 + P0-5
Week 4: P1-1 + P1-6（顺手）
Week 5: P1-2 + P1-5
Week 6: P1-3 + P1-4 + P1-7
后续:  P2 按需
```

---

## 📝 修订记录

- 2026-09-04：初版，基于 GLM-5.3 审计 + 笔记 `.trae/notes/doubao-voice-apis.md` 整理
- 2026-09-09：P0-1 + P0-3 完成（含回归测试）。GLM 提交 983f0e0 一次性合并：删除 `tts.py` 中旧的 `_http_post_bytes` 方法 + 新增 `test_doubao_response_parsing_red.py`（7 场景）+ 修复 `test_doubao_task3_red.py` 与 `test_doubao_task5_red.py` 的 mock 协议不匹配。19/19 相关测试通过。
- 2026-09-09：P0-2 完成。按官方文档 97465 + 1257544 全量重建 `_BUILTIN_VOICES`：187 条官方真实 voice_type（92 条小模型 + 95 条大模型 2.0），新增 6 个元数据字段（languages/supports_emotion/supports_subtitle/supports_language/free/model），删除全部自创 id（BV030~BV613、zh_*_xxx 历史命名空间），修正 BV 拼写 `_stream` → `_streaming`。两个提交：a1b576a（代码）+ 35bb9ea（测试）。147/147 全套测试通过。
