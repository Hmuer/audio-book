# 任务拆解：豆包语音全家桶集成

> 关联规格：`.trae/specs/doubao-voice-suite/spec.md`
> 说明：每个 Task 对应 spec 中一组 FR 与 AC，并明确本地测试要求（rule/rubric）。按 P0→P1→P2 顺序推进。

---

## 总体依赖图（DAG 简述）

```
Task 1 (配置+基类+DB迁移) ──▶ Task 2 (Factory + Registry)
                                    │
             ┌──────────────────────┼───────────────────────┐
             ▼                      ▼                       ▼
         Task 3              Task 5 (ICL)             Task 7 (Multicast)
      DoubaoTTS 2.0         训练表+接口               DoubaoMulticast
             │                      │                       │
             ▼                      ▼                       ▼
         Task 4              Task 6 (ICL UI)          Task 8 (Build 严格模式)
     /api/voices 合并 +    我的音色页面 +          start_build mode/tts_provider +
     后端单测/集成测         VoicePicker ICL Tab     幂等 digest 扩展 + 严格失败
                                    │
                                    ▼
                            Task 9 (VoicePicker 多维筛选)
                                    │
                                    ▼
                       Task 10 (项目构建面板模式选择)
                                    │
                                    ▼
                       Task 11 (端到端 + AC Rubric 验收)
```

---

## Task 1：配置扩展 + 抽象层扩展 + 数据库迁移
- **优先级**：high（P0）
- **对应 FR**：FR-1、FR-17、FR-18、FR-19
- **前置依赖**：无
- **工作内容**：
  1. `backend/app/core/config.py` 新增 `TTS_PROVIDER`、`DOUBAO_AK / DOUBAO_SK / DOUBAO_APP_ID`、`DOUBAO_*_BASE_URL`、`DOUBAO_*_RPM_LIMIT`、`MULTICAST_STRICT_MODE=True`。所有新增字段提供合理默认（AK/SK 为空字符串，不影响未配置环境启动）。
  2. `backend/app/ai/base.py` 的 `BaseTTSProvider`：新增 `provider: str` 抽象字段；`synthesize_to_bytes` 新增可选 `instruction_text: str | None` 与 `speaker_style: str | None`。
  3. `backend/app/db/models.py`：
     - `Project` 表新增 `default_tts_provider: str | None`、`default_build_mode: str | None`。
     - `Build` 表新增 `mode: str`、`tts_provider: str`。
     - 新增 `IclTrainingTask` 表（字段见 spec FR-8）。
  4. 编写 Alembic 迁移脚本（或使用 `db.session` 启动时自动 `CREATE TABLE IF NOT EXISTS`，若项目当前无 Alembic 则走后者，通过 `Base.metadata.create_all` + 显式 `ALTER TABLE ADD COLUMN` 幂等升级；后续视项目习惯选择）。
- **测试要求（TR）**：
  - T-TR1 (rule)：未设置 `DOUBAO_AK` 时 settings 校验通过，`uvicorn` 启动不报 ValidationError → 证据：`python -c "from app.core.config import settings; print(settings.model_dump())"` 无异常。
  - T-TR2 (rule)：`Project.mode/tts_provider`、`Build.mode/tts_provider`、`IclTrainingTask` 三对象实例化 + `session.add/commit` 不抛 → 证据：`tests/test_models_expand.py` 通过。
  - T-TR3 (rule)：升级脚本在已有 DB（含存量 projects/builds）再次执行不破坏数据，且旧表 `mode` 字段为 `classic` 后向兼容值 → 证据：pytest fixture 使用 sqlite 现有数据再跑升级。
- **状态**：pending
- **完成证据**：N/A

---

## Task 2：Factory 多厂商路由 + Registry
- **优先级**：high（P0）
- **对应 FR**：FR-2、FR-3、FR-7 限流雏形
- **前置依赖**：Task 1
- **工作内容**：
  1. `backend/app/ai/factory.py` 新增 `TTSRegistry`：`{prefix: factory_fn}`。
  2. `get_tts(provider)`：默认读 `settings.TTS_PROVIDER`；`get_tts_by_voice_id(vid)`：根据前缀返回正确 provider 实例（用于 FR-20 校验 + 段级路由）。
  3. 全局 TTS 并发信号量保持现有；新增 `_DOUBAO_TTS_RPM` 限流桶（参考 minimax `_rpm_wait_acquire` 实现，保持严格匀速）。
- **测试要求（TR）**：
  - T-TR1 (rule)：`get_tts('minimax').name == 'minimax'`；`get_tts('doubao').name == 'doubao'`（未配置 AK 时 Doubao 实例可创建但 synthesize 抛「未配置凭据」异常）。
  - T-TR2 (rule)：`get_tts_by_voice_id('doubao:xxx')` 返回 Doubao 实例；`get_tts_by_voice_id('minimax:xxx')` 返回 MiniMax；`get_tts_by_voice_id('icl:xxx')` 返回 Doubao（icl 分支）。
