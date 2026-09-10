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
- [x] 完成
- 位置：[icl.py](file:///workspace/backend/app/ai/providers/doubao/icl.py) 整段重写
- 关键改动：
  - 删除自造端点 `/api/v1/voice_clone/create` + `/query`；统一改用 v3 协议
  - `create_training` body 改为官方 schema：`{speaker_id, audio:{data,format}, language, model_type, demo_text?}`
    - `speaker_id` 由我们生成（`icl_<uuid hex>`），避免与官方命名空间（S_/ICL_/BV/uranus 等）冲突
    - `audio.data` 是 base64 编码（与 tts.py v1 响应解码路径一致）
    - `model_type` 默认 `ICL2.0`（推荐；可选 ICL1.0 / DiT）
    - 删除旧 `voice_name` / `audio_b64` / `reqid` 字段
  - `query_training` body `{speaker_id}` → 响应 `status` 字段按官方语义翻译：0 NotFound / 1 Training / 2 Success / 3 Failed / 4 Active
  - 鉴权兼容新旧两版控制台：
    - 新版：`X-Api-Key` + `X-Api-Request-Id`（推荐）
    - 旧版：`X-Api-App-Key` + `X-Api-Access-Key`（纯数字 APP_ID 自动判断）
  - 新增 settings：`DOUBAO_ICL_API_KEY`（新版 key）/ `DOUBAO_ICL_ACCESS_KEY`（旧版 access_key）
  - 业务码非 0 → RuntimeError；音频 < 512 字节 → ValueError
  - `speaker_status[0].model_type` 和 `demo_audio` 透出到上层（前端可展示试听音频 + 模型类型）
  - `DOUBAO_ICL_BASE_URL` 默认值保留旧 `/v1/voice_clone`，但 client 运行时检测到会自动重写到 v3 标准端点，老部署平滑升级
  - 轮询 worker（`backend/app/services/icl.py`）无需改动：仍读 `status` / `cloned_voice_id` / `error` 字段
- 测试：`backend/tests/test_doubao_icl_v3_red.py`（新，11 个场景）+ `backend/tests/test_doubao_task5_red.py`（适配 fake）+ `backend/tests/test_review_fixes_red.py`（T-RF5 fake 适配）
- 完成日期：2026-09-09
- Commit：2679f64（icl.py + config.py）+ c90af45（测试）

---

### P0-5 删除/废弃 multicast.py 自造端点
- [x] 完成
- 位置：物理删除 `backend/app/ai/providers/doubao/multicast.py`（旧 `DoubaoMulticastProvider` + `MAX_CHAPTER_AUDIO_SECS = 120`）
- 关键改动：
  - **采用方案 1**：删除 `multicast.py`；`mode=multicast` 在 `start_build` 入口处自动降级为 `classic`（逐段 TTS + 占位静音 MP3 兜底）
  - `factory.get_multicast_tts()` 改为永远返回 `None`（仅保留向后兼容）
  - `factory._multicast_instance` 属性 + `_doubao_seed_audio_bucket` 保留避免单测 `KeyError`
  - `_validate_tts_namespace` 移除 multicast 校验分支
  - `_validate_multicast_provider` 改为 noop
  - `_should_strict_fail(mode)` 改为恒 `return False`（strict 模式失效）
  - `_estimate_multicast_secs` 改为 `return 0.0`
  - `_multicast_synth_chapter` 改为 `raise RuntimeError("mode=multicast 已废弃（P0-5）")`
  - 删除 build worker 里的 multicast 整章一体化分支
- 测试：扩 `backend/tests/test_doubao_task7_red.py`（T-MC1~T-MC6）+ `test_doubao_task8_red.py`（T-ST1/T-ST3 改写为降级 + partial_success，T-ST5 改写为恒 False）+ `test_review_fixes_red.py`（T-RF1~T-RF3 适配）
- 完成日期：2026-09-09
- Commit：b8a3a3e（feat 代码）+ 8d702f4（test）

---

## 🟡 P1 — 体验提升（P0 跑通后自然衔接）

### P1-1 切到 v3 单向流式 HTTP（解锁 req_params / context_texts / enable_subtitle / loudness_rate）
- [x] 完成（保守骨架，真实 Key 联调前不切默认）
- 位置：新增 `DoubaoTTSProviderV3` 类（tts.py 末尾，约 1550 行起）+ factory 路由 + config 开关
- 关键改动：
  - 新增 `DoubaoTTSProviderV3` 类（v1 保留为兜底）
  - 端点：`https://openspeech.bytedance.com/api/v3/tts/unidirectional`（HTTP Chunked 流式）
  - 鉴权头：新版 `X-Api-Key` + `X-Api-Resource-Id`（按 model 选 seed-tts-1.0/2.0/seed-icl-2.0）+ 固定 `X-Api-App-Key=aGjiRDfUWi` + `X-Api-Request-Id=<uuid>`；纯数字 key 走旧版 `X-Api-App-Id` + `X-Api-Access-Key`
  - 请求体 v3 嵌套结构：`{user:{uid}, req_params:{text, speaker, audio_params:{format, sample_rate, speech_rate, loudness_rate, disable_markdown_filter, enable_subtitle, ...}}}`
  - speech_rate 由 speed [0.5, 2.0] 线性映射到 [-50, 100]
  - 流式响应：按行解析 chunked JSON，拼接 audio 字段（base64 → MP3 bytes）
  - 业务错（code != 0）→ `DoubaoTTSResponseV3Error` 不重试
  - 网络错（429/5xx）→ 重试 5 次
  - list_voices 复用 `_BUILTIN_VOICES`，标记 `protocol="v3"`
  - factory 路由按 `settings.DOUBAO_TTS_USE_V3`（默认 False）切到 v3 / v1
  - ⚠️ GLM 警告：emotion 字段名暂按官方文档放 `audio_params.emotion`；待真实 Key 联调复核
- 测试：`backend/tests/test_doubao_v3_protocol_red.py`（10 个 RED 全过）
- 完成日期：2026-09-09
- Commit：285a154（feat 代码）+ f5c498b（test）

---

### P1-2 情感合成落地（音色元数据 + 对白归属情绪 + 只对支持情感的音色下发）
- [x] 完成
- 位置：[tts.py](file:///workspace/backend/app/ai/providers/doubao/tts.py)（v1 `_build_payload` + v3 `_build_v3_payload` + 模块级 `_voice_supports_emotion` helper）
- 关键改动：
  - `ProjectCharacter` 加 `emotion` 字段（默认空 = 用音色默认）— 沿用 P0-2 元数据，未改模型 schema
  - `Dialogue` 模型加 `emotion` 字段 — 同上，无需 DB 迁移
  - tts.py 渲染时，若 `emotion` 非空且**当前 voice_type 支持**（查 P0-2 元数据）→ 下发；否则降级（不再 hardcode 三处）
  - payload 里 emotion 字段**只塞一处**：v1 仅放 `extend_params.emotion`，删除 `body["emotion"]` / `body["audio"]["emotion"]` 三处冗余；v3 仅放 `audio_params.emotion`
  - v1/v3 共用模块级 `_voice_supports_emotion(speaker_for_api)` helper；supports_emotion=False 时 logger.warning + 跳过下发
- 测试：`backend/tests/test_p1_emotion_srt_cache_red.py::test_p1_2_e1/e2/e3`（3 个 case）
- 完成日期：2026-09-10
- Commit：3ac871c

---

### P1-3 Build 流水线产出 SRT 字幕
- [x] 完成
- 位置：[srt.py](file:///workspace/backend/app/services/srt.py)（新文件）+ [routes.py](file:///workspace/backend/app/api/routes.py)（`api_build_chapter_subtitle` 路由）
- 关键改动：
  - 段合成时若 `enable_subtitle=true`，存 `word[]` 到 artifact 表（P0/P1 阶段已实装，sidecar JSON `build_<id>_ch<NN>_timings.json`）
  - build 结束时按章节合并 `chapter_NN.srt` 到 `data/audio/`：由 `srt.timings_json_to_srt(timings)` 把 sidecar JSON 转换为标准 SRT 文本
  - 长段（>28 字）按字符切多块，时间按比例分配；silence 段跳过；同 speaker 切换后再出现再次带【】前缀
  - 前端下载字幕：新增 `GET /api/projects/{project_id}/builds/{build_id}/chapters/{idx}/subtitle`，归属校验 + artifact.status==done 校验，响应 `application/x-subrip; charset=utf-8`
  - `load_chapter_srt(audio_dir, build_id, ch_idx)` helper（build_id 不含 "build_" 前缀）
- 测试：`backend/tests/test_p1_emotion_srt_cache_red.py::test_p1_3_s1~s5`（5 个 case，含端到端 HTTP 路由）
- 完成日期：2026-09-10
- Commit：4c13f32

---

### P1-4 响度/采样率在合成期统一
- [x] 完成
- 位置：[tts.py](file:///workspace/backend/app/ai/providers/doubao/tts.py)（v3 `_build_v3_payload`）+ [config.py](file:///workspace/backend/app/core/config.py)（新增 settings）+ [build.py](file:///workspace/backend/app/services/build.py)（缓存键 sample_rate 扩展）
- 关键改动：
  - audio_params 默认 `sample_rate=24000, speech_rate=0, loudness_rate=0`
  - 配置项 `settings.DOUBAO_AUDIO_SAMPLE_RATE`（默认 24000Hz）/ `DOUBAO_AUDIO_LOUDNESS_RATE`（默认 0dBFS）
  - v3 路径下从 settings 读取并塞入 `req_params.audio_params.sample_rate` / `.loudness_rate`
  - v1 路径保持原行为不动（v1 协议语义不同，sample_rate 由 reqid 维度固定）
  - **P1-4 联动 P1-7**：`sample_rate` 同步参与段缓存键（settings 改了采样率 → 旧缓存自动失效，避免采样率不一致时返回错乱的 bytes）
  - m4b.py 后处理 loudnorm 保留作为兜底（未动）
- 测试：`backend/tests/test_p1_emotion_srt_cache_red.py::test_p1_4_sample_rate_and_loudness_from_settings`（monkeypatch 改 settings 后断言 audio_params 反映新值）+ `test_p1_7_k5_cache_key_changes_with_sample_rate`（缓存键区分 + 向后兼容）
- 完成日期：2026-09-10
- Commit：3ac871c（首次） + eb89d7c（缓存键联动修复）

---

### P1-5 `instruction_text` 真正下发到 v3
- [x] 完成
- 位置：[tts.py](file:///workspace/backend/app/ai/providers/doubao/tts.py)（v3 `_build_v3_payload`）
- 关键改动：
  - 把 `instruction_text` 塞到 `req_params.context_texts=[instruction_text]`（仅非空时）
  - 复刻音色（`icl_` / `S_` 前缀）下忽略 `instruction_text` 并打 warning 日志（文档冲突约束：ICL 复刻已带音色特征，再叠加 instruction 会引入噪点）
  - 与 `speaker_style` 并列塞入 payload_extras，结构保持嵌套 `req_params` 不变
- 测试：`backend/tests/test_p1_emotion_srt_cache_red.py::test_p1_5_i1/i2`（2 个 case：普通音色下发 + 复刻音色跳过 + warning）
- 完成日期：2026-09-10
- Commit：3ac871c

---

### P1-6 `X-Tt-Logid` 全链路透传
- [x] 完成
- 位置：[tts.py](file:///workspace/backend/app/ai/providers/doubao/tts.py)（v1 `_post_json_for_v1`/`synthesize_to_bytes` + v3 `_post_stream_v3`/`synthesize_to_bytes`）+ [icl.py](file:///workspace/backend/app/ai/providers/doubao/icl.py)（`_http_post_json`/`create_training`/`query_training`）+ [routes.py](file:///workspace/backend/app/api/routes.py)（`_extract_logid_from_exc`/`_http_exc_with_logid` + ICL/TTS 5xx 路由）
- 关键改动：
  - 响应 header `X-Tt-Logid` 永远记到日志（v1/v3/ICL 三处 `logid = resp.headers.get("X-Tt-Logid")`）
  - 异常对象带 `logid` 属性：
    - v1 `DoubaoTTSResponseError(logid=...)` + final `RuntimeError.logid`（网络错包装路径）
    - v3 `DoubaoTTSResponseV3Error(logid=...)`（业务错直接 re-raise）+ final `RuntimeError.logid`（网络错包装路径）
    - ICL `_http_post_json` 把 logid 塞进返回 dict `_logid` 字段，`create_training`/`query_training` 业务错 RuntimeError 带 `.logid`
  - routes.py 5xx 响应附 `logid` 字段：`_extract_logid_from_exc` 沿 `__cause__` 链递归取 logid；`_http_exc_with_logid` 构造 `HTTPException(detail={message, logid})`；ICL 创建/查询/详情/删除 + TTS preview 路由全部改用此 helper
- 测试：`backend/tests/test_logid_propagation_red.py`（新，9 个场景：3 helper 单测 + 3 provider/ICL 异常携带 logid + 1 端到端 HTTP 5xx 含 logid）
- 完成日期：2026-09-09
- Commit：d41441d

---

### P1-7 段缓存 key 加 `req_params.model` + `context_texts_hash`
- [x] 完成
- 位置：[build.py](file:///workspace/backend/app/services/build.py)（`_seg_cache_key` + `_voice_model_lookup` + `_context_texts_hash`）
- 关键改动：
  - 新增 `_voice_model_lookup(voice_id)` helper：
    - `icl_` / `S_` 前缀 → `seed-icl-2.0`（ICL 复刻专用）
    - 内置大模型音色表查找 → `v.model`（兜底 `seed-tts-1.0`）
  - 新增 `_context_texts_hash(instruction)` helper：空字符串 → 空 hash（兼容旧缓存），非空 → sha256.hexdigest()[:16]
  - 缓存 key 加入 `model` + `context_texts_hash` + `sample_rate` 段，公式升级为 `sha256(f"v1|{voice_id}|{speed:.2f}|{text}|e:{emotion}|i:{instruction}|m:{model}|c:{context_texts_hash}|sr:{sample_rate}")`
  - `tts_segment_cache_get` / `tts_segment_cache_put` 签名扩展 `model=""` / `context_texts_hash=""` / `sample_rate=""` 默认值（向后兼容）
  - `_synth_seg` 调用点传入真实 model + ctx hash + sample_rate
- 测试：`backend/tests/test_p1_emotion_srt_cache_red.py::test_p1_7_k1~k5`（5 个 case：键变化 + 向后兼容 + model lookup 映射 + ctx hash 计算 + sample_rate 影响键）
- 完成日期：2026-09-10
- Commit：95fdf33（首次） + eb89d7c（sample_rate 联动修复）

---

## 🟢 P2 — 后续增量（打磨完核心再做）

### P2-1 音色列表动态化（ListSpeakers 接口 + 启动同步缓存）
- [x] 完成
- 位置：[doubao_list_speakers.py](file:///workspace/backend/app/services/doubao_list_speakers.py)（新文件）+ [tts.py](file:///workspace/backend/app/ai/providers/doubao/tts.py)（`list_voices` 三层合并）+ [main.py](file:///workspace/backend/app/main.py)（`lifespan` 启动同步）
- 关键改动：
  - HMAC-SHA256 鉴权：Service=speech_saas_prod / Region=cn-north-1 / Version=2025-05-20 / Action=ListSpeakers，5 步派生 signing_key
  - `_fetch_one_page()` 单页拉取 + `fetch_remote_voices()` 多页循环（自动停止在最后一页）
  - `_speaker_to_builtin_entry()` 转换 Speaker dict → _BUILTIN_VOICES 结构（中英文 Gender/Age 翻译 + ResourceID 透出 model + Languages 截 `-` 前缀）
  - `save_remote_voices()` / `load_remote_voices()` 原子写盘 + 容错（坏 JSON / 不存在）
  - `refresh_remote_voices()` / `ensure_remote_voices_synced_once()` 启动同步：缺失凭据 / 网络错时优雅返回 0，不抛错
  - 三层合并：内置 < 远程 < 自定义（用户 `voices_doubao.json` 始终优先）
  - 写入 `DATA_DIR/voices_doubao_remote.json` 缓存（version=1 + synced_at）
- 测试：`backend/tests/test_p2_1_list_speakers_red.py`（新，15 个场景：HMAC 签名 / 单页 / 多页 / Speaker 转换 / 原子写 / 启动复用 / 三层合并 / 缺凭据容错 / 网络错容错）
- 完成日期：2026-09-10
- Commit：400ae4d

---

### P2-2 长文本异步接口（submit/query）
- [x] 完成
- 位置：[tts_async.py](file:///workspace/backend/app/ai/providers/doubao/tts_async.py)（新文件，`DoubaoAsyncTTSClient`）
- 关键改动：
  - `submit()` POST `/api/v3/tts/submit` → 返回 task_id
  - `query()` POST `/api/v3/tts/query` → 返回 `AsyncTaskResult`（status / audio_url / logid）
  - `wait_for_result()` 轮询到终态（默认 5s × 360 = 30 min）
  - `download_audio()` 拿到官方 audio_url 一次性下载（一次性签名 URL，不能走缓存）
  - `synthesize_long_text()` 顶层封装（submit + wait + download）
  - 状态机：`AsyncTaskStatus.PROCESSING / SUCCESS / FAILED`
  - `should_use_async_tts()` 阈值（≥ 30000 字符走异步）
  - 双模式鉴权 `_is_legacy()`：纯数字 APP_ID 自动走旧版 `X-Api-App-Id` + `X-Api-Access-Key`
- 测试：`backend/tests/test_p2_2_async_tts_red.py`（新，16 个场景：submit / query 状态机 / wait_for_result 轮询 / 下载 / 顶层封装 / 阈值 / 鉴权双模式）
- 完成日期：2026-09-10
- Commit：aed6d06

---

### P2-3 Seed-Audio 1.0 → 章节预告片
- [x] 完成
- 位置：[preview.py](file:///workspace/backend/app/services/preview.py)（新文件）+ [routes.py](file:///workspace/backend/app/api/routes.py)（`api_build_chapter_preview` 路由）
- 关键改动：
  - `_split_frames()` 按比特率截取（mutagen 解析失败时退化 24kbps 估算）
  - `generate_chapter_preview()` 截取 + 写盘
  - `get_or_generate_preview()` 缓存复用 + `force_regenerate=True` 重生成
  - 预告片文件命名 `build_<id>_ch<NN>_preview.mp3`，默认 `DEFAULT_PREVIEW_SECONDS = 15`
  - 复用现有章节 MP3，不重新跑 TTS（节省 API 配额）
  - 新增路由 `GET /api/projects/{project_id}/builds/{build_id}/chapters/{idx}/preview`：归属校验 + 章节 status=done 校验 + FileResponse
- 测试：`backend/tests/test_p2_3_chapter_preview_red.py`（新，12 个场景：_split_frames 比特率/无比特率退化/mutagen 错误吞掉 / generate 写盘 / 缺章节 / 缓存复用 / 强制重生成 / 路由 200/404/401）
- 完成日期：2026-09-10
- Commit：1b03691

---

### P2-4 ~~ASR 反向识别（用户上传 mp3 → 文本 → 校对 → 重导出）~~
- [x] ~~删除（2026-09-10 与用户确认：从计划中移除，暂不做"二次创作"场景）~~

---

### P2-5 小模型免费音色库独立试听 Tab
- [x] 完成
- 位置：[routes.py](file:///workspace/backend/app/api/routes.py)（`api_list_voices` 加 `free_only` 参数）+ [VoiceLibraryPage.tsx](file:///workspace/frontend/src/components/VoiceLibraryPage.tsx)（新增 SmallFreeTab）+ [api.ts](file:///workspace/frontend/src/lib/api.ts)（`api.voices` 支持 `free_only` 选项）
- 关键改动：
  - 后端 `/api/voices?free_only=true`：只返回 `provider=doubao` 且 `free=True` 且 `model=seed-tts-1.0` 的音色
  - `list_voices()` 服务层加 `free_only` 参数（默认 False 向后兼容）
  - 前端「小模型免费」Tab（emerald 配色），独立拉取 `/api/voices?free_only=true`
  - `SmallFreeTab` 复用 `LibraryTab` 渲染与试听逻辑，含 loading / err / 空态
  - 不新建独立 provider：复用现有豆包 v3 单向流式 + 段缓存（声音 TTS 路径与原豆包完全一致）
- 测试：`backend/tests/test_p2_5_small_free_tab_red.py`（新，9 个场景：过滤逻辑 4 项 + HTTP 端到端 3 项 + 兼容 + 真实表自洽）
- 完成日期：2026-09-10
- Commit：b560697

---

## 📊 进度看板

| 类别 | 总数 | 已完成 | 进度 |
|---|---|---|---|
| 🔴 P0 | 5 | 5 | ▰▰▰▰▰ 100% |
| 🟡 P1 | 7 | 6 | ▰▰▰▰▰▰ 86% |
| 🟢 P2 | 4 | 4 | ▰▰▰▰ 100% |
| **合计** | **16** | **15** | **94%** |

> 更新方式：完成时把 `0` 改成实际数字、进度条同步。也可以用 `grep -c '\[x\]' checklist-tts-v3-migration.md` 一键统计。

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
- 2026-09-09：P0-4 完成。`icl.py` 重写对接官方 v3 接口（`/api/v3/tts/voice_clone` + `/get_voice`）：speaker_id 改由我们生成 `icl_<uuid>`，audio 改 v3 schema（base64 + format），language 用官方枚举，model_type 默认 ICL2.0；鉴权自动兼容新旧控制台（X-Api-Key / X-Api-App-Key）；settings 新增 DOUBAO_ICL_API_KEY + DOUBAO_ICL_ACCESS_KEY；DOOUBAO_ICL_BASE_URL 默认仍是 v1 路径但 client 运行时自动重写到 v3。两个提交：2679f64（icl.py + config.py）+ c90af45（测试，新增 11 个 v3 协议测试 + 适配 fake）。158/158 全套测试通过。
- 2026-09-09：P1-1 完成。`DoubaoTTSProviderV3` 骨架上线：HTTP Chunked 单向流式（`/api/v3/tts/unidirectional`），`{user, req_params:{text, speaker, audio_params}}` 嵌套结构；新旧鉴权自动分流（X-Api-Key 新版 / X-Api-App-Id 旧版）；网络错重试 5 次，业务错不重试（`DoubaoTTSResponseV3Error`）；factory 按 `settings.DOUBAO_TTS_USE_V3` 切换（默认 False 保守）。两个提交：285a154（代码）+ f5c498b（10 个 v3 协议 RED 测试）。
- 2026-09-09：P1-6 完成。`X-Tt-Logid` 全链路透传：响应头 → 异常对象 `.logid` → routes.py 5xx detail。v1/v3/ICL 三处 provider 的 `logid = resp.headers.get("X-Tt-Logid")` 全部捕获；业务错 RuntimeError 与网络错 RuntimeError（包装路径）都带 `.logid`；ICL `_http_post_json` 把 logid 塞进返回 dict `_logid`；routes.py 新增 `_extract_logid_from_exc`（沿 `__cause__` 链递归取 logid）+ `_http_exc_with_logid`（构造 `HTTPException(detail={message, logid})`），ICL 创建/查询/详情/删除 + TTS preview 路由全部改用 helper。提交 d41441d（含 9 个 RED 测试）。
- 2026-09-10：P1-2/4/5/7/3 一次性收尾（GLM 指出 P1 一条龙做完）。三个提交：
  - 3ac871c（P1-2/4/5）：v1 emotion 三处冗余修复（仅 `extend_params.emotion` 一处），删除 `body["emotion"]` / `body["audio"]["emotion"]`；v1/v3 共用模块级 `_voice_supports_emotion(speaker)` helper，不支持时降级 + warning；settings 新增 `DOUBAO_AUDIO_SAMPLE_RATE`（默认 24000Hz）/ `DOUBAO_AUDIO_LOUDNESS_RATE`（默认 0dBFS）→ v3 `audio_params.sample_rate` / `.loudness_rate` 自动从 settings 读出；v3 `instruction_text` 真正下发到 `req_params.context_texts=[instruction_text]`，复刻音色（icl_/S_ 前缀）忽略并 warning。
  - 95fdf33（P1-7）：`_seg_cache_key` 加 `model` + `context_texts_hash` 字段（避免切 model 命中旧缓存）；新增 `_voice_model_lookup(voice_id)`（icl_/S_ 前缀 → seed-icl-2.0，内置表 → v.model，兜底 seed-tts-1.0）+ `_context_texts_hash(instruction)`（空字符串 → 空 hash 兼容旧缓存）；`tts_segment_cache_get/put` 签名扩展默认参数。
  - 4c13f32（P1-3）：新增 `backend/app/services/srt.py`（`timings_json_to_srt` 长段切分 + silence 跳过 + speaker 前缀切换；`load_chapter_srt(audio_dir, build_id, ch_idx)`）；新增路由 `GET /api/projects/{project_id}/builds/{build_id}/chapters/{idx}/subtitle`（归属校验 + artifact.status==done 校验 + `application/x-subrip; charset=utf-8` 响应）；新增 `backend/tests/test_p1_emotion_srt_cache_red.py`（15 个 RED 全绿：3 emotion + 1 sample_rate + 2 instruction + 5 SRT + 4 缓存键）。
- 192/192 全套测试通过，零回归。P1 整体进度从 14% → 86%，总进度 35% → 65%。


- 2026-09-10：P2 一条龙收尾（P2-1/2/3/5 全部完成，P2-4 ASR 二次创作场景确认不做，从计划中移除）。四个提交：
  - 400ae4d（P2-1）：新增 `backend/app/services/doubao_list_speakers.py`（HMAC-SHA256 鉴权 + 多页 ListSpeakers + Speaker→_BUILTIN_VOICES 转换 + 启动同步）；tts.py `list_voices` 改三层合并（内置 < 远程 < 自定义）；main.py `lifespan` 加 `ensure_remote_voices_synced_once()`。15 个 RED 测试全过。
  - aed6d06（P2-2）：新增 `backend/app/ai/providers/doubao/tts_async.py`（`DoubaoAsyncTTSClient`：submit / query / wait_for_result 轮询 / download_audio / synthesize_long_text 顶层封装；状态机 + 双模式鉴权 + 30000 字符阈值）。16 个 RED 测试全过。
  - 1b03691（P2-3）：新增 `backend/app/services/preview.py`（`_split_frames` 按比特率截取，mutagen 解析失败退化 24kbps 估算）；新增路由 `GET /api/projects/{pid}/builds/{bid}/chapters/{idx}/preview`（归属校验 + 章节 status=done 校验 + FileResponse）；复用现有章节 MP3，预告片文件命名 `build_<id>_ch<NN>_preview.mp3` 默认 15s；requirements.txt 增 `mutagen>=1.47`。12 个 RED 测试全过。
  - b560697（P2-5）：后端 `/api/voices?free_only=true` 过滤（doubao + free=True + model=seed-tts-1.0）；前端 VoiceLibraryPage 新增「小模型免费」Tab（emerald 配色），独立拉取 `/api/voices?free_only=true`，复用 `LibraryTab` 试听逻辑；`api.voices` 支持 `{tts_provider, free_only}` 选项。不新建独立 provider（声音 TTS 路径与原豆包完全一致，复用 v3 + 段缓存）。9 个 RED 测试全过。
- 245/245 全套测试通过，零回归。P2 整体进度从 0% → 100%，总进度 69% → 94%。
