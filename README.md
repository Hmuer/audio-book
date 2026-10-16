# AI 有声小说生成器 (Novel TTS App)

**LLM 优先** 架构的中文有声小说一键生成器。输入小说文本 → LLM 纠错 / 识别角色 / 标对白 / 分章 / 推音色 → 多音色 TTS → MP3。

一条命令启动，单进程、单端口、默认只监听 127.0.0.1。

---

## 环境要求

| 组件 | 最低版本 |
|---|---|
| Python | **3.11+**（推荐 3.11 / 3.12，本地用 pyenv 安装最稳） |
| Node.js | **18+**（只用于构建前端静态产物，运行时无 Node 进程） |
| OS | macOS / Linux（Windows 可用 WSL2） |
| AI 服务 | 至少配置可用的 LLM 与 TTS 厂商凭据（MiniMax / 豆包 Doubao / OpenAI 兼容端点） |

---

## 启动步骤

### 1. 一键启动

```bash
cd audio-book
./start.sh
```

首次运行 `start.sh` 会自动：
- 从 `.env.example` 复制 `$NOVEL_TTS_HOME/.env`（默认 `~/.novel-tts/.env`；记得填 API Key；至少一个 LLM + 一个 TTS）
- 创建 `$NOVEL_TTS_HOME/.venv`（Python 3.11+）并装依赖
- 安装前端依赖并 `npm run build`（Next.js static export → `frontend/out/`）
- 最后启动 **单进程 uvicorn**（1 worker）
- 把 PID 写到 `$NOVEL_TTS_HOME/data/uvicorn.pid`；日志在 `$NOVEL_TTS_HOME/data/logs/`

> 更新已有部署请运行 `./deploy.sh`。`start.sh` 仅在前端产物不存在时自动构建；`deploy.sh` 会在拉取代码后重建前端，再重启服务。详见下文「升级与数据迁移」。

### 2. 打开浏览器

访问 **http://127.0.0.1:28000/**

- `/api/health` — 健康检查（无需登录）
- `/docs` — FastAPI Swagger 文档
- `/api/voices` — 当前激活厂商的内置音色 JSON
- `/media/<filename>` — 章节 MP3、ZIP、时间轴与预告片访问（需登录；Build 文件只允许所属项目的 owner 或 admin，音色试听只允许创建者或 admin）
- `/api/auth/login` — 默认账号 `admin / admin`（**首次登录后必须改密**）

### 3. 跑测试

```bash
cd audio-book
VENV_PY="${NOVEL_TTS_HOME:-$HOME/.novel-tts}/.venv/bin/python"
"$VENV_PY" -m pip install -r backend/requirements-dev.txt
"$VENV_PY" -m pytest backend/tests -q
```

测试覆盖：鉴权 / 资源归属 / 媒体鉴权（一次性签名 URL）/ 后台任务持久化（启动恢复+看门狗）/ 速率限制（login/prepare/build）/ 配置持久化（providers + settings 重启回填）/ 厂商 API 脱敏 / 上传限制 / Build 取消竞态 / 修复合重 等。

CI / 依赖：
- 依赖锁定见 `backend/requirements.lock` 与 `requirements-dev.txt`；
- Dependabot 配置 `.github/dependabot.yml`（每周一自动开 PR）；
- CI 流水线 `.github/workflows/ci.yml`（lint + pytest + pip-audit + 前端构建）。

---

## `.env` 配置说明

`start.sh` 首次启动时会把 `.env.example` 复制到 `$NOVEL_TTS_HOME/.env`（默认 `~/.novel-tts/.env`）。也可以在启动前手动创建：

```bash
mkdir -p "${NOVEL_TTS_HOME:-$HOME/.novel-tts}"
cp .env.example "${NOVEL_TTS_HOME:-$HOME/.novel-tts}/.env"
```

### 必填 / 推荐字段

