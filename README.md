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
| `DSH_SERVICE_TOKEN` | | 空 | DSH 插件调 `/api/agent/*` 的服务 token（请求头 `X-DSH-Token`）。留空时整个命名空间返回 503；**不要**写进前端或提交进仓库 |
| `AGENT_TASK_BUDGET_CNY` | | `2.0` | 单个创作任务的 LLM 成本熔断阈值（元），上报成本累计超限后任务置 `blocked` |
| `DAILY_OUTPUT_DIR` | | `$DATA_DIR/daily` | 日报产物根目录（每期一个子目录） |
| `DAILY_SOURCES_JSON` | | 空=内置 7 个源 | 信源数组 `[{"name","url","enabled"}]`，整体覆盖内置列表 |
| `DAILY_TTS_VOICE_ID` | | 空=自动挑 | 日报播报音色；留空取当前 TTS 厂商第一个可用音色 |
| `DAILY_FEED_KEY` | | 空 | 播客 RSS 的 `?key=` 订阅密钥；**留空则 RSS 不开放** |
| `DAILY_LLM_ENABLED` | | `true` | 关闭后用离线模板出稿（无 LLM Key 也能跑） |
| `DAILY_TARGET_MINUTES` | | `12` | 目标时长（写进脚本 prompt） |
| `DAILY_DEDUPE_DAYS` | | `7` | 跨期去重窗口（天）：窗口内讲过的选题按 URL / 标题相似度排除或降权 |
| `DAILY_PUBLIC_BASE_URL` | | 空=按请求推导 | RSS enclosure 用的对外地址，生产建议显式设为 `https://audio-book.ls-lh.cn` |
| `DAILY_ALERT_WEBHOOK` | | 空 | 失败/熔断告警 webhook（POST JSON，兼容企业微信/飞书/Slack text 格式）；留空只写日志 |

### 多厂商配置

设置页支持 **MiniMax（LLM 与 TTS）**、**豆包 TTS**、**OpenAI 兼容 LLM** 与自定义 LLM 端点。`PROVIDERS_CONFIG` 保存厂商信息，`ACTIVE_TTS_PROVIDER` / `ACTIVE_TTS_MODEL` / `ACTIVE_LLM_PROVIDER` / `ACTIVE_LLM_MODEL` 指向激活的模型。旧的 `LLM_BASE_URL` / `LLM_MODEL_PRO` / `LLM_MODEL_FAST` 字段仅用于迁移，设置页不展示。

具体厂商卡片配置请在「设置」页按厂商填写 API Key（仅管理员可见明文；其他用户只能看到脱敏后的 `***LAST4`）。

### TTS 厂商与音色

在「设置 → 模型厂商」启用豆包或 MiniMax，并在「激活 TTS」选定厂商及模型。MiniMax 可选 `speech-2.8-hd`（音质优先）或 `speech-2.8-turbo`（速度和成本优先），使用 MiniMax API Key；中文语音接口默认使用官方 `https://api.minimax.cn/v1`。豆包使用官方 `seed-tts-2.0` 系统音色。音色库、试听、角色推荐、全局旁白、构建与重试均限定为当前激活厂商的官方音色。MiniMax 音色从官方 `/v1/get_voice` 的 `system` 分类读取中文系统音色；接口暂不可用时使用仓库附带的官方中文清单。项目不提供上传或复刻音色。

「启动新构建」弹窗为**本次构建**选择默认男声和女声，未选时分别取当前厂商列表中第一条相应性别音色；若与旁白相同，则取下一条。默认音色不能与旁白相同，男女默认音色可以相同。未分配音色的角色与未识别归属的对白按角色性别使用默认男声或女声，未知性别使用默认男声。这两个默认音色写入 Build 快照，重试继承，不写入项目全局设置。切换 TTS 厂商后，旧角色音色须重新选择或让新构建使用默认音色。

书籍识别在角色音色推荐前，会让当前激活的 LLM 根据书名和分布于全书的正文样本，一次生成**书籍分类、标签、卖点、介绍和默认旁白音色**。分类可为男频、女频、文学、非虚构或其他；卖点不超过 20 字（喜马拉雅专辑要求）；介绍要求把核心信息放在前 150 字。旁白从当前激活 TTS 的官方音色中按作品气质选择，男频限定男声、女频限定女声；模型返回无效音色时按分类从官方列表兜底。旁白会先落库，再从角色音色推荐候选中排除。识别重跑只填补空字段，保留已手动编辑的资料；切换 TTS 厂商后会重新选择当前厂商的旁白。LLM 配额耗尽会沿用识别任务的断点与重试机制，普通资料生成失败会记录日志并允许之后重试。

已有识别结果可在「项目设置」点击「AI 重新生成并覆盖」，直接重新生成这五项，无需重新识别角色和对白；该操作会覆盖手动编辑过的对应内容。替换项目源文件时旧书资料与旁白会清空，下次识别按新书重新生成。人工仍可在项目设置中调整生成结果。

识别时还会从喜马拉雅标签体系（`backend/app/resources/ximalaya_tags.json`）自动选择内容分类、受众、题材/子类、情节、恋爱、角色、风格、背景和最多 10 个自定义标签；模型返回的候选值会按该体系校验，自造标签会被丢弃。概览页会按分组展示全部选中的喜马拉雅标签。

书籍详情概览显示自动生成的分类、标签、卖点和介绍。介绍的前 150 字外显，其余内容可展开；这些字段可在「项目设置」中编辑；卖点输入框限制 20 字。详情概览还提供封面框，初始为空，可上传 PNG、JPEG 或 WebP（最大 8 MB）、点击「一键 AI 生成」或删除已有封面。生成时封面框显示阶段进度；成功写入对象存储后才替换原封面，失败时保留原图并显示错误。封面随项目删除，单次 Build 删除不会影响封面。

