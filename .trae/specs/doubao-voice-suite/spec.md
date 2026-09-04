# 规格：豆包语音全家桶集成（TTS 2.0 + ICL 声音复刻 + Seed-Audio 多播剧）

> 生成时间：2025-01-01  
> 自然语言：中文  
> 关键决策：用户已确认「方案 A」、「一步到位、不计成本」、「多播剧模式报错直接抛异常，不降级」。

---

## 1. 问题陈述（Problem）

当前 TTS 方案仅使用 MiniMax，存在以下限制：
1. 音色库缺口：共 23 条记录，其中 11 条为 `-jingpin` 精修变种，实际独立声线约 12–15 条；老年声、少年中段、方言声线明显缺失。
2. 缺少自定义音色能力：用户无法通过录音复刻专属声线。
3. 缺少多播剧生成能力：当前仅支持「每段对白单角色 + 旁白」的串行拼接，无法实现多角色同期对话、情绪爆发、背景音乐/音效一体化的沉浸式有声剧。
4. 缺少厂商切换与错误严格模式：当前 `factory.py` 硬编码 MiniMax；失败章使用「占位静音 MP3」降级，用户明确要求多播剧模式下应严格报错而非降级。

本规格面向「一步到位」集成豆包语音全家桶，补齐以上短板，同时在架构上保持可扩展、可测试、与现有 MiniMax 能力并存。

---

## 2. 目标用户与目标

### 2.1 目标用户
- **有声书创作者**：需要丰富音色库 + 自定义音色 + 有声剧质感输出。
- **运营/管理员**：需要多厂商路由、严格模式、可追溯的 Build 错误。
- **前端交互用户**：需要多维度筛选音色、上传录音训练音色、一键切换「旁白/多播剧」构建模式。

### 2.2 核心目标
| # | 目标 | 衡量方式 |
|---|------|---------|
| G1 | 接入豆包 TTS 2.0，音色 ≥ 28 条且覆盖方言/年龄/场景维度 | list_voices 返回条目 ≥ 28；前端筛选维度 ≥ 4 |
| G2 | 接入 ICL 2.0 声音复刻：上传 3–6s 参考音频 → 训练 → 合成可用 | 训练成功率路径覆盖、训练状态轮询接口可用 |
| G3 | 接入 Seed-Audio 1.0：按章级 Prompt 生成多角色+情绪+音效一体化音频（≤120s），超长章节自动分段再拼接 | 多播剧模式 Build 产物可产出多角色同期音频；错误不降级 |
| G4 | 支持按项目/按 Build 选择模式：`classic`（原有对白-旁白拼接，可 MiniMax 或豆包）/ `multicast`（Seed-Audio 多播剧） | 前端构建模式下拉；后端 StartBuildRequest.mode 字段生效 |
| G5 | 多厂商路由：`project.default_tts_provider` + Build 覆写；音色 ID 命名空间隔离（`minimax:*` / `doubao:*` / `icl:*`） | 路由层在 factory 层决定实例；音色 ID 前缀校验 |
| G6 | **严格错误模式**：`multicast` 模式下合成任何章节失败 → 整 Build 标记 `failed` 并在 `progress_msg` 中保留首条根因；不写占位静音 MP3 | 集成测试断言：失败章节抛异常即终止 |

### 2.3 非目标（明确不做）
- NG1：不删除或迁移 MiniMax Provider；仍可作为默认/备选。
- NG2：不重构现有段缓存键算法（`_seg_cache_key`），仅在 key 前置 provider 命名空间前缀。
- NG3：多播剧模式不在同一期做「章节间背景音效衔接」「片头曲/片尾曲注入」——留后续增量。
- NG4：不在本期做 Seed-Audio 的图片/参考音频输入；只做文本 Prompt 驱动（含多角色 SCRIPT 指令）。

---

## 3. 功能性需求（Functional Requirements）

