const fs = require("fs");
const path = require("path");

const WATCHLIST_STATE_PATH = "reports/watchlist_state.json";
const RESEARCH_REPORTS_PATH = "reports/research_reports.json";

function setCors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Dashboard-Admin-Token");
}

function sendJson(res, status, payload) {
  setCors(res);
  res.status(status).json(payload);
}

function encodePath(filePath) {
  return filePath
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
}

function sanitizeWatchlistStatus(payload) {
  const out = {};
  if (!payload || typeof payload !== "object") return out;
  for (const [code, value] of Object.entries(payload)) {
    const key = String(code || "").trim();
    if (!key) continue;
    const normalized = String(value || "").trim();
    if (normalized === "removed") out[key] = "removed";
  }
  return out;
}

function sanitizeStrongJoinStatus(payload) {
  const out = {};
  if (!payload || typeof payload !== "object") return out;
  for (const [code, value] of Object.entries(payload)) {
    const key = String(code || "").trim();
    if (!key) continue;
    const normalized = String(value || "").trim();
    if (normalized === "joined") out[key] = "joined";
  }
  return out;
}

function sanitizeReports(payload) {
  if (!Array.isArray(payload)) return [];
  return payload
    .map((item, index) => {
      if (!item || typeof item !== "object") return null;
      const target = String(item.target || "").trim();
      const content = String(item.content || "").trim();
      if (!target || !content) return null;
      const date = String(item.date || "").trim();
      const createdAt = Number(item.createdAt || Date.now());
      const id = String(item.id || `report-${index + 1}-${createdAt}`);
      return { id, target, content, date, createdAt };
    })
    .filter(Boolean);
}

function readLocalJson(filePath, fallback) {
  try {
    const absolute = path.join(process.cwd(), filePath);
    return JSON.parse(fs.readFileSync(absolute, "utf-8"));
  } catch (error) {
    return fallback;
  }
}

async function githubRequest(filePath, options = {}) {
  const repo = process.env.GITHUB_REPO || "wendyzhang421/A-Stock-dashboard";
  const branch = process.env.GITHUB_BRANCH || "main";
  const githubToken = process.env.GITHUB_TOKEN;
  const url = `https://api.github.com/repos/${repo}/contents/${encodePath(filePath)}?ref=${encodeURIComponent(branch)}`;
  const headers = {
    Accept: "application/vnd.github+json",
    "User-Agent": "stockkiller-dashboard-sync",
  };
  if (githubToken) {
    headers.Authorization = `Bearer ${githubToken}`;
  }
  const response = await fetch(url, {
    ...options,
    headers: {
      ...headers,
      ...(options.headers || {}),
    },
  });
  return response;
}

async function readRepoJson(filePath, fallback) {
  const githubToken = process.env.GITHUB_TOKEN;
  if (!githubToken) {
    return { data: readLocalJson(filePath, fallback), sha: null, source: "local" };
  }
  const response = await githubRequest(filePath);
  if (response.status === 404) {
    return { data: fallback, sha: null, source: "github" };
  }
  if (!response.ok) {
    throw new Error(`GitHub read failed for ${filePath}: ${response.status}`);
  }
  const payload = await response.json();
  const content = Buffer.from(payload.content || "", "base64").toString("utf-8");
  return { data: JSON.parse(content), sha: payload.sha || null, source: "github" };
}

async function writeRepoJson(filePath, data, message, sha = null) {
  const githubToken = process.env.GITHUB_TOKEN;
  if (!githubToken) {
    throw new Error("Missing GITHUB_TOKEN");
  }
  const repo = process.env.GITHUB_REPO || "wendyzhang421/A-Stock-dashboard";
  const branch = process.env.GITHUB_BRANCH || "main";
  const url = `https://api.github.com/repos/${repo}/contents/${encodePath(filePath)}`;
  const body = {
    message,
    content: Buffer.from(JSON.stringify(data, null, 2) + "\n", "utf-8").toString("base64"),
    branch,
  };
  if (sha) body.sha = sha;
  const response = await fetch(url, {
    method: "PUT",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${githubToken}`,
      "User-Agent": "stockkiller-dashboard-sync",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`GitHub write failed for ${filePath}: ${response.status} ${text}`);
  }
  const payload = await response.json();
  return payload.content?.sha || null;
}

async function loadStateBundle() {
  const watch = await readRepoJson(WATCHLIST_STATE_PATH, {
    watchlistStatus: {},
    strongJoinStatus: {},
    updatedAt: "",
  });
  const reports = await readRepoJson(RESEARCH_REPORTS_PATH, []);
  return {
    watchlistStatus: sanitizeWatchlistStatus(watch.data.watchlistStatus || {}),
    strongJoinStatus: sanitizeStrongJoinStatus(watch.data.strongJoinStatus || {}),
    reports: sanitizeReports(reports.data),
    updatedAt: String(watch.data.updatedAt || ""),
    watchlistSha: watch.sha,
    reportsSha: reports.sha,
    source: watch.source === "github" || reports.source === "github" ? "github" : "local",
  };
}

module.exports = async (req, res) => {
  setCors(res);
  if (req.method === "OPTIONS") {
    res.status(204).end();
    return;
  }

  try {
    if (req.method === "GET") {
      const payload = await loadStateBundle();
      sendJson(res, 200, payload);
      return;
    }

    if (req.method !== "POST") {
      sendJson(res, 405, { error: "Method not allowed" });
      return;
    }

    const expectedToken = process.env.DASHBOARD_ADMIN_TOKEN || "";
    const providedToken = String(req.headers["x-dashboard-admin-token"] || "").trim();
    if (!expectedToken || providedToken !== expectedToken) {
      sendJson(res, 401, { error: "Unauthorized" });
      return;
    }

    const current = await loadStateBundle();
    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
    const nextWatchlistStatus = Object.prototype.hasOwnProperty.call(body, "watchlistStatus")
      ? sanitizeWatchlistStatus(body.watchlistStatus)
      : current.watchlistStatus;
    const nextStrongJoinStatus = Object.prototype.hasOwnProperty.call(body, "strongJoinStatus")
      ? sanitizeStrongJoinStatus(body.strongJoinStatus)
      : current.strongJoinStatus;
    const nextReports = Object.prototype.hasOwnProperty.call(body, "reports")
      ? sanitizeReports(body.reports)
      : current.reports;
    const updatedAt = new Date().toISOString();

    const watchPayload = {
      watchlistStatus: nextWatchlistStatus,
      strongJoinStatus: nextStrongJoinStatus,
      updatedAt,
    };
    await writeRepoJson(
      WATCHLIST_STATE_PATH,
      watchPayload,
      `Update dashboard watchlist state ${updatedAt}`,
      current.watchlistSha
    );
    await writeRepoJson(
      RESEARCH_REPORTS_PATH,
      nextReports,
      `Update dashboard research reports ${updatedAt}`,
      current.reportsSha
    );

    sendJson(res, 200, {
      watchlistStatus: nextWatchlistStatus,
      strongJoinStatus: nextStrongJoinStatus,
      reports: nextReports,
      updatedAt,
      source: "github",
    });
  } catch (error) {
    sendJson(res, 500, {
      error: "State sync failed",
      detail: error instanceof Error ? error.message : String(error),
    });
  }
};