AI 封面需先在「设置 → 模型厂商」激活图片模型。推荐使用 AIHubMix 的 Gemini 图片模型（例如 `gemini-nano-banana-2.1`）：在 aihubmix 厂商卡片填写 HTTPS Base URL（`https://aihubmix.com/v1`）、API Key，并添加 `kind=image` 的模型；服务会调用 Gemini 原生接口 `https://aihubmix.com/gemini/v1beta/models/{model}:generateContent`，请求 `responseModalities=["TEXT","IMAGE"]`、`imageConfig.aspectRatio=1:1`，从 `candidates[].content.parts[].inlineData.data` 读取图片。该模型对中文书名渲染明显更可靠，prompt 参考 `/opt/novels` 的写法：明确要求书名按正确汉字显示、按分类/标签选择场景、从书名提取意象，并附带 negative prompt。旧版 MiniMax 图片模型仍可选（官方 `image-01` / `image-01-live`，`https://api.minimax.cn/v1/image_generation`），但它对中文文字渲染不稳定，可能画出伪汉字。其他图片厂商沿用 OpenAI 兼容 `POST /images/generations`，填写 HTTPS Base URL、API Key、图片模型 ID，请求 `1024x1024`、`response_format=b64_json`，需要返回 `data[0].b64_json`。封面 prompt 会使用书名、分类、标签、卖点和介绍；生成请求超时为 240 秒，期间进度条按请求、解码、上传阶段推进；服务重启中断生成任务后，重新打开页面会显示失败，可点击再次生成。封面只保存到已配置的对象存储，未启用对象存储时上传和生成会提示先配置；浏览器通过登录态读取私有封面。

豆包保留逐段语音指令生成；激活 MiniMax 时，识别流程跳过该 LLM 阶段，合成使用 MiniMax 的音色与情绪参数。

### 对象存储与下载

在「设置 → 对象存储」选用腾讯云 COS 时，`S3_BUCKET` 填完整桶名，`S3_REGION` 填地域（例如 `ap-beijing`）。`S3_ENDPOINT` 可以填地域端点 `https://cos.ap-beijing.myqcloud.com`，也可以填控制台给出的含桶名访问域名 `https://<桶名>.cos.ap-beijing.myqcloud.com`；程序会统一处理，避免下载地址重复拼接桶名。私有桶可直接使用；`S3_PUBLIC_BASE_URL` 仅在有公开可读的 CDN 下载域名时填写，否则留空，下载链接由 COS 临时签名生成（有效期一小时）。

对象存储启用且 `STORAGE_CLEANUP_LOCAL=true`（默认）时，章节 MP3 上传成功即删除本地临时文件；LRC 直接上传；整书 ZIP 从 COS 逐章回源，并流式上传到对象存储（大文件由 SDK 分片），不在本地生成完整 ZIP。构建结束后，MP3、LRC、ZIP 交付物只保留在 COS，下载使用 COS 签名直链。打包期间本地仅暂存当前章节 MP3；目录里会保留体积较小的时间轴 JSON 和音色试听片段。设置为 `local` 或关闭 `STORAGE_CLEANUP_LOCAL` 时，整书 ZIP 会先生成在本地，再按配置归档。整书 ZIP 可能较大，下载可使用支持断点续传的客户端。

`_seg_cache` 是跨构建复用的 TTS 逐段 MP3 缓存，不是章节成品。本地存储模式会按 `TTS_SEGMENT_CACHE_TTL_DAYS`（默认 30 天）及 `TTS_SEGMENT_CACHE_MAX_SIZE_GB`（默认 20 GB）清理。对象存储省盘模式只保留最多 512 条内存缓存，不再写磁盘缓存；升级启动时会删除已有的 `_seg_cache` 目录。这会释放本地空间，但之后重建相同内容可能再次产生 TTS 费用。

对象存储省盘模式下，章节 MP3、LRC 或 ZIP 上传失败会使构建失败，不会把仅存在本地的文件当作已交付成功；检查服务日志中的 `[storage] 归档失败` / `写入失败` 后点击“重试失败章节或打包”。若所有章节已完成而只在 LRC/ZIP 阶段失败，重试会复用远端章节重新打包，不再调用 TTS。旧构建的本地文件不会自动补传。删除构建或项目时，COS 批量删除请求会附上所需的 `Content-MD5`；若远端删除失败，数据库和本地文件仍保留，可在修复后重新点击删除。

---

## 目录结构

