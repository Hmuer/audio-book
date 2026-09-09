# 豆包语音（字节跳动火山引擎）官方 API 阅读笔记

> 适用代码区：`backend/app/ai/providers/doubao/` + `backend/app/core/config.py`
> 文档版本抓取：2026-09-04（最近官方更新 2026-09-02 / 2026-08-26 / 2026-08-20）
> 文档地址均来自 `docs.volcengine.com/docs/6561/`

---

## 0. 速查

| 我要做… | 用哪个接口 | 端点 | Resource-Id |
|---|---|---|---|
| 一句话合成 → mp3（短试听 / 音色库试听 / Build 段缓存） | 单向流式语音合成 **HTTP** | `POST /api/v3/tts/unidirectional` | `seed-tts-2.0` / `seed-icl-2.0` |
| 长文本流式合成 → mp3（Build 边推边播） | 单向流式语音合成 **WebSocket** | `WSS /api/v3/tts/unidirectional/stream` | 同上 |
| 多轮对话式实时合成（边输入边出音） | 双向流式语音合成 **WebSocket** | `WSS /api/v3/tts/bidirection` | 同上 |
| **单次 ≤120s 影视级音效用 prompt 生成（背景乐/音效+人声一体）** | 音频生成 **HTTP**（seed-audio-1.0） | `POST /api/v3/tts/create` | — |
| **双人播客合成（长文总结 / URL / 对话稿 / 联网）** | 播客 **WebSocket v3** | `WSS /api/v3/sami/podcasttts` | `volc.service_type.10050` |
| 上传音频样本训练自定义音色 | 音色训练 HTTP | `POST /api/v3/tts/voice_clone` | — |
| 查询训练结果 / demo 音频 | 音色查询 HTTP | `POST /api/v3/tts/get_voice` | — |
| 把 V1 音色升级到 V3（多产品通用） | 音色升级 HTTP | `POST /api/v3/tts/upgrade_voice` | — |
| 用文本/图片 prompt "设计"新音色 | 音色设计 HTTP | `POST /api/v3/tts/voice_design` | — |
| 大模型录音文件识别（≤5h） | 录音文件识别标准/极速/闲时版 | 见 §14 | `volc.seedasr.auc` / `volc.bigasr.auc_turbo` / `volc.bigasr.auc_idle` |
| 流式 ASR（边说边出字 / 按句返回） | 大模型流式语音识别 | 见 §14 | `volc.seedasr.sauc.duration` / `…concurrent` |
| 错误码排查 | 错误码查询 | 文档 6561/2534853 | — |

**`DOUBAO_AK` 在新版控制台下就是 `X-Api-Key` 的值**，从「控制台 → API Key 管理」拿到。本项目 `DOUBAO_AK` 配置即对应此值。

---

