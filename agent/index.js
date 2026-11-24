// dsh-audio-book-agent — DSH（DeepSeek Harness）宿主机侧插件
//
// 定位：**只做薄接口**。所有工具都只是带 `X-DSH-Token` 调本机后端
// `/api/agent/*`；数据库、对象存储、构建、发布全部留在后端，插件不碰。
//
// 为什么这样分层：
// - 密钥（COS AK/SK、LLM Key）永远只在后端，插件上下文里不会出现；
// - 长任务（TTS 构建可能几十分钟）在后端异步跑，工具只返回/轮询状态；
// - 后端是唯一写者，Agent 重试/重放不会造成双写。
//
// DSH 0.2.x 的插件是 Cordis 插件：导出 `inject` 与 `apply(ctx, config)`，
// 用 `ctx.tools.register(...)` 注册工具（Node ≥ 20，纯 ESM，无构建步骤）。
//
// 配置（cordis.patch.yml 或环境变量）：
// - baseUrl: 默认 http://127.0.0.1:28000
// - token:   默认读 AUDIO_BOOK_DSH_TOKEN，其次 DSH_SERVICE_TOKEN
//            （值必须等于后端 .env 的 DSH_SERVICE_TOKEN；留空时工具会给出明确报错）

const DEFAULT_BASE_URL = "http://127.0.0.1:28000";
const REQUEST_TIMEOUT_MS = 120_000;

export const inject = ["tools"];

export const name = "audio-book-agent";

function resolveBaseUrl(config) {
  return (
    (config && config.baseUrl) ||
    process.env.AUDIO_BOOK_API_BASE ||
    DEFAULT_BASE_URL
  );
}

function resolveToken(config) {
  return (
    (config && config.token) ||
    process.env.AUDIO_BOOK_DSH_TOKEN ||
    process.env.DSH_SERVICE_TOKEN ||
    ""
  );
}

/** 合并取消信号与超时。 */
function requestSignal(exec) {
  const timeout = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  const signal = exec && exec.signal;
  if (!signal) return timeout;
  try {
    return AbortSignal.any([signal, timeout]);
  } catch {
    return signal;
  }
}

/**
 * 调后端。返回解析后的 JSON；非 2xx 抛错（DSH 会把它作为工具错误展示）。
 */
async function callBackend(ctx, config, pathname, options = {}) {
  const { method = "GET", query, body, exec } = options;
  const token = resolveToken(config);
  if (!token) {
    throw new Error(
      "缺少 X-DSH-Token：请设置环境变量 AUDIO_BOOK_DSH_TOKEN（值等于后端 .env 的 DSH_SERVICE_TOKEN），再重启 DSH"
    );
  }

  const url = new URL(pathname, resolveBaseUrl(config));
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }

  const headers = { Accept: "application/json", "X-DSH-Token": token };
  let payload;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: payload,
      signal: requestSignal(exec),
    });
  } catch (error) {
    throw new Error(
      `连接 audio-book 后端失败（${url.origin}）：${error && error.message ? error.message : error}。请确认服务在运行。`
    );
  }

  const raw = await response.text();
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = { raw };
    }
  }

  if (!response.ok) {
    const detail =
      (data && (data.detail || data.error || data.message)) || raw || `HTTP ${response.status}`;
    throw new Error(`audio-book 后端返回 ${response.status}：${detail}`);
  }
  return data;
}

/** 模型侧渲染：统一把 JSON 结果原样给它看。 */
function renderJson(_args, value) {
  return [{ type: "text", text: JSON.stringify(value, null, 2) }];
}

function registerJsonTool(ctx, config, definition) {
  ctx.tools.register({
    name: definition.name,
    description: definition.description,
    parameters: definition.parameters,
    output: { schema: { type: "json" }, render: renderJson },
    async execute(args, exec) {
      return definition.run(args || {}, exec);
    },
  });
}