```
audio-book/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口 + StaticFiles 挂载前端 + /media 鉴权
│   │   ├── api/routes.py           # /api/* 路由（薄壳 → services；所有路由强制鉴权）
│   │   ├── api/agent_routes.py     # /api/agent/*（DSH 插件桥接，独立 X-DSH-Token 鉴权）
│   │   ├── api/daily_routes.py     # /api/daily/*（AI 前沿日报：列表/详情/审核/音频/RSS）
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
│   │   │   ├── cover.py            # AI 封面生成 + 对象存储读写
│   │   │   ├── cover_prompt.py     # 封面 prompt（精确书名 + 类型场景）
│   │   │   ├── ximalaya_tags.py    # 喜马拉雅标签体系解析 + 候选校验
│   │   │   ├── epub_reader.py      # EPUB 解析
│   │   │   └── book_split.py       # 正则分章（兜底；LLM 分章是默认路径）
│   │   │   ├── daily/              # AI 前沿日报流水线
│   │   │   │   ├── collect.py      # RSS/Atom 采集 + 24h 窗口 + 去重
│   │   │   │   ├── cluster.py      # 事件聚类（跨源合并）
│   │   │   │   ├── score.py        # 乘法打分 + 安全一票否决 + 主题闸门
│   │   │   │   ├── script.py       # LLM 脚本（模板兜底）+ 节目简介
│   │   │   │   ├── tts.py          # 分段合成 + 拼接 + 响度标准化
│   │   │   │   ├── feed.py         # RSS 2.0 + iTunes
│   │   │   │   ├── store.py        # 每期产物目录 / 审核状态 / 运行状态
│   │   │   │   └── pipeline.py     # 编排 + 单实例运行锁
│   │   ├── db/
│   │   │   ├── models.py           # users / projects / builds / build_artifacts / project_characters / project_dialogues / project_pronunciation_rules / creation_jobs / agent_runs
│   │   │   └── session.py          # aiosqlite + SQLAlchemy 2.0 async
│   │   ├── resources/ximalaya_tags.json # 喜马拉雅标签原始配置
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
├── agent/                          # DSH 插件：dsh-audio-book-agent
│   ├── package.json                # dsh.bundle.patch 指向 cordis.patch.yml
│   ├── cordis.patch.yml            # 把插件挂进 DSH profile
│   ├── index.js                    # 8 个 audiobook_* 工具（只调 /api/agent/*）
│   ├── skills/audiobook-pitch/     # 选题、20 字卖点、标签、封面 prompt 规范
│   └── README.md                   # 安装 / 环境变量 / 安全边界
├── .dsh/skills/                    # DSH 默认技能根（符号链接到 agent/skills/）
├── frontend/
│   ├── next.config.js              # output: 'export'
│   ├── tailwind.config.js          # 主题样式配置（当前界面固定深色）
│   ├── src/
│   │   ├── app/{layout,page,globals.css}
│   │   ├── lib/api.ts              # fetch 封装（含 token 注入）
│   │   └── components/             # ProviderModelsEditor / ToggleSwitch / DailyPage（日报）/ ...
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
- `/media/build_<build_id>_*` 路径通过文件名反查 build → project → owner，包括章节 MP3、整书 ZIP（兼容历史分卷）、章节预告片和时间轴 JSON。`preview_u<user_id>_*.mp3` 音色试听按用户归属校验，旧版无归属的试听文件拒绝访问。

---

## 核心业务流程（LLM 优先）

需要语义判断的步骤使用 **LLM + JSON mode + Pydantic v2 校验**；切章在本地完成，无需调用模型：

| 步骤 | LLM 做什么 | 输入 | 输出 + 自校验 |
|---|---|---|---|
| 文本纠错 | 错别字修正 + **自我评估是否合理** | 原文 | `polished_text, diff, is_reasonable, reason`。不合理 → 回退原文 + 前端 warning |
| 角色识别 | 直接提取角色（姓名/性别/年龄/性格）| 全文 | `[{name, gender, age, personality}]` |
| 角色去重 | 判断"若雪"和"林若雪"是不是同一人 | 名字对 + 上下文 | `{same_person, canonical_name}` |
| 对白归属 | 每段对白标 speaker + 置信度 | 全文 + 角色列表 | `[{anchor, speaker, confidence}]`，**禁止 narrator/unknown 兜底** |
| 切章与朗读单元 | 本地识别「第X章/节」、正文前缀、卷章合体、Markdown 和连续数字标题；无可靠标题时按段落/长度切分 | 原文 | 章节标题 + 正文；异常超长单元限制在约 3 万字符内 |
| 音色推荐 | 给每个角色匹配内置音色 | 角色 + 音色列表 | `[{character_name, suggested_voice_id, reason}]` |

合成阶段：
- 章节标题 → 旁白音色 TTS → 后插 **1.5s 静音**（直接拼静音 MP3 帧）
- 对白段之间插 **0.25s 静音**
- 段级 `segment_overrides` voice **最高优先级**
- 所有段依次合成 → 字节直接拼接 → 最终 MP3

项目源文件可在详情页替换。替换后会清空旧章节、角色、对白和识别进度，状态回到「已导入」，必须重新识别才能启动新合成；已有 Build 与音频仍作为历史产物保留。识别或合成正在运行时，先取消任务再替换文件或删除项目；合成运行时也不能重新识别。

切章兼容小说、散文、诗集、自传等文本。带明确标题时优先保留原书结构；书首有页码索引的目录会跳过，前言保留。下载站把同一章写成连续的「第 N 节：篇名(1)/(2)」时，先合并为一个篇章；篇章超过约 3 万字符才继续沿自然边界拆分。没有可靠标题时，短于约 1.2 万字符的文本作为一个「正文」单元，更长的文本优先按完整段落组成多个「第 N 部分」；只有单段过长时才在句末或行尾切分，`CHAPTER_SPLIT_HARD_FALLBACK_MAX_CHARS`（默认 3 万字符）是单元硬上限。无法判断语义边界时，合成单元仍可能跨越自然篇章，导入前可检查识别出的单元并按需整理源文本。这些占位名称用于界面和文件标识；合成时不朗读「正文」，「第 N 部分」直接朗读，不额外加章节号。非虚构和诗歌可以没有角色或对白，纯旁白书籍仍可继续合成。源文件或切章结果变化后，旧章节编号对应的识别缓存自动失效。

每个朗读单元单独生成一个 MP3 和对应的 LRC。无标题短书只有一个单元时，单章 MP3 就是整本书；长书切成多个单元时，单章下载只包含所选部分。「下载全部 ZIP」始终生成一个整书 ZIP，其中包含全部单元的独立 MP3/LRC，不会合并成一个整书 MP3。原有 `ZIP_SHARD_CHAPTERS` 配置不再控制新构建；历史分卷仍能在旧构建详情中分别下载。

对白归属按章数与正文字符数双重限制，每次请求正文默认不超过 `DIALOGUE_BATCH_MAX_CHARS=12000`；超长单章会再分块，并把对白锚点坐标还原到原章。`DIALOGUE_BATCH_TIMEOUT_SECONDS=180` 限制单次模型调用时间。明显包含多处对白线索却返回空列表时会重试，仍失败则保留为未完成章。项目只完成部分对白、角色切片或音色推荐时显示「部分完成」，再次点击识别会复用有效断点并补跑未完成部分。大幅改变切章规则后重新识别《元尊》时，会重新处理新章节，产生相应 LLM 调用费用。

角色音色推荐按当前激活的 TTS 厂商分批处理。模型漏掉角色、返回无效音色或某一批失败时，系统会按角色性别从同厂商官方音色中补齐，优先选择未占用且与旁白不同的音色；未知性别优先男声。LLM 配额耗尽仍会中断识别并保留断点。旧项目已经显示“缺少音色推荐”时，部署新代码后在项目概览点击“重新识别（自动补跑失败部分）”即可重新生成角色音色。

MiniMax 官方 [速率限制](https://platform.minimax.cn/docs/guides/rate-limits)规定 T2A v2 免费账号 **10 RPM**、充值账号 **20 RPM**，主账号和子账号共享额度。`MINIMAX_TTS_RPM_LIMIT` 默认设为 10 并匀速发送；确认账号已充值后，可在设置页「限流配置」调至 20。若同一账号还有其他程序调用 T2A v2，应为本项目配置更低的请求预算。此前已在环境变量或设置页显式配置为 20 的实例不会自动改成 10，需要自行调整。

遇到 MiniMax HTTP 429 或返回体中的 RPM 限流错误时，单段先按本地策略冷却约 65 秒并短重试；若仍受限，构建停在当前章节，默认每 5 分钟探测一次，复用本章已成功合成的段，额度恢复后继续。MiniMax 即使返回 HTTP 200，也可能在 `base_resp` 中返回“Token Plan 用量上限”；该错误直接进入构建的限额等待流程，不再把后续章节逐章写成 1 秒静音占位。HTTP 429 响应体明确表示 Token Plan 用量上限时也采用同一流程。服务端提供 `Retry-After` 时优先按该时长等待；等待状态会显示在构建进度中，可随时取消。限流期间自动降低的请求速率会在成功合成后逐步恢复。连续等待默认最多 6 小时，超时后停止构建并保留已完成章节，待额度恢复可点“重试失败章节”。可通过环境变量 `MINIMAX_TTS_QUOTA_RETRY_SECONDS`（默认 `300`）和 `MINIMAX_TTS_QUOTA_WAIT_MAX_SECONDS`（默认 `21600`）调整；官方文档没有承诺固定 5 小时重置时间。音色试听会直接显示 Token Plan 用量上限错误，不会触发长时间后台等待。

旧构建中已经生成的静音占位不会自动变成语音。额度恢复后，在原构建点击“重试失败章节或打包”：新构建只重新合成失败章，并复用已成功的章节音频；源构建的失败占位不会被当作成功音频复制。若账号套餐持续提示升级或购买积分，等待期间仍无法完成合成，需要在 MiniMax 侧恢复可用额度。

识别阶段的 MiniMax LLM 也区分普通瞬时 `429` 和 Token Plan 套餐级限流：错误码 `2062` 或响应包含 `Token Plan` 时，不再按普通 429 重试 2/4/8 秒，而是立即打开 LLM 熔断并保存当前章节 checkpoint；项目会进入可恢复状态，额度恢复后自动或手动补跑未完成章节。这样不会在同一套餐限流窗口内让每章重复发送三次请求。普通 429 仍使用指数退避。润色阶段的 `failed` 统计表示可补跑的网络/限流/结构化失败，`rejected` 表示模型返回了修改但本地锚点无法安全应用；两者都保留原文。`content_rejected` 才表示供应商内容审核拒绝，默认不会重复补跑。

章节 MP3 拼接时仅保留音频帧，插入的静音帧与 MiniMax 的 32 kHz 单声道保持一致。旧版 MiniMax 构建若出现浏览器 `Unsupported midstream configuration change`，部署后在原构建点击“重试失败章节”：系统会检查并重建含旧双声道静音帧的成功章，同时补跑失败章。旧构建已归档到对象存储时，需要保持原桶和前缀配置可访问；重试会回源旧音频。合成与重试可能产生 TTS 费用。

合成完成后按完整 MP3 帧计算章节时长并生成质检报告；章内拼接的 ID3 标签会跳过。旧构建首次打开详情时会尝试从段级时间轴或本地 MP3 回填时长。章节试听从源 MP3 逐帧截取约 15 秒并缓存为 `_preview_v2.mp3`；章节音频更新时会重新生成预告片。删除 Build 或项目会清理对应的预告片和时间轴文件。

章节 MP3 会清除各 TTS 段自带的 `Xing` / `Info` 时长索引标记，否则播放器可能把整章的拖动位置映射到首段，出现进度条前进而声音从头播放。已生成的旧章节在首次播放或单章下载时按需修复，不重新调用 TTS；本地文件原子改写，对象存储中旧文件先探测、回源修复后重新上传。保留原 MP3 帧数与时间轴；如果已下载过旧版“全部 ZIP”，其中的旧 MP3 不会被这个按需修复流程改写。

启动新构建时，默认保留此前的构建记录与产物；构建弹窗可勾选“删除此前构建及全部产物”，系统会先删除旧构建，再启动新构建。删除单次构建或整本书时，确认框会列出删除范围：构建记录、章节 MP3、LRC、ZIP 及本地预告片、时间轴；删除整本书还会清理源文件和识别结果。启用对象存储时，删除会按项目或构建目录清理桶内产物（包括上传后未落库的文件）；若远端清理失败，请求会报错并保留数据库记录和本地文件以便重试。段级 TTS 缓存不属于构建产物；本地模式按缓存策略清理，对象存储省盘模式在启动时删除旧磁盘缓存。更改过对象存储桶或前缀的历史产物仍需到旧桶或旧前缀手动清理。

音色库已从「AI有声书」的二级菜单上移为**系统级一级菜单**（`#/voices`）：它是跨内容类型共享的资源（有声书、日报、后续短剧/漫画都共用同一套音色），旧地址 `#/audiobooks/voices` 会自动重定向。音色库、项目音色配置与新构建弹窗直接显示音色名称，不再在名称前重复放大首字。新构建弹窗中的默认男声、默认女声和旁白音色均可试听；试听使用当前设置的语速，播放与其他试听共享同一个播放器。