### 3.1 抽象层扩展（BaseTTSProvider + Factory）
- FR-1：`BaseTTSProvider` 新增可选字段 `provider: str`（如 "minimax"/"doubao"/"icl"），`synthesize_to_bytes` 新增可选参数 `instruction_text: str | None`、`speaker_style: str | None`；已有子类以默认空值兼容。
- FR-2：`factory.py` 新增 `get_tts(provider: str | None = None) -> BaseTTSProvider`，默认读 `settings.TTS_PROVIDER`；未配置时回退 minimax。
- FR-3：`factory.py` 新增 `TTSRegistry`：音色 ID 前缀 → provider 的映射表：`minimax:→MiniMaxTTSProvider`、`doubao:→DoubaoTTSProvider`、`icl:→DoubaoTTSProvider(icl_mode=True)`。

### 3.2 豆包 TTS 2.0 Provider（DoubaoTTSProvider）
- FR-4：实现 `DoubaoTTSProvider`，封装火山方舟 TTS 2.0 `tts.cloudapi.cn-beijing.volces.com` 接口（按豆包官方文档签名鉴权、或使用豆包直连端点与 API Key）。
- FR-5：`list_voices()` 动态拉取豆包官方音色列表并按 10 分钟缓存；返回字段补齐 `id/name/gender/description/dialect/age_group/scene/role_tags/speaker_style_list`，`id` 统一前缀 `doubao:`。
- FR-6：`synthesize_to_bytes` 支持 `speed`（0.5–2.0）、`emotion`、`instruction_text`（风格/情绪指令）、`speaker_style`；输出 MP3 24kHz 16-bit。
- FR-7：失败时保留 HTTP 状态码与错误码（如 `QuotaExhausted`、`InvalidVoiceId`、`RateLimited`）抛出异常，不做静默降级。提供 RPM/QPS 限流器（配置 `DOUBAO_TTS_RPM_LIMIT`，默认 60）。

### 3.3 ICL 声音复刻（用户自定义音色）
- FR-8：后端新增 `icl_training_tasks` 表：`task_id(pk) / user_id / voice_name / reference_audio_path / reference_audio_size_bytes / status(0排队 1训练中 2成功 3失败 4可用) / progress / doubao_task_id / cloned_voice_id / error_msg / created_at / updated_at`。
- FR-9：接口：
  - `POST /api/icl/tasks`：上传 3–6s WAV/MP3（≤10MB，单说话人）→ 创建训练任务 → 返回 `task_id`。
  - `GET  /api/icl/tasks`：分页列出当前用户的训练任务（含 status/progress/voice_id）。
  - `GET  /api/icl/tasks/{task_id}`：详情（状态、错误信息）。
  - `GET  /api/icl/voices`：列出当前用户训练成功的自定义音色（status=4），id 前缀 `icl:<cloned_voice_id>`。
  - `DELETE /api/icl/tasks/{task_id}`：取消/删除。
- FR-10：`DoubaoTTSProvider.synthesize_to_bytes` 当 voice_id 前缀 `icl:` 时，走「复刻音色」合成分支（等价接口，仅 voice_id 参数指向复刻音色）。
- FR-11：`GET /api/voices` 合并三份来源：minimax 静态 + doubao 动态 + 当前用户的 icl 音色；每条均含 `provider` 字段。

### 3.4 Seed-Audio 多播剧模式
- FR-12：新增 `multicast` 构建模式：`StartBuildRequest.mode ∈ {classic, multicast}`；默认 `classic`。
- FR-13：当 `mode=multicast`：
  - 不使用现有 `_build_segments_for_chapter` → 不做段级缓存（因多播剧为整段生成，段概念不存在）；
  - 按章调用 `DoubaoMulticastProvider.synthesize_chapter_multicast(chapter, dialogues[], characters[], build_opts)`；
  - 单章输入字数折算为「豆包 Seed-Audio 1.0 120s 上限」分段，超出时依据角色对话边界拆分多段再 `concat_mp3_files` 拼接；
  - 每段 Prompt 按豆包官方 SCRIPT 语法：多角色台词（`<speaker>NAME</speaker>` 或等价指令）、情绪标签 `[愤怒]`、音效 `[SFX:关门声]`、背景 `[BGM:悬疑]` 等自动注入（由 LLM 根据对白原文 + 角色人设生成 SCRIPT Prompt）。