| 变量 | 必须 | 默认 | 说明 |
|---|---|---|---|
| `JWT_SECRET` | ✅（prod） | `change-me-...` | JWT 签名密钥。**生产环境必须改成 ≥32 字符随机串**，否则启动直接失败 |
| `SEED_ADMIN_USER` | | `admin` | 首次启动自动创建的默认管理员用户名 |
| `SEED_ADMIN_PASS` | | `admin` | 默认管理员密码。**生产环境必须改成强密码**，否则启动直接失败 |
| `ENV` | | `dev` | `prod` 时启动会强制校验 `JWT_SECRET` / 默认密码 / `DISABLE_AUTH` |
| `DISABLE_AUTH` | | `false` | **生产环境严禁 `true`**，否则启动直接失败 |
| `BIND_HOST` | | `127.0.0.1` | 默认只监听本机；需要从其他设备访问时再按部署环境调整 |
| `PORT` | | `28000` | 监听端口 |
| `NOVEL_TTS_HOME` | | `~/.novel-tts` | `start.sh` 使用的运行时根目录，可在启动前通过环境变量覆盖 |
| `DATA_DIR` | | `$NOVEL_TTS_HOME/data` | `start.sh` 启动时设置的数据库和上传文件根目录 |
| `AUDIO_DIR` | | `$NOVEL_TTS_HOME/data/audio` | `start.sh` 启动时设置的 MP3 / ZIP 目录 |
| `DATABASE_URL` | | 指向 `$NOVEL_TTS_HOME/data/app.db` | `start.sh` 启动时设置的 SQLite URL |

### 多厂商配置

设置页支持 **MiniMax（LLM 与 TTS）**、**豆包 TTS**、**OpenAI 兼容 LLM** 与自定义 LLM 端点。`PROVIDERS_CONFIG` 保存厂商信息，`ACTIVE_TTS_PROVIDER` / `ACTIVE_TTS_MODEL` / `ACTIVE_LLM_PROVIDER` / `ACTIVE_LLM_MODEL` 指向激活的模型。旧的 `LLM_BASE_URL` / `LLM_MODEL_PRO` / `LLM_MODEL_FAST` 字段仅用于迁移，设置页不展示。

具体厂商卡片配置请在「设置」页按厂商填写 API Key（仅管理员可见明文；其他用户只能看到脱敏后的 `***LAST4`）。

### TTS 厂商与音色

在「设置 → 模型厂商」启用豆包或 MiniMax，并在「激活 TTS」选定厂商及模型。MiniMax 可选 `speech-2.8-hd`（音质优先）或 `speech-2.8-turbo`（速度和成本优先），使用 MiniMax API Key；中文语音接口默认使用官方 `https://api.minimax.cn/v1`。豆包使用官方 `seed-tts-2.0` 系统音色。音色库、试听、角色推荐、全局旁白、构建与重试均限定为当前激活厂商的官方音色。MiniMax 音色从官方 `/v1/get_voice` 的 `system` 分类读取中文系统音色；接口暂不可用时使用仓库附带的官方中文清单。项目不提供上传或复刻音色。

「启动新构建」弹窗为**本次构建**选择默认男声和女声，未选时分别取当前厂商列表中第一条相应性别音色；若与旁白相同，则取下一条。默认音色不能与旁白相同，男女默认音色可以相同。未分配音色的角色与未识别归属的对白按角色性别使用默认男声或女声，未知性别使用默认男声。这两个默认音色写入 Build 快照，重试继承，不写入项目全局设置。切换 TTS 厂商后，旧角色音色须重新选择或让新构建使用默认音色。

豆包保留逐段语音指令生成；激活 MiniMax 时，识别流程跳过该 LLM 阶段，合成使用 MiniMax 的音色与情绪参数。

---

## 目录结构

