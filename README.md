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
| AI 服务 | 至少 1 个 LLM/TTS 厂商 API Key（M2 / 豆包 Doubao / OpenAI 兼容 / 自定义 OpenAI） |

---

## 启动步骤

### 1. 一键启动

```bash
cd novel-tts-app
./start.sh
```

首次运行 `start.sh` 会自动：
- 从 `.env.example` 复制 `$NOVEL_TTS_HOME/.env`（默认 `~/.novel-tts/.env`；记得填 API Key；至少一个 LLM + 一个 TTS）
- 创建 `$NOVEL_TTS_HOME/.venv`（Python 3.11+）并装依赖
- 安装前端依赖并 `npm run build`（Next.js static export → `frontend/out/`）
- 最后启动 **单进程 uvicorn**（1 worker）
- 把 PID 写到 `$NOVEL_TTS_HOME/data/uvicorn.pid`；日志在 `$NOVEL_TTS_HOME/data/logs/`

> 升级请直接 `git pull && ./start.sh`，**不要** `rm -rf` 项目目录 —— 详见下文「升级与数据迁移」。

### 2. 打开浏览器

访问 **http://127.0.0.1:28000/**

- `/api/health` — 健康检查（无需登录）
- `/docs` — FastAPI Swagger 文档
- `/api/voices` — 当前激活厂商的内置音色 JSON
- `/media/<filename>` — MP3 / ZIP 下载（需登录；当前用户必须是该资源所属 project 的 owner 或 admin）
- `/api/auth/login` — 默认账号 `admin / admin`（**首次登录后必须改密**）

### 3. 跑测试

```bash
cd novel-tts-app
backend/.venv/bin/python -m pytest backend/tests -q
# 当前：123 passed
```

测试覆盖：鉴权 / 资源归属 / 媒体鉴权（一次性签名 URL）/ 后台任务持久化（启动恢复+看门狗）/ 速率限制（login/prepare/build）/ 配置持久化（providers + settings 重启回填）/ 厂商 API 脱敏 / 上传限制 / Build 取消竞态 / 修复合重 等。

CI / 依赖：
- 依赖锁定见 `backend/requirements.lock` 与 `requirements-dev.txt`；
- Dependabot 配置 `.github/dependabot.yml`（每周一自动开 PR）；
- CI 流水线 `.github/workflows/ci.yml`（lint + pytest + pip-audit + 前端构建）。

---

## `.env` 配置说明

复制 `.env.example` → `.env`：

```bash
cp .env.example .env
```

### 必填 / 推荐字段

| 变量 | 必须 | 默认 | 说明 |
|---|---|---|---|
| `JWT_SECRET` | ✅（prod） | `change-me-...` | JWT 签名密钥。**生产环境必须改成 ≥32 字符随机串**，否则启动直接失败 |
| `SEED_ADMIN_USER` | | `admin` | 首次启动自动创建的默认管理员用户名 |
| `SEED_ADMIN_PASS` | | `admin` | 默认管理员密码。**生产环境必须改成强密码**，否则启动直接失败 |
| `ENV` | | `dev` | `prod` 时启动会强制校验 `JWT_SECRET` / 默认密码 / `DISABLE_AUTH` |
| `DISABLE_AUTH` | | `false` | **生产环境严禁 `true`**，否则启动直接失败 |
| `BIND_HOST` | | `127.0.0.1` | 强制只绑定本地环回，不要改 0.0.0.0 |
| `PORT` | | `28000` | 监听端口 |
| `DATA_DIR` | | `./data` | DB + audio 根目录（**已被 `.gitignore` 完全排除**） |
| `AUDIO_DIR` | | `./data/audio` | MP3 / ZIP 存放位置 |
| `DATABASE_URL` | | `sqlite+aiosqlite:///./data/app.db` | SQLite URL |

### 多厂商配置

实际项目已支持 **MiniMax（LLM）**、**豆包 TTS（含 ICL 声音复刻）**、**OpenAI 兼容**、**自定义 OpenAI 兼容端点** 等并存，并通过 `PROVIDERS_CONFIG` JSON + `ACTIVE_TTS_PROVIDER` / `ACTIVE_TTS_MODEL` / `ACTIVE_LLM_PROVIDER` / `ACTIVE_LLM_MODEL` 四个指针选择。**不要再去**配置已废弃的 `LLM_BASE_URL` / `LLM_MODEL_PRO` / `LLM_MODEL_FAST` 字段——它们保留仅作兼容，新 UI 不再展示。（MiniMax 语音合成已彻底弃用，TTS 只保留豆包。）