/** 工具定义表。所有工具只读或幂等写，危险动作（建项目/构建/发布）留给后续阶段。 */
function buildTools() {
  return [
    {
      name: "audiobook_health",
      description:
        "检查 audio-book 后端 Agent 桥接是否可用（token 是否正确、存储与书库配置状态）。首次使用或报错时先调它。",
      parameters: {},
      run: (args, exec) =>
        callBackend(null, args.config, "/api/agent/health", { exec }),
    },
    {
      name: "audiobook_library_search",
      description:
        "检索公共书库（约 2.9 万本书）。按分类和标题/作者关键字过滤，返回书目元信息（不返回正文）。选题第一步通常先看分类统计再检索。",
      parameters: {
        q: { type: "string", description: "标题/作者关键字，可省略" },
        category: { type: "string", description: "分类名，如 女生言情/玄幻奇幻；可省略" },
        limit: { type: "integer", description: "返回条数，默认 20，最大 100" },
        offset: { type: "integer", description: "分页偏移，默认 0" },
      },
      run: (args, exec) =>
        callBackend(null, args.config, "/api/agent/library/search", {
          query: { q: args.q, category: args.category, limit: args.limit, offset: args.offset },
          exec,
        }),
    },
    {
      name: "audiobook_library_book",
      description:
        "取单本书的元信息 + 开头试读（试读通过对象存储 Range 读取，只取几 KB，不会下载整本）。用于判断文风、题材是否适合改编。",
      parameters: {
        uid: { type: "string", required: true, description: "书库 uid，来自搜索结果的 items[].uid" },
        excerpt_chars: { type: "integer", description: "试读字符数，默认 600，最大 4000；0 表示不试读" },
      },
      run: (args, exec) =>
        callBackend(null, args.config, `/api/agent/library/books/${encodeURIComponent(args.uid)}`, {
          query: { excerpt_chars: args.excerpt_chars },
          exec,
        }),
    },
    {
      name: "audiobook_project_status",
      description:
        "查项目当前状态和最近 3 次构建（状态、进度、失败章节、时长、体积）。构建是后端异步任务，用它轮询，不要等待。",
      parameters: {
        project_id: { type: "string", required: true, description: "项目 id" },
      },
      run: (args, exec) =>
        callBackend(null, args.config, `/api/agent/projects/${encodeURIComponent(args.project_id)}/status`, {
          exec,
        }),
    },
    {
      name: "audiobook_build_logs",
      description:
        "回读后端 app.log 尾部中与该项目相关的日志行（排查构建失败）。按 project_id 及前 8 位匹配。",
      parameters: {
        project_id: { type: "string", required: true, description: "项目 id" },
        tail: { type: "integer", description: "最多返回行数，默认 200，最大 1000" },
      },
      run: (args, exec) =>
        callBackend(null, args.config, `/api/agent/projects/${encodeURIComponent(args.project_id)}/logs`, {
          query: { tail: args.tail },
          exec,
        }),
    },
    {
      name: "audiobook_price_quote",
      description:
        "按字符数预估 TTS 合成成本（元）。写文案/估算单本成本时用；仅预估，实际以厂商账单为准。",
      parameters: {
        chars: { type: "integer", required: true, description: "总字符数（含标点）" },
      },
      run: (args, exec) =>
        callBackend(null, args.config, "/api/agent/pricing/quote", {
          query: { chars: args.chars },
          exec,
        }),
    },
    {
      name: "audiobook_job_create",
      description:
        "创建一个创作任务（流水线状态机）。带 idempotency_key 时重复调用返回同一个 job，可安全重试。",
      parameters: {
        content_type: { type: "string", description: "内容类型，默认 audiobook" },
        title: { type: "string", description: "任务标题（书名/选题名）" },
        project_id: { type: "string", description: "关联项目 id；选题阶段可省略" },
        idempotency_key: { type: "string", description: "幂等键，建议 usecase+日期+序号" },
        budget_cny: { type: "number", description: "单任务 LLM 成本上限（元），默认走后端配置" },
        payload: { type: "json", description: "阶段产物/选题结果等任意 JSON" },
      },
      run: (args, exec) =>
        callBackend(null, args.config, "/api/agent/jobs", {
          method: "POST",
          body: {
            content_type: args.content_type,
            title: args.title,
            project_id: args.project_id,
            idempotency_key: args.idempotency_key,
            budget_cny: args.budget_cny,
            payload: args.payload,
          },
          exec,
        }),
    },
    {
      name: "audiobook_job_report",
      description:
        "上报一次模型调用（角色/模型/prompt 版本/tokens/成本）到任务账本；后端累加成本，超预算自动把任务置 blocked。每次模型调用后都应调用。",
      parameters: {
        job_id: { type: "string", required: true, description: "创作任务 id" },
        role: { type: "string", description: "角色：topic/copy/cover/audio/qa/publish" },
        model: { type: "string", description: "模型名" },
        prompt_version: { type: "string", description: "prompt/技能版本，便于复现" },
        input_hash: { type: "string", description: "输入指纹（可选）" },
        tokens_in: { type: "integer", description: "输入 tokens" },
        tokens_out: { type: "integer", description: "输出 tokens" },
        cost_cny: { type: "number", description: "本次成本（元）" },
        status: { type: "string", description: "ok 或 failed" },
        output: { type: "json", description: "本次输出（可选，便于复现）" },
      },
      run: (args, exec) =>
        callBackend(null, args.config, "/api/agent/runs", {
          method: "POST",
          body: {
            job_id: args.job_id,
            role: args.role,
            model: args.model,
            prompt_version: args.prompt_version,
            input_hash: args.input_hash,
            tokens_in: args.tokens_in,
            tokens_out: args.tokens_out,
            cost_cny: args.cost_cny,
            status: args.status,
            output: args.output,
          },
          exec,
        }),
    },
  ];
}

/**
 * Cordis 插件入口。
 * @param {object} ctx - Cordis 上下文（提供 tools 服务）
 * @param {{baseUrl?: string, token?: string}} [config]
 */
export function apply(ctx, config = {}) {
  const tools = buildTools().map((tool) => ({
    ...tool,
    // 把 config 绑定进闭包：工具执行时不需要模型传 baseUrl/token
    run: (args, exec) => tool.run({ ...args, config }, exec),
  }));
  for (const tool of tools) {
    registerJsonTool(ctx, config, tool);
  }
}

export { buildTools, resolveBaseUrl, resolveToken };