- **状态**：pending
- **完成证据**：N/A

---

## Task 3：实现 DoubaoTTSProvider（TTS 2.0 合成 + 动态音色列表）
- **优先级**：high（P0）
- **对应 FR**：FR-4、FR-5、FR-6、FR-7
- **前置依赖**：Task 2
- **工作内容**：
  1. 新建目录 `backend/app/ai/providers/doubao/`：
     - `__init__.py`
     - `auth.py`：火山签名或豆包直连 Bearer 鉴权封装（凭据读 settings）。
     - `tts.py`：`DoubaoTTSProvider` 类。
  2. `list_voices()`：调用豆包官方「查询音色列表」接口；结果本地 10 分钟 TTL 缓存（`_voices_ttl: float` + `_voices_cache`）；每条返回 dict 中含 `provider='doubao'`，`id` 统一加 `doubao:` 前缀。
  3. `synthesize_to_bytes()`：封装 tts 接口，支持 `speed/emotion/instruction_text/speaker_style`，返回 MP3 + 真实 duration_ms；RPM 限流走 Task 2 桶；指数退避重试最多 4 次，RateLimited 单独按 `DOUBAO_TTS_RPM_LIMIT` 等桶；最终失败 `raise RuntimeError`。
  4. 将 `make_silent_mp3 / concat_mp3_files / _estimate_mp3_duration_ms` 从 minimax 目录抽取到 `app/ai/providers/_utils.py` 公共位置，Minimax 保持引用，Doubao 复用。
- **测试要求（TR）**：
  - T-TR1 (rule)：`list_voices()` 首次请求触发 HTTP（mock），结果 ≥ 28 条；10 分钟内第二次请求不触发 HTTP（通过 httpx call count 断言）。
  - T-TR2 (rule)：`synthesize_to_bytes("你好", "doubao:xxx")` mock 合法响应 → 返回 bytes 首四字节 `0xfffb...`，dur_ms 在正整数范围。
  - T-TR3 (rule)：mock 返回 429/`RateLimited` 时，限流桶等待后重试，最终成功或在 N 次后抛异常且日志中包含 trace_id。
- **状态**：pending
- **完成证据**：N/A

---

## Task 4：/api/voices 合并列表 + 命名空间校验
- **优先级**：high（P0）
- **对应 FR**：FR-11、FR-20、FR-25
- **前置依赖**：Task 3
- **工作内容**：
  1. `backend/app/api/routes.py` `GET /api/voices` 聚合：
     - minimax（必然存在）
     - doubao（若凭据配置）
     - icl（按当前登录 user 过滤 `status=4` 的任务）
     - 支持 `?limit=&offset=&provider=&dialect=&age_group=` 分页/筛选参数。
  2. `start_build` 入参校验：若 `voice_assignments.values()` 与所选 `tts_provider` 命名空间冲突 → HTTP 400 并明确指出冲突的角色名 + 音色 ID（FR-20）。
- **测试要求（TR）**：
  - T-TR1 (rule)：`GET /api/voices` 未配置豆包时至少返回 minimax 条目且每条都含 `provider` 字段。
  - T-TR2 (rule)：`voice_assignments = {'张三':'doubao:female-qingxin'}` 且 `tts_provider='minimax'` → HTTP 400，message 含「张三」「命名空间冲突」。
- **状态**：pending
- **完成证据**：N/A

---

## Task 5：ICL 声音复刻后端（训练表、接口、合成路由）
- **优先级**：medium（P1）
- **对应 FR**：FR-8、FR-9、FR-10
- **前置依赖**：Task 3（Doubao 基础接口能力）
- **工作内容**：
  1. `backend/app/ai/providers/doubao/icl.py`：
     - `submit_training(audio_bytes, voice_name) -> str`（提交到豆包，返回 doubao_task_id）。
     - `poll_training(doubao_task_id) -> (status, progress, cloned_voice_id, error_msg)`。
     - `delete_training(doubao_task_id)`。
  2. 后台轮询 worker（可复用 Build 的 `asyncio.create_task` 模式或新增全局 ICL poller，30s 拉取一次 status∈{0,1} 的任务）。
  3. REST 路由（`/api/icl/*` 实现 FR-9，严格按 user_id 过滤）。
  4. 合成路由：`DoubaoTTSProvider` 识别 `icl:` 前缀 → 等价 `doubao:` 走合成，但使用 `cloned_voice_id` 作为请求参数中的 `voice_id`（豆包接口一致）。