具体厂商卡片配置请在「设置」页按厂商填写 API Key（仅管理员可见明文；其他用户只能看到脱敏后的 `***LAST4`）。

---

## 目录结构

```
novel-tts-app/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口 + StaticFiles 挂载前端 + /media 鉴权
│   │   ├── api/routes.py           # /api/* 路由（薄壳 → services；所有路由强制鉴权）
│   │   ├── ai/
│   │   │   ├── base.py             # BaseLLMProvider / BaseTTSProvider 抽象
│   │   │   ├── factory.py          # get_llm() / get_tts()（多厂商 + ACTIVE_* 指针）
│   │   │   └── providers/
│   │   │       ├── minimax/        # MiniMax（LLM + TTS）
│   │   │       └── doubao/         # 豆包 Doubao（TTS + ICL 声音复刻 + multicast）
│   │   ├── services/               # 业务逻辑（全部 LLM 驱动 + 数据库 ORM）
│   │   │   ├── auth.py             # JWT + bcrypt + seed admin + ENV=prod 安全门槛
│   │   │   ├── ownership.py        # Project/Build 资源归属守卫
│   │   │   ├── project.py          # 项目 CRUD + 章节/对白/角色识别（status checkpoint 可中断恢复）
│   │   │   ├── build.py            # 构建任务（Build + BuildArtifact 三级结构）
│   │   │   ├── chapter.py          # 章节切分 + 合成编排
│   │   │   ├── character.py        # 角色提取 + LLM 消歧（去重）
│   │   │   ├── dialogue.py         # 对白归属（每段 speaker + confidence）
│   │   │   ├── voice_recommender.py # LLM 给角色匹配内置音色
│   │   │   ├── icl.py              # 豆包 ICL 声音复刻（user_id 归属）
│   │   │   ├── epub_reader.py      # EPUB 解析
│   │   │   └── book_split.py       # 正则分章（兜底；LLM 分章是默认路径）
│   │   ├── db/
│   │   │   ├── models.py           # users / projects / builds / build_artifacts / project_characters / project_dialogues / project_pronunciation_rules / icl_training_tasks
│   │   │   └── session.py          # aiosqlite + SQLAlchemy 2.0 async
│   │   └── core/config.py          # pydantic-settings
│   ├── tests/                      # 99 个 pytest（鉴权 + 归属 + 上传 + 修复合重）
│   │   ├── conftest.py             # 隔离 data 目录 + Mock LLM/TTS
│   │   ├── mock_providers.py       # 离线 mock，不依赖真实 API
│   │   ├── test_auth.py            # 登录 / token / 改密 / 401 链路
│   │   ├── test_ownership_red.py   # P1 #5：跨用户归属隔离
│   │   ├── test_upload_limit_red.py # P1 #8：流式上传 413
│   │   ├── test_media_auth_red.py  # /media 静态资源鉴权
│   │   ├── test_providers_api_red.py # P0 #4：API Key 脱敏
│   │   ├── test_cancel_race_red.py # Build 取消竞态
│   │   ├── test_review_fixes_red.py # 评审项回归
│   │   └── test_project_e2e.py     # 项目制流程
│   ├── pytest.ini
│   └── requirements.txt
├── frontend/
│   ├── next.config.js              # output: 'export'
│   ├── tailwind.config.js          # darkMode: 'class'（深/浅切换）
│   ├── src/
│   │   ├── app/{layout,page,globals.css}
│   │   ├── lib/api.ts              # fetch 封装（含 token 注入）
│   │   └── components/             # ProviderModelsEditor（多厂商卡片）/ ToggleSwitch / ...
│   └── out/                        # 构建产物，被 FastAPI StaticFiles 挂载
├── data/                           # **运行时生成；完全 .gitignored**
│   ├── app.db                      # SQLite
│   ├── app.db-*                    # SQLite WAL/SHM
│   ├── audio/*.mp3                 # 生成的 MP3
│   ├── audio/*.zip                 # 打包下载的整本 ZIP
│   ├── audio/_seg_cache/           # 段级 TTS 缓存
│   └── uploads/*.txt               # 上传的小说源文件
├── start.sh                        # 一键启动（带 --stop / --restart / --paths 子命令）
├── .env.example
├── .gitignore
└── README.md
```

