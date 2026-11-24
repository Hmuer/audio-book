# dsh-audio-book-agent

DSH（DeepSeek Harness）插件：把本机 `audio-book` 后端的能力暴露给 Agent。

**定位：薄接口 + 代码化编排**。插件只通过 `X-DSH-Token` 调后端
`/api/agent/*`；数据库、对象存储、TTS 构建、发布全部留在后端。

```
DSH 会话 ── tools ──▶ 本插件 ──HTTP──▶ audio-book 后端（唯一写者）
                                      DB / COS / 构建 / 发布
```

## 安装（本机）

```bash
# 1) 后端配置服务 token（二选一）
#    a. 写进 /opt/audio-book/audio-book/.env
DSH_SERVICE_TOKEN=$(openssl rand -hex 32)
#    b. 或 systemd 的 Environment=

# 2) 让 DSH 进程拿到同一个值（插件侧）
export AUDIO_BOOK_DSH_TOKEN=<同一个值>

# 3) 安装插件到 web profile（本地目录，无需发布 npm）
dsh plugin --profile web add /opt/audio-book/audio-book/agent

# 4) 重启 DSH web（插件宿主代码变更需要重启）
```

装好后新开会话即可看到 `audiobook_*` 工具。技能 `audiobook-pitch` 通过仓库根
`.dsh/skills/` 的符号链接发现（DSH 默认技能根），不需要额外配置。

## 工具

| 工具 | 作用 |
| --- | --- |
| `audiobook_health` | 后端桥接自检（token / 存储 / 书库状态） |
| `audiobook_library_search` | 书库检索（分类 + 标题/作者，约 2.9 万本） |
| `audiobook_library_book` | 单本元信息 + 开头试读（Range 只读几 KB） |
| `audiobook_project_status` | 项目状态 + 最近 3 次构建 |
| `audiobook_build_logs` | app.log 尾部中与该项目的相关行 |
| `audiobook_price_quote` | 按字数预估 TTS 成本 |
| `audiobook_job_create` | 创建创作任务（幂等） |
| `audiobook_job_report` | 上报模型调用成本（超预算自动熔断） |

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AUDIO_BOOK_DSH_TOKEN` | 空 | 必填。等于后端 `.env` 的 `DSH_SERVICE_TOKEN` |
| `AUDIO_BOOK_API_BASE` | `http://127.0.0.1:28000` | 后端地址 |

也可以写在 `cordis.patch.yml` 的 `config` 里（`baseUrl` / `token`），但 token
更推荐走环境变量，避免被提交进仓库。

## 安全边界

- 后端 `DSH_SERVICE_TOKEN` 留空时，`/api/agent/*` 一律返回 503（fail closed）；
- 插件**拿不到** COS AK/SK、LLM Key —— 它们只在后端 `app_settings` / `providers_config.json`；
- 工具返回内容只有业务字段，不含密钥；
- 写操作全部幂等（`idempotency_key`），长任务返回 `job_id` 由调用方轮询。

## 版本

- DSH `>=0.2.1-rc.2 <0.3.0`（`engines.dsh`）
- Node `>=20`（使用全局 `fetch` / `AbortSignal.timeout`）

## 后续阶段（尚未实现）

- 写工具：建项目 / 导入书库 / 启动构建 / 保存文案 / 质检 / 发布；
- 代码化 workflow 脚本（选题 → 文案+封面并行 → 人工闸门 → 构建 → 质检 → 发布）；
- Agent 预设（`@deepseek-ai/dsh-agent-preset`）与创作台侧栏面板。
