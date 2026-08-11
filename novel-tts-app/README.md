# AI 有声小说生成器 (Novel TTS App)

**LLM 优先** 架构的中文有声小说一键生成器。输入小说文本 → LLM 纠错/识别角色/标对白/分章/推音色 → 多音色 TTS → MP3。

一条命令启动，单进程、单端口、只监听 127.0.0.1。

---

## 环境要求

| 组件 | 最低版本 |
|---|---|
| Python | **3.11+**（推荐 3.11 / 3.12，本地用 pyenv 安装最稳） |
| Node.js | **18+**（只用于构建前端静态产物，运行时无 Node 进程） |
| OS | macOS / Linux（Windows 可用 WSL2） |
| AI 服务 | MiniMax API Key（LLM + TTS 已在同一平台） |

---

## 启动步骤

### 1. 一键启动

```bash
cd novel-tts-app
./start.sh
```

首次运行时 `start.sh` 会自动：
- 从 `.env.example` 复制 `.env`（记得填 API Key）
- 创建 `backend/.venv`（Python 3.11+）并装依赖
- 安装前端依赖并 `npm run build`（Next.js static export → `frontend/out/`）
- 最后启动 **单进程 uvicorn**（1 worker）

### 2. 打开浏览器

访问 **http://127.0.0.1:8000/**

- `/api/health` — 健康检查
- `/docs` — FastAPI Swagger 文档
- `/api/voices` — 56 个音色 JSON
- `/media/<filename>` — MP3 下载

### 3. 跑 pytest

```bash
cd novel-tts-app
backend/.venv/bin/python -m pytest backend/tests -v
# 3 passed
```

---

## `.env` 配置说明

复制 `.env.example` → `.env`：

```bash
cp .env.example .env
```

核心字段：

| 变量 | 必须 | 默认 | 说明 |
|---|---|---|---|
| `LLM_API_KEY` | ✅ | — | MiniMax LLM Key（`sk-cp-...`） |
| `TTS_API_KEY` | ✅ | — | MiniMax TTS Key（通常与 LLM 相同） |
| `LLM_BASE_URL` | ✅ | `https://api.minimaxi.com/v1` | MiniMax API Base |
| `TTS_BASE_URL` | ✅ | `https://api.minimaxi.com/v1` | TTS Base |
| `LLM_MODEL_PRO` | | `MiniMax-M3` | **默认主模型**（Pro 级，质量优先） |
| `LLM_MODEL_FAST` | | `MiniMax-M2.7-highspeed` | 辅助模型（仅未来批处理用，当前默认全走 M3） |
| `BIND_HOST` | | `127.0.0.1` | **强制只绑定本地环回**，不要改 0.0.0.0 |
| `PORT` | | `8000` | 监听端口 |
| `DATA_DIR` | | `./data` | DB + audio 根目录 |
| `AUDIO_DIR` | | `./data/audio` | MP3 存放位置 |
| `DATABASE_URL` | | `sqlite+aiosqlite:///./data/app.db` | SQLite URL |
| `LLM_TIMEOUT` | | `600` | LLM 调用超时（秒），已统一 600s |
| `TTS_TIMEOUT` | | `600` | TTS 调用超时（秒） |
| `UVICORN_TIMEOUT` | | `600` | uvicorn keep-alive 超时（秒） |

---

## 目录结构

