const fs = require("fs");
const path = require("path");

const REPORTS_DIR = path.join(process.cwd(), "reports");
const STOCK_INDEX_LIMIT = 320;
const STOCK_ALIAS_OVERRIDES = {
  "江海股份": "002484.SZ",
  "江丰电子": "300666.SZ",
  "绿的谐波": "688017.SH",
};
const TARGET_STOP_WORDS = new Set([
  "股票",
  "公司",
  "基金",
  "行业",
  "产业",
  "市场",
  "投资",
  "推荐",
  "关注",
  "观点",
  "风险",
  "机会",
  "核心",
  "标的",
  "最新",
  "今日",
  "明日",
  "逻辑",
]);
let cachedStockIndex = null;

function setCors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Dashboard-Admin-Token");
}

function sendJson(res, status, payload) {
  setCors(res);
  res.status(status).json(payload);
}

function extractResponseText(payload) {
  if (typeof payload?.output_text === "string" && payload.output_text.trim()) {
    return payload.output_text.trim();
  }
  const parts = [];
  for (const item of payload?.output || []) {
    for (const content of item?.content || []) {
      const textValue = content?.text || content?.value;
      if (typeof textValue === "string" && textValue.trim()) {
        parts.push(textValue.trim());
      }
    }
  }
  return parts.join("\n").trim();
}

function normalizeTargetLabel(value) {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number") {
    const text = String(value).trim();
    return text === "[object Object]" ? "" : text;
  }
  if (typeof value !== "object") return "";
  const candidates = [
    value.name,
    value.stockName,
    value.shortName,
    value.company,
    value.target,
    value.label,
    value.title,
    value.code,
    value.symbol,
  ];
  for (const candidate of candidates) {
    const text = normalizeTargetLabel(candidate);
    if (text) return text;
  }
  for (const candidate of Object.values(value)) {
    const text = normalizeTargetLabel(candidate);
    if (text) return text;
  }
  return "";
}

function normalizeList(values, limit) {
  if (!Array.isArray(values)) return [];
  return values
    .map(normalizeTargetLabel)
    .filter(Boolean)
    .slice(0, limit);
}

function normalizeMatchText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[Ａ-Ｚａ-ｚ０-９]/g, (char) =>
      String.fromCharCode(char.charCodeAt(0) - 0xfee0)
    )
    .replace(/[·・.\s_\-—–（）()【】\[\]{}<>《》「」『』"'“”‘’：:，,。；;、/\\|]/g, "");
}

function normalizeStockCode(value) {
  const raw = String(value || "").trim().toUpperCase();
  if (!raw) return "";
  const withSuffix = raw.match(/\b(\d{6})\.(SH|SZ|BJ|HK)\b/);
  if (withSuffix) return `${withSuffix[1]}.${withSuffix[2]}`;
  const loose = raw.match(/\b(?:SH|SZ|BJ|HK)?(\d{6})\b/);
  if (!loose) return "";
  const code = loose[1];
  if (code.startsWith("6")) return `${code}.SH`;
  if (code.startsWith("8") || code.startsWith("9")) return `${code}.BJ`;
  return `${code}.SZ`;
}

function safeReadJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

function latestReportFiles(prefix, limit = 6) {
  try {
    return fs
      .readdirSync(REPORTS_DIR)
      .filter((name) => name.startsWith(prefix) && name.endsWith(".json"))
      .sort()
      .reverse()
      .slice(0, limit)
      .map((name) => path.join(REPORTS_DIR, name));
  } catch {
    return [];
  }
}

function addStockIndexRow(index, row) {
  if (!row || typeof row !== "object") return;
  const name = normalizeTargetLabel(row.name);
  const code = normalizeStockCode(row.code);
  if (!name || !code) return;
  index.byCode.set(code, { name, code });
  index.byAlias.set(normalizeMatchText(name), { name, code, alias: name });
  index.byAlias.set(normalizeMatchText(code), { name, code, alias: code });
  const bareCode = code.split(".")[0];
  index.byAlias.set(normalizeMatchText(bareCode), { name, code, alias: bareCode });
}

function addReportPayloadToStockIndex(index, payload) {
  if (Array.isArray(payload)) {
    for (const row of payload) addStockIndexRow(index, row);
    return;
  }
  if (!payload || typeof payload !== "object") return;
  if (Array.isArray(payload.stockExposureRows)) {
    for (const row of payload.stockExposureRows) addStockIndexRow(index, row);
  }
  if (Array.isArray(payload.rows)) {
    for (const row of payload.rows) {
      addStockIndexRow(index, row);
      if (Array.isArray(row?.topHoldings)) {
        for (const holding of row.topHoldings) addStockIndexRow(index, holding);
      }
    }
  }
}

function buildStockIndex() {
  if (cachedStockIndex) return cachedStockIndex;
  const index = { byAlias: new Map(), byCode: new Map(), aliases: [] };
  for (const filePath of [
    ...latestReportFiles("watchlist_dashboard_", 8),
    ...latestReportFiles("watchlist_strong_stocks_", 8),
    ...latestReportFiles("institution_holdings_", 8),
  ]) {
    addReportPayloadToStockIndex(index, safeReadJson(filePath));
  }
  for (const [name, code] of Object.entries(STOCK_ALIAS_OVERRIDES)) {
    addStockIndexRow(index, { name, code });
  }
  index.aliases = Array.from(index.byAlias.entries())
    .map(([normalized, item]) => ({ normalized, ...item }))
    .filter((item) => item.normalized.length >= 2)
    .sort((a, b) => b.normalized.length - a.normalized.length)
    .slice(0, STOCK_INDEX_LIMIT);
  cachedStockIndex = index;
  return index;
}

function looksLikeTargetCandidate(value) {
  const text = normalizeTargetLabel(value);
  if (!text || text.length > 24) return false;
  const normalized = normalizeMatchText(text);
  if (!normalized || TARGET_STOP_WORDS.has(normalized)) return false;
  if (/^\d+$/.test(normalized) && normalized.length !== 6) return false;
  if (/^[a-z]{1,2}$/.test(normalized)) return false;
  return /[\u4e00-\u9fff]{2,}|[A-Z]{2,6}|\d{6}/i.test(text);
}

function pushUniqueTarget(out, seen, value) {
  const text = normalizeTargetLabel(value);
  if (!looksLikeTargetCandidate(text)) return;
  const key = normalizeMatchText(text);
  if (!key || seen.has(key)) return;
  seen.add(key);
  out.push(text);
}

function pushExplicitTarget(out, seen, value) {
  const raw = normalizeTargetLabel(value);
  if (!raw) return;
  const pieces = raw
    .split(/[、,，;；和及与]/)
    .map((piece) => piece.replace(/^.*(?:里|看好|推荐|关注)/, "").trim())
    .filter(Boolean);
  for (const piece of pieces.length ? pieces : [raw]) {
    pushUniqueTarget(out, seen, piece);
  }
}

function extractExplicitTargetCandidates(rawText, limit = 12) {
  const text = String(rawText || "");
  const out = [];
  const seen = new Set();
  const patterns = [
    /[【\[]([^【】\[\]\n]{2,24})[】\]]/g,
    /\$([A-Za-z]{1,8}(?:\.[A-Za-z]{1,3})?|\d{3,6}(?:\.[A-Za-z]{1,3})?)/g,
    /#([\u4e00-\u9fffA-Za-z0-9._-]{2,24})/g,
    /([\u4e00-\u9fff]{2,12}(?:股份|科技|电子|集团|光电|通信|精密|铜箔|新能|高科|材料|控股|电气))/g,
    /\b((?:SH|SZ|BJ)?\d{6}(?:\.(?:SH|SZ|BJ))?)\b/gi,
  ];
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) {
      pushExplicitTarget(out, seen, match[1]);
      if (out.length >= limit) return out;
    }
  }
  return out;
}