---

## 前端 UI 步骤

| Step | 关键组件 |
|---|---|
| **项目列表** | ProjectCard（封面 / 章节数 / 上次构建状态 / 删除按钮） |
| **Step 1 输入** | 三选一：①从书库导入（读对象存储 `library/` 下的书目索引，按分类/关键字搜索选中，服务端把原文下载到本地后建项目，名称自动填入书名）；②上传 TXT / Markdown / EPUB；③粘贴文本。上传与粘贴均按 50MB 限额（上传流式、粘贴按 UTF-8 字节数），无效文件在保存前返回 400，可在原项目中重试导入；书库首次导入要先下载原书，冷启动可能较慢，弹窗会显示已用秒数；详情页可替换源文件并重新识别 |
| **Step 2 角色+音色** | diff 对比面板 + polish_warning 黄色警告条 + 角色卡片（性格/标签 + LLM 推荐理由 + 试听） + 内置音色搜索 |
| **Step 3 生成+试听** | 「章节」页只做文本阅读与逐行对白归属校对（只高亮对白片段、正文不整行着色，不提供音频播放）；「构建」页为播放列表（每章播放、MP3、LRC）；点击播放才签发当前章 URL；「构建信息」与「章节列表」之间是内联播放器（左：章节标题 + 来源圆点；中：后退 10 秒 / 播放 / 快进 10 秒 + 细轨进度与两端时间；右：音量、下载、关闭；窄屏折叠为单行「标题 + 时间 / 播放 / 关闭」，进度贴在卡片顶边）；离开「构建」页签时音频随组件卸载停止；概览只展示项目资料，不显示章节列表 |
| **音色库（系统级）** | 侧栏「系统 → 音色库」：全部音色可按厂商/性别/场景搜索与逐个试听（真实 TTS）；被有声书项目、日报播报音色共用 |
| **设置** | ProviderModelsEditor（多厂商卡片化：厂商开关 ToggleSwitch / API Key 输入（脱敏显示 + 「重写」）/ 模型列表） + LLM / TTS 激活厂商 + 模型指针 |
| **AI 前沿日报** | 一级菜单 `#/daily`：期数列表（日期 / 标题 / 状态 / 时长 / 条目数）+ 运行状态条；详情页三个 Tab（节目简介 / 口播脚本 / 选题明细）+ 音频试听 + 人工终审（通过 / 退回 + 备注）+ feed 地址复制 |

