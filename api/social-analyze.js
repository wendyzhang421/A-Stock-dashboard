function setCors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Dashboard-Admin-Token");
}

function sendJson(res, status, payload) {
  setCors(res);
  res.status(status).json(payload);
}

function normalizeList(values, limit) {
  if (!Array.isArray(values)) return [];
  return values
    .map((value) => String(value || "").trim())
    .filter(Boolean)
    .slice(0, limit);
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

function sanitizeAnalysis(payload, rawText, fallbackDate, fallbackKol) {
  const kol = String(payload?.kol || fallbackKol || "").trim();
  const target = String(payload?.target || "").trim();
  const summary = String(payload?.summary || "").trim();
  if (!kol || !target || !summary) {
    throw new Error("Model output missing kol, target or summary");
  }
  return {
    kol,
    platform: String(payload?.platform || "X / Grok").trim(),
    target,
    date: String(payload?.date || fallbackDate || "").trim(),
    summary,
    rawText: String(rawText || "").trim(),
    stance: String(payload?.stance || "").trim(),
    tags: normalizeList(payload?.tags, 8),
    catalysts: normalizeList(payload?.catalysts, 6),
    risks: normalizeList(payload?.risks, 6),
    content: summary,
  };
}

async function callXai({ apiKey, model, rawText, kol }) {
  const systemPrompt = [
    "你是A股社交媒体情绪与观点提炼助手。",
    "请从用户提供的KOL原文中提取结构化关键信息。",
    "不要编造不存在的信息，无法确认就留空或给空数组。",
    "标签尽量贴近A股交易语境。",
    "只输出一个JSON对象，不要输出任何额外说明。",
    'JSON结构必须为：{"kol":"","platform":"X / Grok","target":"","date":"","summary":"","stance":"偏多|中性|偏谨慎","tags":[],"catalysts":[],"risks":[]}',
  ].join("");

  const response = await fetch("https://api.x.ai/v1/chat/completions", {
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
        {
          role: "user",
          content: `KOL/来源：${kol || "未提供"}\n\n原文如下：\n${rawText}`,
        },
      ],
    }),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`xAI request failed: ${response.status} ${text}`);
  }

  const payload = await response.json();
  const text = String(payload?.choices?.[0]?.message?.content || "").trim();
  if (!text) {
    throw new Error("xAI returned empty output");
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

    const apiKey = String(process.env.XAI_API_KEY || "").trim();
    if (!apiKey) {
      throw new Error("Missing XAI_API_KEY");
    }

    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body || {};
    const rawText = String(body.text || "").trim();
    const date = String(body.date || "").trim();
    const kol = String(body.kol || "").trim();
    if (!rawText) {
      sendJson(res, 400, { error: "Missing text" });
      return;
    }
    if (!kol) {
      sendJson(res, 400, { error: "Missing kol" });
      return;
    }

    const model = String(process.env.XAI_MODEL || "grok-3-mini").trim();
    const structured = await callXai({ apiKey, model, rawText, kol });
    const analysis = sanitizeAnalysis(structured, rawText, date, kol);
    sendJson(res, 200, { analysis });
  } catch (error) {
    sendJson(res, 500, {
      error: "Social analyze failed",
      detail: error instanceof Error ? error.message : String(error),
    });
  }
};