```
audio-book/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口 + StaticFiles 挂载前端 + /media 鉴权
│   │   ├── api/routes.py           # /api/* 路由（薄壳 → services；所有路由强制鉴权）
│   │   ├── ai/
│   │   │   ├── base.py             # BaseLLMProvider / BaseTTSProvider 抽象
│   │   │   ├── factory.py          # get_llm() / get_tts()（多厂商 + ACTIVE_* 指针）
│   │   │   └── providers/
│   │   │       ├── minimax/        # MiniMax（LLM + TTS）
│   │   │       └── doubao/         # 豆包 Doubao（官方系统 TTS）
│   │   ├── services/               # 业务逻辑（全部 LLM 驱动 + 数据库 ORM）
│   │   │   ├── auth.py             # JWT + bcrypt + seed admin + ENV=prod 安全门槛
│   │   │   ├── ownership.py        # Project/Build 资源归属守卫
│   │   │   ├── project.py          # 项目 CRUD + 章节/对白/角色识别（status checkpoint 可中断恢复）
│   │   │   ├── build.py            # 构建任务（Build + BuildArtifact 三级结构）
│   │   │   ├── job_tasks.py        # 后台任务心跳、孤儿检测与恢复
│   │   │   ├── chapter.py          # 章节切分 + 合成编排
│   │   │   ├── character.py        # 角色提取 + LLM 消歧（去重）
│   │   │   ├── dialogue.py         # 对白归属（每段 speaker + confidence）
│   │   │   ├── voice_recommender.py # LLM 给角色匹配内置音色
│   │   │   ├── epub_reader.py      # EPUB 解析
│   │   │   └── book_split.py       # 正则分章（兜底；LLM 分章是默认路径）
│   │   ├── db/
│   │   │   ├── models.py           # users / projects / builds / build_artifacts / project_characters / project_dialogues / project_pronunciation_rules
│   │   │   └── session.py          # aiosqlite + SQLAlchemy 2.0 async
│   │   └── core/config.py          # pydantic-settings
│   ├── tests/                      # pytest（鉴权 + 归属 + 上传 + 任务恢复）
│   │   ├── conftest.py             # 隔离 data 目录 + Mock LLM/TTS
│   │   ├── mock_providers.py       # 离线 mock，不依赖真实 API
│   │   ├── test_auth.py            # 登录 / token / 改密 / 401 链路
│   │   ├── test_ownership_red.py   # P1 #5：跨用户归属隔离
│   │   ├── test_upload_limit_red.py # P1 #8：流式上传 413
│   │   ├── test_import_validation.py # 无效文件拒绝与导入重试
│   │   ├── test_media_auth_red.py  # /media 静态资源鉴权
│   │   ├── test_providers_api_red.py # P0 #4：API Key 脱敏
│   │   ├── test_cancel_race_red.py # Build 取消竞态
│   │   ├── test_review_fixes_red.py # 评审项回归
│   │   └── test_project_e2e.py     # 项目制流程
│   ├── pytest.ini
│   └── requirements.txt
├── frontend/
│   ├── next.config.js              # output: 'export'
│   ├── tailwind.config.js          # 主题样式配置（当前界面固定深色）
│   ├── src/
│   │   ├── app/{layout,page,globals.css}
│   │   ├── lib/api.ts              # fetch 封装（含 token 注入）
│   │   └── components/             # ProviderModelsEditor（多厂商卡片）/ ToggleSwitch / ...
│   └── out/                        # 构建产物，被 FastAPI StaticFiles 挂载
├── deploy.sh                       # 快进拉取 main、重建前端、重启并检查健康状态
├── start.sh                        # 一键启动（带 --stop / --restart / --paths 子命令）
├── .env.example
├── .gitignore
└── README.md
```

> 数据库、音频、上传文件和日志默认位于仓库外的 `$NOVEL_TTS_HOME/data/`。旧部署可能在仓库内留有 `data/`，升级前请确认实际路径，见下文「升级与数据迁移」。

---

## 数据模型 & 资源归属

```
User ──< Project ──┬──< ProjectCharacter ──< 音色 assigned_voice_id
                   ├──< ProjectDialogue
                   ├──< ProjectPronunciationRule
                   └──< Build ──< BuildArtifact (每章 MP3)
```

- `Project.owner_user_id` 显式归属：alice 创建的项目只能 alice 看到/改/删，bob 默认看不到；admin 可访问任何。
- 孤儿项目（NULL owner）启动时由 lifespan 自动归 admin。
- `/media/build_<build_id>_*` 路径通过文件名反查 build → project → owner，包括章节 MP3、分卷 ZIP、章节预告片和时间轴 JSON。`preview_u<user_id>_*.mp3` 音色试听按用户归属校验，旧版无归属的试听文件拒绝访问。