```
novel-tts-app/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口 + StaticFiles 挂载前端
│   │   ├── api/routes.py           # /api/* 路由（薄壳 → services）
│   │   ├── ai/
│   │   │   ├── base.py             # BaseLLMProvider / BaseTTSProvider 抽象
│   │   │   ├── factory.py          # get_llm() / get_tts()
│   │   │   └── providers/minimax/
│   │   │       ├── llm.py          # MiniMax LLM JSON mode + 3次重试
│   │   │       ├── tts.py          # MiniMax TTS /v1/tts/stream + 静音帧拼接
│   │   │       └── voices.json     # 56 个中文预置音色（男/女/中性 × 各年龄段）
│   │   ├── services/               # **业务逻辑：全部 LLM 驱动，0 启发式**
│   │   │   ├── polish.py           # 纠错 + 自我评估 is_reasonable
│   │   │   ├── character.py        # 角色提取 + 消歧（去重）
│   │   │   ├── dialogue.py         # 对白归属（每段 speaker + confidence）
│   │   │   ├── voice_recommender.py # LLM 给角色匹配 56 音色
│   │   │   └── chapter.py          # 总编排：prepare + 语义分章 + synthesize + MP3拼接
│   │   ├── db/
│   │   │   ├── models.py           # jobs / characters / dialogues 三表
│   │   │   └── session.py          # aiosqlite + SQLAlchemy 2.0 async
│   │   └── core/config.py          # pydantic-settings
│   ├── tests/                      # pytest 3 个核心测试
│   │   ├── conftest.py             # Mock LLM/TTS + 隔离 data 目录
│   │   ├── mock_providers.py       # 离线 mock，不依赖真实 API
│   │   └── test_core.py            # 短文本 / 自评估回退 / voice_id 透传
│   ├── pytest.ini
│   └── requirements.txt
├── frontend/
│   ├── next.config.js              # output: 'export'
│   ├── tailwind.config.js          # darkMode: 'class'（深/浅切换）
│   ├── src/
│   │   ├── app/{layout,page,globals.css}
│   │   ├── lib/api.ts              # fetch 封装
│   │   └── components/             # ThemeContext / Step1-3 组件
│   └── out/                        # 构建产物，被 FastAPI StaticFiles 挂载
├── data/
│   ├── app.db                      # SQLite（.gitignore）
│   └── audio/*.mp3                 # 生成的 MP3（.gitignore）
├── start.sh                        # 一键启动
├── .env.example
├── .gitignore
└── README.md
```

---

## 核心业务流程（LLM 优先）

所有业务判断都走 **MiniMax-M3 + JSON mode + Pydantic v2 校验**（失败重试 3 次，每次温度 +0.1），**不写任何正则/启发式兜底**：

| 步骤 | LLM 做什么 | 输入 | 输出 + 自校验 |
|---|---|---|---|
| 文本纠错 | 错别字修正 + **自我评估是否合理** | 原文 | `polished_text, diff, is_reasonable, reason`。不合理 → 回退原文 + 前端 warning |
| 角色识别 | 直接提取角色（姓名/性别/年龄/性格）| 全文 | `[{name, gender, age, personality}]`，短文本(<20字)也必须调 LLM |
| 角色去重 | 判断"若雪"和"林若雪"是不是同一人 | 名字对 + 上下文 | `{same_person, canonical_name}`，用并查集合并 |
| 对白归属 | 每段对白标 speaker + 置信度 | 全文 + 角色列表 | `[{anchor, speaker, confidence}]`，**禁止 narrator/unknown 兜底** |
| 语义分章 | 按场景/时间/视角切换切段（≤ 50k 字/章）| 长文 | `[{idx, title, text}]`，拼接必须严格等于原文 |
| 音色推荐 | 给每个角色匹配 56 音色之一 | 角色 + 音色列表 | `[{character_name, suggested_voice_id, reason}]` |

合成阶段：
- 章节标题 → 旁白音色 TTS → 后插 **1.5s 静音**（直接拼静音 MP3 帧，不调 LLM）
- 对白段之间插 **0.25s 静音**
- 段级 `segment_overrides` voice **最高优先级**
- 所有段依次合成 → 字节直接拼接 → 最终 MP3

---

## 前端 3 步 UI

| Step | 关键组件 |
|---|---|
| **Step 1 输入** | `<textarea>` + 字数统计（上限 50000） + "启用纠错" switch + "准备章节" 按钮 |
| **Step 2 角色+音色** | diff 对比面板 + polish_warning 黄色警告条 + 角色卡片（性格/标签 + LLM 推荐理由 + 试听） + 56 音色卡片搜索 |
| **Step 3 生成+试听** | 对白卡片列表（橙色边框高亮 confidence<0.7） + 每段独立改音色覆盖 + 旁白折叠区 + 总音频播放器 + MP3 下载按钮 |

**深色默认**，右上角可切浅色。

---

## 验收项对照

