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

function decodeHtmlEntities(value) {
  return String(value || "")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x2F;/g, "/")
    .replace(/&#(\d+);/g, (_, code) => String.fromCharCode(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCharCode(parseInt(code, 16)))
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function extractTweetId(url) {
  const text = String(url || "").trim();
  const match = text.match(/(?:twitter\.com|x\.com)\/[^/]+\/status(?:es)?\/(\d+)/i);
  return match ? match[1] : "";
}

function extractTweetHandle(url) {
  const text = String(url || "").trim();
  const match = text.match(/(?:twitter\.com|x\.com)\/([^/?#]+)\/status/i);
  return match ? match[1].replace(/^@+/, "") : "";
}

function normalizeTweetUrl(url) {
  const text = String(url || "").trim();
  if (!text) return "";
  return text.replace(/^https:\/\/twitter\.com\//i, "https://x.com/").split("?")[0];
}

async function fetchTweetViaSyndication(tweetId) {
  const response = await fetch(`https://cdn.syndication.twimg.com/tweet-result?id=${encodeURIComponent(tweetId)}&lang=en`, {
    headers: {
      "User-Agent": "Mozilla/5.0 stockkiller-dashboard",
      Accept: "application/json,text/plain,*/*",
    },
  });
  if (!response.ok) {
    throw new Error(`syndication fetch failed: ${response.status}`);
  }
  const payload = await response.json();
  const text = decodeHtmlEntities(payload?.text || payload?.full_text || "");
  if (!text) {
    throw new Error("syndication returned empty tweet text");
  }
  const user = payload?.user || {};
  return {
    text,
    kol: String(user.name || user.screen_name || "").trim(),
    handle: String(user.screen_name || "").replace(/^@+/, "").trim(),
    date: String(payload?.created_at || "").trim(),
  };
}

async function fetchTweetViaOembed(url) {
  const endpoint = `https://publish.twitter.com/oembed?omit_script=true&url=${encodeURIComponent(url)}`;
  const response = await fetch(endpoint, {
    headers: {
      "User-Agent": "Mozilla/5.0 stockkiller-dashboard",
      Accept: "application/json,text/plain,*/*",
    },
  });
  if (!response.ok) {
    throw new Error(`oEmbed fetch failed: ${response.status}`);
  }
  const payload = await response.json();
  const htmlText = decodeHtmlEntities(payload?.html || "");
  const text = htmlText
    .replace(/\s*—\s*.+?\(@[^)]+\)\s*[A-Z][a-z]+ \d{1,2}, \d{4}.*$/s, "")
    .trim();
  if (!text) {
    throw new Error("oEmbed returned empty tweet text");
  }
  return {
    text,
    kol: String(payload?.author_name || "").trim(),
    handle: extractTweetHandle(payload?.author_url || url),
    date: "",
  };
}

async function fetchTweetFromUrl(url) {
  const normalizedUrl = normalizeTweetUrl(url);
  const tweetId = extractTweetId(normalizedUrl);
  if (!normalizedUrl || !tweetId) {
    throw new Error("Invalid X tweet URL");
  }
  const errors = [];
  try {
    const result = await fetchTweetViaSyndication(tweetId);
    return { ...result, tweetUrl: normalizedUrl };
  } catch (error) {
    errors.push(error instanceof Error ? error.message : String(error));
  }
  try {
    const result = await fetchTweetViaOembed(normalizedUrl);
    return { ...result, tweetUrl: normalizedUrl };
  } catch (error) {
    errors.push(error instanceof Error ? error.message : String(error));
  }
  throw new Error(`Unable to fetch tweet text: ${errors.join("; ")}`);
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

function sanitizeAnalysis(payload, rawText, fallbackDate, fallbackKol, sourceMeta = {}) {
  const kol = String(payload?.kol || fallbackKol || "").trim();
  const targets = normalizeList(payload?.targets, 12);
  const target = String(payload?.target || targets[0] || "未提及").trim();
  const summary = String(payload?.summary || "").trim();
  const translatedText = String(payload?.translatedText || payload?.translation || "").trim();
  if (!kol || !summary) {
    throw new Error("Model output missing kol or summary");
  }
  return {
    kol,
    handle: String(payload?.handle || sourceMeta.handle || "").replace(/^@+/, "").trim(),
    platform: String(payload?.platform || "X").trim(),
    target,
    targets: targets.length ? targets : (target && target !== "未提及" ? [target] : []),
    industry: String(payload?.industry || "").trim(),
    date: String(payload?.date || sourceMeta.date || fallbackDate || "").trim(),
    summary,
    translatedText,
    rawText: String(rawText || "").trim(),
    tweetUrl: String(sourceMeta.tweetUrl || "").trim(),
    sourceUrl: String(sourceMeta.tweetUrl || "").trim(),
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
    "请从用户提供的X推文原文中提取结构化关键信息，并翻译成中文。",
    "不要编造不存在的信息，无法确认就留空或给空数组。",
    "标签尽量贴近A股交易语境。",
    "summary 必须用中文概括推文核心观点。",
    "translatedText 必须是推文原文的中文翻译；如果原文已经是中文，就原样整理为中文。",
    "target/targets 填涉及的股票、公司、行业主题或资产；没有明确提及时 target 填“未提及”。",
    "只输出一个JSON对象，不要输出任何额外说明。",
    'JSON结构必须为：{"kol":"","handle":"","platform":"X","target":"","targets":[],"industry":"","date":"","summary":"","translatedText":"","stance":"偏多|中性|偏谨慎","tags":[],"catalysts":[],"risks":[]}',
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
          content: `KOL/来源：${kol || "未提供"}\n\nX推文原文如下：\n${rawText}`,
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
    const tweetUrl = normalizeTweetUrl(body.tweetUrl || body.url || "");
    let rawText = String(body.text || "").trim();
    const date = String(body.date || "").trim();
    let kol = String(body.kol || "").trim();
    let sourceMeta = { tweetUrl };
    if (tweetUrl && !rawText) {
      const tweet = await fetchTweetFromUrl(tweetUrl);
      rawText = tweet.text;
      kol = kol || tweet.kol || tweet.handle;
      sourceMeta = {
        tweetUrl: tweet.tweetUrl,
        handle: tweet.handle || extractTweetHandle(tweet.tweetUrl),
        date: tweet.date || "",
      };
    }
    if (!rawText) {
      sendJson(res, 400, { error: "Missing text or tweetUrl" });
      return;
    }
    if (!kol) {
      kol = extractTweetHandle(tweetUrl) || "X";
    }

    const model = String(process.env.XAI_MODEL || "grok-3-mini").trim();
    const structured = await callXai({ apiKey, model, rawText, kol });
    const analysis = sanitizeAnalysis(structured, rawText, date, kol, sourceMeta);
    sendJson(res, 200, { analysis });
  } catch (error) {
    sendJson(res, 500, {
      error: "Social analyze failed",
      detail: error instanceof Error ? error.message : String(error),
    });
  }
};
