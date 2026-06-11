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
  const summary = String(payload?.summary || "").trim();
  const industry = String(payload?.industry || "").trim();
  const targets = normalizeList(payload?.targets, 12);
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
        description: "文中涉及的具体标的中文简称列表，最多8个",
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
