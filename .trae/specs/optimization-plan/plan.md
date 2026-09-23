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

### A-7 `config_digest` 快照不完整 → 改了内容却复用旧产物 ✅
- 位置：计算 [build.py#L525-L553](file:///workspace/backend/app/services/build.py#L525-L553)；复用查询 [build.py#L869-L889](file:///workspace/backend/app/services/build.py#L869-L889)
- 现象：只哈希 narrator / speed / voice_assignments / mode / tts_provider / 情感 / 角色风格，**不含**章节正文、对白、发音规则、采样率等。
- 后果：用户润色正文、重新识别对白、增删发音规则后再次构建，只要音色语速未变 → digest 不变 → 直接复用历史成功 build，**新内容永不合成**。
- 修复方向：把正文/对白的内容哈希与规范化后的发音规则纳入 digest（或引入 prepare 版本号）。
- 验收：修改正文或发音规则后，digest 必须变化并触发重新合成。
- 实施前：先用一次真实操作复现「改了内容却秒完成」。

---

## 3. Tier B —— 长任务稳定性

### B-1 孤儿 build 恢复会每个看门狗周期重复 spawn ✅
- 位置：[job_tasks.py#L470-L544](file:///workspace/backend/app/services/job_tasks.py#L470-L544)
- 现象：`_recover_build_orphan` 只用 `if build_id in _ACTIVE_BUILDS` 去重，却**从不把自己注册进 `_ACTIVE_BUILDS`**；恢复出的 runner 也不更新该 JobTask 的心跳。
- 后果：JobTask 心跳停止 → 每个看门狗周期（默认 30s）都重新判定为孤儿 → 再次 spawn 同一 build 的 worker。并发跑同一 build → 重复 TTS 调用与重复用量、章节文件 `.tmp` 同名互写、状态互相覆盖。
- 修复方向：恢复时注册 `_ACTIVE_BUILDS` 并把心跳/任务生命周期纳入恢复 runner。

### B-2 `start_build` 竞态：锁只包检查、不包创建 ✅
- 位置：检查 [build.py#L821-L832](file:///workspace/backend/app/services/build.py#L821-L832)；插入/注册 [build.py#L922](file:///workspace/backend/app/services/build.py#L922)
- 现象：`_RUNNING_LOCK` 临界区在创建 Build 之前就释放，两个并发请求可同时通过检查。
- 后果：产生多个 queued/running build；后续用 `.scalar_one_or_none()` 查询活跃 build 会抛 `MultipleResultsFound` → 接口 500，需人工清理。
- 修复方向：把「检查 + 插入 + 注册」合并进同一临界区，或用 DB 层唯一约束兜底。

### B-3 终态覆盖：打包期间取消被写回 success ✅
- 位置：取消检查 [build.py#L1885-L1896](file:///workspace/backend/app/services/build.py#L1885-L1896)；终态写入 [build.py#L1941-L1962](file:///workspace/backend/app/services/build.py#L1941-L1962)
- 现象：打包 ZIP（大书可能耗时较久）期间用户取消，worker 随后**无条件**写回 `success/partial_success`。
- 后果：已取消的任务被「复活」为完成态，与用户意图相反。
- 修复方向：终态写入改为条件更新（`WHERE status='running'`）后判断 rowcount。

### B-4 卡在 queued 的孤儿 build 永久阻塞项目 ✅
- 位置：[build.py#L851-L867](file:///workspace/backend/app/services/build.py#L851-L867)
- 现象：活跃检查里只有 `status == "running"` 有超时兜底；`queued` 分支直接返回该 build。
- 后果：若进程在「提交 Build(queued) 之后、注册 worker 之前」被杀，该 build 永远 queued → 之后每次 start_build 都返回它，项目永久无法合成。
- 修复方向：`queued` 同样设超时兜底（基于 `created_at` / 心跳）。

### B-5 retry build 跨 build 共享同一 MP3 文件 ✅
- 位置：复制文件名 [build.py#L1151-L1164](file:///workspace/backend/app/services/build.py#L1151-L1164)；删除 [build.py#L2055-L2093](file:///workspace/backend/app/services/build.py#L2055-L2093)
- 现象：retry 对非失败章直接复用源 build 的 `audio_filename`；`delete_build` 按 `audio_filename` 无条件 `unlink`。
- 后果：删除任一 build 会连带删掉另一个 build 引用的章节 MP3 → 单章下载/预览/M4B 全部 404。
- 修复方向：retry 复用章硬链接/复制为自身命名，或删除时做引用计数（只删本 build 自产文件）。

### B-6 一次性媒体 token 与 `<audio>` Range 请求不兼容 ✅
- 位置：[media_sign.py#L105-L145](file:///workspace/backend/app/services/media_sign.py#L105-L145)
- 现象：`consume_media_token` 首次请求即写 `used_at`，二次请求返回 None → 401；token TTL 300s，前端无重签逻辑。
- 后果：拖动进度条/重放即 401，播放超过 5 分钟后失效且无恢复路径。
- 修复方向：流式媒体改为「短时多次可用」（只校验 `expires_at`，不置 `used_at`）。

### B-7 SQLite 未设 `busy_timeout` / WAL，也未开启外键 ✅
- 位置：[session.py#L17-L37](file:///workspace/backend/app/db/session.py#L17-L37)
- 现象：连接参数只有 `check_same_thread=False`，无任何 `PRAGMA`。
- 后果：build worker 每章多次 commit，与 prepare 后台、JobTask 看门狗、API 并发写 → `database is locked` 被当作整章失败（降级静音）甚至整 build 失败。外键未开导致 `ondelete` 级联不可靠。
- 修复方向：连接事件里 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;`。
- **实施取舍**：只加了 `journal_mode=WAL` + `busy_timeout=5000` + `synchronous=NORMAL`；**未开 `foreign_keys=ON`**。原因：ORM 关系已用 `cascade="all, delete-orphan"` 在应用层做删除级联，而开启外键会让 `init_db` 前后可能存在的历史悬挂引用（旧库残留）直接导致写失败／拒绝删除，对个人单机库属于「为了更严格而更易坏」。WAL+busy_timeout 才是「database is locked」的真正解药。

### B-8 取消 / 异常路径丢失 TTS 用量 ✅
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

### D-8 对白 anchor 用 `find` 取首次出现 → 重复短句错位 ✅
- 位置：[chapter.py#L243-L262](file:///workspace/backend/app/services/chapter.py#L243-L262)
- 现象：`ch.text.find(dlg.anchor_text)` 对每个对白都返回**首次**出现位置。
- 后果：中文小说中「「嗯。」」这类短对白反复出现时，第 2..N 句对白全被搬到第一处 → 旁白切片错位（挤成一坨）、收尾旁白把已读过的对白原文再朗读一遍。用户听感即「对白和它附近的旁白内容混乱」（2026-09-20 反馈）。
- **已修复 2026-09-20**：两条都做了 —— ① 优先采信 LLM 偏移并做本章内校验（`ch.text.startswith(a_text, local_start)` 且 `local_start >= cursor`）；② 否则 `ch.text.find(a_text, cursor)` **从游标单调往后**搜；③ cursor 之后找不到才退化为全文查找（保持旧行为兜底）。
- 验证：新增 [test_dialogue_anchor_slicing_red.py](file:///workspace/backend/tests/test_dialogue_anchor_slicing_red.py) 4 用例（SA-1 重复对白、SA-2 偏移量全为 0、SA-3「旁白 = 原文去掉对白 anchor」不变式、SA-4 旁白里也引用了该短语时采信准确 start），**在修复前 4 条全红、修复后全绿**。
- 附带发现：`_split_long_text`（拆句）本身没有 bug —— 用「拼接后与原文去空白等价 + 200 例随机」验证过，不丢字、不乱序。本次混乱全部来自 anchor 切片。
- 未覆盖：`anchor_text` 在正文里彻底找不到时（LLM 归一化了引号，如把「」写成""）仍按 LLM 坐标切，理论上可能把对白原文留在旁白里。沙箱库抽样 328 条对白 **0 条**命中该分支（注意：沙箱库是测试夹具数据，非线上正文），暂不处理。

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

### E-6 死代码清理 ✅（2026-09-20 完成）
- 位置：`multicast` 全家桶、`_DoubaoStubProvider` 与 `get_multicast_tts`、`_tts_default_instance`（只写不读）、`_should_strict_fail`（恒 False）
- 现象：废弃能力仍暴露在设置白名单（`DOUBAO_SEED_AUDIO_*`、`MULTICAST_STRICT_MODE` 改之无效）；`_DoubaoStubProvider` 注释仍写「Task 3 尚未实现」。
- 后果：误导后续维护，且 UI 上可改但无效的开关会让人误判。
- 修复方向：清理死代码并从白名单移除废弃项。
- 实施结果：multicast 骨架（`_should_strict_fail` / `_validate_multicast_provider` / `_multicast_synth_chapter` / `_estimate_multicast_secs` / strict 失败分支）、`get_multicast_tts` / `_multicast_instance` / `_DoubaoStubProvider` / seed_audio 限流桶、3 个失效配置键与前端多播剧入口已全部移除。
- **未做**：`_tts_default_instance` 保留未删 —— 它是 `get_tts(None)` 的记忆化默认实例，虽当前只写不读，但删除涉及 provider 缓存语义改动，与本项「清死代码」的收益不成比例，留待有实际诉求时再处理。

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

### 批次 2 —— 数据正确性 + 长任务 ✅ 完成 2026-09-18
- [x] A-6 v3 `saw_error` 一律抛错 — [tts.py#L2035-L2060](file:///workspace/backend/app/ai/providers/doubao/tts.py#L2035-L2060) — 完成 2026-09-18
- [x] A-7 `config_digest` 纳入内容哈希 — [build.py#L525-L553](file:///workspace/backend/app/services/build.py#L525-L553) — 完成 2026-09-18
- [x] B-1 孤儿恢复注册 `_ACTIVE_BUILDS` + 心跳 — [job_tasks.py#L470-L544](file:///workspace/backend/app/services/job_tasks.py#L470-L544) — 完成 2026-09-18
- [x] B-2 `start_build` 临界区含创建与注册 — [build.py#L821-L832](file:///workspace/backend/app/services/build.py#L821-L832) — 完成 2026-09-18
- [x] B-3 终态写入加取消保护 — [build.py#L1941-L1962](file:///workspace/backend/app/services/build.py#L1941-L1962) — 完成 2026-09-18
- [x] B-4 `queued` 超时兜底 — [build.py#L851-L867](file:///workspace/backend/app/services/build.py#L851-L867) — 完成 2026-09-18
- [x] B-5 retry 复用章改为独立文件（硬链接/复制，非引用计数） — [build.py#L2055-L2093](file:///workspace/backend/app/services/build.py#L2055-L2093) — 完成 2026-09-18
- [x] B-6 媒体 token 改为短时多次可用 — [media_sign.py#L105-L145](file:///workspace/backend/app/services/media_sign.py#L105-L145) — 完成 2026-09-18
- [x] B-7 SQLite 设 WAL / busy_timeout（**未开 foreign_keys**，原因见实施记录） — [session.py#L17-L37](file:///workspace/backend/app/db/session.py#L17-L37) — 完成 2026-09-18
- [x] B-8 取消/异常路径补记用量 — [build.py#L1890-L1896](file:///workspace/backend/app/services/build.py#L1890-L1896) — 完成 2026-09-18

### 批次 3 —— 切模型 / 配豆包 ✅ 完成 2026-09-18
- [x] C-1 设置页补齐「豆包配置/合成质量」分组 — [SettingsPage.tsx#L110-L115](file:///workspace/frontend/src/components/SettingsPage.tsx#L110-L115) — 完成 2026-09-18
- [x] C-2 厂商编辑器补豆包凭据 + 修 key 失焦 — [ProviderModelsEditor.tsx](file:///workspace/frontend/src/components/ProviderModelsEditor.tsx) — 完成 2026-09-18
- [x] C-3 工厂缓存纳入配置指纹 + `invalidate_tts_cache` — [factory.py#L103-L173](file:///workspace/backend/app/ai/factory.py#L103-L173) — 完成 2026-09-18
- [x] C-4 MiniMax 区分可重试/永久错误（业务码 + HTTP 4xx） — [minimax/tts.py#L122-L155](file:///workspace/backend/app/ai/providers/minimax/tts.py#L122-L155) — 完成 2026-09-18
- [x] C-5 MiniMax `_internal_model` 复用 `_strip_model_prefix` — [minimax/tts.py#L122-L133](file:///workspace/backend/app/ai/providers/minimax/tts.py#L122-L133) — 完成 2026-09-18

### 批次 4 —— 日常体验
- [ ] D-1 轮询不覆盖未保存编辑 — [ProjectDetailPage.tsx#L1238-L1242](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L1238-L1242)
- [ ] D-2 试听防乱序（请求序号/Abort） — [ProjectDetailPage.tsx#L181-L195](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L181-L195)
- [ ] D-3 仅补签未签发的章节 URL — [ProjectDetailPage.tsx#L1856-L1885](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L1856-L1885)
- [ ] D-4 `VoicePicker` 视口边界钳制 — [VoicePicker.tsx#L240-L251](file:///workspace/frontend/src/components/VoicePicker.tsx#L240-L251)
- [ ] D-5 `WaveformPlayer` src 变化复位状态 — [WaveformPlayer.tsx#L164-L167](file:///workspace/frontend/src/components/WaveformPlayer.tsx#L164-L167)
- [ ] D-6 ~~收紧拟声词替换规则~~ — **已失效**：豆包 2.0 单一化后 MiniMax TTS 被弃用，`minimax/onomatopoeia.py` 已随 provider 一并删除，无替换规则可收紧
- [ ] D-7 静音帧精确化 + 豆包时长统一 — [mp3_util.py#L180](file:///workspace/backend/app/core/mp3_util.py#L180)
- [x] D-8 对白 anchor 从游标后搜索 — [chapter.py#L243-L262](file:///workspace/backend/app/services/chapter.py#L243-L262) — 完成 2026-09-20（详见 §5 D-8）

### 批次 5 —— 工程化
- [ ] E-1 统一测试导入路径 + 重置全局单例 — [conftest.py](file:///workspace/backend/tests/conftest.py)
- [ ] E-2 `start.sh` 安装策略 + `.gitignore` 补 `.env*` — [start.sh#L250](file:///workspace/start.sh#L250)
- [ ] E-3 重新生成 `requirements.lock`
- [ ] E-4 CI 门禁真正阻断 — [ci.yml](file:///workspace/.github/workflows/ci.yml)
- [ ] E-5 README 据实校准 — [README.md](file:///workspace/README.md)
- [x] E-6 死代码清理（multicast / Stub / 无效开关） — 完成 2026-09-20（`_tts_default_instance` 除外，见 §6 E-6）

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

### 批次 2（2026-09-18）

| 项 | 改动 | 测试 / 证据 |
|---|---|---|
| A-6 | v3 流式解析：`if saw_error and not chunks:` → `if saw_error:`，即「先收到若干音频分片、中途才报错」也一律抛错，不再把被截断的 MP3 当成功返回 | [test_doubao_v3_protocol_red.py](file:///workspace/backend/tests/test_doubao_v3_protocol_red.py) 新增 `test_a6_v3_error_midstream_must_not_return_truncated_audio` |
| A-7 | `_calc_config_digest` 增加 `content_digest` 参数并纳入哈希；新增 `_calc_content_digest(session, project_id, chapters)` 对章节正文/标题、ProjectDialogue、ProjectPronunciationRule 求 sha256；digest 计算移入有 session 的代码块（在读完 chapters 之后） | 新增 [test_config_digest_content_red.py](file:///workspace/backend/tests/test_config_digest_content_red.py) 4 个用例（正文/对白/发音规则改动均使 digest 变化） |
| B-1 | `_recover_build_orphan`：把「是否已在跑」判断与 `_ACTIVE_BUILDS` 注册合入同一 `_RUNNING_LOCK` 临界区；恢复 runner 用 `HeartbeatContext(job_task_id)` 持续心跳，退出时 `finish_task` + `_unregister_active_build` | 新增 [test_batch2_lifecycle_red.py](file:///workspace/backend/tests/test_batch2_lifecycle_red.py) `test_b1_*`（断言注册进 `_ACTIVE_BUILDS`、心跳被刷新、二次恢复不重复 spawn） |
| B-2 | 新增 `_START_LOCKS`（project 粒度）+ `_serialize_start_per_project`，对外 `start_build` 是包装后的串行化版本，`_start_build_impl` 为真实实现；把「检查—创建—注册」整段串起来 | `test_b2_*`：机制层断言同 project 并发度恒为 1；集成层并发 `gather` 两次 `start_build` 只产生 1 条 Build 且 build_id 相同 |
| B-3 | 新增 `_apply_terminal_status()`：终态改为 `UPDATE ... WHERE build_id=? AND status='running'` + rowcount 判定，返回是否写回；`_run_build_inner` 据此跳过打包期间已被取消的写回，并在未写回时不把 Project 置为 done | `test_b3_terminal_write_skipped_if_not_running`（running→写回；cancelled→不写回且 zip_filename 保持为空） |
| B-4 | 新增 `BUILD_QUEUED_TIMEOUT_MINUTES`（默认 30，可在设置页「超时配置」调整）；活跃检查里 `queued` 分支也按 `created_at` 做孤儿超时兜底（超时→cancelled 并起新 build） | `test_b4_stale_queued_build_is_cancelled`（超时孤儿被取消、新 build 启动） + `test_b4_fresh_queued_build_is_reused_not_cancelled`（未超时仍复用，防误杀） |
| B-5 | 新增 `_link_or_copy()`（优先硬链接、异常退化复制）；retry 复用章改为另存为**新 build 自己命名**的文件（`build_{new}_chXXXX.mp3`）并同步另存时间轴 sidecar，新 artifact 指向新文件；不再直接复用源 build 的 filename | `test_b5_retry_reused_chapter_has_own_file`：reuse 章文件名不同；`delete_build(源)` 后 retry build 的章文件仍存在 |
| B-6 | `consume_media_token` 去掉「已使用即拒绝」，仅校验 `expires_at`；`used_at` 只记录首次消费时间用于审计/清理 | [test_media_sign_red.py](file:///workspace/backend/tests/test_media_sign_red.py) 改为 `test_token_reusable_within_ttl`（第 2/3 次请求仍 200） |
| B-7 | `session.py` 新增 `_install_sqlite_pragmas`：连接事件里 `journal_mode=WAL` / `busy_timeout=5000` / `synchronous=NORMAL`；chmod 0600 覆盖 `-wal`/`-shm`。**未开 `foreign_keys`**（见 B-7 实施取舍） | 新增 [test_sqlite_pragmas_red.py](file:///workspace/backend/tests/test_sqlite_pragmas_red.py)（PRAGMA 生效 + 多连接保持） |
| B-8 | 新增 `_record_build_usage()`（写 Build.tts_calls/chars + `record_tts_usage`）：取消早退分支补记；终态路径在条件写回后无条件补记（打包期间被取消也入账）；成功章提交处增量落库 `tts_calls/chars`，使打包/DB 异常退出也保留到最近一章的真实用量 | `test_b8_cancelled_build_still_records_usage`（合成中取消→tts_calls>0 且写入 UsageEvent） + `test_b8_exception_path_keeps_consumed_usage`（打包抛异常仍保留用量） |

**回归结果**
- 新增测试文件 [test_batch2_lifecycle_red.py](file:///workspace/backend/tests/test_batch2_lifecycle_red.py)（9 用例），连同批次 1/2 相关套件：`74 passed`
- 全量：`320 passed, 3 failed, 1 skipped`；3 个失败均已用 `git worktree` 在 **HEAD 基线**上复现，属**既有**测试隔离缺陷（非本批引入）：
  - `test_project_e2e.py::test_project_full_lifecycle` — 单跑必过，全量跑失败（对应 E-1 / P2-4）
  - `test_project_prepare_voice_pool_red.py::test_prepare_passes_user_id_to_recommend` — `username` 常量 + 跨测试 DB/模块身份污染导致 UNIQUE 冲突
  - `test_review_fixes_red.py::test_retry_failed_inherits_provider_and_mode` — 段缓存跨测试串味，使「首次调用失败」的 mock 行为失效
- 后端 `import backend.app.main` ✅

**B-5 方案调整说明**
原计划写「硬链接/复制为自身命名，或删除时做引用计数」。实施选**另存为独立文件**（硬链接优先、异常退化复制），未做引用计数。原因：引用计数需在 DB 里维护额外计数并在所有删除路径同步，复杂且易漏；另存文件把「每个 build 只拥有/删除自己的文件」这一不变式直接固化，`delete_build` 无需改动即可正确。硬链接同盘零拷贝，成本可忽略。

**B-8 异常路径说明**
「中途异常」采用**增量落库**（每章结束把 `tts_calls/chars` 写回 Build 行）而非把整段 worker 包进 `try/finally`：后者需要大范围重排缩进、风险高。增量方案的取舍是：极端情况下若异常发生在某个**章内**，该章的用量可能不计（前一章的已落库）；已可覆盖「已完成章节的用量不丢」这一主要诉求，且不触碰控制流。

### 批次 3（2026-09-18）

| 项 | 改动 | 测试 / 证据 |
|---|---|---|
| C-1 | `groupOrder` 补入 **「豆包配置」与「合成质量」**（此前 9 个 `DOUBAO_*` / `ICL_MAX_AUDIO_BYTES` / `TTS_MAX_SEGMENT_CHARS` / `POLISH_ENABLED` 项在高级配置页完全不可见）；`GROUP_META` 同步补两组图标与描述，并把「超时配置」描述更新为「…与 Build 运行/排队超时」 | `npx tsc --noEmit` ✅ / `npx next build` ✅ |
| C-2 | `ProviderConfig` 类型补 `secret` / `app_id` / `icl_api_key` / `icl_access_key` / `icl_endpoint` / `tts_v3_endpoint` / `seed_audio_endpoint` 与 5 个 `*_configured` 标记；新增通用 `SecretInput`（占位符 `***LAST4` → 保留 / 「重写」→ 清空 / 输入 → 覆盖）与 `secretField` / `endpointField` 渲染助手；豆包卡片新增 4 个专属凭据 + 2 个专属端点输入；**修 key 失焦**：`ProviderCard` 的 React key 由 `p.id` 改为数组下标，`ModelSection` 行 key 由 `${m.id}_${idx}_${globalIdx}` 改为 `globalIdx`（两处都是「被编辑字段同时充当 key」导致的卸载重建） | `npx tsc --noEmit` ✅ / `npx next build` ✅ |
| C-3 | `_tts_instances` 由 `dict[str, BaseTTSProvider]` 改为 `dict[str, tuple[指纹, 实例]]`；新增 `_tts_config_fingerprint()`（哈希全部 `DOUBAO_*` / `TTS_*` / `ACTIVE_TTS_*` 字段 + `PROVIDERS_CONFIG`）与 `invalidate_tts_cache()`；`get_tts` 命中指纹不一致时重建实例 | [test_batch3_model_config_red.py](file:///workspace/backend/tests/test_batch3_model_config_red.py) `test_c3_*`（指纹随 TTS 配置变化、改配置后免手工清缓存即重建、同配置复用同实例） |
| C-4 | 新增 `MiniMaxTTSError(retryable, code)` 与 `_PERMANENT_BIZ_CODES = {1004, 1008, 1026, 1027, 2013}`；HTTP **4xx（429 除外）** 判为永久错误；`base_resp.status_code` 命中永久集合判为永久错误；重试循环对 `retryable=False` 立即 `raise`（不再白等约 34s 退避） | `test_c4_*` 4 用例（永久业务码不重试 / 可重试业务码重试 / HTTP 4xx 不重试 / HTTP 5xx 仍重试） |
| C-5 | 抽出 `_strip_model_prefix()`（剥 `minimax:` 冒号前缀与 `MiniMax-` 厂商前缀，空值回退 `_DEFAULT_MINIMAX_MODEL`）；**激活与未激活两条构造分支共用**，未激活分支不再把 `_internal_model` 硬编码为 `speech-2.8-turbo`，显式 `model` 与 `ACTIVE_TTS_MODEL` 恢复生效 | `test_c5_*` 4 用例（变体剥离 / 未激活分支尊重显式 model / 未激活分支回退 ACTIVE_TTS_MODEL / 激活分支同样剥离） |

**回归结果**
- 新增 [test_batch3_model_config_red.py](file:///workspace/backend/tests/test_batch3_model_config_red.py)（11 用例）：`11 passed`
- 全量：`331 passed, 3 failed, 1 skipped`（用例数 324 → 335，为本次新增 11 个用例）
- 3 个失败与批次 2 完全一致（`test_project_e2e` / `test_project_prepare_voice_pool_red` / `test_review_fixes_red`），均已在 HEAD 基线复现，属**既有**测试隔离缺陷（对应 E-1），非本批引入
- 前端：`npx tsc --noEmit` ✅ / `npx next build` ✅

**C-1 范围调整说明**
原计划写「补齐 `groupOrder` 与分组元信息」，未区分三组性质。实施时只补了**「豆包配置」「合成质量」**——它们是真正「有配置项但 UI 无入口」的组。第三个组「模型配置」的字段为 `PROVIDERS_CONFIG` / `ACTIVE_TTS_*` 与 5 个标注「（遗留）」的扁平凭据，均已由「模型厂商」标签页（ProviderModelsEditor）承载或明确不再展示；把它加入 `groupOrder` 会让用户暴露在一个可手改的 `PROVIDERS_CONFIG` JSON 输入框与遗留凭据上，与页面既有设计（`SettingsPage.tsx` 原注释）相悖。

**C-2 脱敏回写说明**
未改动后端：`PUT /api/providers` 已对 5 个敏感字段（`api_key` / `secret` / `app_id` / `icl_api_key` / `icl_access_key`）做「`***` 开头 → 保留旧值」处理，与本批前端 `SecretInput` 的占位符语义天然对齐；端点类字段按原值透传，后端 `doubao_field()` 负责「以 `/` 开头则拼 `base_url`」。

**未做真机验证**
C-1 / C-2 只做了类型检查与生产构建，**未在浏览器中实际点击验证**（新增豆包凭据表单、id 输入框连续输入不失焦）。

### 批次 3.5 —— 豆包 TTS 2.0 单一化 + LLM 逐段语音指令（2026-09-20）

> 本批不由 plan.md 的 Tier 清单驱动，而是根据实际账号能力（只开通「语音合成 2.0」99 音色 + 「声音复刻 2.0」）与用户诉求新增；其中「清理 multicast」对应本文件 §6 的 **E-6**。

| 工作流 | 改动 | 测试 / 证据 |
|---|---|---|
| ① 弃用 MiniMax 语音合成 | `TTSRegistry` 移除 `minimax`、`_resolve_provider_name` / `get_tts` 兜底改 `doubao`；`get_tts_by_voice_id` 遇 `minimax:` 前缀**显式报迁移提示**（不静默兜底，否则 `minimax:xxx` 会被当豆包 speaker 发出去）；`TTS_PROVIDER` / `ACTIVE_TTS_PROVIDER` / `ACTIVE_TTS_MODEL` 默认改豆包；删除只服务 MiniMax 的 `TTS_API_KEY` / `TTS_BASE_URL` / `TTS_RPM_LIMIT`；删除 `providers/minimax/{tts.py,voices.json,onomatopoeia.py}`；MP3 工具（`concat_mp3_files`）迁至 `core/mp3_util.py`，静音占位采样率统一到 `DOUBAO_AUDIO_SAMPLE_RATE`；默认旁白 `minimax:male-qn-jingying` → `doubao:zh_male_qingcang_uranus_bigtts`（擎苍 2.0）；前端去掉 MiniMax 厂商 Tab 与类型 | 全量 `336 passed, 3 failed`（失败均为既有隔离缺陷）；`tsc --noEmit` ✅ |
| ② 只保留豆包 2.0 音色 | `_BUILTIN_VOICES` 删除 1.0 区块 94 条（187 → 93，全部 `seed-tts-2.0`）；`_resolve_model_for_speaker` / `_resolve_resource_id_for_v3` 的**未知值兜底**、`list_voices` 默认 model、`build._voice_model_lookup` 全部改 2.0（对显式 1.0 入参仍如实映射，交由 `is_voice_usable_on_v3` 过滤）；远程音色同步 resource 收敛为 2.0/ICL 2.0；下线音色库「小模型免费」Tab | 全量 `329 passed, 2 failed`；`tsc --noEmit` ✅ |
| ③ LLM 逐段语音指令 | 新增 `services/voice_instruction.py`（按章批量 6 章/次、并发 2、批级重试、防御式回填、clamp 512）；`ProjectDialogue.instruction` 新列 + 老库 ALTER；prepare 在 dialogues 与 voice_recs 之间新增 `instructions` stage（带 checkpoint）；`chapter.py` 逐段指令优先于角色级（子段共享）；`_calc_content_digest` 纳入逐段指令 | 新增 [test_voice_instruction_red.py](file:///workspace/backend/tests/test_voice_instruction_red.py) 10 用例 |
| ④ 清理 multicast 残留（E-6） | 见 §6 E-6 | 全量 `328 passed, 2 failed`；`tsc --noEmit` ✅ |

**回归结果（四轮后最终）**
- 后端全量：`328 passed, 2 failed, 1 skipped`（跑前需 `rm -rf data/audio/_seg_cache`）
- 2 个失败均为**既有**测试隔离缺陷（对应 E-1），四轮改动前后一致、非本批引入：
  - `test_project_e2e.py::test_project_full_lifecycle`
  - `test_project_prepare_voice_pool_red.py::test_prepare_passes_user_id_to_recommend`
- 前端：`npx tsc --noEmit` ✅
- 抖动预警：`test_review_fixes_red.py::test_retry_failed_inherits_provider_and_mode` 会因 `test_path_env_override_red.py` reload `core.config` 导致的**段缓存跨用例泄漏**而偶发失败（全量跑首次合成被缓存命中）。属 E-1 的同一根因，跑前清缓存即可复现「恰好 2 个失败」。

**关键决策与依据**
1. **MiniMax 只弃用 TTS，LLM 完整保留** —— MiniMax 同时提供 TTS 与 LLM，代码路径分离；角色识别/对白归属/音色推荐/润色仍走 `MiniMaxLLMProvider`。
2. **1.0 音色物理删除而非隐藏** —— 账号未开通 1.0 资源，实测请求 `X-Api-Resource-Id: seed-tts-1.0` 返回 HTTP 403 + `code=45000030 "requested resource not granted"`（服务端把它解析成 `volc.service_type.10029`）；且 1.0 音色不支持语音指令。
3. **语音指令不做「逐句都加」** —— prompt 明确允许平淡叙述型对白留空（日常应答、纯信息交代），每句硬加情绪指令反而使整体听感浮夸失真。
4. **本轮只上语音指令，不做语音标签 CoT** —— 官方模型列表写明只有高表现力版 `seed-tts-2.0-expressive` 支持语音标签，且警告其「生成效果稳定性存在波动」；同时 HTTP 文档说 `model` 参数「仅当 speaker 为复刻音色时需指定，且指定后不支持 `context_texts`」——两处文档有张力，需真机验证后再定。
   - ⚠️ **2026-09-20 更正**：这条当时只读到「expressive 支持标签」，漏了同一句话的前半段 —— **standard 连语音指令也不支持**。因为当时不传 `model`（默认 standard），逐段指令全程被上游静默忽略，等于白生成。详见上文「批次 3.5 补丁 2」。

**未做 / 已知缺口（如实记录）**
- **未做真机验证**：本批全部改动只跑过单元/集成测试与类型检查，**未用真实豆包 Key 实际合成过一句**。两个必须真机确认的点：(a) 2.0 音色 + 逐段 `context_texts` 的实际听感与稳定性；(b) `model` / `use_tag_parser` 的字段语义（若要开语音标签）。
- **存量数据迁移**：老项目的 `Build.narrator_voice_id` / `voice_assignments` 里可能是 `minimax:xxx` 或无前缀的 MiniMax 音色 id、`Build.tts_provider` 可能是 `minimax`。带前缀的会得到明确的迁移提示，**无前缀的老 id 会走到豆包侧换回一个上游 `speaker not found`**（未做「豆包音色表预检」这类额外守卫，避免误伤远程/自定义音色）。处理方式是：重新识别（会 `delete(ProjectCharacter)` 后按新推荐重写 `assigned_voice_id`）或手动改音色。
- **逐段指令只能读不能改**：章节详情已透出（前端以「指令」标签展示），但没有「人工修正单条指令」的接口；改指令目前需重跑 prepare 或改库。
- `models.py::TTS_RESOURCE_IDS` 仍列 1.0 资源 id（官方参考清单，仅供设置页展示、不参与合成）；选它不会生效（合成按音色表的 `model` 走）。

#### 批次 3.5 补丁 —— 真机联调暴露的 v3 响应解析缺陷（2026-09-20 同日）

上面「未做真机验证」的缺口在部署后立即暴露：**v3 流式路径此前从未成功合成过一次**，403（资源未开通）解决后连着翻出两个互相掩盖的解析 bug，随后又翻出第三个。

| 提交 | 症状 | 根因 | 修复 |
|---|---|---|---|
| `91de09a` / `7e04909` | 试听 500：`403 Forbidden`，日志无 body | `_post_stream_v3` 对 4xx 直接 `raise_for_status()`，上游放在 body 里的 `header.code` 被丢弃 → 无法区分「Key 没权限」「资源未开通」「resource id 与音色不匹配」 | **所有 `>= 400` 都先 `aread()` 读 body** 再构造异常；新增 `_v3_http_error_hint()` 把 `45000030 requested resource not granted` 翻译成可执行提示 |
| `bab57f3` + `d015a67` | 试听 500：`DoubaoTTSResponseV3Error: code=20000000 msg=OK`（**Build 章节合成同报此错，同一处代码**） | 两个坑叠加：(a) 成功码只认 `code == 0`，实测是 `20000000`；(b) 音频字段读的是 `audio`，官方是 `data`。坑 (a) 的错误分支会 `continue`，顺带跳过同 chunk 的音频 —— 于是「取不到音频」被「业务错」永久掩盖 | `_V3_SUCCESS_CODES = {0, 20000000}` 且**以 `message == "OK"` 为主判据**；改读 `data`（保留 `audio` 兜底）；中途报错不再返回被截断的 MP3 |
| 本次 | **部分音色**试听 500：`无音频分片（成功码但既无 data 也无 audio 字段）；实际收到的字段=['code','message','data']` | 日志自相矛盾（字段里有 `data`）说明又踩了新形态：上游返回 `{"code":20000000,"message":"OK","data":""}` —— **成功码 + 音频为空**。触发条件：中文文本发给纯外语音色（`en_female_stokie_uranus_bigtts`，音色表声明语种仅「美式英语」），上游不报错也不产音频 | ① 试听文案按音色 `languages` 选语种（音色库页 `previewTextFor`）：无 `zh` 的音色改用英文例句，否则这类音色永远试听不了；② 空音频报错改为区分「字段不存在 / 字段为空 / 解不出音频」三种形态并打印 `data 形态=type=… len=…`；③ 新增 `_v3_empty_audio_hint()`，命中「中文文本 + 非中文音色」时直接在报错里点明语种不匹配 |

回归：后端 `340 passed, 2 failed, 1 skipped`（相较批次 3.5 基线 337 passed 多出 3 个新用例，2 个失败与基线同）；`tsc --noEmit` ✅。

新用例（`test_doubao_v3_stream_shape_red.py`）：T-S10 空 `data` 与字段缺失分开报；T-S11 中文文本 + 外语音色 → 报错含语种不匹配；T-S12 文本语种匹配时**不加**语种提示（避免把其它成因误归到语种）。

**仍未解决**：ICL 复刻音色没有语种元数据（`icl_voices_for_user` 不返回 `languages`），英文克隆音色试听仍会用中文例句 → 报错里没有语种提示，只能看到 `data 形态=type=str len=0`。修它需要先有「克隆音色语种」这个字段。

#### 批次 3.5 补丁 2 —— 逐段语音指令「静默失效」：从不传 `req_params.model`（2026-09-20）

用户反馈：正文写着「一虚弱的声音」，生成的音频毫无虚弱感，「语音指令好像没生效」。

**根因（官方《模型列表》6561/2499930）**：豆包语音合成大模型 2.0 分两版 ——
`seed-tts-2.0-standard`（**接口默认值**）**不支持语音指令 QA 和语音标签 CoT**；
只有 `seed-tts-2.0-expressive` 支持。本项目**从不传 `req_params.model`** → 一律落在 standard
→ 逐段指令（`context_texts`）发出去被上游**静默忽略**：HTTP 200、`code=20000000`、音频正常、
无任何报错或 warning，听感与不加指令一模一样。批次 3.5 只验证了「指令有没有送出去」，
没验证「送出去之后上游认不认」——这正是当时记录的「未做真机验证」缺口。

| 改动 | 说明 |
|---|---|
| `DOUBAO_TTS_MODEL`（新增，默认 `seed-tts-2.0-expressive`） | 下发到 `req_params.model`；设 `""` 可一键回退旧行为（不下发） |
| 官方音色 | 下发 `model`；**有无指令都下发** —— 模型选择是「整章统一」的，混用会让同章音色/韵律跳变 |
| 复刻音色 | **不下发 `model`**：HTTP 文档写明「model 仅当 speaker 为复刻音色时需指定，且指定后不支持 `context_texts`」，复刻场景保住 `context_texts` 更有价值 |
| 段缓存键 `_seg_cache_key` | 新增 `tts_model` 参与哈希 —— 不纳入的话，改完设置重建会命中旧的「standard + 无情绪」音频 |
| build `_calc_config_digest` | 新增 `tts_model` —— 否则切 model 重新合成会命中**历史成功 build** 直接复用旧产物，用户听不出任何变化 |

验证：新增 [test_tts_model_switch_red.py](file:///workspace/backend/tests/test_tts_model_switch_red.py) 6 用例
（TM-1 官方音色同时带 model+context_texts / TM-2 无指令也带 model / TM-3 空设置不下发 /
TM-4 复刻音色不下发 model / TM-5 缓存键与 digest 随 tts_model 变 / TM-6 默认值必须是 expressive），
**修复前 6 条全红、修复后全绿**。全量后端 `350 passed, 2 failed, 1 skipped`（2 个失败与基线同）。

**同轮未做（等确认）**：
- **语音标签（CoT）仍未实现** —— 上一轮用户选的是「先只上语音指令」。且官方标注其为「抢鲜体验」，预置音色里只有少数支持（可爱女生/调皮公主/爽朗少年/天才同桌）或**声音复刻 2.0** 音色；预置音色走 `{{"additions":{"context_texts":[...]}}}` 内联标签（`}}` 前必须留空格），复刻音色走 `<cot text="...">文本</cot>`。要做需单独一批。
- **复刻音色仍丢弃逐段指令**（`is_clone_speaker` 分支只打 warning）。官方产品动态称「豆包声音复刻模型 2.0 支持…结合语音指令标签」，与 HTTP 文档的 model/context_texts 互斥说明存在张力，未真机验证前不擅自改动。

#### 批次 3.5 补丁 3 —— 未归属对白退化成旁白音色（2026-09-20）

用户反馈：原文里「有人」「另一人」这类**没有明确角色归属**的对白（路边议论、群杂），
全篇都用**旁白音色**念出来 —— 听感上是旁白在自言自语，完全没有「有人在说话」的层次。
需求原文：「对白没有明确的角色归属，全文中对于这种没有归属的对白，自动分配和旁白不同的音色。」

**根因**：这类 speaker（「有人」「另一人」，或空 speaker）不在 `ProjectCharacter` 表里，
于是 `voice_assignments` 也没有它 → [`chapter.py`](file:///workspace/backend/app/services/chapter.py) 的
`voice_assignments.get(speaker, narrator_voice_id)` 按默认值**直接退回旁白音色**。

**改动**（[`build.py`](file:///workspace/backend/app/services/build.py)）：

| 函数 | 作用 |
|---|---|
| `_unknown_speaker_voice_candidates(narrator)` | 候选池 = 内置音色里 `scene` 含「通用」的条目，**排除旁白音色**；额外要求音色声明了 `zh` 能力 |
| `_fallback_voice_for_unknown_speaker(speaker, narrator)` | `sha256(speaker) % len(池)` → **同名稳定、异名大概率不同** |
| `_with_unknown_speaker_voices(session, pid, va, narrator)` | 查 `ProjectDialogue.speaker` 去重，把不在 `va` 里的补上（不覆盖已有分配） |

调用点两处：`start_build`（**必须放在 `_calc_config_digest` 之前** —— 兜底音色改变了实际使用的
音色映射，digest 不跟着变的话用户重新合成会命中历史成功 build 的旧产物，仍然听不出变化）、
`retry_failed_build`（老快照里没有兜底音色，重试失败章要补一次）。

**两个「不动」**：① 项目完全没有角色分配时不动 —— 连主角都没音色，逐句兜底只会让全书对白
变成同一个路人音色，不如先做识别；② 没有对白记录时映射原样返回。

**踩到的坑（自查发现，已修）**：初版常量写成 `"通用场景"`，而官方音色表里该 scene 的**字面值是
「通用」**（实测分布 `{'通用': 57, 'S2S': 4, '角色扮演': 20, ...}`，含「通用场景」的 0 条）→
候选池退化成**全部 92 条内置音色**，把 3 条纯外语音色（`en_male_tim_uranus_bigtts` /
`en_female_dacey_uranus_bigtts` / `en_female_stokie_uranus_bigtts`，`languages` 仅 `["en"]`）
也选了进来 —— 中文正文配它们正好命中上一条补丁刚修的「成功码但音频为空」，等于把对白变成静音。

验证：新增 [test_unknown_speaker_voice_red.py](file:///workspace/backend/tests/test_unknown_speaker_voice_red.py) 6 用例
（US-1 候选池口径：非空 / 全中文可用 / 不含旁白音色 / 纯外语音色不入池；US-2 同名稳定且永不为旁白；
US-3 不同临时说话人不撞成同一人；US-4 未知 speaker 被补且已有分配不被覆盖；US-5 两种「不动」；
US-6 端到端 `start_build` 落库快照里带上了兜底音色），全绿。全量后端
`358 passed, 3 failed, 1 skipped`；去掉本改动重跑同一套为 `352 passed, 3 failed, 1 skipped`
（**失败数与基线完全一致，本改动零新增失败**）。3 个失败均为 E-1 类的跨用例隔离/缓存泄漏抖动
（`test_project_e2e.py::test_project_full_lifecycle`、
`test_project_prepare_voice_pool_red.py::test_prepare_passes_user_id_to_recommend`、
`test_review_fixes_red.py::test_retry_failed_inherits_provider_and_mode`），单跑全绿。

#### 批次 3.5 补丁 4 —— 声音复刻（ICL）请求体不符合 V3 协议 + 错误体被丢弃（2026-09-21）

用户反馈：声音复刻上传参考音频后任务立刻失败。
```
[icl_worker] FAIL HTTPStatusError: Server error '500 Internal Server Error'
  for url 'https://openspeech.bytedance.com/api/v3/tts/voice_clone'
```

**两个问题叠在一起：**

**① 报错信息什么也没说。** [`icl.py::_http_post_json`](file:///workspace/backend/app/ai/providers/doubao/icl.py)
直接 `resp.raise_for_status()`，而官方文档写明**复刻接口的失败通道就是「HTTP 非 200 +
body 里的 code/message」**（「训练失败时候 HTTP 返回非 200，code 字段返回详细错误码」）——
body 被丢掉后日志里只剩一句 `500 Internal Server Error`。这与批次 3.5 补丁在 TTS 侧修的
403 是**同一类缺陷**，只是换了个接口。

**② 请求体本来就不可能成功。** 按官方文档（6561/2534906 + 2227958）逐字段核对：

| 字段 | 历史实现 | 官方 V3 要求 |
|---|---|---|
| `model_type` | 下发 `"ICL2.0"` | **V3 请求参数表里没有这个字段**。它是 **V1** 训练接口（/api/v1/mega_tts/audio/upload）的整型字段（1/2/3/4/5）。V3 一次训练的音色对 1.0/2.0 **同时可用**，「用哪一版合成」由**合成时**的 `X-Api-Resource-Id` 决定；算法版本只体现在**响应** `speaker_status[].model_type`（4=ICL V2 / 5=ICL V3） |
| `speaker_id` | 自己生成的 `"icl_<hex>"` | 必须要么是控制台购买音色槽位得到的 `S_xxx`（预付费），要么是固定字面值 `"custom_speaker_id"`（后付费，真实代号写在 `custom_speaker_id`） |
| 自定义代号 | `icl_<hex>` | **非法**：官方防冲突正则 `^((?i:S_\|ICL_\|MIX_\|DiT_\|BV)\|...)` 里 `ICL_` 是大小写不敏感保留前缀 |
| `demo_text` | 顶层 | 属于 `extra_params` |
| 查音色 | `{"speaker_id": "<自定义代号>"}` | 后付费必须成对 `{"speaker_id":"custom_speaker_id","custom_speaker_id":"<代号>"}` |

**改动：**

| 文件 | 改动 |
|---|---|
| `icl.py` | `_http_post_json` 在 `status_code >= 400` 时先读 body，抛新增的 `DoubaoICLHTTPError`（带 `status_code` / `code` / `logid` / `body`）；新增 `_icl_http_error_hint()` 把 45001001/45001102/45001107/45001109/45001114/45001122/45001123/4500xxxx 与 403/5xx 翻译成可执行提示 |
| `icl.py` | 新增 `_OFFICIAL_SPEAKER_ID_FORBIDDEN_RE`（官方防冲突正则）、`_generate_custom_speaker_id()`（`iclvoice<hex>`，前缀刻意不带下划线）、`_speaker_lookup_payload()`（预付费/后付费两种定位形态） |
| `icl.py` | `create_training`：`speaker_id="custom_speaker_id"` + `custom_speaker_id=<生成>`；**去掉 `model_type`**；`demo_text` 移入 `extra_params`；服务端返回的 `speaker_id` 优先（顶层，不在 `data` 里），兜底用自己生成的代号（**绝不**回退成字面量 `custom_speaker_id`） |
| `icl.py` | `query_training` 改用 `_speaker_lookup_payload`；错误信息也附提示 |
| `icl.py` | 新增 `is_cloned_speaker_id()` —— **复刻音色判定的唯一实现**（`S_` / `icl_` / `iclvoice`，含 `icl:`、`doubao:` 命名空间剥离） |
| `tts.py` | 新增 `_is_icl_cloned_speaker()`（函数内延迟 import，避免 tts↔icl 模块级成环），替换原先散在 3 处的 `startswith("icl_")/startswith("S_")`：`_voice_supports_emotion`、v1 `_build_payload` 的 cluster 路由、v3 `_resolve_model_for_speaker` 的 `X-Api-Resource-Id` 路由、`is_clone_speaker` |
| `build.py` | `_voice_model_lookup`（段缓存键的模型反查）改用同一个判定 |
| `models.py` | `TRAIN_MODEL_TYPES` 的注释更正：该字段**不下发上游**（只在参数校验收口），V3 无算法选择能力 |

> ⚠️ `tts.py` 的 `_resolve_model_for_speaker` 只认 `S_`/`icl_` 前缀——如果不同步改成
> `iclvoice`，新的复刻音色会被当成普通音色路由到 `seed-tts-2.0`，上游会回
> `resource ID is mismatched with speaker related resource`（55000000）。这是本次一并修的关键联动点。

**验证：** 新增 [test_icl_voice_clone_http_error_red.py](file:///workspace/backend/tests/test_icl_voice_clone_http_error_red.py) 6 用例
（IH-1 HTTP 500+顶层 code/message 透出 / IH-2 网关级 4xx 的 `header.code` / IH-3 非 JSON body 原样带出 /
IH-4 两种定位形态 / IH-5 随机 300 次生成的代号都不命中官方正则（并断言历史 `icl_xxx` 命中）/
IH-6 create_training 撞 500 时能看到业务码）；更新 T-IC3-V4（+V4b）、T-MT-3/T-MT-7、
T-ICL-V1-4（+V1-7）、T-D5、T-RF1-ICL 等原先编码了错误契约的断言。

全量后端 `367 passed, 3 failed, 1 skipped`（3 个失败与基线同，均 E-1 类抖动，单跑全绿）。

**未做 / 待确认：**
- **后付费 vs 预付费**：本改动按**后付费**（自定义音色代号）实现 —— 项目里从来没有任何「音色槽位
  id（S_xxx）」配置项，历史实现一直是动态生成 id，语义上只能是后付费。若账号是**预付费**
  （控制台购买槽位），需要新增一个「音色槽位 id」配置并把 `speaker_id` 填成 `S_xxx`。
- **必须先开通后付费音色服务**：控制台需开通「豆包声音复刻模型 2.0 + 后付费音色服务」，
  否则会拿到 403 / 45000030（现在报错里会直接写明）。
- **前端的「训练模型算法」下拉已失效**：V3 不接受 `model_type`，该下拉不再影响训练（值也不落库）。
  本次保留（避免 UI 级联改动），建议后续直接删掉。
- **仍未真机验证**：本改动只跑了单元/集成测试，没有真实 Key 合成过一次；下次复刻请留意日志里是否
  出现 `code=...` 与提示语。

#### 批次 3.5 补丁 4 追加 —— 真机 403 归因：`volc.megatts.timbre`（2026-09-21 同日）

补丁 4 上线后重试，报错信息终于说清了原因（这正是补丁 4 第一部分的价值）：

```
[icl_worker] FAIL DoubaoICLHTTPError: 豆包 ICL 接口返回 HTTP 403
  code=45000030 msg=[resource_id=volc.megatts.timbre] requested resource not granted
  logid=20260921110349804E5998CBA8D1A58557 提示：账号未开通该资源，请在控制台开通对应服务
```

用户说「我确定我开通了声音复刻2.0」——**这就对了，因为开通项不止一个**。核对官方文档后确认：

| 官方原文 | 出处 |
|---|---|
| 「后付费音色需要开通**声音复刻模型2.0服务**，**并单独开通后付费音色服务**」 | 声音复刻下单及使用指南（新版控制台） |
| 「后付费音色需要开通勾选声音复刻模型2.0**和音色服务**，并**手动开通后付费音色服务**」 | 同文档（旧版控制台） |
| 「服务类型、资源包、并发、音色等可按需下单，下单前请务必在**对应的项目**下下单，以免下错资源」 | 新版控制台快速入门 |
| 「请求的服务未开通，请确认是否已经在控制台上开通服务」 | API 接入 FAQ（6561/111522） |

即 `resource_id=volc.megatts.timbre` 指向的是**「音色」资源**，而「声音复刻模型 2.0」
只是同一服务里的另一项。**并且服务/资源包/音色槽位都是按项目隔离的**，
API Key 所属项目必须与开通服务的项目一致。

**本轮改动（把这次排查经验固化进报错，避免下次再靠人肉翻文档）：**

| 文件 | 改动 |
|---|---|
| `icl.py` | `_icl_http_error_hint()` 把 45000030 / `403 + not granted` 单列一条长提示：用正则从 message 里抽出 `resource_id=xxx`，给出①②③④ 四步控制台核对清单（声音复刻2.0 / **后付费音色服务需单独开通** / 项目隔离 / 本次鉴权方式） |
| `icl.py` | 新增 `_auth_mode_desc()`：报错里附带「本次实际鉴权方式 + 凭据来源（icl_api_key / api_key / env）+ key 末 4 位」（**不打印明文**）。因为 `X-Api-App-Key`（旧版 AppID）按 AppID 校验资源、`X-Api-Key`（新版）按项目校验，用错控制台版本同样会得到这个 403 |
| `icl.py` | `_http_error` 由 staticmethod 改为实例方法（要读鉴权信息），报错里增加 `鉴权=...` 段 |
| `services/icl.py` | `error_msg` 截断上限 500 → 2000（提示现在有 4 步清单，500 会把后半段切掉） |
| `.trae/notes/doubao-voice-apis.md` | 新增 §12.2.1（403/45000030 归因 + 开通清单 + 项目隔离 + 鉴权差异）；新增 §9.1（**音色管理 HTTP `BatchListMegaTTSTrainStatus`** = 控制台文档说的「批量查询接口」，能列出账号下已购买的音色槽位 `S_xxx`、剩余训练次数、别名 —— 走 AK/SK 控制面签名，与数据面 `/api/v3/tts/*` 不是一套鉴权） |

验证：`test_icl_voice_clone_http_error_red.py` 增 IH-7（45000030 必须带出 `resource_id`、
「后付费音色服务」、项目隔离、鉴权方式）与 IH-8（鉴权方式能区分新旧控制台且不泄露明文密钥）。
全量后端 `369 passed, 3 failed, 1 skipped`（3 个失败与基线同）。

**结论（给用户的处置，不是代码问题）：** 需要在**该 API Key 所属项目**下，控制台【开通管理】里
同时具备 ①「豆包声音复刻模型 2.0」②「音色服务」③「后付费音色服务」；三者缺一即 403。

**顺带回答的另一个问题：** 控制台/页面上做的复刻音色**不会自动同步**到本平台
（`icl_voices_for_user` 只读本地 `icl_training_tasks`）。要做同步，可用 §9.1 的
`BatchListMegaTTSTrainStatus`（只覆盖**预付费音色槽位**；后付费自定义代号的音色不在其中）——
尚未实现，等确认后再做。

**仍未做：**
- 预付费路径（用已购买的 `S_xxx` 音色槽位训练）：需要新增「音色槽位 id」配置；
  有了 §9.1 的批量查询接口，还可以做成「自动挑一个剩余训练次数的槽位」。
- 前端「训练模型算法」下拉去除（该字段不下发上游，已失效）。

#### 批次 3.5 补丁 5 —— 同步控制台已有的复刻音色（2026-09-21 同日）

承接上一条：用户选择「先做从 §9.1 接口同步已有音色」。背景是平台里的 `icl:` 音色
**只来自本地 `icl_training_tasks` 表**，在豆包控制台/页面做的复刻音色不会自动出现
（`icl_voices_for_user()` 只查本地库；`ListSpeakers` 拉的是官方精品音色库，不含个人复刻）。

**做法**：调**控制面**「音色管理 HTTP」`BatchListMegaTTSTrainStatus`（6561/2235883，
即控制台文档说的「批量查询接口」），把账号下已购买的音色槽位拉进来，按
`cloned_voice_id = SpeakerID` **幂等 upsert** 成本地 `IclTrainingTask` 行。

> 关键设计：**不新增表、不新增字段，也不写 `voices_doubao.json`**。复用现有任务行后，
> 同步来的音色自动出现在三个地方 —— ①「声音复刻」列表 ②`/api/voices` 音色库
> （`icl_voices_for_user` 过滤 `status ∈ {2,4}`）③角色推荐候选池；合成时
> `icl:S_xxx` 经 `is_cloned_speaker_id()` 路由到 `seed-icl-2.0`。零 DB 迁移（`init_db` 只有 `create_all`）。

| 文件 | 改动 |
|---|---|
| `icl.py` | 新增控制面常量（host/service/region/version 2023-11-07/Action）+ `batch_list_train_status()`：AK/SK 签名**复用** `doubao_list_speakers.py::_build_authorization`（不复制密码学代码）；分页用 `PageNumber` 递增 + 「本页不足一页即停」（不与 `NextToken` 混用）；新增 `_control_plane_error()` 解析 `ResponseMetadata.Error`（Code/Message），401/403 时提示「这是 AK/SK 签名，与合成的 API Key 不是一套」 |
| `services/icl.py` | 新增 `sync_icl_voices_from_console(user_id)`：`State → status` 映射（Success/Active→4、Training→1、Unknown→0、Expired/Reclaimed→3 并写可读 error_msg）；按 `cloned_voice_id` upsert，**不覆盖本地已有的参考音频等字段**，只更新状态/别名 |
| `routes.py` | 新增 `POST /api/icl/sync`（配置缺失 400 / 上游失败 502 + logid） |
| 前端 | `api.ts` 加 `iclSyncVoices()` + `IclSyncResp`；`VoiceLibraryPage` 在「我的复刻音色」标题旁加「同步控制台音色」按钮（`title` 里写明需要 APP_ID + SK），成功提示 `控制台返回 N 个音色，其中可用 M 个（新增 x / 更新 y）` |

**前置条件（用户侧）**：设置页需填 **豆包 APP_ID（纯数字）** 与 **豆包 SK**，且 AK/SK 要是
火山引擎控制台【访问控制-API 访问密钥】里的 Access Key ID / Secret Access Key；
签名不对会得到 `AccessDenied`，报错里会直接说明。

验证：新增 [test_icl_console_sync_red.py](file:///workspace/backend/tests/test_icl_console_sync_red.py) 6 用例
（CS-1 State 映射 + 同步后能被 `icl_voices_for_user` 看见 / CS-2 幂等且不覆盖本地参考音频 /
CS-3 缺 APP_ID·SK 的提示 / CS-4 分页与请求形状（Action·Version 在 query、AppID 在 body、
AK/SK 签名在 header）/ CS-5 控制面错误（401 AccessDenied 与 200+ResponseMetadata.Error）/
CS-6 路由 200 与 400）。前端 `npx tsc --noEmit` ✅。
全量后端 `375 passed, 3 failed, 1 skipped`（3 个失败与基线同，均 E-1 类抖动，单跑全绿）。

**已知限制（如实记录）**：
- **只覆盖预付费音色槽位**：接口只列已购买槽位，后付费自定义代号的音色不在其中
  （那种要继续用 `/api/icl/voices` 训练出来的本地记录，或走 `voices_doubao.json` 手工登记）。
- **同步是「拉进来」，不是「双向」**：平台内删除同步来的任务行，只是删本地记录，
  不会动豆包侧音色；下次同步会重新出现。反过来改别名也会被下次同步覆盖。
- **依赖 AppID**：`AppID` 是该接口的必填参数；未配置时直接 400 并提示去设置页补。

#### 功能下线 —— 移除 M4B 有声书打包（2026-09-21）

用户决定**废弃打包 M4B 功能**，故整体移除（不是隐藏入口，是删代码）。

**被移除的能力**：把一次 Build 的全部章节 MP3 用 ffmpeg 转成单个带章节元数据的 `.m4b`
（AAC + loudnorm 响度归一），含后台任务、状态轮询、签名下载。

| 文件 | 改动 |
|---|---|
| `services/m4b.py` | **整个文件删除**（约 249 行：ffmpeg 转码、FFMETADATA1 章节元数据、`start_m4b_task` / `get_m4b_status`、JobTask 编排） |
| `api/routes.py` | 删除 `POST/GET /projects/{id}/builds/{id}/m4b` 两个端点；`GET /media/sign` 去掉 `kind=book_m4b` 合法值；`/media/stream` 去掉 `book_m4b` 分支（含 `audio/mp4` FileResponse 与 `.m4b` 下载名）；区块注释改回「合成前预估 / 用量 / 字幕」 |
| `services/media_sign.py` | `MediaKind` Literal 去掉 `"book_m4b"` |
| `services/build.py` | 去掉 `from .m4b import m4b_filename`；`delete_build` 去掉 M4B 产物清理 |
| `main.py` | 静态媒体归属校验的 `build_*` 文件名正则去掉 `book\.m4b` 分支 |
| `db/models.py` | `JobTask.kind` 注释改为 `chapter_mp3 \| all_zip` |
| 前端 `lib/api.ts` | 删除 `buildM4bStart` / `buildM4bStatus` / `buildM4bDownload` |
| 前端 `ProjectDetailPage.tsx` | 删除 M4B 状态机（4 个 state）、状态轮询 useEffect、`onStartM4b`，以及「有声书成品」区里的打包/下载/转码中/失败 UI；该区块现在只剩字幕 SRT / 歌词 LRC |
| 注释清理 | `config.py`（sample_rate 注释里的 m4b 兜底）、`subtitles.py`（2 处「与 ZIP/M4B 同口径」→「与 ZIP 同口径」）、`mp3_util.py`（时长漂移举例去掉「M4B 章节标记」） |

**副作用（正向）**：
- **系统依赖少一个**：全仓库再无 `ffmpeg` 调用（`grep -i ffmpeg` 只剩 README 里「将来可选」的一句与一个浏览器 MediaError 文案），部署不再需要装 ffmpeg。
- `JobTask` 与媒体签名 token 各少一种 kind，`job_tasks.py` 无需改动（未知 kind 本来就只走孤儿兜底）。

**兼容性说明**：
- 老 Build 里已生成的 `build_*_book.m4b` 文件不会被主动删除（也没必要），只是平台不再提供入口与下载路由。
- 5 分钟 TTL 的旧媒体签名 token 若 kind=`book_m4b`，`/media/stream` 会回 400 `unknown kind`（token 本就短时，忽略）。
- 前端 `tsc --noEmit` ✅；后端全量 `377 passed, 1 failed, 1 skipped`，唯一失败是既有 E-1 类抖动
  （`test_project_e2e.py::test_project_full_lifecycle`，单跑通过）。原本没有任何测试引用 M4B，故无需删用例。

---

## 11. 规模化评估与修复计划（1000~5000 章）· 2026-09-22

> 触发：用户询问「当前功能是否适用于 1000~5000 章的有声小说」。
> **结论**：1000 章**勉强可跑**（prepare 数小时、合成数小时、前端明显卡顿、磁盘 ~10GB）；5000 章**当前不适用**（1 个必修缺陷 + 3 个架构级瓶颈）。
> 量级假设：网文平均 2500 字/章 → **1000 章 ≈ 250 万字 / 7.5MB**；**5000 章 ≈ 1250 万字 / 37.5MB**。

### 11.0 评估依据（均为读码实测配置，非估算）

| 项 | 实测值 | 位置 |
|---|---|---|
| 上传上限 | `MAX_SIZE = 50MB` | [routes.py#L874](file:///workspace/backend/app/api/routes.py#L874) |
| LLM 并发 | `LLM_MAX_CONCURRENCY = 1`（**全局串行**） | [config.py#L195](file:///workspace/backend/app/core/config.py#L195) |
| 角色识别切片 | `LLM_CHAR_EXTRACT_SLICE_SIZE = 50000` 字/片，**按字符偏移切，会切断章** | [project.py#L886-L889](file:///workspace/backend/app/services/project.py#L886-L889) |
| 对白归属批 | 14 章/批（**已按章切**），并发 2 | [config.py#L211-L216](file:///workspace/backend/app/core/config.py#L211-L216) |
| 语音指令批 | 6 章/批（**已按章切**），并发 2 | [config.py#L228-L230](file:///workspace/backend/app/core/config.py#L228-L230) |
| 润色 | 1 章/次调用（仅 `POLISH_ENABLED` 时） | [project.py#L772-L790](file:///workspace/backend/app/services/project.py#L772-L790) |
| TTS RPM | `DOUBAO_TTS_RPM_LIMIT = 60` → **最小间隔 1 秒/请求** | [config.py#L133](file:///workspace/backend/app/core/config.py#L133) |
| TTS 并发 | 段级 semaphore 200（**实际被 RPM 桶压到 1/s**） | [config.py#L236](file:///workspace/backend/app/core/config.py#L236) |
| 段长 | `TTS_MAX_SEGMENT_CHARS = 600` | [config.py#L248](file:///workspace/backend/app/core/config.py#L248) |
| 合成粒度 | **按章串行**（章内段级并发） | [build.py#L1870](file:///workspace/backend/app/services/build.py#L1870) |
| 段缓存 | 内存 LRU 20000 条；磁盘上限 20GB / TTL 30 天 | [config.py#L237-L243](file:///workspace/backend/app/core/config.py#L237-L243) |

**规模换算**

| 规模 | LLM 调用（串行，不含润色） | 含润色 | TTS 段数 | RPM 地板 | 音频时长 | 磁盘(128kbps) |
|---|---|---|---|---|---|---|
| 1000 章 | 50+72+167+1 = **290 次** | **1290 次** | ≈4,200 | ≈70 分钟 | ≈154 小时 | ≈9 GB |
| 5000 章 | 250+358+834+1 = **1443 次** | **6443 次** | ≈20,800 | **≈5.8 小时** | ≈772 小时 | **≈45 GB** |

按每次 40~60s 估：1000 章 prepare ≈ **5 小时**；5000 章 ≈ **24 小时**；开润色后 5000 章 ≈ **2~3 天**（且全程串行，中断只能靠 checkpoint 续跑）。

---

### 11.1 Tier F —— 规模化（P0/P1 必做）

#### F-1 打包阶段 LRC 生成是 O(N²) ✅（2026-09-22 引入，必修）
- 位置：[build.py#L2242-L2249](file:///workspace/backend/app/services/build.py#L2242-L2249) 逐章调 `generate_chapter_lrc`；每次调用在 [subtitles.py#L142-L156](file:///workspace/backend/app/services/subtitles.py#L142-L156) 重新 `json.loads(chapters_json)` + `select(ProjectDialogue).where(project_id=...)`。
- 后果：5000 章 = **≈187GB 的 JSON 解析 + ≈7.5 亿行扫描**，打包从分钟级退化到不可完成。单章 LRC 接口同因（每次请求解析全书 + 全表扫描）。
- 修复方向：循环外一次性加载 `chapters_json` 与按章分组的 dialogues，`_collect_build_segments` 改为接收已加载数据（或新增批量入口 `generate_chapter_lrc_bulk`）。
- 验收：打包阶段 DB 查询次数与章节数**线性**相关（5000 章时查询数 ≤ 个位数量级），而非平方。

#### F-2 `project_dialogues` 无任何索引 ✅
- 位置：[models.py#L237-L254](file:///workspace/backend/app/db/models.py#L237-L254)（`project_id` / `chapter_idx` 均无 `index=True`）。
- 后果：全部查询都是 `WHERE project_id=? [AND chapter_idx=?]` → **全表扫描**；10~30 万行时每次查询都是秒级。
- 修复方向：加复合索引 `(project_id, chapter_idx)`；`init_db` 侧补 `CREATE INDEX IF NOT EXISTS`（老库无迁移机制）。
- 验收：查询计划走索引；章节详情接口 P95 明显下降。

#### F-3 批处理必须按「完整章节」对齐，不可按字数硬切 ✅（**用户补充 1**）
- 位置：[project.py#L886-L889](file:///workspace/backend/app/services/project.py#L886-L889) 用 `full_text[i : i+slice_size]` 切字符，会把一章拦腰截断。
- 需求原文：「分批次不能只按字数，因为不能把章节拆分开，比如限制了一次 50K 字，那就只取这个字数范围内的完整章节内容。」
- 修复方向：改为**按章累积装桶** —— 依次累加完整章节文本，直到「再加下一章会超过 `slice_size`」即封桶；单章本身 > `slice_size` 时**独占一桶**（绝不切章）。
- 对白归属（14 章/批）与语音指令（6 章/批）**已经是按章切批**，无需改动。
- ⚠️ **必须同时处理 checkpoint 失效**：`char_slice_completed` / `char_failed_slices` 以**切片序号**为键（[project.py#L891-L892](file:///workspace/backend/app/services/project.py#L891-L892)）。切分规则一变，旧序号全部错位 → 续跑会把「没跑的片」当成「已跑的片」跳过。须在切分口径变化时**清空该 checkpoint**（或改用「章节区间」作键）。
- 验收：断言任一桶内只含完整章节（首/尾不得是半章）；断言桶容量 ≤ 限制且尽量装满；断言旧 checkpoint 在新口径下被重置。

#### F-4 `chapters_json` 单列存全书正文，且每个请求全量反序列化
- 位置：写入 [project.py#L1334](file:///workspace/backend/app/services/project.py#L1334)/[#L1400](file:///workspace/backend/app/services/project.py#L1400)；读取 [project.py#L1770](file:///workspace/backend/app/services/project.py#L1770)（详情页）、[#L2021](file:///workspace/backend/app/services/project.py#L2021)（章节列表）、[#L2046](file:///workspace/backend/app/services/project.py#L2046)（**看单章也要解析全书**）。
- 说明：API 只回 `text_len`、不返回正文（这点是对的），但解析成本仍是 O(全书)。
- 修复方向（择一）：① 新增 `ProjectChapter` 表（一行一章，`text` 独立列），`chapters_json` 降级为兼容快照；② 至少把「章节列表/单章详情」改为不解析全文（列表用 `chapter_count` + 轻量摘要；单章用正则定位 or 拆表）。
- 验收：章节列表接口不解析全书；单章详情的内存峰值与总字数无关。

#### F-5 前端逐章串行签发 + 无虚拟滚动 ⚠️（用户已感知到「批量签发慢」）
- 位置：[ProjectDetailPage.tsx#L1009-L1018](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L1009-L1018)（章节列表）、[#L1919-L1937](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L1919-L1937)（构建详情）。
- 后果：5000 章 = **5000 次串行 HTTP**（每次还带一次 DB 查询），耗时 100~250 秒；且**超过签名 URL 的 5 分钟 TTL** → 先签的还没播就过期，滚动时大面积 401 再逐个重签。列表一次渲染 5000 个节点（每个含播放器组件）。
- 修复方向：后端新增**批量签发**接口（一次 N 章）；前端改虚拟滚动（或仅签当前可见 ± 少量预取）；`onNeedNewSrc` 已有重签兜底。
- 验收：进入页面 1 次请求完成签发；DOM 节点数与可视区相关而非章节总数。

#### F-6 合成按章串行，RPM 桶利用率被章内并发度限制
- 位置：[build.py#L1870](file:///workspace/backend/app/services/build.py#L1870)；章内 `gather` 全部段（[build.py#L2080-L2081](file:///workspace/backend/app/services/build.py#L2080-L2081)）。
- 现象：章内并发 ≈ 段数（2500 字/600 ≈ 4~5 段），下一章要等上一章全部落盘 + DB 提交；RPM 桶（1/s）在「章内段已并发完、等落盘」的间隙空转。
- 修复方向：改为「跨章流水线」——维护一个全局段级任务队列（受 RPM 桶与 TTS semaphore 双重约束），章内/章间统一调度，落盘与合成解耦。
- 验收：TTS 请求发出速率稳定贴近 RPM 上限（不再出现周期性空档）。
- 备注：此项是**吞吐优化**，RPM=60 的硬地板（5000 章 ≥5.8 小时）无法靠代码绕开，只能调大配额。

#### F-7 整包 ZIP 在目标规模下不可用 → **改为每 50 章一个独立 ZIP**（决策 5）
- 位置：[build.py#L74-L100](file:///workspace/backend/app/services/build.py#L74-L100)（`ZIP_STORED` 不压缩全量打包）。
- 原问题：5000 章产出 **≈45GB 单个 .zip**；打包期间本地需同时保住「全量 MP3 + 全量 ZIP」→ 峰值 **≈90GB**；浏览器侧 45GB 单文件基本无法可靠下载（无分片/断点续传）。
- **已定方案**：按 `ZIP_SHARD_CHAPTERS`（默认 **50**）切片，每片一个**自包含** ZIP（内部仍是《书名》/第NNN章_标题.mp3 + 同名 .lrc），命名建议 `第001-050章.zip`。5000 章 → 100 个 ZIP，单卷 ≈450MB。
- 连带收益：打包可在**每片章节凑齐后立即执行并上传**，不再需要等全书完成；峰值本地磁盘降到「全量 MP3 + 单卷 ZIP」。
- 连带约束：
  - 前端「下载全部」要改为**分片列表**（逐个可下），而不是单个链接；`Build` 需能记录多个 zip 产物（现为单一 `zip_filename` 字段）。
  - `delete_build` 的清理逻辑要覆盖整批分片。
  - **F-1 的 O(N²) 仍必须先修**：分片只降低单次打包体积，不改变「每章 LRC 都要重新解析全书 + 全表扫描」的成本。
- 验收：单卷 ≤1GB；任一卷可独立解压使用；下载入口按卷列出。

#### F-8 LLM 全串行是 prepare 时长的根因（设计取舍，需重新评估）
- 位置：`LLM_MAX_CONCURRENCY = 1`（[config.py#L195](file:///workspace/backend/app/core/config.py#L195)），使 `DIALOGUE_BATCH_CONCURRENCY=2` / `VOICE_INSTRUCTION_BATCH_CONCURRENCY=2` **形同虚设**。
- 背景：该默认值是为「按量套餐 RPM 严格」准备的（[config.py#L193](file:///workspace/backend/app/core/config.py#L193)）。
- 建议：按套餐实际情况调高（如 2~4），并让批并发真正生效；同时给 prepare 增加「预计耗时」提示，避免用户误判卡死。
- 备注：不做也没错，但 5000 章下 24 小时~数天的 prepare 需要明确告知用户。

---

### 11.2 Tier G —— 对象存储（**用户补充 2**）

> 需求原文：「存储的问题很好解决，支持对象存储即可，在设置中添加对象存储配置，生成的文件（MP3、LRC、ZIP等业务数据）都存储在对象存储中，页面点击下载时，直接从对象存储下载（对象存储权限是：公有读私有写）。」

#### G-1 新增存储抽象层（`StorageBackend`）
- 现状：产物路径散落在 `settings.AUDIO_DIR` 直接拼盘（`audio_dir / fname`、`p.is_file()`、`os.path.getsize`、`FileResponse(path=...)`），[build.py](file:///workspace/backend/app/services/build.py)、[subtitles.py](file:///workspace/backend/app/services/subtitles.py)、[preview.py](file:///workspace/backend/app/services/preview.py)、[media_sign.py](file:///workspace/backend/app/services/media_sign.py)、[routes.py](file:///workspace/backend/app/api/routes.py) 均直接操作本地文件。
- 设计：抽出 `put_bytes / put_file / open_stream / exists / size / delete / url_for(key)` 接口，两个实现 `LocalStorage`（现有行为，保留为默认）与 `S3Storage`。**默认仍 local**，避免破坏单机部署。
- 注意：`concat_mp3_files`、`mp3_duration_ms`、ffmpeg 前的分片截取（preview）都需要**本地可读**，因此合成期必须保留本地暂存目录，对象存储是「产物归档 + 分发」，不是「实时随机读写后端」。这是本项最容易踩空的地方。

#### G-2 设置页对象存储配置
- 字段（建议）：`STORAGE_BACKEND`(local|s3)、`S3_ENDPOINT`、`S3_REGION`、`S3_BUCKET`、`S3_ACCESS_KEY`、`S3_SECRET_KEY`、`S3_PREFIX`（key 前缀/目录）、`S3_PUBLIC_BASE_URL`（下载域名）、`S3_PATH_STYLE`(默认 false) 以及 `ZIP_SHARD_CHAPTERS`（默认 50，归「合成质量」或独立组）。
- 腾讯云 COS 取值参考（决策 3）：`S3_ENDPOINT=https://cos.<region>.myqcloud.com`；`S3_REGION` 填地域（如 `ap-guangzhou`）；`S3_PATH_STYLE=false`；`S3_PUBLIC_BASE_URL` 可用 COS 默认访问域名（`https://<bucket>.cos.<region>.myqcloud.com`）或绑定的 CDN/自定义域名。桶权限需设为**公有读私有写**。
- 落地：加入 [routes.py#L1846](file:///workspace/backend/app/api/routes.py#L1846) `_EDITABLE_SETTINGS` 白名单的新分组「对象存储」，前端 `groupOrder` / `GROUP_META` 同步补组（参照 C-1 的做法）；密钥类字段复用 C-2 的 `SecretInput` 脱敏占位。

#### G-3 产物上传（仅交付物：MP3 / LRC / 分片 ZIP）· 决策 4
- 归档范围已定为**仅交付物**：章节 MP3、每章 LRC、分片 ZIP。**不上传**段级缓存、timings sidecar、preview 片段、导入的源 TXT、ICL 参考音频（后者含隐私/版权内容，且公有读桶会使其可被任意人获取）。
- 时机：① 章节 MP3 合成落盘后上传；② 该章 LRC 生成后上传；③ 每凑满 `ZIP_SHARD_CHAPTERS`（默认 50）章即打一卷并上传（配合 F-7）。
- key 规划（建议）：
  - 章节：`{S3_PREFIX}/{project_id}/{build_id}/ch{idx:04d}.mp3` 与 `ch{idx:04d}.lrc`
  - 分片 ZIP：`{S3_PREFIX}/{project_id}/{build_id}/第{start:03d}-{end:03d}章.zip`
- 实现提示：COS 的 S3 兼容接口可用 `put_object` / `upload_file`（大文件自动分片）；`S3_PUBLIC_BASE_URL` 用于拼公有读下载链接。
- ⚠️ **公有读意味着「URL 即权限」**：现有 `/media/*` 有归属校验（[main.py](file:///workspace/backend/app/main.py) 静态媒体归属校验）、`/media/sign` 有一次 token —— 换成公有读对象存储后，任何拿到 URL 的人都能下载。key 里的 `build_id` 是 uuid4 hex（不可猜测），据此可接受；「不做访问控制」按产品决策记录。
- ⚠️ **删除语义要同步**：`delete_build` 目前按 `audio_filename` 删本地文件（含 B-5 的「各 build 自有文件」不变式）；改为对象存储后需按 key 前缀（`.../{build_id}/`）批量删除，并注意对象存储的最终一致性（删除后短时间内可能仍可访问）。

#### G-4 下载直连对象存储
- 现状：单章/ZIP 走 `/api/media/sign` 签发一次性 URL → `/media/stream` 由**后端 FileResponse 转发**（消耗服务器带宽）。
- 改造后：`sign` 直接返回 `{S3_PUBLIC_BASE_URL}/{key}`（对象存储/CDN 出流量），后端不再转发字节 —— 这同时**顺带缓解 F-7 的下载体验**（CDN + Range/断点续传），也解除 `/media/stream` 的带宽瓶颈。
- 兼容：`local` 后端保持现有签名转发路径不变（两条路并存，用 `STORAGE_BACKEND` 分流）。

#### G-5 本地副本清理 · 决策 6（上传成功后清本地）
- **已定方案**：合成期保留本地暂存（`concat_mp3_files` / `mp3_duration_ms` / preview 截取都需要本地可读），**该章上传成功后即可清理本地 MP3 与 LRC**；分片 ZIP 在打包并上传成功后删除本地 ZIP。
- 需要处理的容错路径（否则会踩坏现有能力）：
  - **`retry_failed_build` 的章节复用**（B-5「各 build 自有文件」不变式）：复用的是**本地文件**。改为「上传即清」后，复用必须改为**回源下载对象**再另存，否则重试会因源文件已删而退化。
  - **`config_digest` 复用历史成功 build**：复用时不重新合成，但用户仍要能下载 → 下载链路必须能直接由对象存储提供（G-4），不能假设本地有文件。
  - **`delete_build`**：本地文件可能已不存在，删除要以对象存储为主、本地为容错（缺失不报错）。
  - **`/api/media/sign` + `/media/stream` 的本地兜底**：`local` 后端行为不变；`s3` 后端下若本地已清，签名接口需回落到直连对象存储 URL（即 G-4 成为**必需**而非可选）。
- 遗留：`data/audio/` 中历史产物的一次性上云迁移脚本，以及回滚路径（把 `STORAGE_BACKEND` 切回 `local` 时不得因对象已清而丢交付物）。

---

### 11.3 已确认的执行决策（2026-09-22）

| # | 决策点 | 结论 | 影响 |
|---|---|---|---|
| 决策 3 | 对象存储服务商 | **腾讯云 COS** | 用 COS S3 兼容协议接入（`endpoint` 形如 `https://cos.<region>.myqcloud.com`，`S3_PATH_STYLE=false`）；下载域名可用 COS 默认域名或绑定 CDN/自定义域名填入 `S3_PUBLIC_BASE_URL` |
| 决策 4 | 归档范围 | **仅交付物**（章节 MP3 / 每章 LRC / ZIP） | 段级缓存、timings sidecar、preview、源 TXT、ICL 参考音频**均不上传**；key 规划只需覆盖三类交付物 |
| 决策 5 | 整包 ZIP 去留 | **每 50 章一个独立 ZIP**（每卷自包含可单独解压） | 5000 章 → 100 个 ZIP，单卷 ≈450MB；峰值本地磁盘从 ≈90GB 降到 ≈「全量 MP3 + 单卷 ZIP」；卷大小做成可配置项（默认 50） |
| 决策 6 | 本地副本清理 | **上传成功后清本地** | 磁盘占用可控；代价是「重新打包/重试复用」需回源下载对象（需确认 G-5 的容错路径） |

---

### 11.4 建议执行批次

| 批次 | 内容 | 说明 |
|---|---|---|
| **批次 6** | F-1、F-2、F-3 | 三项都是小改动、风险低，直接决定 1000+ 章能否交付；F-3 含 checkpoint 重置 |
| **批次 7** | F-5、F-7 | 前端体验 + 打包可用性；F-7 已定「每 50 章一卷」（决策 5） |
| **批次 8** | G-1 ~ G-5 | 对象存储（腾讯云 COS，决策 3~6 已定） |
| **批次 9** | F-4、F-6、F-8 | 结构性优化：拆 `chapters_json`、合成流水线、LLM 并发 |

### 11.5 跟踪清单

> 完成一项把 `[ ]` 改为 `[x]`，并填写完成日期。

#### 批次 6 —— 规模化止血（P0）✅ 完成 2026-09-22
- [x] F-1 打包 LRC 去掉 O(N²)（一次加载 + 按章复用） — [build.py#L2242-L2249](file:///workspace/backend/app/services/build.py#L2242-L2249) / [subtitles.py#L237-L266](file:///workspace/backend/app/services/subtitles.py#L237-L266) — 完成 2026-09-22
- [x] F-2 `project_dialogues` 加 `(project_id, chapter_idx)` 索引 — [models.py#L237-L254](file:///workspace/backend/app/db/models.py#L237-L254) — 完成 2026-09-22
- [x] F-3 角色识别切片改为「按完整章节装桶」+ 重置旧 checkpoint — [project.py#L886-L889](file:///workspace/backend/app/services/project.py#L886-L889) — 完成 2026-09-22

#### 批次 7 —— 可用性与体验 ✅ 完成 2026-09-22
- [x] F-5 分批渲染 + 批量签发（**未做真虚拟滚动**，取舍见 §11.6） — [ProjectDetailPage.tsx](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L999-L1029) / [routes.py](file:///workspace/backend/app/api/routes.py#L1275-L1327) — 完成 2026-09-22
- [x] F-7 ZIP 改为「每 50 章一个独立 ZIP」（含前端分片下载列表、Build 多产物字段、delete_build 覆盖） — [build.py#L74-L165](file:///workspace/backend/app/services/build.py#L74-L165) — 完成 2026-09-22

#### 批次 8 —— 对象存储（腾讯云 COS）
- [ ] G-1 `StorageBackend` 抽象（local | s3，默认 local）
- [ ] G-2 设置页「对象存储」配置分组（COS 取值 + `ZIP_SHARD_CHAPTERS`）
- [ ] G-3 产物上传（章节 MP3 / 每章 LRC / 分片 ZIP）
- [ ] G-4 下载直连对象存储（`sign` 返回公有 URL）
- [ ] G-5 本地副本清理（含 retry 复用回源、digest 复用可下载、delete_build 容错）

#### 批次 9 —— 结构性优化
- [ ] F-4 拆分 `chapters_json`（单章/列表不再解析全书）
- [ ] F-6 合成改跨章流水线（贴近 RPM 上限）
- [ ] F-8 重估 `LLM_MAX_CONCURRENCY` 让批并发生效

---

### 11.6 实施记录

#### 批次 6（2026-09-22）

| 项 | 改动 | 测试 / 证据 |
|---|---|---|
| F-1 | [subtitles.py](file:///workspace/backend/app/services/subtitles.py)：把逐章渲染逻辑抽成 `_render_lrc(segs)`；新增**批量入口** `generate_chapters_lrc(build_id)`（只调一次 `_collect_build_segments`，再按 `chapter_idx` 分组渲染）；`_collect_build_segments` 在给定 `ch_idx` 时给对白查询加 `WHERE chapter_idx=?`（单章歌词不再拉全项目对白）。[build.py](file:///workspace/backend/app/services/build.py#L2236-L2253) 的 `_finalize` 改为调用批量入口，并把异常从「逐章静默忽略」改为**整体告警 + ZIP 不含 .lrc**（原来每章各自 try/except，失败完全无感） | 新增 [test_scale_batch6_red.py](file:///workspace/backend/tests/test_scale_batch6_red.py) 的 F-1 三项：批量入口 `_collect_build_segments` **只被调用 1 次**（旧实现是 N 次）、无内容章不出现在结果里、单章接口仍透传 `ch_idx` |
| F-2 | [models.py](file:///workspace/backend/app/db/models.py#L237-L254)：`ProjectDialogue` 加 `Index("ix_project_dialogues_project_chapter", "project_id", "chapter_idx")`。[session.py](file:///workspace/backend/app/db/session.py)：新增 `_NEW_INDEXES` 并在 `_migrate_existing_sync` 里用 `CREATE INDEX IF NOT EXISTS` 补建 —— **`create_all` 不会给已存在的表补索引**，不补的话老库永远享受不到 | F-2 三项：模型确实声明该复合索引、迁移 DDL 幂等且指向该表、**老库场景**（跑完 init_db 后 DROP 掉索引再跑一次）索引被重新补建 |
| F-3 | [project.py](file:///workspace/backend/app/services/project.py)：新增纯函数 `_bucket_chapters_by_chars(chapters, max_chars)` —— 按**完整章节**贪心装桶，单章超限时独占一桶（绝不切章）；桶文本用 `\n` 连接，`start/end` 与 `full_text = "\n".join(...)` 的区间对齐。`char_current_slice` 的 `start/end` 改为取桶的真实区间（旧代码按 `slice_idx * char_slice_size` 推算，装桶后桶长不等会算错），并补 `chapters_n` / `chapter_range` 便于排查。新增 `_CHAR_SLICE_MODE="chapter"` 与 `_char_checkpoint_incompatible(prog)`：**旧口径 checkpoint 一律重置**，防止续跑把「没跑过的桶」当「已跑过的片」跳过而静默漏识 | F-3 七项：不切章（桶内等于原文、每章只属一桶、不重不漏）、不超限且贪心装满、超限章独占一桶且原文不变、`start/end` 与 full_text 区间一致、空章节列表返回空、旧 checkpoint 判为不兼容（三种旧形态）、新口径与全新项目判为兼容 |

**同批顺带修正（同一函数内的既有缺陷）**
- [subtitles.py](file:///workspace/backend/app/services/subtitles.py#L184-L193)：跳过失败章的判据原来只查 `audio_filename`/`duration_ms`，但失败章也有占位 MP3（`duration_ms=1000`）→ **注释写着「失败章跳过」而代码并不跳过**。结果是给 1 秒静音占位音频用估算回退编出一整篇时间轴全错的 LRC 并打进 ZIP。现改为按 `art.status == "done"` 判定。

**回归结果**
- 新增 [test_scale_batch6_red.py](file:///workspace/backend/tests/test_scale_batch6_red.py)（14 用例）全绿；连同既有歌词用例 `20 passed`
- 全量后端：`396 passed, 2 failed, 1 skipped`（改前基线为 `380 passed, 4 failed`）—— **失败数下降**，且出现的失败项在基线上同样出现，属既有的跨用例隔离抖动（E-1），非本批引入
- `import backend.app.main` ✅；本批未改前端，故未跑 `tsc`

**基线取证（确认无新增失败）**
用 `git stash` 把本批 5 个后端文件还原后跑全量：`4 failed`，其中**包含**本次全量跑出的 `test_batch2_lifecycle_red.py::test_b5_retry_reused_chapter_has_own_file` 与 `test_project_e2e.py::test_project_full_lifecycle`（另两项为 `test_project_prepare_voice_pool_red` / `test_review_fixes_red`）。两者单独跑均通过 → 顺序相关的用例污染，非本批引入。

**发现但未处理（留待确认）**
- `project.py::_split_50k_and_run_chars_serial` 是**死代码**（全仓库仅定义、无调用点），且它实现的正是被 F-3 废止的「按字符偏移硬切」。本批只在 docstring 上加了「已废弃、勿复用」标注，**未删除**（删除属 E-6 类清理，不在本批范围）。建议后续删除或改造为调用 `_bucket_chapters_by_chars`。
- 本批只解决「打包/识别的算法复杂度」，**不改变量级瓶颈**：5000 章的 prepare 仍受 `LLM_MAX_CONCURRENCY=1` 串行限制（F-8），合成仍受 `DOUBAO_TTS_RPM_LIMIT=60` 限制（F-6），前端仍会逐章签发（F-5）。

#### 批次 7 之 F-7（2026-09-22）

| 项 | 改动 | 测试 / 证据 |
|---|---|---|
| 配置 | `ZIP_SHARD_CHAPTERS`（默认 50，<=0 表示不分片）新增于 [config.py](file:///workspace/backend/app/core/config.py#L256-L260)，并入设置白名单「合成质量」分组（[routes.py](file:///workspace/backend/app/api/routes.py#L1919)） | — |
| DB | `Build.zip_filenames_json`（TEXT，`[{"filename","start","end","size_bytes"}]`），`zip_filename` 保留为**第一个分片**以兼容旧代码路径；老库经 `_BUILD_NEW_COLUMNS` 自动 ADD COLUMN | 见下 T-ZS4 的老库兜底用例 |
| 打包 | [build.py](file:///workspace/backend/app/services/build.py)：`_build_book_zip` 增加 `start/end`（只打包该区间，**章节序号仍按全书统一编号**，任一卷可独立解压）；新增 `_zip_shard_size` / `_zip_shard_ranges` / `_zip_shard_filename` / `_parse_zip_shards`；`_finalize` 逐片打包并累计清单 | T-ZS1 区间切分（整除/余数/不分片/空书）；T-ZS2 命名（全量覆盖沿用 `_all.zip`，分片带 `ch0001-0050`）；T-ZS3 **分片 ZIP 只含本区间内容**且序号为 003/004/005 |
| 下载 | `/media/sign?kind=all_zip&idx=N` 用 `idx` 选分片（复用既有 `chapter_idx` 字段，未新增 kind）；`/media/stream` 按分片解析并把下载名带上章节区间（`…_第051-060章.zip`）；`/download-all?shard=N` 同步支持 | T-ZS6 分别取 idx=0/1 并断言 stream 返回的是对应分片的字节；T-ZS7 `?shard=1` 返回第 1 片且文件名含区间 |
| 清理 | `delete_build` 删除**全部分片**；`delete_project` 把所有 build 的 `zip_filename` + `zip_filenames_json` 里的文件名合并去重后一起删 | T-ZS4 兜底：老库（只有 `zip_filename`）也能被 `_parse_zip_shards` 覆盖到 |
| 前端 | [api.ts](file:///workspace/frontend/src/lib/api.ts)：新增 `ZipShard` 类型、`BuildDetailResp.zip_shards`、`buildDownloadAll(..., shard=0)`；[ProjectDetailPage.tsx](file:///workspace/frontend/src/components/ProjectDetailPage.tsx)：单包时保持原「下载全部 ZIP」按钮，多片时改为「ZIP 共 N 卷」+ 逐卷下载列表（带章节区间与体积） | `npx tsc --noEmit` ✅ |

**兼容性说明**
- 老 build 的 `zip_filenames_json` 为 NULL → `_parse_zip_shards` 用 `zip_filename` 合成「单条覆盖全书」→ 前端 `zip_shards` 长度为 1 → 仍显示原按钮，行为与改造前完全一致。
- 章节数 ≤ 阈值时文件名仍是 `build_<id>_all.zip`，历史链接/脚本不受影响。
- 已生成的旧单包不会自动拆分（无需迁移）。

**回归结果**
- 新增 [test_zip_shard_red.py](file:///workspace/backend/tests/test_zip_shard_red.py)（9 用例）全绿
- 全量后端：`403 passed, 4 failed, 1 skipped`；4 项失败与基线**完全一致**（`test_b5_retry_reused_chapter_has_own_file` / `test_project_e2e` / `test_project_prepare_voice_pool_red` / `test_review_fixes_red`），均属既有 E-1 类跨用例污染，**非本批引入**
- 前端 `npx tsc --noEmit` exit=0

**踩坑记录（对后续批次有用）**
- 本批的 F-7 路由用例在**单跑时通过、全量跑时 404**。根因是 E-1：`test_path_env_override_red.py` 会 `importlib.reload(core.config)`，而 `routes.py` 在更早 import 时已绑定**旧的 `settings` 对象**，于是 conftest 打在「新对象」上的 `AUDIO_DIR` 对路由不可见。修法沿用仓库既有约定（见 `test_voice_instruction_red.py` / `test_tts_model_switch_red.py` 的注释）：**patch 路由模块自己绑定的那个 `settings`**。

**未做**
- F-5（批量签发 + 虚拟滚动）尚未开始，留作下一步。

#### 批次 7 之 F-5（2026-09-22）

| 项 | 改动 | 测试 / 证据 |
|---|---|---|
| 批量签发接口 | [routes.py](file:///workspace/backend/app/api/routes.py#L1275-L1327) 新增 `GET /projects/{pid}/builds/{bid}/chapter-signs?start&count&ttl_seconds`：一次为「一段章节」签发媒体 token，只返回**有音频产物**的章节；`count` 夹到 1~200（防一次签发过多） | T-BS1 一次请求返回区间内全部章节的 URL；T-BS2 返回的 URL 直接喂 `/media/stream` 能取到该章字节；T-BS3 `count=100000` 被夹到 200；T-BS4 无音频产物的章节不出现在 items；T-BS5 非归属项目 → 403/404 |
| 前端 API | [api.ts](file:///workspace/frontend/src/lib/api.ts#L518-L531) 新增 `buildChapterSigns(projectId, buildId, start, count) → Record<chapterIdx, url>` | `tsc --noEmit` ✅ |
| 章节列表 | [ProjectDetailPage.tsx](file:///workspace/frontend/src/components/ProjectDetailPage.tsx#L999-L1029)：改为**分批渲染**（每批 60 条，底部「加载更多章节（还有 N 章）」）+ **批量签发**（每次 120 章一块，`signedUntilRef` 只签新区间；某块失败则 `break` 并保留进度，下次展开/加载更多时重试） | 单测覆盖后端契约；前端为类型检查 + 静态审阅 |
| 构建产物列表 | 同一文件 `BuildDetailContent` 采用相同策略（`ARTIFACTS_PAGE_SIZE=60`） | 同上 |

**方案取舍：为什么是「分批渲染」而不是「真虚拟滚动」**
- 列表行内含 `WaveformPlayer`，展开态还会插入逐行文本标注 → **行高可变**，真虚拟滚动需要动态测量 + 占位补偿，容易出现视觉抖动与滚动跳变，改动面与回归风险显著更高。
- 分批渲染把「初始渲染 5000 个节点 + 5000 次串行签发」降到「60 个节点 + 1 次批量请求」，已消除本项要解决的两个瓶颈（DOM 数与请求数）。
- 代价：滚动到底需要点一下「加载更多」。若将来确实需要无缝滚动，可在此基础上再叠虚拟化。

**已知限制（如实记录）**
- 签名 TTL 仍为 5 分钟；长时间停留后过期由 `WaveformPlayer.onNeedNewSrc` 做**单章**重签兜底（既有机制，未改动）。
- `signedUntilRef` 记录的是「已签发到的下标」；若某块签发失败会中断后续并在下次交互时重试，但**不会自动重试**（没有定时器）。

**回归结果**
- 新增 [test_batch_sign_red.py](file:///workspace/backend/tests/test_batch_sign_red.py)（5 用例）全绿
- 全量后端：`410 passed, 2 failed, 1 skipped`；2 项失败均在基线（`4 failed`）的子集内，属既有 E-1 类跨用例污染，**非本批引入**
- 前端 `tsc --noEmit` exit=0

**环境备注（沙箱）**：本轮开始前测试依赖与 `node_modules` 均被重置；已重装 `requirements*.txt` 与前端依赖。`npm install` 默认跳过 devDependencies（`NODE_ENV=production` 环境），导致 `tsc` 被解析到全局新版并报 `baseUrl has been removed`；需 `NODE_ENV=development npm install --include=dev` 才能拿到 `package.json` 锁定的 typescript 5.5.3。