章节页不再为每条渲染独立 `<audio>`，也不在展开时批量签发媒体 URL；音频只在「构建」页点击播放/下载某一章时才请求该章资源，避免对象存储产生额外的预取/缓存流量。

界面当前固定使用深色主题。

---

## DSH 创作编排（Agent 插件，P0）

仓库自带一个 DSH（DeepSeek Harness）插件 `agent/`：让 DSH 的 Agent 做「选题 → 文案/封面 →
人工确认 → 构建 → 质检 → 发布」的**编排与决策**，执行和数据仍由本后端负责。

```
DSH 会话 ── audiobook_* 工具 ──▶ agent/（薄插件）──HTTP X-DSH-Token──▶ /api/agent/*
                                                                        │
                                      DB / COS / 构建 / 发布（后端是唯一写者）
```

设计约束（这也是为什么**不**把流水线搬进 DSH）：

- **密钥不出后端**：COS AK/SK、LLM Key 只存在设置页 / `app_settings`，插件上下文里没有；
- **长任务不占 Agent 回合**：TTS 构建可能几十分钟，插件只拿 `job_id`，轮询
  `audiobook_project_status`；
- **幂等**：`audiobook_job_create` 带 `idempotency_key`，重试返回同一个 job，不会双写；
- **可复现 + 成本熔断**：每次模型调用用 `audiobook_job_report` 落账（模型 / prompt 版本 /
  tokens / 成本），累计超过 `AGENT_TASK_BUDGET_CNY` 后任务自动置 `blocked`。

