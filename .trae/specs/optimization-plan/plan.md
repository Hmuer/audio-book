# 整体优化方案 · 全项目审阅结论与修复计划

> 生成日期：2026-09-17
> 审阅范围：`backend/`（AI Provider 层 / 服务层 / API 层 / 配置 / DB）+ `frontend/`（全部组件与 lib）+ 工程化（start.sh / 依赖 / CI / 测试）
> 审阅方式：多路静态审阅 + 关键结论逐行读码复核
> 关联文档：
> - `.trae/specs/doubao-voice-suite/spec.md`（豆包语音全家桶规格）
> - `.trae/specs/doubao-voice-suite/checklist-tts-v3-migration.md`（v3 迁移清单）
> - `.trae/notes/doubao-voice-apis.md`（豆包 API 速查）

---

## 0. 前提与范围约束（决定优先级排序）

本项目为**个人自用 + 单机部署**，据此对优先级做如下调整：

| 类别 | 处置 | 理由 |
|---|---|---|
| 密钥明文外泄、无管理员校验、水平越权（IDOR）、XFF 限流绕过、JWT 弱密钥 | **不修** | 单用户单机，无攻击面 |
| 多 worker 下限流/并发 sem 各进程一份 | **不修** | 已确认单进程单 worker |
| `owner_user_id IS NULL` 可被任意登录用户读取 | **不修** | 同上（仅一个用户） |
| 一次性媒体 token 的单用途语义 | **仍修** | 这是**功能 bug**（拖动进度条即 401），不是安全问题 |

排序原则：**功能可用性 > 数据正确性 > 省钱/省时 > 日常体验 > 工程化**。

### 已确认的执行决策

| # | 决策点 | 结论 |
|---|---|---|
| 决策 1 | `tags` 契约统一方向 | **改前端**：提交时 `join(',')` 为字符串；类型声明与展示处按字符串处理。后端 schema（`tags: str \| None`）与 DB 列（`String(256)`）保持不变 |
| 决策 2 | `PROVIDERS_CONFIG` 持久化权威源 | **只保留 `app_settings` 表**；`data/providers_config.json` 降级为一次性迁移来源，迁移完成后不再作为权威源、不再双写 |

---

## 1. 优先级分层总览

| Tier | 主题 | 条数 | 说明 |
|---|---|---|---|
| **A** | 功能阻断 / 直接花钱 | 7 | 必修；多数为小改动、收益立竿见影 |
| **B** | 长任务稳定性 | 8 | 合成一本小说必然踩到 |
| **C** | 「切模型 / 配豆包」场景相关 | 5 | 决定新模型能否真正生效与配置 |
| **D** | 日常体验 | 8 | 高频交互痛点 |
| **E** | 工程化收口 | 6 | 可延后，但 E-1 建议顺带修 |

**验证状态图例**
- ✅ 已读码复核：结论由本次审阅直接读源码确证
- 🟡 待复核：来自静态审阅，形态可信但未逐行验证（实施前需先复现）

---

## 2. Tier A —— 功能阻断 / 直接花钱（必修）