function extractIndexedTargets(rawText, limit = 12) {
  const index = buildStockIndex();
  const normalizedText = normalizeMatchText(rawText);
  const out = [];
  const seenCodes = new Set();
  for (const item of index.aliases) {
    if (out.length >= limit) break;
    if (!item.normalized || !normalizedText.includes(item.normalized)) continue;
    const isShortName = item.alias && item.alias.length <= 2 && !/^\d{6}$/.test(item.alias);
    if (isShortName && !String(rawText || "").includes(`【${item.alias}】`)) continue;
    if (seenCodes.has(item.code)) continue;
    seenCodes.add(item.code);
    out.push(item.name);
  }
  return out;
}

function enrichTargets(structuredTargets, rawText, limit = 12) {
  const out = [];
  const seen = new Set();
  const index = buildStockIndex();
  const add = (value) => {
    const label = normalizeTargetLabel(value);
    if (!label) return;
    const code = normalizeStockCode(label);
    const normalized = normalizeMatchText(label);
    const mapped = (code && index.byCode.get(code)) || index.byAlias.get(normalized);
    pushUniqueTarget(out, seen, mapped?.name || label);
  };
  for (const target of structuredTargets || []) add(target);
  for (const target of extractIndexedTargets(rawText, limit)) add(target);
  for (const target of extractExplicitTargetCandidates(rawText, limit)) add(target);
  return out.slice(0, limit);
}

function sanitizeAnalysis(payload, rawText, fallbackDate) {
  const summary = String(payload?.summary || "").trim();
  const industry = String(payload?.industry || "").trim();
  const targets = enrichTargets(normalizeList(payload?.targets, 12), rawText, 12);
  if (!summary || !industry) {
    throw new Error("Model output missing summary or industry");
  }
  return {
    date: String(payload?.date || fallbackDate || "").trim(),
    industry,
    summary,
    rawText: String(rawText || "").trim(),
    targets,
    content: summary,
  };
}

function getJsonFromText(text) {
  const cleaned = String(text || "").trim();
  if (!cleaned) {
    throw new Error("Model returned empty output");
  }
  const fenced = cleaned.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidate = fenced ? fenced[1].trim() : cleaned;
  return JSON.parse(candidate);
}