### 后端接口（`/api/agent/*`，独立鉴权）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agent/health` | 桥接自检（token / 存储 / 书库状态） |
| GET | `/api/agent/library/search` | 书库检索（分类 + 标题/作者关键字） |
| GET | `/api/agent/library/books/{uid}` | 单本元信息 + 开头试读（Range 只读几 KB） |
| GET | `/api/agent/projects/{id}/status` | 项目状态 + 最近 3 次构建 |
| GET | `/api/agent/builds/{build_id}` | 按 build_id 查构建状态 |
| GET | `/api/agent/projects/{id}/logs` | app.log 尾部中与该项目相关的行 |
| GET | `/api/agent/pricing/quote` | 字符数 → TTS 成本预估 |
| POST | `/api/agent/jobs` | 创建创作任务（幂等） |
| GET | `/api/agent/jobs`、`/api/agent/jobs/{job_id}` | 任务列表 / 详情（含调用流水） |
| POST | `/api/agent/runs` | 上报一次模型调用的成本（超预算熔断） |

鉴权：请求头 `X-DSH-Token` 必须等于 `DSH_SERVICE_TOKEN`；未配置时整个命名空间返回 503
（fail closed）。这些接口**不挂**全局 JWT 依赖，也不接受用户 token。

### 插件安装（本机）

```bash
# 1) 后端写服务 token（写入 $NOVEL_TTS_HOME/.env 后重启后端）
echo "DSH_SERVICE_TOKEN=$(openssl rand -hex 32)" >> "${NOVEL_TTS_HOME:-$HOME/.novel-tts}/.env"

# 2) 同一个值交给 DSH 进程（启动 DSH 前 export）
export AUDIO_BOOK_DSH_TOKEN=<同一个值>

# 3) 安装插件并重启 DSH web
dsh plugin --profile web add /opt/audio-book/audio-book/agent
```

插件暴露 8 个 `audiobook_*` 工具，并自带技能 `audiobook-pitch`（20 字卖点、喜马拉雅式标签、
方图封面 prompt、成本口径）；技能通过仓库根 `.dsh/skills/` 的符号链接被发现（DSH 默认技能根）。

### 数据模型

- `creation_jobs`：创作任务状态机（`draft → in_review → approved → building → ready →
  published`，另有 `failed / cancelled / blocked`），字段含 `payload_json`（阶段产物）、
  `budget_cny` / `spent_cny`；
- `agent_runs`：模型调用审计（角色、模型、prompt 版本、输入哈希、tokens、成本、输出）。

### 当前范围与后续

P0 已完成：桥接 API、两张表、插件与 8 个工具、技能、后端测试与插件端到端冒烟。
P1 待做：建项目 / 导入书库 / 启动构建 / 保存文案 / 质检 / 发布等写工具；代码化 workflow
（选题 → 文案 + 封面并行 → 人工闸门 → 构建 → 质检 → 发布）；DSH Agent 预设。

---

## AI 前沿日报（每日播客）

一级菜单「AI 前沿日报」（`#/daily`）：每天把 AI / 科技资讯采集成一期约 12 分钟的单人
口播节目，产出**口播稿 + 音频 + 节目简介**，人工终审通过后可对公网提供播客 RSS。

内容红线（与方案一致）：只做 AI / 科技前沿；只用自己的 RSS 与官方来源；不翻译、不
大段复述外媒；不做时政 / 军事 / 外交；开头带「本节目由 AI 生成」声明。

### 流水线（`backend/app/services/daily/`）

| 阶段 | 模块 | 做什么 | 失败策略 |
|---|---|---|---|
| 采集 | `collect.py` | 多源 RSS / Atom、24h 窗口、URL 去重 | 单源失败只记录，不阻塞整期 |
| 聚类 | `cluster.py` | 标题 token Jaccard（0.34）合并同一事件 | 宁拆勿并 |
| 打分 | `score.py` | 乘法打分 + 安全一票否决 + 主题闸门 | 0 分直接淘汰 |
| 脚本 | `script.py` | LLM 可插拔；关闭/失败时模板兜底 | 模板仍可出稿 |
| 跨期去重 | `score.py` + `store.previous_topics` | 近 7 天讲过的选题：URL 相同或标题相似度 ≥0.45 排除，0.30–0.45 降权 | 宁可少一条，不重复播 |
| 合成 | `tts.py` | 分段合成 + MP3 拼接 + 可选 `ffmpeg -16 LUFS` | 无 ffmpeg 跳过标准化 |
| 落盘 | `store.py` | 每期一个目录：`meta.json` / `script.md` / `shownotes.md` / `audio.mp3` | 失败不落半成品 |
| RSS | `feed.py` | RSS 2.0 + iTunes，enclosure 为绝对地址 | `?key=` 鉴权 |

三个关键设计：

1. **乘法打分**：`Score = Π(dim^w) × safety × demote`，任一项趋零整体归零；
2. **闸门分层**：标题命中主题词 → 通过；标题未命中时要求摘要命中 ≥2 个；
3. **ASCII 词边界**：`ai` / `llm` / `gpu` 按独立词匹配，不会命中 `said` / `email`。

### 信源（默认 26 个，海外为主）

科技新闻以海外信息源为主，默认信源里 **5 个中文 + 21 个海外**：

- 中文：量子位、雷峰网、爱范儿、钛媒体、开源中国
- 海外：TechCrunch AI、The Verge AI、MIT Tech Review AI、Wired AI、IEEE Spectrum AI、
  ScienceDaily AI、Ars Technica AI、Google AI Blog、DeepMind Blog、NVIDIA Blog、
  TechXplore、SyncedReview、The Decoder、MarkTechPost、Engadget、ZDNet AI、
  SiliconANGLE、The Next Web、TechRadar、AI Business、Analytics India Magazine