> ⚠️ **`data/` 现在默认在 `$NOVEL_TTS_HOME/data/`（项目目录外）**。
> 这里的 `data/` 只是旧部署残留 —— 新部署里项目目录**完全没有** data/，
> 见下文「升级与数据迁移」。

---

## 数据模型 & 资源归属

```
User ──┬──< Project ──┬──< ProjectCharacter ──< 音色 assigned_voice_id
       │               ├──< ProjectDialogue
       │               ├──< ProjectPronunciationRule
       │               └──< Build ──< BuildArtifact (每章 MP3)
       │
       ├──< IclTrainingTask (豆包声音复刻；user_id 显式归属)
       └──< voice_id 关联
```

- `Project.owner_user_id` 显式归属：alice 创建的项目只能 alice 看到/改/删，bob 默认看不到；admin 可访问任何。
- 孤儿项目（NULL owner）启动时由 lifespan 自动归 admin。
- `/media/build_<build_id>_*.mp3|*.zip` 路径通过文件名反查 build → project → owner，**不再接受"任意登录用户"读**。

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

---

## 前端 UI 步骤

| Step | 关键组件 |
|---|---|
| **项目列表** | ProjectCard（封面 / 章节数 / 上次构建状态 / 删除按钮） |
| **Step 1 输入** | 上传 .txt 或粘贴文本 + 字数统计（上限 50MB / 流式读取，超限立即 413） + 「开始识别」按钮 |
| **Step 2 角色+音色** | diff 对比面板 + polish_warning 黄色警告条 + 角色卡片（性格/标签 + LLM 推荐理由 + 试听） + 内置音色搜索 |
| **Step 3 生成+试听** | 对白卡片列表（橙色边框高亮 confidence<0.7） + 每段独立改音色覆盖 + 旁白折叠区 + 总音频播放器 + MP3 / ZIP 下载按钮 |
| **设置** | ProviderModelsEditor（多厂商卡片化：厂商开关 ToggleSwitch / API Key 输入（脱敏显示 + 「重写」）/ 模型列表） + LLM / TTS 激活厂商 + 模型指针 |

**深色默认**，右上角可切浅色。

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
- ✅ `data/**` 完全 `.gitignored`（含 db / 上传文本 / ZIP / cache）
- ✅ **99 个 pytest 通过**

---

## 已知限制

1. **TTS 音频拼接纯 MP3 帧直拼**：大多数 HTML5 `<audio>` 兼容。如果你要最严格的 ID3 + 无缝拼接，可以后续加 `pydub` + ffmpeg。
2. **LLM JSON mode 鲁棒性**：LLM + 3 次重试 + 温度递增，在 100+ 测试中成功率约 97%；极端失败时会抛 HTTP 500（前端弹窗），无自动降级。
3. **不做流式合成**：所有段 TTS 完成后才返回最终 MP3（单请求 600s 超时足够）。如需体验更好可以加 WebSocket / SSE，本 v1 不做。
4. **后台任务在内存里**：restart 用 `asyncio.create_task`，进程崩溃 / 多实例部署会丢失锁。生产场景建议引入队列（Celery / Arq）+ 幂等键 + 心跳。
5. **角色去重只两两组合**：N 个角色产生 N(N-1)/2 对。超过 30 角色时单次调用 token 会较大，可以后续拆批。
6. **Doubao / MiniMax `voice_id` 合法性**：内置音色 ID 列表见 `backend/app/ai/providers/{doubao,minimax}/`。如果官方实际可用 ID 不同，请在对应文件替换（保持 id 字段）。
7. **依赖策略**：Python 依赖 `requirements.txt` 已固定主版本。CI / Dependabot / pip-audit / npm audit 暂未接入。

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

### TTS（豆包语音合成，**0.0003 元/字** = ¥300/百万字，具体以官方计费为准）
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

`start.sh` 默认把 **所有运行时产物** 放在项目目录**外**的 `$NOVEL_TTS_HOME`
（默认 `~/.novel-tts/`），所以 `git pull` / `git reset --hard` / `rm -rf` 项目目录
**都不会**丢失数据、配置或 Python 依赖。