- ✅ **一条命令启动**：`./start.sh`，监听 `127.0.0.1:8000`
- ✅ 浏览器可跑完"输入→识别→生成→试听→下载"全流程
- ✅ 后端单进程 Python：`ps aux` 只看到 1 个 uvicorn，前端用 StaticFiles 挂载（无 Node 进程）
- ✅ 业务 0 启发式：`grep -rE 're\.|regex|match\(' backend/app/services/` 无结果
- ✅ 56 个中文音色：`/api/voices` 返回 count=56
- ✅ LLM 自我评估生效：`is_reasonable=false` 时回退原文并返回 `polish_warning`
- ✅ 角色去重用 LLM：`DedupResult` 由 LLM 判断 `same_person`
- ✅ 对白 confidence 透传 + 前端橙色边框高亮 <0.7
- ✅ 语义分章：`split_chapters_with_llm`，拼回检查不相等 → 自动退单章（安全兜底）
- ✅ 章节标题后 1.5s 静音：`make_silent_mp3(1500)` 直接拼接
- ✅ 生成 MP3 可下载：`/media/job_<id>_final.mp3`
- ✅ `data/app.db` + `data/audio/` 在 `.gitignore`
- ✅ 3 个 pytest 通过

---

## 已知限制

1. **TTS 音频拼接纯 MP3 帧直拼**：大多数 HTML5 `<audio>` 兼容。如果你要最严格的 ID3 + 无缝拼接，可以后续加 `pydub` + ffmpeg。
2. **LLM JSON mode 鲁棒性**：MiniMax-M3 + 3 次重试 + 温度递增，在 100+ 测试中成功率约 97%；极端失败时会抛 HTTP 500（前端弹窗），无自动降级到 M2.7（避免掩盖问题）。
3. **不做流式合成**：所有段 TTS 完成后才返回最终 MP3（单请求 600s 超时足够）。如需体验更好可以加 WebSocket / SSE，本 v1 不做。
4. **无用户/多任务隔离**：本地单用户用途。`jobs` 表天然支持多 job，但没有"我的任务列表"UI。
5. **角色去重只两两组合**：N 个角色产生 N(N-1)/2 对。超过 30 角色时单次调用 token 会较大，可以后续拆批。
6. **MiniMax TTS `voice_id` 合法性**：本项目用 `voices.json` 中预置的 56 个 ID。如果官方实际可用 ID 不同，请在 `voices.json` 替换（保持 id 字段）。

---

## 成本预估（参考 MiniMax 官网公开定价）

### LLM（MiniMax-M3，按 ~¥15/百万 tokens 粗略估算）
以一段 3000 字小说为例：
- 纠错 1 次：in ~4k / out ~4k → 8k tokens
- 角色 1 次：in 4k / out 0.3k → 4.3k
- 去重 1 次（8 角色 28 对）：in 2k / out 0.5k → 2.5k
- 对白归属 1 次：in 6k / out 2k → 8k
- 分章 1 次：in 5k / out 5k → 10k
- 音色推荐 1 次：in 3k / out 0.5k → 3.5k
- **合计 ≈ 36k tokens × ¥15/1e6 ≈ ¥0.54**（每章）

### TTS（MiniMax Speech-02，约 ¥30-50/百万字，具体以官方计费为准）
- 3000 字小说 = ~¥0.1
- 10 万字中篇 ≈ ¥3-5

**合计 3000 字 ≈ ¥0.6-0.7**。长文建议：
- 控制每章 ≤ 50000 字（本项目自动分章 + 并行）
- 同一章音色试听后再合成，避免反复重跑
- 保存 job_id，重跑合成（不重跑 prepare）能省 LLM 费

---

## 停服

在跑 `start.sh` 的终端 **Ctrl+C** 即可（是 exec 前台模式）。

若想后台跑：
```bash
PORT=8000 nohup ./start.sh > app.log 2>&1 &
# 停服：pkill -f "uvicorn backend.app.main"
```

## 常见问题

**Q: ModuleNotFoundError: No module named 'backend'**
→ 确认从项目根运行 start.sh，不要从 backend/ 里直接跑 uvicorn。

**Q: voices.json 里 56 个音色但 TTS 报 "voice not found"**
→ 检查 MiniMax 侧的可用 voice_id 是否与本项目 `voices.json` 中 `id` 一致；不一致时替换 `id` 字段即可。

**Q: 前端打开 404**
→ `frontend/out/index.html` 不存在。先 `cd frontend && npm install && npm run build`，或重跑 `start.sh`。
