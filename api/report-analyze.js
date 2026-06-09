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

function normalizeList(values, limit) {
  if (!Array.isArray(values)) return [];
  return values
    .map((value) => String(value || "").trim())
    .filter(Boolean)
    .slice(0, limit);
}

function sanitizeAnalysis(payload, rawText, fallbackDate) {
  const target = String(payload?.target || "").trim();
  const summary = String(payload?.summary || "").trim();
  if (!target || !summary) {
    throw new Error("Model output missing target or summary");
  }
  return {
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

    const apiKey = String(process.env.OPENAI_API_KEY || "").trim();
    if (!apiKey) {
      throw new Error("Missing OPENAI_API_KEY");
    }

    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
    const rawText = String(body.text || "").trim();
    const date = String(body.date || "").trim();
    if (!rawText) {
      sendJson(res, 400, { error: "Missing text" });
      return;
    }

    const model = String(process.env.OPENAI_MODEL || "gpt-5-mini").trim();
    const schema = {
      type: "object",
      additionalProperties: false,
      required: ["target", "summary", "tags", "catalysts", "risks", "stance"],
      properties: {
        target: { type: "string", description: "报告核心标的名称，尽量使用中文证券简称" },
        date: { type: "string", description: "若原文中出现报告日期则提取，否则留空" },
        summary: { type: "string", description: "用中文提炼3句内的核心结论" },
        stance: {
          type: "string",
          enum: ["偏多", "中性", "偏谨慎"],
          description: "根据原文语气给出简短判断",
        },
        tags: {
          type: "array",
          items: { type: "string" },
          description: "3到6个简洁标签，例如 算力/CPO/业绩弹性",
        },
        catalysts: {
          type: "array",
          items: { type: "string" },
          description: "最多3条潜在催化",
        },
        risks: {
          type: "array",
          items: { type: "string" },
          description: "最多3条风险提示",
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
                  "你是A股投研助理。请从用户粘贴的投研原文中提取结构化关键信息。输出必须符合JSON schema。不要编造不存在的信息，无法确认就留空或给空数组。标签尽量贴近A股交易语境。",
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
    const analysis = sanitizeAnalysis(JSON.parse(text), rawText, date);
    sendJson(res, 200, { analysis });
  } catch (error) {
    sendJson(res, 500, {
      error: "Report analyze failed",
      detail: error instanceof Error ? error.message : String(error),
    });
  }
};