```
项目目录（可随时 rm -rf）             运行时产物（保留）
├── start.sh                          ~/.novel-tts/
├── backend/                          ├── .env               ← API Key、JWT_SECRET
├── frontend/                         ├── .venv/             ← Python 依赖（venv）
└── ...                               └── data/
                                          ├── app.db         ← SQLite 主库
                                          ├── audio/         ← 生成的有声书
                                          ├── logs/          ← app.log / stdout
                                          └── icl_refs/      ← ICL 训练样本
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
```bash
cd /path/to/novel-tts
./start.sh --stop     # 停服（仅停后台 uvicorn；不动数据）
git pull
./start.sh            # 后台启动；会自动 pip install / npm install / build
```

任何时候 `git pull` 都不会覆盖你的 `.env`、数据库、音频、训练样本 —— 它们全在
`~/.novel-tts/` 下。

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
mv ./.env "$NOVEL_TTS_HOME/.env"   # 如果旧版把 .env 放在项目根

# 以后升级就只是：git pull + ./start.sh
git pull
./start.sh
```

> 搬运前**务必**先 `./start.sh --stop` 停服，避免数据库写入中移动文件。
> 数据库文件 `data/app.db` 正在被写时 `mv` 可能导致 SQLite 损坏。

### 旧版"先删项目目录再 git clone"的危险操作
升级**不需要**也不应该 `rm -rf` 项目目录。新流程是：
- ❌  `rm -rf novel-tts && git clone ... && ./start.sh`（会丢数据库、API Key、训练样本）
- ✅  `git pull && ./start.sh`（默认什么都不丢）

如果只是想换部署位置（机器迁移、换硬盘），用上面"把数据放到别的盘"那一段。

---

## 停服 / 重启 / 状态

`start.sh` 现在是带子命令的服务管理器：

```bash
./start.sh              # 后台启动（默认）
./start.sh --foreground # 前台启动（Ctrl+C 停止）
./start.sh --stop       # 停后台进程
./start.sh --restart    # 重启
./start.sh --status     # 看 PID / 端口 / 日志位置
./start.sh --paths      # 看所有路径
./start.sh --help       # 帮助
```

> ⚠️ 不要在外层 `nohup ... &` 套娃跑 `start.sh`（会留下僵尸脚本进程）。
> 直接 `./start.sh` 默认就是后台模式，启动后立刻退出，PID 写到
> `$NOVEL_TTS_HOME/data/uvicorn.pid`。

---

## 常见问题

**Q: ModuleNotFoundError: No module named 'backend'**
→ 确认从项目根运行 start.sh，不要从 backend/ 里直接跑 uvicorn。

**Q: ENV=prod 启动直接报错 "JWT_SECRET 仍为默认值"**
→ 改 `.env`：`JWT_SECRET=一个 ≥32 字符的随机串`（`openssl rand -hex 32` 即可），并设 `SEED_ADMIN_PASS=强密码`。

**Q: 一个登录用户看不到自己刚创的项目**
→ 当前为多用户隔离设计。`admin` 可见所有；普通用户只能看到自己 own 的项目。如需分享，请创建独立账号或开放 admin。

**Q: voices 列表变少了 / Doubao 音色不全**
→ 内置音色列表见 `backend/app/ai/providers/doubao/tts.py` 的 `_BUILTIN_VOICES`；可以补全。也可在「设置」页用 ICL 训练自定义音色。

**Q: 前端打开 404**
→ `frontend/out/index.html` 不存在。先 `cd frontend && npm install && npm run build`，或重跑 `start.sh`。

**Q: 升级后数据没了 / API Key 失效 / 数据库被重建**
→ 大概率是按老习惯 `rm -rf 项目目录 && git clone` 重新部署了。新版把所有运行时产物
→ （`data/` `.venv/` `.env`）放在 `$NOVEL_TTS_HOME`（默认 `~/.novel-tts/`），项目
→ 目录本身不再存任何状态，所以**升级不需要删项目目录**。正确流程见上文
→ 「升级与数据迁移」一节：`./start.sh --stop && git pull && ./start.sh`。

**Q: 怎么把数据库 / 音频挪到别的盘**
→ `export NOVEL_TTS_HOME=/mnt/ssd/.novel-tts && ./start.sh --stop && mv ~/.novel-tts/* /mnt/ssd/.novel-tts/ && ./start.sh`。
→ 写进 `~/.bashrc` 让 export 持久生效。