闸门规则：

- AI 垂类源：标题命中主题词，或摘要命中 ≥3 个主题词；
- 泛科技源（开源中国/爱范儿/钛媒体/TechXplore/NVIDIA/Engadget/TechRadar）：**只认标题命中**，
  避免摘要里的侧栏/标签把「网课系统更新」之类混进来；
- `开源` 这种过泛的词已从闸门词表移除（避免所有开源软件发版都命中）；
- 同一来源每期最多 2 条；多家外媒报道同一事件（共享 ≥2 个实体词，如 nvidia/jensen/huang）
  只保留一条。

### 跨期重复怎么办

同一件事常连续多天出现，处理分两层：

1. **当期内**：事件聚类合并（标题 token Jaccard ≥0.34）+ 实体去重；
2. **跨期**：生成前读取近 `DAILY_DEDUPE_DAYS`（默认 7）天的 `meta.json` 选题，
   - 相同 URL 或标题相似度 ≥0.45 → **直接排除**（`run_summary.stats.duplicates_removed` 计数）；
   - 相似度 0.30–0.45 → 降权 ×0.15（最多进速览，不抢主/次话题）；
   - 每条被排除的选题都会在 `meta.topics[].reasons` 里写明「近期已讲过（日期）：标题」。

### 音频命名规范

- 公开 URL：`/api/daily/issues/YYYY-MM-DD/audio.mp3`（播客客户端按 URL 末段取名）；
- 下载名（`Content-Disposition`）：`AI前沿日报_YYYY-MM-DD.mp3`，ASCII 回退
  `AI-daily-YYYY-MM-DD.mp3`；
- 详情页播放器下方有「下载音频（AI前沿日报_日期.mp3）」入口；
- RSS enclosure 同样用 `.mp3` + `?key=` 鉴权。

### 接口（`/api/daily/*`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/daily/config` | 节目信息、信源列表、LLM/TTS 配置状态、feed 是否开放 |
| GET | `/api/daily/issues?limit=` | 期数列表（状态：draft / approved / rejected） |
| GET | `/api/daily/issues/{date}` | 单期详情（简介 / 脚本 / 选题明细） |
| GET | `/api/daily/issues/{date}/audio` | 音频（支持 Range；JWT 鉴权） |
| POST | `/api/daily/issues/{date}/review` | 人工终审：`approve` / `reject` + 备注 |
| POST | `/api/daily/run` | 触发生成（后台任务；运行中返回 409） |
| GET | `/api/daily/run/status` | 阶段 / 进度 / 错误 / 日志尾 |
| GET | `/api/daily/feed.xml?key=` | 播客 RSS（公开，`?key=` 鉴权） |

### 使用

1. 设置页确认 TTS 厂商可用（日报复用平台 TTS；建议显式配置 `DAILY_TTS_VOICE_ID`）；
2. 进入「AI 前沿日报」→ 右上角「立即生成」→ 轮询进度（采集合成一般 1–3 分钟，
   取决于 TTS 速度）；
3. 生成完成后在详情页试听（音频在生成阶段一次性合成，播放走 Range 流式，
   不会边听边合成）、检查简介里的信源链接；播放器下方可直接下载
   `AI前沿日报_日期.mp3`；需要整期重做时点右上角「重生成本期」；
4. 人工终审：「通过」后状态变 `approved`；「退回」并写备注（可重跑覆写当日）；
5. 需要对外提供播客：配置 `DAILY_FEED_KEY` + `DAILY_PUBLIC_BASE_URL`，把
   `<你的域名>/api/daily/feed.xml?key=<key>` 提交到喜马拉雅「我有音频 RSS」等通道。
   **RSS 只包含人工终审通过（approved）的期数**；enclosure 音频同样用 `?key=` 鉴权，
   未审核期数通过订阅源访问会返回 403。

### 生成设置（页面内切换）

列表页右上角「生成设置」：

- **默认播报音色（多期稳定）**：只在这里设置一次。多期节目必须用同一音色，
  所以详情页不再提供逐期音色模块；选好后一般不需要再改。
  默认自动挑「新闻/播报/主播」类音色（当前 MiniMax 下即「新闻女声」）；选为空 = 恢复自动。
  列表支持搜索/点选，并保留一个「试听所选」（新闻口播样本，约 35 字，真实 TTS）；
  **逐条试听全部音色请去系统「音色库」（`#/voices`）**。偏好保存在
  `$DAILY_OUTPUT_DIR/prefs.json`，不需要重启。
- **信源**：内置源可勾选启停，也可以添加自定义 RSS（http(s)）；至少保留 1 个启用源。
  默认信源以**中文科技媒体**为主（量子位 / 雷峰网 / 爱范儿 / 钛媒体 / 开源中国），
  英文源（TechCrunch / The Verge / MIT Tech Review）权重更低，只做补充；
  专题内同一来源最多 2 条，避免变成单家媒体通稿联播。

### 熔断与恢复

- 连续 **2 期**生成失败（含进程重启导致的未完成），自动置 `circuit_open=true`，
  此时「立即生成」返回 409，页面出现红色熔断条；
- 排查后点页面「重置熔断」（或 `POST /api/daily/reset-circuit`）恢复；
- 成功一期即自动清零计数；
- 配置 `DAILY_ALERT_WEBHOOK` 后，每次失败/熔断会推送一条告警（失败推送尽力而为，
  不影响流水线状态；无人值守时建议配置）。

### 定时运行