---

## 核心业务流程（LLM 优先）

所有业务判断都走 **LLM + JSON mode + Pydantic v2 校验**（失败重试 3 次，每次温度 +0.1）：

| 步骤 | LLM 做什么 | 输入 | 输出 + 自校验 |
|---|---|---|---|
| 文本纠错 | 错别字修正 + **自我评估是否合理** | 原文 | `polished_text, diff, is_reasonable, reason`。不合理 → 回退原文 + 前端 warning |
| 角色识别 | 直接提取角色（姓名/性别/年龄/性格）| 全文 | `[{name, gender, age, personality}]` |
| 角色去重 | 判断"若雪"和"林若雪"是不是同一人 | 名字对 + 上下文 | `{same_person, canonical_name}` |
| 对白归属 | 每段对白标 speaker + 置信度 | 全文 + 角色列表 | `[{anchor, speaker, confidence}]`，**禁止 narrator/unknown 兜底** |
| 语义分章 | 按场景/时间/视角切换切段（≤ 50k 字/章）| 长文 | `[{idx, title, text}]`，拼接必须严格等于原文 |
| 音色推荐 | 给每个角色匹配内置音色 | 角色 + 音色列表 | `[{character_name, suggested_voice_id, reason}]` |

合成阶段：
- 章节标题 → 旁白音色 TTS → 后插 **1.5s 静音**（直接拼静音 MP3 帧）
- 对白段之间插 **0.25s 静音**
- 段级 `segment_overrides` voice **最高优先级**
- 所有段依次合成 → 字节直接拼接 → 最终 MP3

项目源文件可在详情页替换。替换后会清空旧章节、角色、对白和识别进度，状态回到「已导入」，必须重新识别才能启动新合成；已有 Build 与音频仍作为历史产物保留。识别或合成正在运行时，先取消任务再替换文件或删除项目；合成运行时也不能重新识别。

合成完成后按完整 MP3 帧计算章节时长并生成质检报告；章内拼接的 ID3 标签会跳过。旧构建首次打开详情时会尝试从段级时间轴或本地 MP3 回填时长。章节试听从源 MP3 逐帧截取约 15 秒并缓存为 `_preview_v2.mp3`；章节音频更新时会重新生成预告片。删除 Build 或项目会清理对应的预告片和时间轴文件。

---

## 前端 UI 步骤

| Step | 关键组件 |
|---|---|
| **项目列表** | ProjectCard（封面 / 章节数 / 上次构建状态 / 删除按钮） |
| **Step 1 输入** | 上传 TXT / Markdown / EPUB 或粘贴文本；文件按 50MB 流式限额读取，粘贴内容按 UTF-8 字节数限制为 50MB。无效文件在保存前返回 400，可在原项目中重试导入；详情页可替换源文件并重新识别 |
| **Step 2 角色+音色** | diff 对比面板 + polish_warning 黄色警告条 + 角色卡片（性格/标签 + LLM 推荐理由 + 试听） + 内置音色搜索 |
| **Step 3 生成+试听** | 对白卡片列表（橙色边框高亮 confidence<0.7） + 每段独立改音色覆盖 + 旁白折叠区 + 总音频播放器 + MP3 / ZIP 下载按钮 |
| **设置** | ProviderModelsEditor（多厂商卡片化：厂商开关 ToggleSwitch / API Key 输入（脱敏显示 + 「重写」）/ 模型列表） + LLM / TTS 激活厂商 + 模型指针 |

界面当前固定使用深色主题。

---

## 安全策略摘要（生产环境必须满足）