- FR-14：`DoubaoMulticastProvider` 实现：封装 Seed-Audio 1.0 HTTP 接口，支持 `text`/`prompt` 输入，产出 MP3；返回时长。
- FR-15：**严格模式（用户强制要求）**：`mode=multicast` 下，任何段级/章级合成异常：
  - 立即 `raise RuntimeError(message_with_trace)`；
  - Build worker 捕获后 `Build.status = "failed"`，`progress_msg = "多播剧合成失败(第N章第M段): 原始异常"`；
  - **不得**写入占位静音 MP3；已完成章可保留产物（便于排查），但最终 ZIP **不打包**；
  - 用户只能通过 `retry-failed` 重试失败章节（复用已完成章产物）。
- FR-16：`mode=classic` 模式维持现有行为（失败章占位静音 + `partial_success`），不引入破坏性变更。

### 3.5 多厂商路由 + 配置
- FR-17：`config.py` 新增：
  - `TTS_PROVIDER: str = "minimax"`（`minimax | doubao`）—— 全局默认。
  - `DOUBAO_AK / DOUBAO_SK / DOUBAO_APP_ID`（火山或豆包直连凭据）。
  - `DOUBAO_TTS_BASE_URL / DOUBAO_ICL_BASE_URL / DOUBAO_SEED_AUDIO_BASE_URL`，默认官方线上地址。
  - `DOUBAO_TTS_RPM_LIMIT / DOUBAO_SEED_AUDIO_RPM_LIMIT`，默认 60 / 10。
  - `MULTICAST_STRICT_MODE: bool = True`（开关，true=严格报错，false=允许 partial_success 占位）。
- FR-18：`Project` 表新增：
  - `default_tts_provider: str | None`（优先于全局默认）；
  - `default_build_mode: str | None`（`classic | multicast`，优先于 UI 默认）。
- FR-19：`Build` 表新增：
  - `mode: str`（必填，创建时持久化）；
  - `tts_provider: str`（创建时持久化，方便查询/审计）。
  - `_calc_config_digest` 追加 `mode + tts_provider` 到哈希输入，避免模式切换但其他配置一致错误命中历史成功 Build。
- FR-20：`ProjectCharacter.assigned_voice_id` 兼容跨厂商 id；创建 Build 时校验：
  - 若某角色音色前缀与 `Build.tts_provider` 不匹配（如 minimax provider 被分配 `doubao:xxx`），**立即报错 400**，不启动 Build（严格匹配，不跨厂商偷偷替换）。

### 3.6 前端：音色选择器 VoicePicker 扩展
- FR-21：`Voice` TS 类型新增 `provider: 'minimax' | 'doubao' | 'icl'`、`dialect?`、`age_group?`、`scene?`、`role_tags?`、`speaker_style_list?`。
- FR-22：`VoicePicker` 在下拉面板顶部追加多维筛选 chips：
  - Tab 一级：`全部 / MiniMax / 豆包官方 / 我的音色(ICL)`；
  - Filter chips：性别（男/女/中性）、方言（普通话/粤语/东北话/四川话/…）、年龄段（儿童/少年/青年/中年/老年）、场景（新闻/有声书/客服/广告/…）。
  - 搜索框保留：`name/id/description/dialect/scene/role_tags` 全匹配。
- FR-23：项目详情页「构建有声书」面板，新增：
  - `TTS 厂商` 选择（项目默认 + 本次覆写）；
  - `构建模式`：`经典（旁白+对白拼接）` / `多播剧（Seed-Audio 一体化）`；
  - 切换到多播剧模式时，显示 Banner：**多播剧模式下任何章节生成失败，整本书构建将被标记为失败且不降级为占位静音**。
