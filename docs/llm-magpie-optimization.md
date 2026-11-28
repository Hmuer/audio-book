# AI 有声书 · LLM 接入 magpie 与优化方案

> 面向：AI 有声书流水线（中文长篇 → 结构化抽取 → 有声书）
> 依据：`docs/llm-requirements.md` + 实际代码（`backend/app/ai/providers/minimax/llm.py`）+ 在 magpie 网关上做的实测
> 生成日期：2026-11-27
> 相关：magpie 部署与运维见 `/opt/magpie/RUNBOOK.md`

---

## 0. TL;DR

- 项目目前**硬指标已基本满足**（128K 上下文 / 24K 输出 / 16 并发 / 长超时），直连 MiniMax 官方。
- 用 magpie 的价值：**限流/故障自动转移 + 多通道吞吐 + 集中限额/用量**，让整本识别不被单家限流拖垮。
- **关键卡点**：项目对非-MiniMax 网关发的 `enable_thinking:false` 经 magpie **不生效**（仍思考，MiniMax 还会把 `<think>` 泄进正文→非法 JSON）。**必须改发 `thinking:{"type":"disabled"}`**（实测通用有效）。
- 已建 magpie 分组 `group/book`：`workbuddy/deepseek-v4.1-flash` → `deepseek/deepseek-flash`（order，故障转移），**不含 MiniMax**（额度留给 TTS）。
- 落地只需：改 1 行代码 + 把 LLM 指向 magpie `group/book` + 重启。

---

## 1. 项目现状（实测）

来源：`/root/.novel-tts`（活库 `app_settings` 覆盖 env）

| 项 | 实际值 | 需求是否满足 |
|---|---|---|
| 供应商 | MiniMax 官方直连 `api.minimaxi.com` | OpenAI 兼容 ✓ |
| 主模型 | `MiniMax-M3` | 中文/JSON ✓ |
| 上下文 / 输出上限 | 1,000,000 / 524,288 | ≥128K / ≥24K ✓ |
| 全局并发 `LLM_MAX_CONCURRENCY` | **16**（env 写的 1 被 DB 覆盖） | 16–32 ✓ |
| 对白批并发 / 章 / 字数 | 10 / 4 章 / 4000 字 | ✓ |
| 超时 | 客户端 1200s；magpie 反代 3600s | ≥600s ✓ |
| 对白归属 `max_tokens` | 24000 | ≥24K ✓ |
| 关思考 | MiniMax 原生 `thinking:{type:disabled}` | ✓ |
| 润色 `POLISH_ENABLED` | **true**（≈+1300 次/本） | 成本/时长来源 |

**结论**：硬指标满足，提升空间在「质量 / 失败率 / 吞吐」。

---

## 2. 目标

- **质量**：严格 JSON 合法率、角色/对白抽取准确度、少丢章节（422）。
- **效率**：整本识别 4–6h（需求文档），高并发不掉链、不被单家限流卡死。

---

## 3. 关键发现（实测）

### 3.1 关思考参数陷阱（最重要）

magpie 是 OpenAI 兼容网关。项目对「非 MiniMax」分支发 `enable_thinking:false`，实测 **magpie 不认**：

| 请求体里的写法 | 经 magpie 结果 |
|---|---|
| `enable_thinking:false`（**现用**） | ❌ 仍在思考（reasoning_tokens≈121）；MiniMax 把 `<think>` 泄进正文 → **非法 JSON** |
| `reasoning_effort:"none"` | DeepSeek ✅（0 思考）／ **MiniMax ❌**（magpie 不知道 M3 档位） |
| **`thinking:{"type":"disabled"}`** | ✅ **通用**：MiniMax-M3 / DeepSeek官方 / workbuddy / group 组 —— 全部 0 思考 + 合法 JSON + ~1.3s |

> 备注：项目 MiniMax 原生分支本来发的就是 `thinking:{type:disabled}`，所以这个写法对「MiniMax 直连」和「magpie 任意网关」都正确。