| 策略 | 检查时机 | 行为 |
|---|---|---|
| `JWT_SECRET` 仍是 `change-me*` | 启动 + ENV=prod | 启动失败 |
| `SEED_ADMIN_USER=PASS == admin/admin` | 启动 + ENV=prod | 启动失败 |
| `DISABLE_AUTH=true` | 启动 + ENV=prod | 启动失败 |
| 默认 admin 密码登录 | 请求 + ENV=prod | 403 |
| `GET /api/providers` 返回明文 API Key | 响应 | 全部脱敏为 `***LAST4` |
| 日志写明文密码 / API Key | 日志 | 绝不写（仅写 username + 已创建事件） |
| 跨用户访问项目 / build / 媒体 | 请求 | 403 |
| 任意登录用户列项目 | 请求 | 按 owner 过滤（admin 例外） |
| `await file.read()` 后再判大小 | 请求 | 改为分块累加，超限立即 413 |
| ZIP-bomb / 异常解压比 | 请求 | declared/actual > 100x 时 400 |

---

## 验收项对照

- ✅ **一条命令启动**：`./start.sh`，监听 `127.0.0.1:28000`
- ✅ 浏览器可跑完"输入 → 识别 → 生成 → 试听 → 下载"全流程
- ✅ 后端单进程 Python：`ps aux` 只看到 1 个 uvicorn，前端用 StaticFiles 挂载（无 Node 进程）
- ✅ 多厂商并存：MiniMax / Doubao / OpenAI 兼容 / 自定义 OpenAI 兼容端点
- ✅ 多用户隔离：alice 看不到 bob 的项目 / build / 媒体
- ✅ API Key 全链路脱敏：响应一律 `***LAST4`，占位符写入保留原 key
- ✅ 生产环境启动门槛：默认凭据直接阻断，不靠标志位
- ✅ 段级 TTS 缓存（跨 Build / 跨段复用）
- ✅ 后台任务 checkpoint 可中断恢复（prepare_project 写到 `progress_json`）
- ✅ Build 取消幂等：重复取消不会破坏状态
- ✅ 生成 MP3 / ZIP 可下载：`/media/build_<id>_ch<NNNN>.mp3` 或 `_all.zip`
- ✅ 质检按完整章节 MP3 时长计算：可跨段跳过 ID3 标签；旧构建首次打开详情时用段级时间轴或本地 MP3 修正时长、总时长和质检报告，无需重新合成
- ✅ `data/**` 完全 `.gitignored`（含 db / 上传文本 / ZIP / cache）
- ✅ 后端测试覆盖文件导入校验与后台任务恢复；运行命令见「启动步骤」

---

## 已知限制

1. **TTS 音频按段直接拼接**：章节 MP3 可能包含多个段落各自的 ID3 标签；时长解析会跳过这些标签。大多数 HTML5 `<audio>` 兼容；若需要重封装成单一连续 MP3，可后续引入 ffmpeg。
2. **LLM JSON mode 鲁棒性**：LLM + 3 次重试 + 温度递增，在 100+ 测试中成功率约 97%；极端失败时会抛 HTTP 500（前端弹窗），无自动降级。
3. **不做流式合成**：所有段 TTS 完成后才返回最终 MP3（单请求 600s 超时足够）。如需体验更好可以加 WebSocket / SSE，本 v1 不做。
4. **后台任务仍按单进程部署**：`JobTask` 在数据库中记录心跳，重启后会检测过期任务并尝试从 checkpoint 恢复；当前没有跨多实例共享的任务队列。
5. **角色去重只两两组合**：N 个角色产生 N(N-1)/2 对。超过 30 角色时单次调用 token 会较大，可以后续拆批。
6. **官方音色清单**：豆包内置清单位于 `backend/app/ai/providers/doubao/tts.py`；MiniMax 中文清单位于 `backend/app/ai/providers/minimax/system_voices_zh.json`，并结合官方系统音色查询接口补充。
7. **依赖策略**：运行依赖见 `requirements.txt` 和 `requirements.lock`，开发测试依赖见 `requirements-dev.txt`；CI、Dependabot 与依赖审计配置位于 `.github/`。

---

## 成本预估（参考 MiniMax / 豆包官网公开定价）