1. 安装 systemd 单元（仓库自带模板）：

   ```bash
   cp deploy/audio-book-daily.service deploy/audio-book-daily.timer /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now audio-book-daily.timer   # 每天 03:00 Asia/Shanghai
   ```

2. 定时脚本 `deploy/daily-generate.sh` 用 `.env` 里的 `DSH_SERVICE_TOKEN`
   调 `POST /api/daily/run`（`X-DSH-Token` 鉴权），无需管理员密码；生成失败
   脚本退出码非 0，交给 systemd 记录。

> 自动任务**只生成、不发布**：发布前必须人工终审（方案 §四/§五）。若不希望无人值守
> 产生费用，跳过上面第 1 步即可，只用页面手动触发。

### 已知边界

- 服务器在国内，个别信源可能超时 / 403 / 429：`meta.stats.source_errors` 会列出来，
  可在 `DAILY_SOURCES_JSON` 里换源；
- 单期 TTS 成本约 2–5 元（按当前厂商价），音频 5–15 MB；
- 审核状态与产物在同一个目录里（`meta.json`），**不要手动删目录里的 meta**；
- 日报音频走对象存储/COS 属于 P3 范围，当前落本地盘，RSS enclosure 指向本服务；
  桶未配置公开域名时不能直接用 COS 直链（私有桶签名 1 小时会过期）。
- 海外源占比高时，主话题可能是英文报道；脚本由 LLM 译写为中文，
  人工终审时重点核对专有名词与数字（防止转述失真）。
- 跨期去重依赖历史 `meta.json`；如果手动删掉某期目录，那期的选题不再参与去重。
- `:fire:` 之类的标题短代码、`【重磅】` 前缀会在采集阶段清洗掉。

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
| 未配置 / 错误 `X-DSH-Token` 访问 `/api/agent/*` | 请求 | 503（未配置）/ 401 |
| 插件密钥泄漏 | 响应 | `/api/agent/*` 只返回业务字段，绝不回显 S3/LLM Key |

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

1. **TTS 音频按帧拼接**：章节 MP3 会剔除段内 ID3 标签及尾部元数据，清除失效的段级时长索引，并保持同一厂商预期的采样率和声道模式。若上游返回不同音频配置，MiniMax 构建会明确失败，避免交付浏览器无法播放的音频。
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
- 切章在本地执行，不消耗 LLM tokens
- 音色推荐 1 次：in 3k / out 0.5k → 3.5k
- **合计 ≈ 26k tokens × ¥15/1e6 ≈ ¥0.39**（每章，仅供粗估）

### TTS（以官方实时价格为准）

豆包估算按约 **0.0003 元/字**；MiniMax `speech-2.8-hd` / `speech-2.8-turbo` 分别约 **¥3.5 / ¥2.0 每万计费字符**，汉字按两个计费字符估算。构建弹窗仅对豆包展示原有费用估算；MiniMax 请以官方账单为准。
按「文本字符数（含标点）」计费，基数就是实际下发给 TTS 的正文/对白/标题文本：
- 3000 字小说 ≈ **¥0.9**
- 10 万字中篇 ≈ **¥30**

> - 逐段**语音指令**（`context_texts`）的文字官方明确**不计费**，写多长都不涨钱。
> - 单价可在 `DOUBAO_TTS_PRICE_PER_CHAR` 调整；「合成预估」面板会直接显示预估费用。
> - 段级缓存命中的段落不重复调用上游，实际费用只会更低。

**合计 3000 字约 ¥1.3**（LLM 粗估 ¥0.39 + TTS 粗估 ¥0.9）。长文建议：
- 识别前检查章节标题；异常长的单元会按段落拆到约 3 万字符以内
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

`deploy.sh` 要求工作区没有未提交修改，只接受 `origin/main` 的快进更新；随后执行 `npm ci`、`npm run build`。如果 `audio-book.service` 已启用，它会先停 systemd 服务、安装后端依赖、再启动服务；未启用 systemd 时回退到 `start.sh --restart`。最后按 `.env` 中的监听地址和端口轮询 `/api/health`。这台服务器的 `/opt/audio-book/reinstall.sh` 是调用仓库内 `deploy.sh` 的本地入口。

手动升级时也要重建前端；systemd 托管时用 `systemctl restart`：

```bash
git pull --ff-only origin main
(cd frontend && npm ci && npm run build)
sudo systemctl restart audio-book
# 未使用 systemd 时： ./start.sh --restart
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

### systemd 托管（推荐，支持崩溃自愈与开机自启）

仓库自带 `deploy/audio-book.service`，使用 `Restart=always` 和
`WantedBy=multi-user.target`：

```bash
sudo cp deploy/audio-book.service /etc/systemd/system/audio-book.service
sudo systemctl daemon-reload
sudo systemctl enable --now audio-book

systemctl status audio-book          # 当前状态 / 最近日志
journalctl -u audio-book -f          # 实时日志
sudo systemctl restart audio-book    # 重启
sudo systemctl stop audio-book       # 停止
```

`ExecStart` 使用 `start.sh --systemd`：前台 `exec` uvicorn，跳过
`pip install` 和前端构建（升级时由 `deploy.sh` 负责），也不写
`uvicorn.pid`。启用 systemd 后请优先使用 `systemctl` 管理，
不要再用 `./start.sh --stop/--restart`，避免和 `Restart=always` 互相打架。

### 直接使用 start.sh（未启用 systemd 时）

```bash
./start.sh              # 后台启动（默认）
./start.sh --foreground # 前台启动（Ctrl+C 停止）
./start.sh --systemd    # 供 systemd 调用：前台运行，跳过安装/构建
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
