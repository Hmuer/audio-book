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
│   │   ├── db/
│   │   │   ├── models.py           # users / projects / builds / build_artifacts / project_characters / project_dialogues / project_pronunciation_rules
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

音色库、项目音色配置与新构建弹窗直接显示音色名称，不再在名称前重复放大首字。新构建弹窗中的默认男声、默认女声和旁白音色均可试听；试听使用当前设置的语速，播放与其他试听共享同一个播放器。

---

## 前端 UI 步骤

| Step | 关键组件 |
|---|---|
| **项目列表** | ProjectCard（封面 / 章节数 / 上次构建状态 / 删除按钮） |
| **Step 1 输入** | 上传 TXT / Markdown / EPUB 或粘贴文本；文件按 50MB 流式限额读取，粘贴内容按 UTF-8 字节数限制为 50MB。无效文件在保存前返回 400，可在原项目中重试导入；详情页可替换源文件并重新识别 |
| **Step 2 角色+音色** | diff 对比面板 + polish_warning 黄色警告条 + 角色卡片（性格/标签 + LLM 推荐理由 + 试听） + 内置音色搜索 |
| **Step 3 生成+试听** | 章节/构建产物统一为播放列表（每章播放、MP3、LRC）；点击播放才签发当前章 URL；页面底部固定播放器（暂停/播放、进度拖动、音量、总时长/已播放时长）；概览右侧同样显示章节播放列表 |
| **设置** | ProviderModelsEditor（多厂商卡片化：厂商开关 ToggleSwitch / API Key 输入（脱敏显示 + 「重写」）/ 模型列表） + LLM / TTS 激活厂商 + 模型指针 |

章节列表不再为每条渲染独立 `<audio>`，也不在展开时批量签发媒体 URL；只有点击播放/下载某一章时才请求该章资源，避免对象存储产生额外的预取/缓存流量。

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
