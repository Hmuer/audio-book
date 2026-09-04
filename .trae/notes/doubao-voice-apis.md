# 豆包语音（字节跳动火山引擎）官方 API 阅读笔记

> 适用代码区：`backend/app/ai/providers/doubao/` + `backend/app/core/config.py`
> 文档版本抓取：2026-09-04（最近官方更新 2026-09-02）
> 文档地址均来自 `docs.volcengine.com/docs/6561/`

---

## 0. 速查

| 我要做… | 用哪个接口 | 端点 | 鉴权 header |
|---|---|---|---|
| 一句话合成 → mp3（短试听 / 音色库试听 / Build 段缓存） | 单向流式语音合成 **HTTP** | `POST https://openspeech.bytedance.com/api/v3/tts/unidirectional` | `X-Api-Key` |
| 长文本流式合成 → mp3（Build 边推边播） | 单向流式语音合成 **WebSocket** | `WSS openspeech.bytedance.com/api/v3/tts/unidirectional/stream` | `X-Api-Key` |
| 多轮对话式实时合成（边输入边出音） | 双向流式语音合成 **WebSocket** | `WSS openspeech.bytedance.com/api/v3/tts/bidirection` | `X-Api-Key` + `X-Api-Connect-Id` |
| 上传音频样本训练自定义音色 | 音色训练 HTTP | `POST openspeech.bytedance.com/api/v3/tts/voice_clone` | `X-Api-Key` |
| 查询训练结果 / demo 音频 | 音色查询 HTTP | `POST openspeech.bytedance.com/api/v3/tts/get_voice` | `X-Api-Key` |
| 把 V1 音色升级到 V3（多产品通用） | 音色升级 HTTP | `POST openspeech.bytedance.com/api/v3/tts/upgrade_voice` | `X-Api-Key` |
| 用文本/图片 prompt "设计"新音色 | 音色设计 HTTP | `POST openspeech.bytedance.com/api/v3/tts/voice_design` | `X-Api-Key` |
| 错误码排查 | 错误码查询 | 文档 6561/2534853 | — |

**`DOUBAO_AK` 在新版控制台下就是 `X-Api-Key` 的值**，从「控制台 → API Key 管理」拿到。本项目 `DOUBAO_AK` 配置即对应此值。

---

## 1. 鉴权（所有豆包 TTS/复刻接口通用）

**新版控制台（推荐，本项目对接的就是这个）**：
```http
Content-Type: application/json
X-Api-Key: <your-api-key>           # = DOUBAO_AK 配置值
X-Api-Resource-Id: seed-tts-2.0     # 详见 §2
X-Api-Request-Id: <uuid>            # 每次请求一个 UUID
X-Control-Require-Usage-Tokens-Return: "*"   # 可选：要返回计费字符数时设
```

**旧版控制台**（即将下线，不建议接）：用 `X-Api-App-Key` + `X-Api-Access-Key` 两个 header，APP ID + Access Token 鉴权。

**响应 header 一定带回**：
```
X-Tt-Logid: 20260616211035105B13415E264F69E61D
```
任何报错排查请带上此 logid 找火山技术支持。

---

## 2. `X-Api-Resource-Id` 决定后端走哪个模型

| 值 | 模型 | 配套 speaker 类型 |
|---|---|---|
| `seed-tts-2.0` | 豆包语音合成大模型 2.0 | 豆包 2.0 官方精品音色 |
| `seed-icl-2.0` | 豆包声音复刻大模型 2.0 | 通过 voice_clone 训练出的音色 |

> 单声复刻音色合成时还要带 `req_params.model`，仅 2 个枚举值：
> - `seed-tts-2.0-standard`（默认，标准）
> - `seed-tts-2.0-expressive`（表现力更强，但更慢）
>
> 用复刻音色时**不能同时用** `context_texts`（语音指令），文档明确说明会冲突。

---

## 3. 单向流式语音合成 HTTP — 最常用的"一句一段"