- **测试要求（TR）**：
  - T-TR1 (rule)：`POST /api/icl/tasks` 上传 >10MB 文件 → 400；<3s 音频 → 400；3–6s → 201 + task_id。
  - T-TR2 (rule)：mock poll 训练完成后，`GET /api/icl/voices` 包含该 voice_id 且 `provider='icl'`。
  - T-TR3 (rule)：用户 B 的 token 请求用户 A 的 task_id 详情 → 404/403（隔离）。
- **状态**：pending
- **完成证据**：N/A

---

## Task 6：前端 ICL 音色管理页面
- **优先级**：medium（P1）
- **对应 FR**：FR-24
- **前置依赖**：Task 5 后端接口就绪
- **工作内容**：
  1. `frontend/src/lib/api.ts` 新增 `IclTask`/`IclVoice` 类型及接口（create/list/get/delete/list_voices）。
  2. `SettingsPage.tsx` 新增 Tab 「我的音色」：
     - 列表（名称、状态徽章、创建时间、操作）；
     - 「训练新音色」对话框：文件上传（限制 ≤10MB、格式 wav/mp3、长度校验 3–6s 通过 `new Audio().duration`），名称输入，提交；
     - 状态轮询：每 30s 刷新；success 后自动弹 Toast 「新音色训练完成」。
  3. 音色试听按钮复用已有 `WaveformPlayer` 或现有 `POST /api/tts/preview`。
- **测试要求（TR）**：
  - T-TR1 (rule)：上传 2s 音频 → 前端即时提示「时长不足 3s」，不发送请求。
  - T-TR2 (rule)：训练成功后，回到项目详情页 VoicePicker「我的音色」Tab 立即可见新音色。
- **状态**：pending
- **完成证据**：N/A

---

## Task 7：Seed-Audio 多播剧 Provider + Prompt 构造
- **优先级**：medium（P1）
- **对应 FR**：FR-12、FR-13、FR-14
- **前置依赖**：Task 3
- **工作内容**：
  1. `backend/app/ai/providers/doubao/multicast.py`：
     - `DoubaoMulticastProvider.synthesize_prompt_to_bytes(prompt, *, length_secs=120) -> (bytes, dur_ms)`；调用 Seed-Audio 1.0 文本生成接口。
     - `synthesize_chapter_multicast(chapter, dialogues[], characters[], opts)`：
       - 通过 LLM（现有 `get_llm()`）把章节文本+角色人设转为 Seed-Audio SCRIPT Prompt（包含多角色 `<speaker>X</speaker>`、情绪标签 `[愤怒]`、环境音效 `[SFX:xxx]`、BGM 提示 `[BGM:xxx]`）。
       - 按预估字数分块（目标每块 ≤ 100s 输出，预留 20s 缓冲）；分块边界遵循对白段 anchor，不硬切。
       - 并行调用每块后用 `concat_mp3_files` 合并，返回整章 bytes+dur。
- **测试要求（TR）**：
  - T-TR1 (rule)：长章（2000 字+）被拆成 ≥2 段且每段 SCRIPT Prompt 包含 speaker 标签。
  - T-TR2 (rule)：合成失败时 provider 抛异常，不返回 bytes。
  - T-TR3 (rule)：SCRIPT 生成（LLM 调用）失败抛出显式异常 `MulticastScriptBuildError`。
- **状态**：pending
- **完成证据**：N/A

---

## Task 8：Build 管线扩展（mode/tts_provider/严格模式/幂等 digest）
- **优先级**：high（P0）
- **对应 FR**：FR-13、FR-15、FR-16、FR-19、FR-26、FR-27
- **前置依赖**：Task 1、Task 7
- **工作内容**：
  1. `StartBuildRequest` 新增 `mode?`、`tts_provider?`；`start_build` 签名扩展，两字段持久化到 Build。
  2. `_calc_config_digest` 追加输入：`mode + tts_provider`。
  3. `_run_build_inner` 新增分支：
     - `mode=classic`：现有路径（段级缓存 + 失败占位 1s）。
     - `mode=multicast`：
       - 跳过 `_build_segments_for_chapter`；
       - 每章调用 `DoubaoMulticastProvider.synthesize_chapter_multicast(...)`；
       - 任何异常直接 raise → worker 外层 catch：`status=failed`，不写占位 MP3，不打包 ZIP（ZIP 仅在 `success/partial_success` 时）。
  4. `_ensure_default_narrator` 在 multicast 模式下改为选默认 narrator 但不强制使用（multicast 用 Seed-Audio 多角色，不用 narrator），但 Build 表仍需填充默认值。
  5. `BuildResp/BuildDetailResp` 新增 `mode`/`tts_provider` 字段；`retry-failed` 路径把 mode/tts_provider 也原样继承。