async function callOpenAI({ apiKey, model, rawText }) {
  const schema = {
    type: "object",
    additionalProperties: false,
    required: ["summary", "industry", "targets"],
    properties: {
      date: { type: "string", description: "若原文中出现报告日期则提取，否则留空" },
      industry: { type: "string", description: "报告对应的行业或细分方向，例如 存储/光模块/机器人减速器" },
      summary: { type: "string", description: "用中文提炼五句话以内的核心观点" },
      targets: {
        type: "array",
        items: { type: "string" },
        description: "文中明确涉及的具体股票/公司中文简称或代码列表，最多8个；包括【】、$ticker、股票代码、简称、全称提及的标的；不要只写泛行业词。",
      },
    },
  };

  const response = await fetch("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model,
      input: [
        {
          role: "system",
          content: [
            {
              type: "input_text",
              text:
                "你是A股投研助理。请从用户粘贴的投研原文中提取结构化关键信息。输出必须符合JSON schema。不要编造不存在的信息，无法确认就留空或给空数组。核心观点控制在五句话以内，行业请尽量细化到A股交易语境下的细分方向。",
            },
          ],
        },
        {
          role: "user",
          content: [
            {
              type: "input_text",
              text: rawText,
            },
          ],
        },
      ],
      text: {
        format: {
          type: "json_schema",
          name: "research_report_extract",
          schema,
          strict: true,
        },
      },
    }),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`OpenAI request failed: ${response.status} ${text}`);
  }

  const payload = await response.json();
  const text = extractResponseText(payload);
  if (!text) {
    throw new Error("OpenAI returned empty output");
  }
  return getJsonFromText(text);
}

async function callCompatibleChat({ baseUrl, apiKey, model, rawText, providerLabel }) {
  const systemPrompt = [
    "你是A股投研助理。",
    "请从用户粘贴的投研原文中提取结构化关键信息。",
    "不要编造不存在的信息，无法确认就留空或给空数组。",
    "行业请尽量贴近A股交易语境下的细分方向。",
    "核心观点控制在五句话以内。",
    "targets必须尽量完整提取文中明确出现的股票/公司/代码/ticker，特别是【标的】、$ticker、六位股票代码、公司简称、公司全称。",
    "只提明确出现的标的，不要把纯行业词当作股票标的；行业词放到industry。",
    "只输出一个JSON对象，不要输出任何额外说明。",
    'JSON结构必须为：{"date":"","industry":"","summary":"","targets":[]}',
  ].join("");

  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/chat/completions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model,
      temperature: 0.2,
      response_format: { type: "json_object" },
      messages: [
        { role: "system", content: systemPrompt },
        { role: "user", content: rawText },
      ],
    }),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${providerLabel} request failed: ${response.status} ${text}`);
  }

  const payload = await response.json();
  const text = String(payload?.choices?.[0]?.message?.content || "").trim();
  if (!text) {
    throw new Error(`${providerLabel} returned empty output`);
  }
  return getJsonFromText(text);
}

module.exports = async (req, res) => {
  setCors(res);
  if (req.method === "OPTIONS") {
    res.status(204).end();
    return;
  }
  if (req.method !== "POST") {
    sendJson(res, 405, { error: "Method not allowed" });
    return;
  }

  try {
    const expectedToken = String(process.env.DASHBOARD_ADMIN_TOKEN || "").trim();
    const providedToken = String(req.headers["x-dashboard-admin-token"] || "").trim();
    if (!expectedToken || providedToken !== expectedToken) {
      sendJson(res, 401, { error: "Unauthorized" });
      return;
    }

    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
    const rawText = String(body.text || "").trim();
    const date = String(body.date || "").trim();
    if (!rawText) {
      sendJson(res, 400, { error: "Missing text" });
      return;
    }

    const provider = String(process.env.LLM_PROVIDER || "openai").trim().toLowerCase();
    const model = String(process.env.LLM_MODEL || process.env.OPENAI_MODEL || "gpt-5-mini").trim();

    let structured;
    if (provider === "deepseek") {
      const apiKey = String(
        process.env.DEEPSEEK_API_KEY || process.env.LLM_API_KEY || ""
      ).trim();
      if (!apiKey) {
        throw new Error("Missing DEEPSEEK_API_KEY");
      }
      const baseUrl = String(process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com").trim();
      structured = await callCompatibleChat({
        baseUrl,
        apiKey,
        model,
        rawText,
        providerLabel: "DeepSeek",
      });
    } else {
      const apiKey = String(process.env.OPENAI_API_KEY || process.env.LLM_API_KEY || "").trim();
      if (!apiKey) {
        throw new Error("Missing OPENAI_API_KEY");
      }
      structured = await callOpenAI({ apiKey, model, rawText });
    }

    const analysis = sanitizeAnalysis(structured, rawText, date);
    sendJson(res, 200, { analysis });
  } catch (error) {
    sendJson(res, 500, {
      error: "Report analyze failed",
      detail: error instanceof Error ? error.message : String(error),
    });
  }
};