### LLM（按 ~¥15/百万 tokens 粗略估算）
以一段 3000 字小说为例：
- 纠错 1 次：in ~4k / out ~4k → 8k tokens
- 角色 1 次：in 4k / out 0.3k → 4.3k
- 去重 1 次（8 角色 28 对）：in 2k / out 0.5k → 2.5k
- 对白归属 1 次：in 6k / out 2k → 8k
- 分章 1 次：in 5k / out 5k → 10k
- 音色推荐 1 次：in 3k / out 0.5k → 3.5k
- **合计 ≈ 36k tokens × ¥15/1e6 ≈ ¥0.54**（每章）

### TTS（以官方实时价格为准）

豆包估算按约 **0.0003 元/字**；MiniMax `speech-2.8-hd` / `speech-2.8-turbo` 分别约 **¥3.5 / ¥2.0 每万计费字符**，汉字按两个计费字符估算。构建弹窗仅对豆包展示原有费用估算；MiniMax 请以官方账单为准。
按「文本字符数（含标点）」计费，基数就是实际下发给 TTS 的正文/对白/标题文本：
- 3000 字小说 ≈ **¥0.9**
- 10 万字中篇 ≈ **¥30**

> - 逐段**语音指令**（`context_texts`）的文字官方明确**不计费**，写多长都不涨钱。
> - 单价可在 `DOUBAO_TTS_PRICE_PER_CHAR` 调整；「合成预估」面板会直接显示预估费用。
> - 段级缓存命中的段落不重复调用上游，实际费用只会更低。

**合计 3000 字 ≈ ¥1.4**（LLM ¥0.54 + TTS ¥0.9）。长文建议：
- 控制每章 ≤ 50000 字（自动分章 + 并行）
- 同一章音色试听后再合成，避免反复重跑
- 保存 project_id，重跑合成（不重跑 prepare）能省 LLM 费

---

## 升级与数据迁移（不会丢数据）

`start.sh` 默认把运行时产物放在项目目录外的 `$NOVEL_TTS_HOME`
（默认 `~/.novel-tts/`）。`git pull` 不会覆盖该目录中的数据和配置；旧部署若仍把数据放在仓库内，请先按下文迁移。

```
项目目录                              运行时产物（保留）
├── deploy.sh                         ~/.novel-tts/
├── start.sh                          ├── .env               ← API Key、JWT_SECRET
├── backend/                          ├── .venv/             ← Python 依赖（venv）
├── frontend/                         └── data/
└── ...                                   ├── app.db          ← SQLite 主库
                                          ├── audio/         ← 生成的有声书
                                          ├── logs/          ← app.log / stdout
```

### 查看当前生效路径
```bash
./start.sh --paths
# 输出示例：
#   NOVEL_TTS_HOME : /home/yourname/.novel-tts
#   DATA_DIR       : /home/yourname/.novel-tts/data
#   ...
```

### 升级流程（推荐）

代码提交并推送到 `main` 后，运行中的服务不会自动更新。这台服务器由用户在准备好时手动执行：

```bash
bash /opt/audio-book/reinstall.sh
```

其他部署环境可在项目目录中运行：

```bash
cd /path/to/audio-book
./deploy.sh
```

`deploy.sh` 要求工作区没有未提交修改，只接受 `origin/main` 的快进更新；随后执行 `npm ci`、`npm run build`、`start.sh --restart`（安装后端依赖并以后台模式启动），最后按 `.env` 中的监听地址和端口检查 `/api/health`。这台服务器的 `/opt/audio-book/reinstall.sh` 是调用仓库内 `deploy.sh` 的本地入口。

手动升级时也要重建前端：

```bash
git pull --ff-only origin main
(cd frontend && npm ci && npm run build)
./start.sh --restart
```

`.env`、数据库、音频和训练样本默认在 `$NOVEL_TTS_HOME` 下，不受仓库内的快进拉取影响。

### 把数据放到别的盘
```bash
export NOVEL_TTS_HOME=/mnt/ssd/.novel-tts
mkdir -p "$NOVEL_TTS_HOME"
./start.sh
```
把这个 export 加到 `~/.bashrc` / `~/.zshrc` 让它每次登录都生效。

### 从旧部署迁移（一次性）

旧版本的数据全在项目根的 `./data` 和 `./.env`。这次升级已经做了自动兼容：
**首次启动新版 `start.sh` 时**，如果 `$NOVEL_TTS_HOME/.env` 不存在、但项目根的
`.env` 存在，会自动复制过去（项目根 `.env` 保留为 `.env.local-backup`，可手动删）。