- **测试要求（TR）**：
  - T-TR1 (rule)：mock Seed-Audio 合成异常 → `Build.status == 'failed'` 且 `failed_chapters` 对应章有 error_msg，音频目录不存在该章 mp3 文件（AC-5）。
  - T-TR2 (rule)：两 Build 仅 mode 不同，其他配置完全相同 → `config_digest` 不同（AC-6）。
  - T-TR3 (rule)：`MULTICAST_STRICT_MODE=False` 环境下，失败允许写 partial_success（与 strict 模式对照验证 AC-8）。
  - T-TR4 (rule)：start_build 返回 BuildResp 中 `mode/tts_provider` 与请求一致；retry-failed 新 build 继承原两字段。
- **状态**：pending
- **完成证据**：N/A

---

## Task 9：VoicePicker 多维度筛选 + Tab 分组
- **优先级**：medium（P1）
- **对应 FR**：FR-21、FR-22、AC-9
- **前置依赖**：Task 4 `/api/voices` 接口扩展
- **工作内容**：
  1. `Voice` 接口类型新增字段（FR-21）。
  2. `VoicePicker`：
     - 顶部一级 Tab：`全部 / MiniMax / 豆包官方 / 我的音色(ICL)`，默认「全部」。
     - 下方 chips 横向筛选：性别/方言/年龄段/场景，支持多选 + 清空。
     - 搜索框搜索范围扩展到 dialect/scene/role_tags。
     - 分组显示：按 `性别` 分组；非普通话方言行尾加方言小徽章。
- **测试要求（TR）**：
  - T-TR1 (rubric)：筛「豆包+女声+青年+粤语」结果集非空且交互 3 步内可达（AC-9 评分 ≥ 2）。
  - T-TR2 (rule)：搜索 `粤语` 能命中方言字段的语音条目。
- **状态**：pending
- **完成证据**：N/A

---

## Task 10：项目构建面板 — TTS 厂商 + 构建模式选择
- **优先级**：medium（P1）
- **对应 FR**：FR-23、FR-26、FR-27
- **前置依赖**：Task 8 后端接口就绪 + Task 9 VoicePicker 扩展
- **工作内容**：
  1. `ProjectDetailPage.tsx`「构建有声书」面板：
     - TTS 厂商 Select（MiniMax / 豆包），默认读 `project.default_tts_provider`。
     - 构建模式 Select：`经典（旁白+对白拼接）` / `多播剧（Seed-Audio 一体化）`。
     - 选多播剧后显示红色 Banner：「多播剧模式为严格失败模式：任何章节生成失败将中止整本书构建，不生成占位静音。请确认所有角色声线为豆包厂商音色后再开始。」
  2. 触发 `POST /projects/:id/builds` 时带上 `tts_provider` 与 `mode`。
  3. Build 历史列表新增两列（徽标或 chip）：`mode`（经典/多播剧）与 `provider`。
- **测试要求（TR）**：
  - T-TR1 (rule)：选「多播剧」+ provider=「豆包」→ 请求 body 带 `mode=multicast, tts_provider=doubao`。
  - T-TR2 (rule)：切换到多播剧后 Banner 文案出现且不消失直到切回经典。
- **状态**：pending
- **完成证据**：N/A

---

## Task 11：端到端与 Rubric 验收
- **优先级**：low（P2，但必须做完才能 Review 通过）
- **对应 AC**：AC-9、AC-10、AC-11
- **前置依赖**：Task 6 + Task 10 完成
- **工作内容**：
  1. 编写 1 条 e2e pytest：上传小说 → prepare（或 mock `ProjectDialogue`/`ProjectCharacter` 数据）→ `classic` build 走 minimax 成功 → `multicast` build 切换到豆包：
     - 失败情形：mock Seed-Audio 抛错 → 断言 failed + 无占位。
     - 成功情形：mock Seed-Audio 正常 → 断言 success + ZIP 完整章节数。
  2. 录制 3 次真实豆包环境合成样例（1 段 ICL 训练、1 段经典 TTS、1 段多播剧）产出报告（含 MP3 下载链接或嵌入 `data/audio/` 下），由人工评审 AC-10（听感）。
  3. 录制验收短视频/截图：VoicePicker 多维筛选交互（AC-9）、我的音色训练完整流程（AC-11）。
- **测试要求（TR）**：
  - T-TR1 (rule)：e2e 测试失败场景无占位 MP3。
  - T-TR2 (rubric)：听感评审 AC-10 评分 ≥ 1（有轻微瑕疵可接受，发行级=2）。
  - T-TR3 (rubric)：ICL 训练流程录屏验收 AC-11。
- **状态**：pending
- **完成证据**：N/A

---

## 取消/延期
- 所有任务默认不取消；如需取消，需用户显式批准并在对应 Task 下写「原因 + 批准证据」。
