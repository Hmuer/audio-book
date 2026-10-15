# AI 有声小说生成器

将小说文本整理为项目，通过角色与对白识别、音色分配和多音色 TTS 生成章节 MP3、LRC 字幕及 ZIP 下载包。后端为 FastAPI，前端为 Next.js 静态导出；默认由一个 Uvicorn 进程同时提供 API 和前端页面。

## 环境要求

- Python 3.11+，Node.js 18+（Node 仅用于构建前端）。
- macOS 或 Linux；Windows 建议使用 WSL2。
- 实际合成需要可用的 LLM 和 TTS 服务凭据。仅安装依赖或填写占位 Key 不能调用厂商。

## 快速启动

在项目根目录执行：

```bash
./start.sh
./start.sh --status
```

默认后台启动，访问 <http://127.0.0.1:28000/>。首次启动会将根目录 `.env.example` 复制到 `$NOVEL_TTS_HOME/.env`（默认 `~/.novel-tts/.env`），创建 `$NOVEL_TTS_HOME/.venv`，安装后端运行依赖；如果 `frontend/out/index.html` 不存在，还会安装前端依赖并构建静态页面。请在启动前或首次启动后填写凭据；修改 `.env` 后需重启。通过 `./start.sh --paths` 查看实际路径，日志默认位于 `$NOVEL_TTS_HOME/data/logs/`。

常用命令：

```bash
./start.sh --foreground
./start.sh --stop
./start.sh --restart
./start.sh --status
./start.sh --paths
./start.sh --help
```

`start.sh` 默认监听 `127.0.0.1:28000`，使用单个 worker。不要直接把 `BIND_HOST` 改成公网地址并使用默认账号、默认密钥对外提供服务。`/api/health` 可检查服务状态，`/docs` 提供 API 文档。

### 配置厂商与安全凭据

`start.sh` 使用 `$NOVEL_TTS_HOME/.env`；项目根目录 `.env` 仅用于旧部署首次迁移。可先在该文件中配置 `LLM_API_KEY`、`LLM_BASE_URL` 等兼容字段，再由管理员登录「设置」页维护各厂商凭据、模型和激活指针。设置保存的厂商配置也会持久化到运行时数据目录；注意保护 `.env`、数据库及包含明文凭据的配置文件。

- LLM：支持配置 MiniMax 与 OpenAI 兼容端点等；实际请求使用激活的 LLM 厂商和模型。
- TTS：支持豆包和 MiniMax。激活的 TTS 厂商决定默认合成、音色列表、推荐候选池及旁白兜底；明确带厂商前缀的音色 ID 仍可按对应厂商路由。MiniMax 默认模型为 `speech-2.8-hd`，支持内置普通话音色与符合中文特征的远端音色。
- MiniMax TTS 不支持逐段语音指令，prepare 会跳过该阶段；切回支持指令的豆包并重新执行 prepare 时会补生成。
- 应用**不提供 ICL 声音复刻训练**。豆包官方系统音色 ID 中的 `ICL_` 字样不代表训练入口仍存在。

生产环境使用 `ENV=prod`，设置强随机 `JWT_SECRET`、非默认管理员凭据，并保持 `DISABLE_AUTH=false`。启动会拒绝 `change-me*` 密钥、`admin/admin` 凭据组合及生产环境关闭认证；建议密钥至少 32 字符，但当前代码没有单独的长度门槛。新安装开发环境的种子账号为 `admin/admin`，请及时修改密码；已有管理员不会因修改 `SEED_ADMIN_PASS` 而自动重置密码。

`DATA_DIR`、`AUDIO_DIR`、`DATABASE_URL` 和 `LOG_FILE` 在通过 `start.sh` 启动时会强制指向 `$NOVEL_TTS_HOME` 下的路径；如需整体迁移运行数据，请设置 `NOVEL_TTS_HOME`，不要仅在 `.env` 中覆盖这四项。直接运行后端而不使用启动脚本时，路径取决于相应环境变量及后端默认值。

## 从文本到音频

1. **创建并导入项目**：上传 `.txt`、`.md`、`.epub`，或粘贴文本；上传文件按字节分块读取，默认上限为 50 MiB。EPUB 需能够成功解析为正文。粘贴文本通过单独接口提交，限制与文件上传的字节计数不同。
2. **开始识别（prepare）**：按配置的正则识别原文章节标题；默认命中不足会提示调整规则，不自动调用 LLM 分章。可选择开启润色纠错（默认关闭）；角色识别、对白归属和音色推荐使用 LLM。后台保存进度和 checkpoint，可在异常或重启后恢复。默认 TTS 不支持语音指令时，该阶段标记为跳过。
3. **校对内容和音色**：在项目工作台查看章节、角色、对白和推荐音色；可以手动调整角色音色、旁白及发音规则。构建弹窗可指定默认男声／女声：未分配角色依性别选择，未知性别或未知说话人优先用默认男声。预填时会避开已选旁白音色；旧构建没有默认音色时保留原有回退行为。
4. **开始构建**：当前新构建使用 `classic` 模式；合成任务在后台运行，列表与详情轮询进度。可从构建行执行暂停、继续和停止，也可以对失败章节发起新构建重试。
5. **试听与下载**：提供章节 MP3、章节 LRC 及 ZIP。ZIP 默认每 50 章一卷；单卷未必覆盖全书，按页面显示的章节范围下载。失败章节可能包含占位音频，应查看构建结果。