### A-1 段级缓存 get/put 键不一致 → 缓存 100% 不命中 ✅
- 位置：[build.py#L452-L455](file:///workspace/backend/app/services/build.py#L452-L455)（get）vs [build.py#L489-L493](file:///workspace/backend/app/services/build.py#L489-L493)（put）
- 现象：`tts_segment_cache_get` 调 `_seg_cache_key(...)` 时**漏传 `sample_rate=`**，而 `tts_segment_cache_put` 传了。调用方（[build.py#L1704-L1708](file:///workspace/backend/app/services/build.py#L1704-L1708)、[#L1723-L1727](file:///workspace/backend/app/services/build.py#L1723-L1727)）两边都传 `sample_rate=24000`。`_seg_cache_key` 在 `sample_rate` 非空时把它拼进 `style_part`（[build.py#L388-L394](file:///workspace/backend/app/services/build.py#L388-L394)）。
- 后果：写入键带 `|sr:24000`、查询键不带 → 内存与磁盘**永远查不到**。缓存能力完全失效，每次构建全额重调 TTS（显著变慢 + 真实扣费）。函数注释声称的「命中时零花费」从未生效。
- 漏测原因：现有测试只直接断言 `_seg_cache_key` 的输出，未做 get/put 往返（roundtrip）验证。
- 修复方向：get 侧补传 `sample_rate=sample_rate`。
- 验收：新增 roundtrip 测试 —— put 后 get 必须命中；且改 `sample_rate` 后必须不命中。

### A-2 `DoubaoTTSProviderV3` 5xx 路径必然 AttributeError ✅（回归缺陷）
- 位置：常量定义于 v1 [tts.py#L1217-L1220](file:///workspace/backend/app/ai/providers/doubao/tts.py#L1217-L1220)；V3 类体 [tts.py#L1754-L1769](file:///workspace/backend/app/ai/providers/doubao/tts.py#L1754-L1769) **未定义**；引用处 [tts.py#L2129](file:///workspace/backend/app/ai/providers/doubao/tts.py#L2129)、[#L2140](file:///workspace/backend/app/ai/providers/doubao/tts.py#L2140)
- 现象：`DoubaoTTSProviderV3(BaseTTSProvider)`（非继承 v1）类体只有 `MIN_SPEED/MAX_SPEED/MAX_RETRIES/BASE_BACKOFF_SECS/JITTER_SECS/DEFAULT_SAMPLE_RATE`，而重试分支引用 `self.MAX_5XX_RETRIES` 与 `self.HTTP_5XX_BACKOFF_SECS`。
- 后果：`DOUBAO_TTS_USE_V3=True` 且上游返回 5xx 时抛 `AttributeError`，完全绕过重试与错误封装。测试之所以通过，是因为用例在实例上手工补了这两个属性（[test_doubao_v3_protocol_red.py](file:///workspace/backend/tests/test_doubao_v3_protocol_red.py)）。
- 来源：上一轮「5xx fastfail」改动只落到 v1，未同步 v3。
- 修复方向：将两个常量补到 V3 类，或抽到共享基类（推荐后者，避免再次分叉）。
- 验收：V3 路径的 5xx 用例不再依赖手工注入属性即可通过。

### A-3 音色试听全线 401（`<audio>` 无法携带 token）✅
- 位置：preview 返回裸 URL [routes.py#L625-L629](file:///workspace/backend/app/api/routes.py#L625-L629)；`/media` 强制鉴权 [main.py#L288-L293](file:///workspace/backend/app/main.py#L288-L293)
- 现象：`audio_url` 是 `/media/{fname}`，而前端把该 URL 直接塞进 `<audio src>`（[VoiceLibraryPage.tsx](file:///workspace/frontend/src/components/VoiceLibraryPage.tsx)、[ProjectDetailPage.tsx](file:///workspace/frontend/src/components/ProjectDetailPage.tsx)），无法附带 `Authorization` 头。
- 后果：音色库试听、角色音色试听、旁白试听**全部播不出**（除非显式开启 `DISABLE_AUTH`）。选音色是核心交互闭环。
- 修复方向：preview 也走一次性媒体签名（与章节音频一致的方案），或在返回的 `audio_url` 上附带 `?token=`。
- 验收：默认配置（`DISABLE_AUTH=False`）下，音色库试听可正常播放。

### A-4 `tags` 前后端契约不匹配 → 保存必失败 + 详情页可能白屏 ✅
- 位置：后端 `tags: str | None` [routes.py#L660](file:///workspace/backend/app/api/routes.py#L660)；前端按数组提交 [ProjectDetailPage.tsx#L2487-L2497](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L2487-L2497)；前端类型 [api.ts#L559](file:///workspace/frontend/src/lib/api.ts#L559)；展示处 `.map` [ProjectDetailPage.tsx#L676-L682](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L676-L682)
- 现象：前端 `tagsInput.split(...)` 得到 `string[]` 并无条件提交（即使为空数组）；Pydantic v2 不会把 list 强转为 str。
- 后果：项目设置页**每次点保存必然 422**；若 DB 中已有 tags 字符串，则详情页 `project.tags.map(...)` 抛 `TypeError` → 白屏。
- 修复方向（**决策 1**）：前端提交时 `join(',')` 为字符串；`ProjectDetailResp.tags` 类型改为 `string | null`；展示与编辑处按字符串 `split(',')` 处理。后端与 DB 不动。
- 验收：项目设置页可保存成功；含 tags 的项目详情页正常渲染。
- 待核实：`ProjectListPage` 等处是否也消费 `tags`，需一并排查。

### A-5 显式 `mode="classic"` 被项目默认值覆盖 → 该项目永久无法构建 ✅
- 位置：[build.py#L772-L794](file:///workspace/backend/app/services/build.py#L772-L794)
- 现象：判断条件为 `if not mode or resolved_mode == "classic":`，而 `mode` 的默认值就是 `"classic"`（且 `resolved_mode` 由 `mode or "classic"` 得出），条件恒真 → 总是回读 `Project.default_build_mode` 覆盖。
- 后果：只要项目 `default_build_mode="multicast"`，即便调用方显式传 `classic` 也会在 [build.py#L790](file:///workspace/backend/app/services/build.py#L790) 抛错，且**无任何接口可绕过**（只能改库）。
- 修复方向：签名改为 `mode: str | None = None`，仅在 `mode is None` 时才回落到项目默认值。
- 验收：项目 `default_build_mode=multicast` 时，显式传 `classic` 可正常构建。

### A-6 v3 流式「部分音频 + 中途错误」静默返回截断音频 ✅
- 位置：[tts.py#L2035-L2060](file:///workspace/backend/app/ai/providers/doubao/tts.py#L2035-L2060)
- 现象：仅 `if saw_error and not chunks:` 才抛错；若先收到若干音频 chunk、中途返回错误 chunk，则直接 `return b"".join(chunks)` 当成功。
- 后果：返回不完整 MP3 且不报错 → 上层按成功写盘、算时长、记账 → 用户听到半句，字幕/进度全错。属静默数据错误。
- 修复方向：`saw_error` 为真时一律抛错（无论是否已有 chunks），并把 `err_code/err_msg` 带出。
- 验收：构造「先音频 chunk、后错误 chunk」的假响应，断言抛错而非返回截断数据。

### A-7 `config_digest` 快照不完整 → 改了内容却复用旧产物 🟡
- 位置：计算 [build.py#L525-L553](file:///workspace/backend/app/services/build.py#L525-L553)；复用查询 [build.py#L869-L889](file:///workspace/backend/app/services/build.py#L869-L889)
- 现象：只哈希 narrator / speed / voice_assignments / mode / tts_provider / 情感 / 角色风格，**不含**章节正文、对白、发音规则、采样率等。
- 后果：用户润色正文、重新识别对白、增删发音规则后再次构建，只要音色语速未变 → digest 不变 → 直接复用历史成功 build，**新内容永不合成**。
- 修复方向：把正文/对白的内容哈希与规范化后的发音规则纳入 digest（或引入 prepare 版本号）。
- 验收：修改正文或发音规则后，digest 必须变化并触发重新合成。
- 实施前：先用一次真实操作复现「改了内容却秒完成」。

---

## 3. Tier B —— 长任务稳定性

### B-1 孤儿 build 恢复会每个看门狗周期重复 spawn 🟡
- 位置：[job_tasks.py#L470-L544](file:///workspace/backend/app/services/job_tasks.py#L470-L544)
- 现象：`_recover_build_orphan` 只用 `if build_id in _ACTIVE_BUILDS` 去重，却**从不把自己注册进 `_ACTIVE_BUILDS`**；恢复出的 runner 也不更新该 JobTask 的心跳。
- 后果：JobTask 心跳停止 → 每个看门狗周期（默认 30s）都重新判定为孤儿 → 再次 spawn 同一 build 的 worker。并发跑同一 build → 重复 TTS 调用与重复用量、章节文件 `.tmp` 同名互写、状态互相覆盖。
- 修复方向：恢复时注册 `_ACTIVE_BUILDS` 并把心跳/任务生命周期纳入恢复 runner。

### B-2 `start_build` 竞态：锁只包检查、不包创建 🟡
- 位置：检查 [build.py#L821-L832](file:///workspace/backend/app/services/build.py#L821-L832)；插入/注册 [build.py#L922](file:///workspace/backend/app/services/build.py#L922)
- 现象：`_RUNNING_LOCK` 临界区在创建 Build 之前就释放，两个并发请求可同时通过检查。
- 后果：产生多个 queued/running build；后续用 `.scalar_one_or_none()` 查询活跃 build 会抛 `MultipleResultsFound` → 接口 500，需人工清理。
- 修复方向：把「检查 + 插入 + 注册」合并进同一临界区，或用 DB 层唯一约束兜底。

### B-3 终态覆盖：打包期间取消被写回 success 🟡
- 位置：取消检查 [build.py#L1885-L1896](file:///workspace/backend/app/services/build.py#L1885-L1896)；终态写入 [build.py#L1941-L1962](file:///workspace/backend/app/services/build.py#L1941-L1962)
- 现象：打包 ZIP（大书可能耗时较久）期间用户取消，worker 随后**无条件**写回 `success/partial_success`。
- 后果：已取消的任务被「复活」为完成态，与用户意图相反。
- 修复方向：终态写入改为条件更新（`WHERE status='running'`）后判断 rowcount。

### B-4 卡在 queued 的孤儿 build 永久阻塞项目 🟡
- 位置：[build.py#L851-L867](file:///workspace/backend/app/services/build.py#L851-L867)
- 现象：活跃检查里只有 `status == "running"` 有超时兜底；`queued` 分支直接返回该 build。
- 后果：若进程在「提交 Build(queued) 之后、注册 worker 之前」被杀，该 build 永远 queued → 之后每次 start_build 都返回它，项目永久无法合成。
- 修复方向：`queued` 同样设超时兜底（基于 `created_at` / 心跳）。

### B-5 retry build 跨 build 共享同一 MP3 文件 🟡
- 位置：复制文件名 [build.py#L1151-L1164](file:///workspace/backend/app/services/build.py#L1151-L1164)；删除 [build.py#L2055-L2093](file:///workspace/backend/app/services/build.py#L2055-L2093)
- 现象：retry 对非失败章直接复用源 build 的 `audio_filename`；`delete_build` 按 `audio_filename` 无条件 `unlink`。
- 后果：删除任一 build 会连带删掉另一个 build 引用的章节 MP3 → 单章下载/预览/M4B 全部 404。
- 修复方向：retry 复用章硬链接/复制为自身命名，或删除时做引用计数（只删本 build 自产文件）。

### B-6 一次性媒体 token 与 `<audio>` Range 请求不兼容 🟡
- 位置：[media_sign.py#L105-L145](file:///workspace/backend/app/services/media_sign.py#L105-L145)
- 现象：`consume_media_token` 首次请求即写 `used_at`，二次请求返回 None → 401；token TTL 300s，前端无重签逻辑。
- 后果：拖动进度条/重放即 401，播放超过 5 分钟后失效且无恢复路径。
- 修复方向：流式媒体改为「短时多次可用」（只校验 `expires_at`，不置 `used_at`）。

### B-7 SQLite 未设 `busy_timeout` / WAL，也未开启外键 🟡
- 位置：[session.py#L17-L37](file:///workspace/backend/app/db/session.py#L17-L37)
- 现象：连接参数只有 `check_same_thread=False`，无任何 `PRAGMA`。
- 后果：build worker 每章多次 commit，与 prepare 后台、JobTask 看门狗、API 并发写 → `database is locked` 被当作整章失败（降级静音）甚至整 build 失败。外键未开导致 `ondelete` 级联不可靠。
- 修复方向：连接事件里 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;`。

### B-8 取消 / 异常路径丢失 TTS 用量 🟡
- 位置：取消分支 [build.py#L1890-L1896](file:///workspace/backend/app/services/build.py#L1890-L1896)；顶层异常 [build.py#L963-L981](file:///workspace/backend/app/services/build.py#L963-L981)
- 现象：检测到 cancelled 后直接 `return`，跳过写 `tts_calls/tts_chars` 与 `record_tts_usage`。
- 后果：被取消或中途失败的任务，已真实消耗的调用与字数全部不入账（个人使用下影响统计准确性）。
- 修复方向：用量写入移入 `finally` 或取消分支单独记录。

---

## 4. Tier C —— 「切模型 / 配豆包」场景相关

### C-1 设置页漏渲染三个分组 → 大量配置项 UI 不可见 🟡
- 位置：[SettingsPage.tsx#L111](file:///workspace/frontend/src/components/SettingsPage.tsx#L111)
- 现象：前端 `groupOrder` 白名单只列 7 个分组，而后端 `_EDITABLE_SETTINGS` 还有「模型配置 / 合成质量 / 豆包配置」。
- 后果：`DOUBAO_*`（RPM、采样率、端点、v3 开关…）、`ICL_MAX_AUDIO_BYTES`、`TTS_MAX_SEGMENT_CHARS`、`POLISH_ENABLED` 等在设置页**完全不可见、不可配置**。与「豆包运行参数页面化」的目标直接冲突。
- 修复方向：补齐 `groupOrder` 与分组元信息。

### C-2 厂商编辑器：豆包凭据无法录入 + 输入框失焦 🟡
- 位置：字段渲染 [ProviderModelsEditor.tsx#L543-L604](file:///workspace/frontend/src/components/ProviderModelsEditor.tsx#L543-L604)；React key [ProviderModelsEditor.tsx#L433](file:///workspace/frontend/src/components/ProviderModelsEditor.tsx#L433)
- 现象：
  1. UI 只暴露 `label/id/enabled/api_key/base_url/models`，而豆包实际需要 `secret`、`app_id`、`icl_api_key`、`icl_access_key`、`icl_endpoint`、`tts_v3_endpoint` 等（后端 `_redact_provider` 也确实管理这些字段）。
  2. `ProviderCard` 的 key 用了被编辑的 `p.id`，改动 id 会导致整棵组件卸载重建。
- 后果：豆包（`doubao:` / `icl:` 音色的唯一后端通道）无法从 UI 配置；且 id 输入框每敲一个字符就失焦，无法连续输入。
- 修复方向：补齐豆包凭据输入（含脱敏占位处理）；key 改用稳定的内部 id。

### C-3 工厂按 provider 名缓存单例 → 运行中改配置不生效 🟡
- 位置：[factory.py#L131-L134](file:///workspace/backend/app/ai/factory.py#L131-L134)
- 现象：缓存键是 provider 标识（如 `"doubao"`），实例内部持有 endpoint/model/v3 开关等配置。
- 后果：运行中修改 `DOUBAO_TTS_USE_V3`、端点或模型后，`get_tts("doubao")` 仍返回旧实例 → 必须重启后端才生效。（测试也因此在用例里手工清空 `_tts_instances`。）
- 修复方向：缓存键纳入配置指纹，或在设置更新时提供失效接口。

### C-4 MiniMax 永久性错误也重试 5 次 🟡
- 位置：[minimax/tts.py#L244-L334](file:///workspace/backend/app/ai/providers/minimax/tts.py#L244-L334)
- 现象：业务错误码（HTTP 200 但 `base_resp.status_code != 0`）未做可重试分类，一律走通用退避。
- 后果：音色不存在、参数非法等**永久错误**会白等约 34s 才失败，且每次重试都可能被上游计费。
- 修复方向：按 `base_resp.status_code` 区分可重试/不可重试。

### C-5 未激活多厂商时 MiniMax 的 `model` 参数被硬编码忽略 🟡
- 位置：[minimax/tts.py#L143-L148](file:///workspace/backend/app/ai/providers/minimax/tts.py#L143-L148)
- 现象：`else` 分支中 `self.model = model or "speech-2.8-turbo"`，但 `self._internal_model` 被硬编码为 `"speech-2.8-turbo"`，且不剥离 `MiniMax-` 前缀。
- 后果：未走 `PROVIDERS_CONFIG` 激活分支时，显式传入的 model 与 `ACTIVE_TTS_MODEL` 均不生效，实际请求固定用死值。
- 修复方向：统一复用 `self.model` 的前缀剥离逻辑赋值给 `_internal_model`。

---

## 5. Tier D —— 日常体验

### D-1 轮询刷新会重置未保存的语速 / 旁白选择 🟡
- 位置：[ProjectDetailPage.tsx#L1238-L1242](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L1238-L1242)
- 现象：effect 依赖 `project.characters`（每次 reload 都是新数组引用），因此 `preparing` 轮询（每 2s）或任何刷新都会执行 `setNarratorVoice/setSpeed/setChars`。
- 后果：用户正在拖语速滑杆或选旁白时，值被服务端旧值覆盖，乐观编辑被回滚。
- 修复方向：仅在 `project_id` 变化时同步，或区分「编辑中」状态。

### D-2 试听存在响应乱序竞态 🟡
- 位置：[ProjectDetailPage.tsx#L181-L195](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L181-L195)、[VoiceLibraryPage.tsx#L203-L241](file:///workspace/frontend/src/components/VoiceLibraryPage.tsx#L203-L241)
- 现象：`playingKeyRef` 在 `await` 之后才写入，快速连点 A→B 时若 A 的响应更晚返回，最终 `src` 为 A，但高亮显示 B。
- 后果：听到的与看到的音色不一致。
- 修复方向：引入请求序号或 `AbortController`，只接受最新一次结果。

### D-3 轮询期间重复签发 URL 会打断正在播放的音频 🟡
- 位置：[ProjectDetailPage.tsx#L1856-L1885](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L1856-L1885)
- 现象：签名 effect 依赖 artifact 数量，每次有章节完成就给**全部** artifact 重新签发（不像 `ChaptersTab` 会跳过已签发的），`signedUrls` 全量替换。
- 后果：构建运行中边听边被打断；同时产生 O(N) 次重复签发请求。
- 修复方向：仅对未签发的 artifact 补签，保持已签发 URL 稳定。

### D-4 `VoicePicker` 浮层无视口边界约束 🟡
- 位置：[VoicePicker.tsx#L240-L251](file:///workspace/frontend/src/components/VoicePicker.tsx#L240-L251)
- 现象：面板 `position: fixed`，宽度取 `max(trigger.width, 400)`，`max-h-[440px]` 向下展开，无左右/上下钳制与翻转。
- 后果：触发器靠右或窄屏时面板溢出视口被裁切；靠底时底部内容不可见。
- 修复方向：按视口计算位置，必要时左对齐或向上翻转。

### D-5 `WaveformPlayer` 在 `src` 变化时不复位播放状态 🟡
- 位置：[WaveformPlayer.tsx#L164-L167](file:///workspace/frontend/src/components/WaveformPlayer.tsx#L164-L167)
- 现象：`src` 替换时只重置了 autoplay 重试计数，`isPlaying/currentTime/duration/loaded` 未复位。
- 后果：出现「按钮显示播放中，实际未播放」的状态错位（与 D-3 叠加时更明显）。
- 修复方向：`useEffect([src])` 中暂停旧音频并复位相关 state。

### D-6 拟声词替换过于激进，且只作用于 MiniMax 🟡
- 位置：[onomatopoeia.py#L48-L53](file:///workspace/backend/app/ai/providers/minimax/onomatopoeia.py#L48-L53)、[#L105-L133](file:///workspace/backend/app/ai/providers/minimax/onomatopoeia.py#L105-L133)
- 现象：单字规则（如 `唉`、`哼(?=[！!。])`）会误替换「唉声叹气」「哼唱」等正常语词；重复 tag 合并规则可能吞掉刻意重复。
- 后果：文本被误改；且该处理只在 MiniMax 生效，跨厂商听感不一致。
- 修复方向：收紧规则边界；如需保留则统一到两个 provider。

### D-7 静音帧恒多 1 帧导致时间轴漂移；跨厂商时长口径不一 🟡
- 位置：[mp3_util.py#L180](file:///workspace/backend/app/core/mp3_util.py#L180)；豆包估算 [tts.py#L33-L96](file:///workspace/backend/app/ai/providers/doubao/tts.py#L33-L96) vs MiniMax 精确 [minimax/tts.py#L98-L105](file:///workspace/backend/app/ai/providers/minimax/tts.py#L98-L105)
- 现象：`make_silent_mp3` 用 `int(duration_ms/frame_ms)+1`，每段静音实际 ≥ 请求值（约多 1 帧）；而 sidecar 写入的是请求值 → 段数多时累积漂移。豆包侧用「首帧码率 × 总大小」粗估，MiniMax 用逐帧精确。
- 后果：SRT/LRC 与音频、章间标记、进度显示出现数百毫秒级漂移；不同厂商口径不一致。
- 修复方向：静音帧按精确帧数生成；豆包统一改用 `core/mp3_util.mp3_duration_ms`。

### D-8 对白 anchor 用 `find` 取首次出现 → 重复短句错位 🟡
- 位置：[chapter.py#L244-L248](file:///workspace/backend/app/services/chapter.py#L244-L248)
- 现象：`ch.text.find(dlg.anchor_text)` 对每个对白都返回首次出现位置。
- 后果：中文小说中「「嗯。」」这类短对白反复出现时，旁白切片错位 → 对白被重复朗读或漏读。
- 修复方向：从游标之后搜索，或优先采用 LLM 返回的偏移并做本章内校验。

---

## 6. Tier E —— 工程化收口

### E-1 测试使用两套导入路径，隔离失效并会读写真实 `./data` 🟡
- 位置：[test_providers_api_red.py#L18-L19](file:///workspace/backend/tests/test_providers_api_red.py#L18-L19)、[test_logid_propagation_red.py](file:///workspace/backend/tests/test_logid_propagation_red.py)、[test_minimax_emotion_compat_red.py](file:///workspace/backend/tests/test_minimax_emotion_compat_red.py)；`pytest.ini` 的 `pythonpath = ..:.`
- 现象：其余用例统一用 `backend.app.*`，上述文件用顶层 `app.*`，二者是**不同的模块对象**（conftest 注释中已承认）。autouse fixture 只 patch 了 `backend.app.core.config.settings`。
- 后果：这些用例的 app 仍使用未隔离的 settings → **读写真实 `./data` 与真实 DB**。个人使用下会污染自己的数据。
- 修复方向：统一为 `backend.app.*`；`pytest.ini` 只保留一个 pythonpath 根；conftest 统一重置全局单例（`_ACTIVE_BUILDS`、`_tts_instances`、`_multicast_instance`）。

### E-2 `start.sh` 启动健壮性
- 位置：[start.sh#L250](file:///workspace/start.sh#L250)、[#L177-L179](file:///workspace/start.sh#L177-L179)、[.gitignore](file:///workspace/.gitignore)
- 现象：每次启动无条件 `pip install -r requirements.txt`（断网即整体启动失败，且未用 lock）；迁移旧 `.env` 时生成的 `.env.local-backup`（含明文密钥）未被 `.gitignore` 忽略。
- 修复方向：仅在新建 venv 时安装或已装则跳过；`.gitignore` 增加 `.env*`（保留 `!.env.example`）。

### E-3 `requirements.lock` 缺失运行时依赖
- 位置：[requirements.lock](file:///workspace/backend/requirements.lock) vs [requirements.txt](file:///workspace/backend/requirements.txt)
- 现象：lock 中缺 `mutagen`（[preview.py](file:///workspace/backend/app/services/preview.py) 强依赖），也缺 `greenlet` 等，说明并非真实 `pip freeze`。
- 后果：按 lock 建环境会出现运行时 `ImportError`。
- 修复方向：按既有流程重新生成完整 lock。

### E-4 CI 门禁形同虚设
- 位置：[ci.yml](file:///workspace/.github/workflows/ci.yml)
- 现象：lint 步骤仅在存在 ruff 配置时执行（仓库无 ruff 配置 → 恒跳过）；`npx tsc --noEmit || echo` 非阻断；前端测试步骤依赖 `package.json` 中的 `test` script（实际不存在）→ 恒跳过。
- 修复方向：补 ruff 配置、去掉 `|| echo`、补 `"test": "node --test"`。

### E-5 README 与实际不符
- 位置：[README.md](file:///workspace/README.md)
- 现象：测试数量自相矛盾（123 / 99，实际 41 个测试文件）；跑测试命令写的 venv 路径与 `start.sh` 实际的 `$NOVEL_TTS_HOME/.venv` 不符；「CI 未接入」与仓库中存在的 CI 配置矛盾；宣称的「OpenAI 兼容多厂商」超出实现（`get_llm()` 恒返回 MiniMax）。
- 修复方向：据实校准，或明确标注能力边界。

### E-6 死代码清理
- 位置：`multicast` 全家桶（[build.py#L703-L718](file:///workspace/backend/app/services/build.py#L703-L718)、[#L1278-L1291](file:///workspace/backend/app/services/build.py#L1278-L1291)）、`_DoubaoStubProvider` 与 `get_multicast_tts`（[factory.py#L66-L78](file:///workspace/backend/app/ai/factory.py#L66-L78)、[#L152-L160](file:///workspace/backend/app/ai/factory.py#L152-L160)）、`_tts_default_instance`（只写不读）、`_should_strict_fail`（恒 False）
- 现象：废弃能力仍暴露在设置白名单（`DOUBAO_SEED_AUDIO_*`、`MULTICAST_STRICT_MODE` 改之无效）；`_DoubaoStubProvider` 注释仍写「Task 3 尚未实现」。
- 后果：误导后续维护，且 UI 上可改但无效的开关会让人误判。
- 修复方向：清理死代码并从白名单移除废弃项。

---

## 7. 建议执行批次

| 批次 | 内容 | 说明 |
|---|---|---|
| **批次 1** | A-1、A-2、A-3、A-4、A-5 | 小改动、收益立竿见影；A-1 一行、A-2 补常量、A-3/A-4/A-5 各一处 |
| **批次 2** | A-6、A-7、B-1 ~ B-8 | 数据正确性 + 任务生命周期；A-7 与 B-1 需先复现 |
| **批次 3** | C-1 ~ C-5 | 让新模型与豆包配置真正可用 |
| **批次 4** | D-1 ~ D-8 | 体验优化 |
| **批次 5** | E-1 ~ E-6 | 工程化收口（E-1 建议提前到批次 2 顺带做） |

### 每批次的完成约定
1. 修复前先写/补对应测试（沿用 `backend/tests/test_<主题>_red.py` 命名约定）
2. 代码改动 + 测试通过
3. 不影响既有测试（全量跑一遍，确认无新增失败）
4. 在本文件第 8 节勾选并填写日期

---

## 8. 跟踪清单

> 完成一项把 `[ ]` 改为 `[x]`，并填写完成日期。

### 批次 1 —— Tier A（前 5 条）✅ 完成 2026-09-17
- [x] A-1 段缓存 get 补传 `sample_rate` — [build.py#L452-L455](file:///workspace/backend/app/services/build.py#L452-L455) — 完成 2026-09-17
- [x] A-2 V3 补 `MAX_5XX_RETRIES` / `HTTP_5XX_BACKOFF_SECS` — [tts.py#L1754-L1769](file:///workspace/backend/app/ai/providers/doubao/tts.py#L1754-L1769) — 完成 2026-09-17
- [x] A-3 试听音频改走鉴权 fetch + Blob URL（非签名 URL，见实施记录） — [routes.py#L625-L629](file:///workspace/backend/app/api/routes.py#L625-L629) — 完成 2026-09-17
- [x] A-4 `tags` 前端改为字符串提交（决策 1） — [ProjectDetailPage.tsx#L2487-L2497](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L2487-L2497) — 完成 2026-09-17
- [x] A-5 `mode` 用 `None` 区分「未指定」 — [build.py#L772-L794](file:///workspace/backend/app/services/build.py#L772-L794) — 完成 2026-09-17

### 批次 2 —— 数据正确性 + 长任务
- [ ] A-6 v3 `saw_error` 一律抛错 — [tts.py#L2035-L2060](file:///workspace/backend/app/ai/providers/doubao/tts.py#L2035-L2060)
- [ ] A-7 `config_digest` 纳入内容哈希 — [build.py#L525-L553](file:///workspace/backend/app/services/build.py#L525-L553)
- [ ] B-1 孤儿恢复注册 `_ACTIVE_BUILDS` + 心跳 — [job_tasks.py#L470-L544](file:///workspace/backend/app/services/job_tasks.py#L470-L544)
- [ ] B-2 `start_build` 临界区含创建与注册 — [build.py#L821-L832](file:///workspace/backend/app/services/build.py#L821-L832)
- [ ] B-3 终态写入加取消保护 — [build.py#L1941-L1962](file:///workspace/backend/app/services/build.py#L1941-L1962)
- [ ] B-4 `queued` 超时兜底 — [build.py#L851-L867](file:///workspace/backend/app/services/build.py#L851-L867)
- [ ] B-5 retry 复用章改为独立文件 / 引用计数 — [build.py#L2055-L2093](file:///workspace/backend/app/services/build.py#L2055-L2093)
- [ ] B-6 媒体 token 改为短时多次可用 — [media_sign.py#L105-L145](file:///workspace/backend/app/services/media_sign.py#L105-L145)
- [ ] B-7 SQLite 设 WAL / busy_timeout / foreign_keys — [session.py#L17-L37](file:///workspace/backend/app/db/session.py#L17-L37)
- [ ] B-8 取消/异常路径补记用量 — [build.py#L1890-L1896](file:///workspace/backend/app/services/build.py#L1890-L1896)

### 批次 3 —— 切模型 / 配豆包
- [ ] C-1 设置页补齐「模型配置/合成质量/豆包配置」分组 — [SettingsPage.tsx#L111](file:///workspace/frontend/src/components/SettingsPage.tsx#L111)
- [ ] C-2 厂商编辑器补豆包凭据 + 修 key 失焦 — [ProviderModelsEditor.tsx#L433](file:///workspace/frontend/src/components/ProviderModelsEditor.tsx#L433)
- [ ] C-3 工厂缓存纳入配置指纹 / 提供失效 — [factory.py#L131-L134](file:///workspace/backend/app/ai/factory.py#L131-L134)
- [ ] C-4 MiniMax 区分可重试业务错误 — [minimax/tts.py#L300-L334](file:///workspace/backend/app/ai/providers/minimax/tts.py#L300-L334)
- [ ] C-5 MiniMax `_internal_model` 复用 model 剥离逻辑 — [minimax/tts.py#L143-L148](file:///workspace/backend/app/ai/providers/minimax/tts.py#L143-L148)

### 批次 4 —— 日常体验
- [ ] D-1 轮询不覆盖未保存编辑 — [ProjectDetailPage.tsx#L1238-L1242](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L1238-L1242)
- [ ] D-2 试听防乱序（请求序号/Abort） — [ProjectDetailPage.tsx#L181-L195](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L181-L195)
- [ ] D-3 仅补签未签发的章节 URL — [ProjectDetailPage.tsx#L1856-L1885](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L1856-L1885)
- [ ] D-4 `VoicePicker` 视口边界钳制 — [VoicePicker.tsx#L240-L251](file:///workspace/frontend/src/components/VoicePicker.tsx#L240-L251)
- [ ] D-5 `WaveformPlayer` src 变化复位状态 — [WaveformPlayer.tsx#L164-L167](file:///workspace/frontend/src/components/WaveformPlayer.tsx#L164-L167)
- [ ] D-6 收紧拟声词替换规则 — [onomatopoeia.py#L48-L53](file:///workspace/backend/app/ai/providers/minimax/onomatopoeia.py#L48-L53)
- [ ] D-7 静音帧精确化 + 豆包时长统一 — [mp3_util.py#L180](file:///workspace/backend/app/core/mp3_util.py#L180)
- [ ] D-8 对白 anchor 从游标后搜索 — [chapter.py#L244-L248](file:///workspace/backend/app/services/chapter.py#L244-L248)

### 批次 5 —— 工程化
- [ ] E-1 统一测试导入路径 + 重置全局单例 — [conftest.py](file:///workspace/backend/tests/conftest.py)
- [ ] E-2 `start.sh` 安装策略 + `.gitignore` 补 `.env*` — [start.sh#L250](file:///workspace/start.sh#L250)
- [ ] E-3 重新生成 `requirements.lock`
- [ ] E-4 CI 门禁真正阻断 — [ci.yml](file:///workspace/.github/workflows/ci.yml)
- [ ] E-5 README 据实校准 — [README.md](file:///workspace/README.md)
- [ ] E-6 死代码清理（multicast / Stub / 无效开关）

---

## 9. 明确不改的部分（个人自用 + 单机）

- `/api/settings`、`/api/providers` 的管理员校验（仅一个用户，无越权面）
- `GET /api/settings` 的密钥脱敏（自己看自己的密钥）
- `retry-failed` 的 `project_id` / `build_id` 一致性校验（无第二用户）
- 登录限流的 `X-Forwarded-For` 信任与内存清理（单机无反代）
- 进程内限流 / 并发 semaphore 的跨进程协调（单 worker）
- `owner_user_id IS NULL` 的可见性收紧
- JWT 弱默认密钥的强制校验（本地 dev）

---

## 10. 实施记录

### 批次 1（2026-09-17）

| 项 | 改动 | 测试 / 证据 |
|---|---|---|
| A-1 | [build.py](file:///workspace/backend/app/services/build.py#L452-L455) `tts_segment_cache_get` 补传 `sample_rate=sample_rate`，与 put 完全对齐 | 新增 2 个往返测试于 [test_p1_emotion_srt_cache_red.py](file:///workspace/backend/tests/test_p1_emotion_srt_cache_red.py)；另以脚本取证：旧 get 键 `7b160089…` ≠ put 键 `130494e8…`，确认旧实现永不命中 |
| A-2 | 5xx 常量提到模块级 `_MAX_5XX_RETRIES` / `_HTTP_5XX_BACKOFF_SECS`，v1 与 v3 类均引用（消除分叉根源） | [test_doubao_v3_protocol_red.py](file:///workspace/backend/tests/test_doubao_v3_protocol_red.py) 移除原先「在实例上手工注入两个属性」的掩盖写法，改为断言类级常量存在 |
| A-3 | **改用鉴权 fetch + Blob URL**，而非签名 URL：新增 `api.fetchMediaObjectUrl()`，音色库/角色/旁白试听先带 `Authorization` 取字节再 `URL.createObjectURL` | 已处理 Blob URL 释放（替换播放源 + 组件卸载）；`tsc --noEmit` 与 `next build` 均通过。**未做真实浏览器试听验证** |
| A-4 | 前端 `tags` 统一为字符串：`ProjectDetailResp.tags` 改为 `string \| null`，新增 `splitTags` / `normalizeTagsInput`，提交时 `join(',')`，展示时拆分 | `tsc --noEmit` 通过 |
| A-5 | `start_build(mode: str \| None = None)` + `StartBuildRequest.mode` 改 `None` 默认，仅在 `mode is None` 时回落项目默认值 | 新增 [test_start_build_mode_red.py](file:///workspace/backend/tests/test_start_build_mode_red.py) 4 个用例（显式 classic 不被 multicast 覆盖 / 未传回落默认 / 无默认兜底 classic / 显式 multicast 仍抛错） |

**回归结果**
- 后端：`306 passed, 1 failed, 1 skipped`（用例数 300 → 306，为本次新增 6 个用例）
- 前端：`npx tsc --noEmit` ✅ / `npx next build` ✅
- 唯一失败 `test_project_e2e.py::test_project_full_lifecycle` 为**既有**的测试隔离问题（单独跑必过；批次 1 改动前的基线全量跑同样失败），对应本文件 **E-1 / P2-4**，非本次引入

**A-3 方案调整说明**
原计划写「preview 试听走签名 URL」，实施时改为 **鉴权 fetch + Blob URL**。原因：签名 token 是**单用途**的（`consume_media_token` 首次请求即写 `used_at`），把它交给 `<audio>` 后拖动进度条会触发第二次 Range 请求而 401 —— 等于把 B-6 的问题复制到试听上。Blob 方案一次性把字节读进内存：不经过 URL token、天然支持 seek、且不需要后端改动。
副作用：音频较大时会占用内存（试听片段很短，可忽略）；章节 MP3 仍走既有签名 URL，其 seek 问题留给 **B-6**。