## 1. 鉴权（所有豆包 TTS/复刻/音频生成接口通用）

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
| `seed-tts-2.0` | 豆包语音合成大模型 2.0 | 豆包 2.0 官方精品音色（[6561/1257544](https://docs.volcengine.com/docs/6561/1257544?lang=zh)） |
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

## 6. 音频生成 HTTP — Seed-Audio 1.0（影视级音效）

文档：[6561/2550782](https://docs.volcengine.com/docs/6561/2550782?lang=zh)

**这是和"语音合成"完全不同的另一条产品线**——单条 prompt 生成"人声 + 背景乐 + 音效"一体的成品音频。最大 120 秒/次。

```http
POST https://openspeech.bytedance.com/api/v3/tts/create
Headers: X-Api-Key + X-Api-Request-Id  （不需要 X-Api-Resource-Id）
Body:
{
  "model": "seed-audio-1.0",
  "text_prompt": "先是一声手机震动的声音，环境中持续的鸟鸣声……",
  "references": [                       // 可选；0~3 条参考音频 / 0~1 张图片
    { "speaker": "zh_female_vv_uranus_bigtts" }   // 或 audio_data / audio_url
    // { "image_data": "<base64>" }     // 或 image_url；图片与音频互斥
  ],
  "audio_config": {
    "format": "mp3", "sample_rate": 48000,
    "speech_rate": 0, "loudness_rate": 0, "pitch_rate": 0,
    "enable_subtitle": false
  },
  "watermark": {}                         // 含 aigc_watermark / aigc_metadata
}
```

**能力边界**（文档 [6561/2499930](https://docs.volcengine.com/docs/6561/2499930?lang=zh)）：
- 支持 18 种语种（含中日韩英西葡等）
- prompt 最长 **3000 字符**，单次合成人声**建议 ≤400 字**
- 单次 ≤2 分钟
- 输出 wav/mp3/pcm/ogg_opus
- **0 样本多模态生成**：纯文本 / 文本+图片 / 文本+参考音频

**响应**：
```json
{
  "code": 0, "message": "OK",
  "audio": "<base64>", "duration": 82.8, "original_duration": 82.8,
  "url": "<audio_url?expires=2h>",
  "subtitle": { "text": "...", "sentences": [...], "words": [...] }   // 仅 enable_subtitle=true
}
```

**计费依据**：`original_duration`（不是 `duration`）。

**项目映射**：
- 适合做「章节开头 5 秒环境音」「多角色影视化对白 + 背景乐」「广告/电商宣传片头」等需要"声音设计"的场景
- 与"逐字合成有声书"的 Build 流水线是不同赛道，**不该混入 Build worker**，应作为独立功能

---

## 7. 播客 WebSocket v3 — 双人对谈 / 长文总结 / URL 解析 / 联网

文档：[6561/1668014](https://docs.volcengine.com/docs/6561/1668014?lang=zh)

> 端点：`wss://openspeech.bytedance.com/api/v3/sami/podcasttts`
> Resource-Id：`volc.service_type.10050`（播客语音合成专用）
> App-Key：**固定 `aGjiRDfUWi`**
> 鉴权 header：`X-Api-App-Id` + `X-Api-Access-Key`（**旧版鉴权方式**——播客接口目前还没切到单 X-Api-Key）

**核心特点**：使用自定义**二进制协议**（不是普通 JSON over WS），需解析帧头。

### 7.1 三种工作模式

| action | 输入 | 输出 | 适用 |
|---|---|---|---|
| **0** | `input_text`（≤32k） 或 `input_info.input_url`（网页/pdf/doc/txt） | 自动总结→双人播客 | 长文总结、网页解读 |
| **3** | `nlp_texts[]`（多轮对话稿，speaker+text） | 直接合成 | **用户已写好对白** |
| **4** | `prompt_text`（如"火山引擎"） | 联网搜索→双人播客 | 热点话题、科普 |

### 7.2 二进制帧结构

```
Byte 0    : [v1: 0001] [header_size 4x: 0001]
Byte 1    : [msg_type] [flags]
Byte 2    : [serialization: 0001=JSON / 0000=Raw] [compression: 0001=gzip]
Byte 3    : reserved
Byte 4-7  : optional event number (大端)
Byte 8..  : payload size + payload
```

整数全部**大端**。

### 7.3 关键事件

| Event | 含义 |
|---|---|
| 150 | SessionStarted |
| 360 | PodcastRoundStart（带轮次 idx + speaker） |
| 361 | PodcastRoundResponse（音频片段） |
| 362 | PodcastRoundEnd（含 `start_time` / `end_time` / `audio_duration`） |
| 363 | PodcastEnd（**含完整播客 mp3 url，1h 有效**） |
| 152 | SessionFinished |
| 154 | UsageResponse（`input_text_tokens` / `output_audio_tokens`） |

### 7.4 参数

```json
{
  "input_text": "分析下当前的大模型发展",
  "action": 0,
  "use_head_music": false,
  "use_tail_music": false,
  "audio_config": { "format": "mp3", "sample_rate": 24000, "speech_rate": 0 },
  "speaker_info": {
    "random_order": true,
    "speakers": ["zh_male_dayixiansheng_v2_saturn_bigtts", "zh_female_mizaitongxue_v2_saturn_bigtts"],
    "speaker_additions": {   // 可选；用 TTS/ICL 音色时附 instructions
      "zh_female_vv_uranus_bigtts": "{\"model\":\"seed-tts-2.0-standard\"}"
    }
  },
  "input_info": {
    "input_text_max_length": 12000,   // action=0 自动截断阈值
    "max_char_length_per_round": 300, // 每轮最大字符（默认 300）
    "strict_audit": false             // 2026-05-12 新增
  },
  "aigc_watermark": false,
  "aigc_metadata": { "enable": false, ... },
  "retry_info": { "retry_task_id": "...", "last_finished_round_id": 5 }  // 断点续传
}
```

**支持的 2 人组合**：咪仔/大壹、刘飞/潇磊、灿灿/擎苍…同一系列配对效果最好。
**自定义 TTS/ICL 音色**：必须与播客开通的 APPID 同一个才能鉴权通过。

**项目映射**：
- 是「小说解读节目化」「双人对话式书评」「章节科普化」等"播客化改造"的入口
- 收到 PodcastEnd 的 `meta_info.audio_url`（1h 有效）后，建议立即下载到 `data/audio/_podcast/` 持久化
- 二进制协议解析可独立成 `backend/app/services/podcast_protocol.py`，避免污染 TTS 路径

---

## 8. 音色训练 HTTP — 声音复刻入口

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
  "language": 0,               // cn=0 默认；en=1；日=2... 详见 §8.1
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

### 8.1 `language` 枚举

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

### 8.2 `custom_speaker_id` 命名规范（后付费音色槽位用）

- 8~256 字符，仅 `[a-zA-Z0-9_-]`
- 必须英文字母开头，首末位不能 `-` 或 `_`
- 同 accountID 不能重名
- 不能与官方精品音色冲突（防冲突正则）：`^((?i:S_|ICL_|MIX_|DiT_|BV)|[a-z]{2}_|(?i:(wvae|moon|mercury|venus|earth|mars|jupiter|saturn|uranus|neptune|pluto|umm)_)).*|.*_(?i:bigtts|bigtts_cc|tob|cs_tob|streaming)$|^[^a-zA-Z]|.*[-_]$|^.{0,7}$|^.{257,}$|.*[^a-zA-Z0-9_-].*`
- **首次调用合成视为"转正"扣音色槽位费**，务必先试听满意再合成

---

## 9. 音色查询 / 升级 HTTP

**查询** [6561/2535742](https://docs.volcengine.com/docs/6561/2535742?lang=zh)：
```http
POST /api/v3/tts/get_voice
{ "speaker_id": "S_xxx", "custom_speaker_id": "可选" }
```
返回同 §8（少 `audio`）。

**升级** [6561/2535751](https://docs.volcengine.com/docs/6561/2535751?lang=zh)：
```http
POST /api/v3/tts/upgrade_voice
{ "speaker_id": "S_xxx" }
```
返回里 `speaker_status` 会出现 2 个：`model_type=1`（V1 旧）+ `model_type=5`（V3 新）。

---

## 10. 音色设计 HTTP — 用 prompt "造"新音色

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

## 11. 音色库 — 大模型 vs 小模型

文档：[6561/1257544](https://docs.volcengine.com/docs/6561/1257544)（大模型） + [6561/97465](https://docs.volcengine.com/docs/6561/97465)（小模型）

**大模型（seed-tts-2.0 / seed-icl-2.0）**：
- 200+ 款精品音色：通用、角色扮演、视频配音、教育、客服、有声阅读、外语、方言
- **指令遵循**（`context_texts`）能力：可注入"用特别特别痛心的语气说话"等风格
- 部分音色支持多情感多语种多方言
- 资源命名以 `zh_female_..._uranus_bigtts` / `en_male_..._uranus_bigtts` / `ICL_uranus_zh_..._tob` 为主

**小模型（V4/V5 streaming 端点 BV***系列）**：
- 28 种情感/风格 + 8 国语言 + 11 种方言
- **部分音色免费**（21 款：BV700 灿灿、BV001 通用女声、BV002 通用男声 等）
- 通过 `emotion` + `language` 显式控制风格和语种
- 多情感/风格/语言的能力边界**每个音色不一样**（参考原文档末尾详细说明表）
- 时延低，适合实时对话场景

**项目映射**：本项目当前音色库 `_BUILTIN_VOICES` 是大模型音色列表。建议：
- **Build 段合成**：继续用大模型（自然度更高）
- **音色库试听**：可同时给大/小模型音色入口，按"是否需要情感/方言/外语"分流
- **字幕/SRT 导出**：开启 `enable_subtitle: true`（仅大模型 2.0 支持）

---

## 12. 错误码速查

文档：[6561/2534853](https://docs.volcengine.com/docs/6561/2534853?lang=zh)

**HTTP 状态码 + 业务 code 双重语义**：
- 4xx = 客户端问题（参数、文本审核、声纹等）
- 5xx = 服务端问题（DB / TOS / 合成失败等）

### 12.1 通用 / 流式接口（HTTP + WS）

| code | message | 原因 | 处置 |
|---|---|---|---|
| 45000000 | `payload unmarshal: ...` / `quota exceeded for types: concurrency` / `single request size too large` | JSON 反序列化失败 / 超过并发上限 / payload 过大 | 检查 JSON 格式 / 增购并发 / 拆分请求 |
| 45000001 | `[Invalid argument] EmptyRequest` / `speaker not found` / `InvalidModel` / `InvalidDialect` | 必填字段缺失 / 音色 ID 不存在 / model 枚举错 / 方言枚举错 | 补字段、查音色库、改枚举 |
| 45002000 | `TTS invalid speaker` | speaker 为空 | 必传 speaker ID |
| 45002001 | `No readable text!` | 没有可读文本 | 检查 text |
| 55000000 | 服务端内部 error / `connect downstream service timeout` / `synthesis processing timeout` / `client send timeout` / `resource ID is mismatched with speaker related resource` | 网关超时 / 合成超时 / 客户端空闲超时 / resourceId 与 speaker 不匹配 | 重试 / 检查服务是否开通 / 音色是否过期 / 拼写 |

### 12.2 复刻接口（voice_clone / get_voice / upgrade_voice）

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

### 12.3 音频生成 HTTP 特殊

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

## 13. 项目对接要点（踩坑预警）

1. **凭据就一个**：`DOUBAO_AK` = `X-Api-Key`。`DOUBAO_SK`、`DOUBAO_APP_ID` 实际只在旧版控制台鉴权下需要，新版下这两个值忽略。`config.py` 里保留是为了不破坏 .env 兼容性，可以再加注释说明。
2. **音色库试听**：当前项目音色库走的是「合成一段试听文本」流程 → 用 §3 的 HTTP 单向流式即可，不需要起 WS。
3. **复刻轮询**：训练是异步的（`status=1` → `2`），轮询 `get_voice` 间隔建议 ≥ `DOUBAO_ICL_POLL_INTERVAL_SECS`（默认 5s），超时 `DOUBAO_ICL_TIMEOUT_SECS`（默认 1800s）。
4. **计费**：开 `X-Control-Require-Usage-Tokens-Return: "*"` 会在响应里带 `usage.text_words`，日志里打出来便于核对账单。
5. **Logid 必须记**：所有 TTS / 复刻失败日志应包含响应 header `X-Tt-Logid`，找技术支持时这是唯一 trace key。
6. **markdown / emoji 默认不过滤** → 朗读时会念出 `**` / `#` 等符号，本项目已传 `disable_markdown_filter=true`。
7. **方言 + 音色要匹配**：传 `explicit_dialect` 必须 speaker 也支持该方言，否则会 55000000；用前查音色库说明。
8. **`section_id` 仅多轮对话**：Build 段缓存场景不要复用 section_id，每次独立请求即可。
9. **PROVIDERS_CONFIG 优先**：设置页保存的 key 落到 `PROVIDERS_CONFIG`，豆包三个 provider 必须先查它再读 .env（之前已修）。
10. **播客接口用旧版鉴权**：第 §7 节是唯一仍需 `X-Api-App-Id + X-Api-Access-Key` 的接口，接入时要单独走一个 `_auth_headers_podcast()` 分支。

---

## 14. 语音识别（ASR）— 选型速查

文档：[6561/163032](https://docs.volcengine.com/docs/6561/163032)（产品总览） + [6561/2499930](https://docs.volcengine.com/docs/6561/2499930)（模型列表）

### 14.1 流式 ASR（实时）

| Resource-Id | 模式 | 计费 | 适用 |
|---|---|---|---|
| `volc.seedasr.sauc.duration` | 双向流式 / 流式输入 | 按时长（资源包预付 / 后付） | 通用 |
| `volc.seedasr.sauc.concurrent` | 同上 | 按并发数包月 | 高并发场景（不限时长） |

### 14.2 录音文件识别（非实时）

| 模型 | Resource-Id | 时长上限 | 返回时间 |
|---|---|---|---|
| 标准版 | `volc.seedasr.auc` | <5h | <3h |
| 极速版 | `volc.bigasr.auc_turbo` | <2h | 30min 音频 ≈ 10s |
| 闲时版 | `volc.bigasr.auc_idle` | <5h | <24h |

**项目映射**：本项目当前没有 ASR。如果未来要做：
- 用户上传**有声书成品 mp3** → 反向识别为文本 → 校对 → 重导出（极速版）
- 实时跟读纠音 / 课堂录音 → 双向流式
- 字幕生成（小说章节）→ 极速版批量
- 「你说一句，AI 续写一段」式交互 → 流式输入

---

## 15. 历史已记录的"豆包凭据"问题

来源：`RuntimeError: 未配置豆包凭据：请设置 DOUBAO_AK 环境变量（或 MEGACORE_ACCESS_KEY_FROM_ENV）`

**根因**：豆包 3 个 provider（TTS / ICL / 多播剧）早期只读 `settings.DOUBAO_AK`、不读 `PROVIDERS_CONFIG[id="doubao"].api_key`。
**修复**：三个 provider 都加了 3 级 fallback：PROVIDERS_CONFIG → .env DOUBAO_AK → `os.environ["MEGACORE_ACCESS_KEY_FROM_ENV"]`。
**回归测试**：`backend/tests/test_doubao_authorization_red.py`（6 用例覆盖所有优先级组合）。