- FR-24：新增「我的音色」页面（/settings/my-voices，或 SettingsPage 内标签页）：
  - 列表：名称、状态（训练中/可用/失败）、创建时间、操作（试听/删除）；
  - 「训练新音色」弹窗：上传 3–6s 音频、填名称、提交 → 轮询列表刷新状态 → 成功后自动出现在 VoicePicker 的「我的音色」Tab。

### 3.7 后端 REST 接口新增/变更
- FR-25：`GET /api/voices` 返回新增 `provider` 字段；分页参数 `?limit=&offset=`（豆包 170+ 条需要分页）。
- FR-26：`POST /api/projects/{id}/builds` 的 `StartBuildRequest` 新增字段：
  - `mode?: 'classic' | 'multicast'`（缺省读项目 `default_build_mode` 再缺省 `classic`）；
  - `tts_provider?: 'minimax' | 'doubao'`（缺省读项目 `default_tts_provider` 再缺省全局配置）。
- FR-27：`BuildResp/BuildDetailResp` 新增 `mode`、`tts_provider` 字段。
- FR-28：ICL 接口集合（见 FR-9）。

---

## 4. 非功能性需求（Non-Functional Requirements）
- NFR-1 **向后兼容**：未配置 DOUBAO_ 环境变量时，系统行为等同现状，不抛错；`GET /api/voices` 至少返回 MiniMax 音色。
- NFR-2 **性能**：`list_voices` 豆包列表在后端 10 分钟内 TTL 缓存；ICL 列表 ≤ 50 条不缓存；总接口响应 ≤ 500ms P95。
- NFR-3 **幂等**：`config_digest` 包含 `mode` 与 `tts_provider`；多播剧段级缓存不开启（整章合成，不做段粒度）。
- NFR-4 **安全**：`icl_training_tasks.reference_audio_path` 不对外暴绝对路径；下载仅通过 `/media/` 受控白名单或 JWT 参数化路由。ICL 接口按 user 隔离（只能看自己的任务）。
- NFR-5 **可观测性**：豆包 TTS / ICL / Seed-Audio 均打印结构化日志（trace_id、耗时、字节数、失败分类）。
- NFR-6 **可维护性**：新增 Provider 全部放在 `app/ai/providers/doubao/` 目录下，与 `minimax/` 并列；不散落逻辑到 services 层。

---

## 5. 约束、依赖、假设、开放问题

### 5.1 约束
- C1：豆包 TTS 2.0 / ICL / Seed-Audio 的 API 凭据以环境变量方式注入，**不**写入 DB、**不**出现在前端。
- C2：多播剧严格模式为用户强约束：除非 `MULTICAST_STRICT_MODE=false` 明确在 `.env` 中关闭，否则绝不写占位降级。
- C3：音色 ID 必须按前缀命名空间；不允许无前缀的历史 voice_id 在新接口返回后被混淆。

### 5.2 依赖
- D1：账号侧需开通火山方舟豆包 TTS 2.0、ICL 2.0、Seed-Audio 1.0 三产品权限（由用户环境配置）。
- D2：httpx 异步客户端已在 `requirements.txt` 中存在；无需额外加。
- D3：`pydantic-settings` 新增字段无破坏性。

### 5.3 假设
- A1：豆包 TTS 音色 ID 格式稳定，可直接前缀拼接；若官方返回 id 本身含非 ASCII，用 slug 化后再拼。
- A2：Seed-Audio 1.0 的 120s 限制按输出音频时长计；实现中按字符预估 + 实际轮询返回时长做二次切分。
- A3：ICL 训练完成后返回的 `cloned_voice_id` 可直接用于合成接口。

### 5.4 已解决的开放问题
- Q1：多播剧模式失败时是否降级？**不降级，严格失败。**（用户回答 #4）
- Q2：方案选择？**方案 A：一步到位接入 TTS 2.0 + ICL + Seed-Audio 多播剧 + 多厂商路由 + 严格模式。**（用户回答 #1 A 与 #5 A）