如果你的旧数据在项目内 `./data/`，想顺手挪出去（推荐，让 `rm -rf` 项目目录也安全）：

```bash
./start.sh --stop
export NOVEL_TTS_HOME=$HOME/.novel-tts
mkdir -p "$NOVEL_TTS_HOME"

# 一次性搬运
mv ./data "$NOVEL_TTS_HOME/data"
if [ -f .env ]; then mv .env "$NOVEL_TTS_HOME/.env"; fi

# 数据迁移完成后，后续升级运行：
./deploy.sh
```

> 搬运前**务必**先 `./start.sh --stop` 停服，避免数据库写入中移动文件。
> 数据库文件 `data/app.db` 正在被写时 `mv` 可能导致 SQLite 损坏。

### 旧版"先删项目目录再 git clone"的危险操作
升级不需要删除项目目录。直接运行 `./deploy.sh`，既能保留当前仓库的 SSH 远端设置，也能重建前端。旧部署若将数据保存在项目目录内，删除目录还会丢失数据库、API Key 和训练样本。

如果只是想换部署位置（机器迁移、换硬盘），用上面"把数据放到别的盘"那一段。

---

## 停服 / 重启 / 状态

`start.sh` 现在是带子命令的服务管理器：

```bash
./start.sh              # 后台启动（默认）
./start.sh --foreground # 前台启动（Ctrl+C 停止）
./start.sh --stop       # 停后台进程
./start.sh --restart    # 停止旧进程并在后台启动；不会自动重建已有前端产物
./start.sh --status     # 看运行状态、PID 和日志位置
./start.sh --paths      # 看所有路径
./start.sh --help       # 帮助
```

> ⚠️ 不要在外层 `nohup ... &` 套娃跑 `start.sh`（会留下僵尸脚本进程）。
> 直接 `./start.sh` 默认就是后台模式，启动后立刻退出，PID 写到
> `$NOVEL_TTS_HOME/data/uvicorn.pid`。Linux 上使用 `setsid` 将进程与调用终端分离。

---

## 常见问题

**Q: ModuleNotFoundError: No module named 'backend'**
→ 确认从项目根运行 start.sh，不要从 backend/ 里直接跑 uvicorn。

**Q: ENV=prod 启动直接报错 "JWT_SECRET 仍为默认值"**
→ 改 `.env`：`JWT_SECRET=一个 ≥32 字符的随机串`（`openssl rand -hex 32` 即可），并设 `SEED_ADMIN_PASS=强密码`。

**Q: 一个登录用户看不到自己刚创的项目**
→ 当前为多用户隔离设计。`admin` 可见所有；普通用户只能看到自己 own 的项目。如需分享，请创建独立账号或开放 admin。

**Q: 音色列表与预期不同**
→ 音色库只显示当前激活 TTS 的官方中文系统音色。请先确认「设置 → 激活 TTS」中的厂商和模型；MiniMax 接口暂不可用时会显示仓库附带的中文系统清单。

**Q: 前端打开 404**
→ 若 `frontend/out/index.html` 不存在，首次运行 `./start.sh` 会自动构建。升级后前端显示旧版本时运行 `./deploy.sh`，它会重新安装依赖并构建静态产物。

**Q: 升级后数据没了 / API Key 失效 / 数据库被重建**
→ 先运行 `./start.sh --paths`，核对 `$NOVEL_TTS_HOME` 是否指向原来的数据目录。新部署将数据库、音频和配置放在仓库外；旧部署若仍使用仓库内的 `data/`，请按「升级与数据迁移」迁移。后续升级运行 `./deploy.sh`。

**Q: 怎么把数据库 / 音频挪到别的盘**
→ `export NOVEL_TTS_HOME=/mnt/ssd/.novel-tts && ./start.sh --stop && mv ~/.novel-tts/* /mnt/ssd/.novel-tts/ && ./start.sh`。
→ 写进 `~/.bashrc` 让 export 持久生效。