### 3.2 候选模型能力（magpie `/v1/models`）

| 模型 | 上下文 | 最大输出 | 关思考档位 | 说明 |
|---|---|---|---|---|
| `minimax-cn/MiniMax-M3` | 1.0M | 524K | 需 `thinking:disabled`（无 level） | 现用，额度留给 TTS |
| `deepseek/deepseek-flash` (V4.1 Flash 官方) | 1.05M | 393K | none/low/high/max | 快、便宜、高 RPM，首选兜底/主力 |
| `workbuddy/deepseek-v4.1-flash` | 1.0M | 128K | none/low/high/max | 订阅额度，先用 |
| `workbuddy/glm-5.3` | 1.0M | 64K | low/high/max | 无法完全关思考 |
| `workbuddy/kimi-k3.1-pro` | 1.0M | 32K | low/high/max | 无法完全关思考 |

### 3.3 长输出稳定性（模拟对白归属 ~40 句 JSON，max_tokens 2500）

| 模型 | 用时 | 输出 tokens | 合法 JSON | 句数 |
|---|---|---|---|---|
| `workbuddy/deepseek-v4.1-flash` | 5.6s | 1451 | ✅ | 40 |
| `deepseek/deepseek-flash` | 3.3s | 986 | ✅ | 40 |

结论：两者都能出长 JSON；**deepseek 官方更快**。workbuddy 是 Free plan，高并发+24K 输出时可能限流/变慢（曾见其挂起 8 分钟），order 会自动落到 deepseek。

### 3.4 超时（已在网关侧修复）

magpie 反代（1Panel openresty）已设 `proxy_read_timeout 3600s` / `proxy_send_timeout 3600s` / `proxy_buffering off`，解决了早期「60s 被 504 切断」。详见 `/opt/magpie/RUNBOOK.md` 第 10 节。

---

## 4. magpie 侧配置（已就绪）

- 网关地址：`https://magpie.ls-lh.cn/v1`（OpenAI 兼容；Anthropic/Gemini 见网关页）
- 网关密钥：用 `audio-book` 这把（magpie「网关 → 网关密钥」生成；一设备一把）
- 分组：

```
group/book   routing=order   stays=auto
  1  workbuddy/deepseek-v4.1-flash   # 订阅额度先用
  2  deepseek/deepseek-flash         # 官方，workbuddy 限流/额度用完/报错时接手
```

> 不含 MiniMax（额度留给后续 TTS）。
> 若想"最重步骤用最快"，可另建 `book-heavy` = `deepseek/deepseek-flash` → `workbuddy/deepseek-v4.1-flash`（order），并让对白归属走它。

---

## 5. 项目侧改造

### 🔴 A. 必做（切 magpie 前提）

**A-1 关思考参数** — `backend/app/ai/providers/minimax/llm.py`，非 minimax 分支（约 L513–516）：

```python
else:
    req_json["response_format"] = {"type": "json_object"}
    if not self.thinking_enabled:
        req_json["thinking"] = {"type": "disabled"}     # ← 改这里
    # 删除：req_json["enable_thinking"] = bool(self.thinking_enabled)
```

### 🟠 B. 强烈建议（质量 + 效率提升最大）

| # | 优化 | 为什么 | 怎么做 |
|---|---|---|---|
| B-1 | 切到 magpie | 故障转移 + 多通道吞吐 | `LLM_BASE_URL=https://magpie.ls-lh.cn/v1`，模型 `group/book`，key=`audio-book` 网关密钥 |
| B-2 | 最重步骤（对白归属 24K）优先 deepseek 官方 | workbuddy Free 高并发+长输出易限流/变慢 | 现在 `model_pro=model_fast`（llm.py L409–410）；改成可分开：重活 `deepseek/deepseek-flash`，轻活 `group/book` |
| B-3 | 422 不再直接跳过，换供应商重试 | 现在 422 直接丢章节 = 数据丢失 | 在 `LLMContentRejectedError` 处加「换 provider/中转重试一次」 |
| B-4 | 提示词缓存 | DeepSeek 官方 prompt cache 命中价差~10x | 「schema+角色表+few-shot」放 system 最前且逐字不变，本章正文放最后 |
| B-5 | 并发 16 → 24~32 | deepseek 官方 RPM 高，吃更高并发缩短整本时间 | `LLM_MAX_CONCURRENCY=24~32` |