---

## 6. 验收标准（Acceptance Criteria）

> 类型仅允许 `rule` 或 `rubric`。

| ID | 类型 | 描述 | 通过条件 | 证据来源 |
|---|------|------|---------|---------|
| AC-1 | rule | 未配置 DOUBAO_AK/SK 时，应用启动成功且 `/api/voices` 返回 MiniMax 列表 | pytest `test_voices_without_doubao_skips` 通过 + 手动 curl | 后端单元测试 + HTTP 接口 |
| AC-2 | rule | 配置豆包凭据后，`GET /api/voices` 合并返回条目 ≥ 28，且含 `provider ∈ {minimax, doubao, icl}` 字段 | `count >= min_expected + 28` + 每条存在 provider | HTTP 接口 |
| AC-3 | rule | `DoubaoTTSProvider.synthesize_to_bytes("你好，世界","doubao:zh_female_qingxin")` 返回 (bytes, duration_ms)；MP3 头校验 `0xFFFB`；dur_ms ∈ [500, 5000] | 集成测试断言 + 日志 trace | 豆包沙箱 API 或 mock httpx |
| AC-4 | rule | 上传 3–6s WAV → `POST /api/icl/tasks` → `GET /api/icl/tasks` 可见 status 0/1/2/4，成功后音色出现在 `GET /api/voices` 的 `provider=icl` 分组 | 端到端：录音上传 → 轮询 → VoicePicker 列表可见 | HTTP 接口 + 前端手工 |
| AC-5 | rule | `StartBuildRequest.mode='multicast'` + `tts_provider='doubao'`，当 Seed-Audio 返回错误时，Build.status 直接为 `failed`，对应 BuildArtifact.status 为 `failed`，失败章**不产生**占位 MP3（文件系统无 `*_failed.mp3`，也无同名正常文件） | pytest 注入 httpx mock 返回 5xx → 断言 status + 检查 fs | 集成测试 |
| AC-6 | rule | `mode='multicast'` 时 `_calc_config_digest` 与 `mode='classic'` 即使其他配置完全相同也产出不同 digest | 单元测试：两模式 digest 不相等 | 后端单测 |
| AC-7 | rule | `ProjectCharacter.assigned_voice_id = 'doubao:xxx'` + Build.tts_provider='minimax' 时，start_build 返回 400 且不创建 Build | HTTP 接口测试 | 后端单测 |
| AC-8 | rule | `MULTICAST_STRICT_MODE=false`（仅用于关闭严格模式的验证开关）时，失败章可走 partial_success（默认 true，仅留配置点做回归） | 环境变量切换下分别跑 AC-5 / 对照 | 后端单测 |
| AC-9 | rubric | 前端 VoicePicker 多维筛选可用性：170+ 音色下能在 3 步内定位到「豆包 女声 青年 粤语」且结果非空 | 0=完全无法筛；1=可筛但交互繁琐；2=筛选直观高效（Tab + chips 两级，筛选后集非空） | 手工验收截图 + 记录 |
| AC-10 | rubric | 多播剧模式生成音频听感：多人角色对白不串声、情绪与文字一致、无异常噪声（取 3 个典型章节样本人工打分） | 0=明显错；1=轻微瑕疵不影响；2=达到有声剧发行听感 | 人工听审 |
| AC-11 | rule | 「我的音色」页面：创建训练任务后列表显示 training 状态；刷新至 Success 后，在项目「构建有声书」VoicePicker 中可见该音色并可成功预览 2–3 字 | 手工流程录制 | 前端验收 |
| AC-12 | rule | 多厂商并发：豆包 TTS 按 `DOUBAO_TTS_RPM_LIMIT` 不超限（并发 100 请求打 provider，实际 HTTP 请求间隔 ≥ 60/LIMIT 秒） | `_rpm_next_allowed` 测试 or httpx 记录间隔 | 单测 + 日志 |
