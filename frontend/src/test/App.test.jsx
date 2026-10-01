import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { api } from "../api";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("offline UI", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline"))));

  it("keeps the product identity and reports that disconnected work is not complete", async () => {
    render(<App />);
    expect(screen.getByText("童声配音台")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "继续处理" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("本地后端未连接")).toBeInTheDocument());
    expect(screen.getByText(/演示状态不会标记任务完成/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /新建系列/ })).toBeDisabled();
  });
});

describe("independent health and data loading", () => {
  it("shows a real project while the health check is pending", async () => {
    let rejectHealth;
    vi.spyOn(api, "health").mockImplementation(() => new Promise((_, reject) => { rejectHealth = reject; }));
    vi.spyOn(api, "listSeries").mockResolvedValue([]);
    vi.spyOn(api, "listProjects").mockResolvedValue([{ id: "project-1", title: "真实项目标题", status: "created" }]);
    vi.spyOn(api, "settings").mockResolvedValue({ levels: { L1: "只保留常用短句" } });
    vi.spyOn(api, "getProject").mockResolvedValue({});
    vi.spyOn(api, "listSegments").mockResolvedValue([]);
    vi.spyOn(api, "listAnomalies").mockResolvedValue([]);
    vi.spyOn(api, "getPreview").mockResolvedValue(null);

    render(<App />);

    expect(await screen.findByRole("heading", { level: 3, name: "真实项目标题" })).toBeInTheDocument();
    expect(screen.getByText("本地服务运行中")).toBeInTheDocument();
    expect(screen.queryByText("本地后端未连接")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "设置" }));
    expect(await screen.findByText("只保留常用短句")).toBeInTheDocument();

    await act(async () => rejectHealth(new Error("health timeout")));
    await waitFor(() => expect(screen.getAllByText("未知").length).toBeGreaterThan(0));
    expect(screen.getByText("本地服务运行中")).toBeInTheDocument();
    expect(screen.queryByText("本地后端未连接")).not.toBeInTheDocument();
  });
});