### 暂停、继续与停止

- **暂停**会先显示“暂停中”，允许正在处理的章节或 ZIP 分片完成；worker 退出后显示“已暂停”。暂停状态会持久化，不会因服务重启自动继续。
- **继续**沿用同一个构建和已有产物；若原文、对白、相关配置或实际 TTS 模型等输入已改变，会拒绝继续，以免混合不同版本的音频。
- **停止**需要确认，最终状态为“已停止”；已完成产物仍可查看，但原构建不能再继续。停止与暂停均不是即时取消厂商请求：在途合成可能继续完成并产生费用。
- 暂停中的项目禁止修改影响合成的输入或新建构建；需要先继续或停止现有构建。停止不同于删除记录，也不同于创建失败章节的重试构建。

## 存储、权限与费用

默认 `STORAGE_BACKEND=local`，数据库、上传文件和交付物位于 `$NOVEL_TTS_HOME/data/`；TTS 段缓存位于音频目录下，可在后续构建中复用。还可配置 S3 兼容对象存储（包括腾讯云 COS）归档 MP3、LRC 和 ZIP。对象存储模式可能在归档成功后清理本地交付文件；下载使用配置的公有读链接时**不经过本地 `/media` 的登录鉴权**，请在启用前评估桶的读取权限及内容敏感性。

项目按所有者隔离，管理员可以管理其他用户的项目；本地媒体访问结合资源归属与签名 URL。不要直接公开运行目录中的数据库、上传文本、缓存或厂商配置文件。

费用由实际激活的 LLM／TTS 服务、模型、请求及其计费规则决定。逐段语音指令本身是否计费应以厂商账单为准，生成指令的 LLM 调用也可能产生费用。MiniMax TTS 用量统计优先使用响应中的 `usage_characters`，缺失时不把文本长度冒充账单字符数。段缓存命中不会再次请求厂商；暂停或停止前的在途调用仍可能计费。请以厂商官方价格和账单为准，不要把应用预估当成最终账单。

## 升级与备份

运行数据默认在项目目录**外**的 `$NOVEL_TTS_HOME`（默认 `~/.novel-tts`）：

```text
$NOVEL_TTS_HOME/
├── .env
├── .venv/
└── data/
    ├── app.db
    ├── providers_config.json
    ├── audio/
    ├── logs/
    └── uvicorn.pid
```

升级前停止服务并备份 `.env`、数据库及所需音频；SQLite 使用 WAL 时应在停服后连同相关数据库文件备份。**不要依赖 `git pull` 代替备份。**

```bash
./start.sh --stop
# 在此备份 $NOVEL_TTS_HOME
# git pull
cd frontend && npm ci && npm run build
cd ..
./start.sh
```

已有 `frontend/out/index.html` 时，`start.sh` **不会自动重新构建前端**；前端代码升级后需手动执行上面的构建命令。`start.sh` 会安装 `backend/requirements.txt`，不使用 `backend/requirements.lock`。旧部署项目根目录的 `.env` 可在首次运行时迁移；如果旧数据仍在项目内 `./data/`，应在停服和备份后确认目标路径并迁移，再用 `./start.sh --paths` 核实路径。

旧版 ICL 训练记录表 `icl_training_tasks` 会在数据库初始化迁移中删除；如需保留历史记录，务必在升级前备份数据库。不要使用 `rm -rf` 或 `git reset --hard` 作为常规升级步骤，以免误删项目内尚未迁移的数据或未提交改动。

## 开发与验证

在项目根目录、已经安装后端开发依赖的 Python 环境中运行：

```bash
python -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
TTS_PROVIDER=mock LLM_PROVIDER=mock ENV=dev python -m pytest backend/tests -q
```

如使用启动脚本创建的虚拟环境，可用 `$NOVEL_TTS_HOME/.venv/bin/python` 替换上述 `python`。前端检查：

```bash
cd frontend
npm ci
node --test tests/*.test.mjs
npx tsc --noEmit
npm run build
```

CI 中的后端 pytest 与 Python `pip-audit` 会运行；Ruff 未配置规则时跳过，前端类型检查和 npm 审计当前不作为阻断项，现有前端 `node:test` 测试也未被 CI 的 `npm test` 步骤自动执行。`npm run lint` 目前缺少 ESLint 配置，首次调用会要求交互初始化。不要将这些步骤理解为 CI 已全部严格验证。

## 实现边界

- 任务执行仍依赖进程内 `asyncio` worker；虽然构建记录、prepare checkpoint、JobTask 和看门狗支持一定程度的重启恢复，**不等于**多实例分布式任务队列或即时中断正在请求的厂商。
- 小说章节切分依赖标题规则；无匹配时需补充规则或显式开启按字数硬切回退。润色默认关闭，EPUB 解析和 LLM 输出也可能失败，需要在页面查看错误并修正输入或配置。
- 浏览器下载的是已完成的章节／ZIP，不提供边合成边播放整章的实时流。部分 TTS 上游接口可能使用流式协议，与页面实时播放是两回事。