文档：[6561/2528925](https://docs.volcengine.com/docs/6561/2528925?lang=zh)

```http
POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
Headers: §1 鉴权
Body:
{
  "req_params": {
    "text": "你好，这是一个语音测试",
    "speaker": "zh_female_vv_uranus_bigtts",
    "audio_params": {
      "format": "mp3",                // mp3 | pcm | ogg_opus | wav
      "sample_rate": 24000,           // 默认 24000
      "bit_rate": 64000,              // mp3 默认 64000，可选 [64000, 160000]
      "speech_rate": 0,               // 语速 [-50, 100]，100=2x，-50=0.5x
      "loudness_rate": 0,             // 音量 [-50, 100]
      "enable_subtitle": false,       // 字幕时间戳（仅 seed-tts-2.0，中英）
      "silence_duration": 0,          // 末尾静音 ms [0, 30000]
      "disable_markdown_filter": false,
      "disable_emoji_filter": false,
      "latex_parser": "v2",           // 教育场景才开
      "explicit_language": "zh-cn",   // 限朗读语种
      "explicit_dialect": "yue",      // 方言：beijing/dongbei/henan/shaanxi/shanghai/sichuan/tianjin/yue
      "aigc_watermark": false,        // 末尾加节奏标识
      "aigc_metadata": { "enable": false, ... },
      "post_process": { "pitch": 0 }, // [-12, 12]
      "context_texts": ["你可以用特别特别痛心的语气说话吗?"],  // 仅 2.0 音色
      "section_id": "...",            // 跨包语义保留
      "tone_fidelity": false          // 仅复刻音色：尽量还原 prompt 音色风格
    }
  }
}
```

**响应**：
```json
{
  "code": 0,
  "message": "OK",
  "data": "<base64 encoded audio chunk>",   // 整段一次性 base64
  "sentence": { "phonemes": [...], "text": "...", "words": [...] },
  "usage": { "text_words": 7 }              // 计费字符数
}
```

**项目映射**：
- `DOUBAO_TTS_BASE_URL` 当前实现的就是这个端点
- 段落缓存场景（短句 ≤ 100 字）用 HTTP 版最省事，不需要流式框架

---

## 4. 单向流式语音合成 WebSocket — 长文/Build 用

文档：[6561/2534913](https://docs.volcengine.com/docs/6561/2534913?lang=zh)

```
WSS openspeech.bytedance.com/api/v3/tts/unidirectional/stream
```

协议流程：
1. 建连（同 §1 header）
2. 发 `full_client_request`（即 §3 的 body）
3. 服务端按事件回推：
   - `TTSSentenceStart` — 句开始
   - `TTSResponse` — 音频片段（`MsgType=AudioOnlyServer`，`PayloadSize` 是字节数）
   - `TTSSentenceEnd` — 句结束
   - `TTSSubtitle` — 字级时间戳（开启 `enable_subtitle` 时）
   - `SessionFinished` — 会话结束（`Payload.usage.text_words` 计费字符数）
4. 客户端聚合所有 `TTSResponse.payload` 字节得到完整 mp3

**项目映射**：
- 长段（>300 字 / 单 Build 章节 ≥ 5000 字）切句后用 WS 更稳
- 段缓存 key 仍然按 `(speaker, text, params)` 算，能复用就复用

---

## 5. 双向流式语音合成 WebSocket — 实时对话场景

文档：[6561/2532486](https://docs.volcengine.com/docs/6561/2532486?lang=zh)

```
WSS openspeech.bytedance.com/api/v3/tts/bidirection
```

事件流（精简）：
```
StartConnection → ConnectionStarted
StartSession    → SessionStarted
TaskRequest ×N  → TTSSentenceStart / TTSResponse / TTSSentenceEnd / TTSSubtitle
FinishSession   → SessionFinished
FinishConnection → ConnectionFinished
```

**额外 header**：`X-Api-Connect-Id`（UUID，跟踪连接）

**额外参数**：`req_params.section_id` — 多轮对话同 ID 让服务端保留历史

**项目映射**：本项目当前**不直接需要**；预留即可，未来加"实时朗读 AI 回复"会用到。

---

## 6. 音色训练 HTTP — 声音复刻入口

文档：[6561/2534906](https://docs.volcengine.com/docs/6561/2534906?lang=zh)

```http
POST https://openspeech.bytedance.com/api/v3/tts/voice_clone
Headers: §1（Resource-Id 不需要，训练接口是独立端点）
Body:
{
  "speaker_id": "可选；不传则服务端自动生成",
  "audio": {
    "data": "<base64>",        // 二进制音频的 base64
    "format": "wav"            // wav/mp3/ogg/m4a/aac/pcm；pcm 仅 24k 单声道
  },
  "text": "参考文本",          // 服务对比 WER，差异大返回 45001109
  "language": 0,               // cn=0 默认；en=1；日=2... 详见 §6.1
  "extra_params": {
    "demo_text": "hello this is a test",  // 4~300 字，与 language 一致
    "enable_audio_denoise": false,        // 噪声大才开
    "disable_volume_normalization": false // 音量归一化开关
  }
}
```

**限制**：单文件 ≤ 10MB。

**响应关键字段**：
- `speaker_id`：服务端生成的 `S_xxxxxx` ID，**保留下来给后续查询 / 合成用**
- `status`：`1=Training / 2=Success / 3=Failed / 4=Active` — 2 或 4 都可立即合成
- `speaker_status[].demo_audio`：试听音频，**1 小时有效**，要存就下载下来
- `speaker_status[].model_type`：`5` = 复刻 2.0
- `available_training_times`：剩余训练次数

**项目映射**：`backend/app/services/icl.py` 的训练轮询即对应本接口的 `status=1 → 2` 等待循环。

### 6.1 `language` 枚举

| 值 | 语种 |
|---|---|
| 0 | 中文（默认） |
| 1 | 英文 |
| 2 | 日语 |
| 3 | 西班牙语 |
| 4 | 印尼语 |
| 5 | 葡萄牙语 |
| 6 | 德语 |
| 7 | 法语 |
| 8 | 韩语 |
| 9 | 意大利语 |
| 10 | 泰语 |
| 11 | 越南语 |
| 12 | 俄语 |
| 13 | 菲律宾语 |
| 14 | 马来语 |
| 15 | 阿拉伯语 |
| 16 | 墨西哥西班牙语 |
| 17 | 巴西葡萄牙语 |
| 19 | 波兰语 |
| 20 | 土耳其语 |
| 21 | 瑞典语 |

### 6.2 `custom_speaker_id` 命名规范（后付费音色槽位用）

- 8~256 字符，仅 `[a-zA-Z0-9_-]`
- 必须英文字母开头，首末位不能 `-` 或 `_`
- 同 accountID 不能重名
- 不能与官方精品音色冲突（防冲突正则）：`^((?i:S_|ICL_|MIX_|DiT_|BV)|[a-z]{2}_|(?i:(wvae|moon|mercury|venus|earth|mars|jupiter|saturn|uranus|neptune|pluto|umm)_)).*|.*_(?i:bigtts|bigtts_cc|tob|cs_tob|streaming)$|^[^a-zA-Z]|.*[-_]$|^.{0,7}$|^.{257,}$|.*[^a-zA-Z0-9_-].*`
- **首次调用合成视为"转正"扣音色槽位费**，务必先试听满意再合成

---

## 7. 音色查询 / 升级 HTTP

**查询** [6561/2535742](https://docs.volcengine.com/docs/6561/2535742?lang=zh)：
```http
POST /api/v3/tts/get_voice
{ "speaker_id": "S_xxx", "custom_speaker_id": "可选" }
```
返回同 §6（少 `audio`）。

**升级** [6561/2535751](https://docs.volcengine.com/docs/6561/2535751?lang=zh)：
```http
POST /api/v3/tts/upgrade_voice
{ "speaker_id": "S_xxx" }
```
返回里 `speaker_status` 会出现 2 个：`model_type=1`（V1 旧）+ `model_type=5`（V3 新）。

---

## 8. 音色设计 HTTP — 用 prompt "造"新音色

文档：[6561/2277844](https://docs.volcengine.com/docs/6561/2277844?lang=zh)

```http
POST /api/v3/tts/voice_design
{
  "speaker_id": "...",
  "text": "试听文本，限制 300 字",
  "prompt": {
    "text_prompt": "女性，语速中等偏快，语调低沉有力",      // ≤200 字
    "image_prompt": {                                        // 可选
      "image_url": "https://...",
      "image_bytes": "<base64>"                              // 二选一，bytes 优先级更高
    }
  },
  "language": 0
}
```

`text_prompt` + `image_prompt` 不能同时为空。

---

## 9. 错误码速查

文档：[6561/2534853](https://docs.volcengine.com/docs/6561/2534853?lang=zh)

**HTTP 状态码 + 业务 code 双重语义**：
- 4xx = 客户端问题（参数、文本审核、声纹等）
- 5xx = 服务端问题（DB / TOS / 合成失败等）

### 9.1 通用 / 流式接口（HTTP + WS）

| code | message | 原因 | 处置 |
|---|---|---|---|
| 45000000 | `payload unmarshal: ...` / `quota exceeded for types: concurrency` / `single request size too large` | JSON 反序列化失败 / 超过并发上限 / payload 过大 | 检查 JSON 格式 / 增购并发 / 拆分请求 |
| 45000001 | `[Invalid argument] EmptyRequest` / `speaker not found` / `InvalidModel` / `InvalidDialect` | 必填字段缺失 / 音色 ID 不存在 / model 枚举错 / 方言枚举错 | 补字段、查音色库、改枚举 |
| 45002000 | `TTS invalid speaker` | speaker 为空 | 必传 speaker ID |
| 45002001 | `No readable text!` | 没有可读文本 | 检查 text |
| 55000000 | 服务端内部 error / `connect downstream service timeout` / `synthesis processing timeout` / `client send timeout` / `resource ID is mismatched with speaker related resource` | 网关超时 / 合成超时 / 客户端空闲超时 / resourceId 与 speaker 不匹配 | 重试 / 检查服务是否开通 / 音色是否过期 / 拼写 |

### 9.2 复刻接口（voice_clone / get_voice / upgrade_voice）

| code | 含义 |
|---|---|
| 45001001 | 参数缺失/格式不对/不符合约束 |
| 45001101 | 音频上传失败/超时 |
| 45001102 | ASR 转写失败（音频不清晰） |
| 45001104 | 声纹检测未通过（敏感声纹） |
| 45001105 | 音频数据获取失败（base64 解码/下载失败） |
| 45001107 | speaker_id 未找到 |
| 45001108 | 音频转码失败 |
| 45001109 | WER 检测错误（prompt 音频与文本不对应） |
| 45001110 | 音色删除失败 |
| 45001112 | SNR 检测错误 |
| 45001113 | 降噪失败 |
| 45001114 | 音频质量较差 |
| 45001122 | ASR 未检测到人声 |
| 45001123 | 达到上传次数上限 |
| 45001124 | ASR 文本审核拒绝 |
| 45001125 | demo 文本审核拒绝 |
| 45001126 | demo 文本长度错误 |
| 45001127 | prompt 音频审核拒绝 |
| 45001128 | prompt 音频文本审核拒绝 |
| 55001301~07 | DB / TOS / 克隆下游失败 — 通常服务端异常，可重试 |

### 9.3 音频生成 HTTP 特殊

| code | 含义 |
|---|---|
| 45001001 / 45001115 | 参考音色不存在 |
| 45001116 | prompt 文本超 3000 字 |
| 45001117 | 参考音频时长超 30s |
| 45001125 | demo 文本审核失败 |
| 45001104 | 声纹敏感 |
| 45001127 | 参考音频审核失败 |
| 45001130 | 参考图片审核失败 |
| 45001131 | 参考音频下载失败 |
| 45001132 | 参考图片下载失败 |
| 55001309 | 下游内部错误 / 预测合成音频 > 2min |
| 55001310 | 合成音频审核失败 |
| 55001311 | 合成音频声纹检查失败 |

---

## 10. 项目对接要点（踩坑预警）

1. **凭据就一个**：`DOUBAO_AK` = `X-Api-Key`。`DOUBAO_SK`、`DOUBAO_APP_ID` 实际只在旧版控制台鉴权下需要，新版下这两个值忽略。`config.py` 里保留是为了不破坏 .env 兼容性，可以再加注释说明。
2. **音色库试听**：当前项目音色库走的是「合成一段试听文本」流程 → 用 §3 的 HTTP 单向流式即可，不需要起 WS。
3. **复刻轮询**：训练是异步的（`status=1` → `2`），轮询 `get_voice` 间隔建议 ≥ `DOUBAO_ICL_POLL_INTERVAL_SECS`（默认 5s），超时 `DOUBAO_ICL_TIMEOUT_SECS`（默认 1800s）。
4. **计费**：开 `X-Control-Require-Usage-Tokens-Return: "*"` 会在响应里带 `usage.text_words`，日志里打出来便于核对账单。
5. **Logid 必须记**：所有 TTS / 复刻失败日志应包含响应 header `X-Tt-Logid`，找技术支持时这是唯一 trace key。
6. **markdown / emoji 默认不过滤** → 朗读时会念出 `**` / `#` 等符号，本项目已传 `disable_markdown_filter=true`。
7. **方言 + 音色要匹配**：传 `explicit_dialect` 必须 speaker 也支持该方言，否则会 55000000；用前查音色库说明。
8. **`section_id` 仅多轮对话**：Build 段缓存场景不要复用 section_id，每次独立请求即可。

---

## 11. 用户报错："未配置豆包凭据"

来源：`RuntimeError: 未配置豆包凭据：请设置 DOUBAO_AK 环境变量（或 MEGACORE_ACCESS_KEY_FROM_ENV）`

这是 **配置侧**问题，**不**是 API 文档问题。本项目 `backend/app/core/config.py` 已定义 `DOUBAO_AK`，音色库试听路径走的是：

```
用户点「试听」
  → /api/voice-library/preview   (POST)
  → backend 读取 settings.DOUBAO_AK
  → 为空 → 立即抛 "未配置豆包凭据"
```

修复方法（**不属于本次笔记范围**，按需处理）：
1. 在 `backend/.env` 写入 `DOUBAO_AK=<your-api-key>`
2. 或在设置页「模型厂商」→ 火山引擎豆包语音 → 启用 → 填 API Key → 保存
3. 或在测试环境临时设 `DOUBAO_AK=xxx` 启动

> 注意：之前提到的 `MEGACORE_ACCESS_KEY_FROM_ENV` 看起来是项目内备用环境变量名（grep 后再确认）。