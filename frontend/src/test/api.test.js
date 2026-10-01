import { afterEach, describe, expect, it, vi } from "vitest";
import { api, normalizeList } from "../api";

afterEach(() => vi.restoreAllMocks());

describe("API contract client", () => {
  it("normalizes list envelopes without inventing rows", () => {
    expect(normalizeList({ items: [{ id: 1 }] })).toEqual([{ id: 1 }]);
    expect(normalizeList({ projects: [{ id: 2 }] })).toEqual([{ id: 2 }]);
    expect(normalizeList(null)).toEqual([]);
  });

  it("marks an unreachable backend as a real connection error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    await expect(api.health()).rejects.toMatchObject({ name: "ApiError", code: "network_error" });
  });

  it("uses the project-level job enqueue contract", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ job_id: "job-1" }), { status: 200, headers: { "content-type": "application/json" } })));
    await api.enqueue("project-9");
    expect(fetch).toHaveBeenCalledWith("/api/projects/project-9/jobs", expect.objectContaining({ method: "POST", body: JSON.stringify({ kind: "process", from_stage: "auto", force: false }), headers: { "Content-Type": "application/json" } }));
  });

  it("posts a local source path to the series project import route", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "project-1" }), { status: 200, headers: { "content-type": "application/json" } })));
    await api.importProject({ seriesId: "series-1", sourcePath: "Z:\\videos\\episode-01.mp4", title: "第 01 集" });
    expect(fetch).toHaveBeenCalledWith("/api/series/series-1/projects", expect.objectContaining({ method: "POST", body: JSON.stringify({ source_path: "Z:\\videos\\episode-01.mp4", title: "第 01 集" }) }));
  });

  it("preserves structured backend failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: "模型缺失", code: "dependency_missing", action: "请安装模型" } }), { status: 503, headers: { "content-type": "application/json" } })));
    await expect(api.health()).rejects.toEqual(expect.objectContaining({ message: "模型缺失", status: 503, code: "dependency_missing", action: "请安装模型" }));
  });
});
