const DEFAULT_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(message, { status = 0, code = "network_error", action = "", details = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.action = action;
    this.details = details;
  }
}

export function normalizeList(value) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return [];
  for (const key of ["items", "data", "results", "series", "projects", "jobs", "segments", "anomalies", "characters"]) {
    if (Array.isArray(value[key])) return value[key];
  }
  return [];
}

export function resolveUrl(path) {
  if (!path) return "";
  if (/^https?:\/\//i.test(path)) return path;
  return `${DEFAULT_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

function pathPart(value) {
  return encodeURIComponent(String(value));
}

async function readBody(response) {
  const contentType = response.headers.get("content-type") || "";
  if (response.status === 204) return null;
  if (contentType.includes("application/json")) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  return response.text();
}

async function request(path, options = {}) {
  let response;
  try {
    const headers = options.body instanceof FormData
      ? { ...options.headers }
      : { "Content-Type": "application/json", ...options.headers };
    response = await fetch(resolveUrl(path), { ...options, headers });
  } catch (error) {
    throw new ApiError("无法连接本地后端，请先运行启动器。", { details: error.message });
  }

  const body = await readBody(response);
  if (!response.ok) {
    const error = body?.error || body || {};
    const message = error.message || error.detail || `请求失败（HTTP ${response.status}）`;
    throw new ApiError(message, {
      status: response.status,
      code: error.code || "request_failed",
      action: error.action || "",
      details: error.details || body,
    });
  }
  return body;
}

export const api = {
  health: () => request("/api/health"),
  settings: () => request("/api/settings"),
  recheckSettings: () => request("/api/settings/recheck", { method: "POST" }),
  listSeries: async () => normalizeList(await request("/api/series")),
  createSeries: (payload) => request("/api/series", { method: "POST", body: JSON.stringify(payload) }),
  listProjects: async ({ seriesId, status } = {}) => {
    const params = new URLSearchParams();
    if (seriesId) params.set("series_id", seriesId);
    if (status) params.set("status", status);
    const suffix = params.toString() ? `?${params}` : "";
    return normalizeList(await request(`/api/projects${suffix}`));
  },
  getProject: (projectId) => request(`/api/projects/${pathPart(projectId)}`),
  importProject: ({ sourcePath, seriesId, title }) => {
    if (!sourcePath || !seriesId) {
      throw new ApiError("请输入视频的完整本地路径并选择所属系列。", { code: "invalid_import" });
    }
    return request(`/api/series/${pathPart(seriesId)}/projects`, {
      method: "POST",
      body: JSON.stringify({ source_path: sourcePath, ...(title ? { title } : {}) }),
    });
  },
  enqueue: (projectId, payload = {}) => request(`/api/projects/${pathPart(projectId)}/jobs`, {
    method: "POST",
    body: JSON.stringify({ kind: "process", from_stage: "auto", force: false, ...payload }),
  }),
  cancelJob: (jobId) => request(`/api/jobs/${pathPart(jobId)}/cancel`, { method: "POST" }),
  retryJob: (jobId, fromStage) => request(`/api/jobs/${pathPart(jobId)}/retry`, {
    method: "POST",
    body: JSON.stringify(fromStage ? { from_stage: fromStage } : {}),
  }),
  confirmStage: (projectId, stage, payload) => request(`/api/projects/${pathPart(projectId)}/stages/${pathPart(stage)}/confirm`, {
    method: "POST",
    body: JSON.stringify(payload),
  }),
  getJob: (jobId) => request(`/api/jobs/${pathPart(jobId)}`),
  listSegments: async (projectId) => normalizeList(await request(`/api/projects/${pathPart(projectId)}/segments`)),
  listAnomalies: async (projectId) => normalizeList(await request(`/api/projects/${pathPart(projectId)}/anomalies`)),
  listCharacters: async (seriesId) => normalizeList(await request(`/api/series/${pathPart(seriesId)}/characters`)),
  updateSegment: (projectId, segmentId, patch) => request(`/api/projects/${pathPart(projectId)}/segments/${pathPart(segmentId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  }),
  regenerateSegment: (projectId, segmentId) => request(`/api/projects/${pathPart(projectId)}/segments/${pathPart(segmentId)}/regenerate`, { method: "POST" }),
  getPreview: (projectId) => request(`/api/projects/${pathPart(projectId)}/preview`),
  mediaUrl: (projectId, kind) => `/api/projects/${pathPart(projectId)}/media/${String(kind).split("/").map(pathPart).join("/")}`,
  exportProject: (projectId, burnSubtitles = true) => request(`/api/projects/${pathPart(projectId)}/export`, {
    method: "POST",
    body: JSON.stringify({ burn_subtitles: burnSubtitles }),
  }),
};