### 🟡 C. 可选调参

| # | 优化 | 说明 |
|---|---|---|
| C-1 | `POLISH_ENABLED`（现 true） | 每章一次润色≈+1300 次/本；必要时关掉或只对低置信章节润色 |
| C-2 | 对白批 4 章/4000 字（偏保守） | 确认输出 <24K 不截断前提下调到 6~8 章/批，减少请求数提吞吐；用 `finish_reason==length` 监控 |
| C-3 | 客户端重试 3 → 1~2 | magpie 已做跨供应商故障转移，避免客户端重试叠加放大调用/成本 |
| C-4 | 流式 `stream=true`（可选） | 非流式靠 magpie 3600s 已够；想更稳可加，代码改动较大，非必须 |

### ✅ 现状已良好（无需改）

- 角色识别**全书覆盖**（按完整章节切片，非"只抽前 50 万字"）→ 不漏角色
- 402/429/422 分类处理、JSON 抢救（形状归一 / 逐条丢弃）、thinking 循环检测——设计到位

---

## 6. 落地步骤（均可回滚）

1. 改 A-1 那一行 → `git commit`（可回滚）
2. 设置页新增厂商卡片：Base URL `https://magpie.ls-lh.cn/v1`、Key=`audio-book` 网关密钥、模型 `group/book`；设为「激活 LLM」
   - 或改 `/root/.novel-tts/.env` 的 `LLM_BASE_URL` 并在 UI/DB 选 magpie + `group/book`
3. 重启后端（`systemctl restart` 对应服务 / 重跑 start.sh）
4. 拿**一本小书**跑通验证（见 §7），再全量

**回滚**：`LLM_BASE_URL` 改回 `https://api.minimaxi.com/v1` + 模型 `MiniMax-M3`，改回代码（git revert），重启。

---

## 7. 验证清单

- [ ] 对白归属能产出 **24K tokens 内的合法 JSON**（`finish_reason=stop`，非 `length`）
- [ ] 响应里 **无 `<think>`**、`reasoning_tokens=0`
- [ ] magpie「用量」页能看到 `agent=audio-book`、走 `group/book` 的请求
- [ ] 单章/单批耗时正常（重请求 30–180s）
- [ ] 整本小样跑完，统计 **422 率 / JSON 失败率 / 每章平均调用**
- [ ] 成本对比：MiniMax 直连 vs magpie(deepseek) 每本费用

---

## 8. 待定 / 下一步

1. **是否直接改代码 + 切 `group/book` + 重启验证？**（生产变更，需确认）
2. **是否要先做真实章节 A/B**：同章 `workbuddy` vs `deepseek 官方` 跑对白归属，对比 JSON 合法率/角色准确/耗时，用数据决定"最重步骤打头模型"。
3. 是否启用 B-2（重活/轻活分离）——需把 `model_pro` / `model_fast` 从"同一个"改成两个可配。

---

## 附录：本次实测环境

- magpie：`ghcr.io/yetone/magpie` v0.1.4829，网关 `127.0.0.1:3425` / Web `127.0.0.1:3430`，反代 `https://magpie.ls-lh.cn`（3600s 超时）
- 测试用网关密钥为客户端的 `audio-book`/临时 key；模型能力取自 `GET /v1/models`
- 关思考矩阵、长输出对比均在 magpie 上以 OpenAI `json_object` + `thinking` 参数复现
