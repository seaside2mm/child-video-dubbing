import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Dashboard } from "../views/Dashboard";

vi.mock("../api", () => ({
  api: { confirmStage: vi.fn().mockResolvedValue({ status: "queued" }), enqueue: vi.fn() },
  resolveUrl: (path) => path || "",
}));
import { api } from "../api";

describe("Dashboard", () => {
  it("does not preview preserved song lyrics as dialogue", () => {
    render(<Dashboard
      data={{
        selectedProject: { id: "project-1", title: "Episode", status: "processing", current_stage: "rewrite" },
        jobs: [],
        health: { connected: true, services: {} },
        projects: [],
        preview: {},
        segments: [
          { id: "song-1", kind: "song", source_text: "歌曲歌词" },
          { id: "dialogue-1", kind: "dialogue", source_text: "原对白", target_text: "简化对白" },
        ],
        anomalies: [],
      }}
      onNewSeries={() => {}}
      onImport={() => {}}
      onNavigate={() => {}}
    />);

    expect(screen.getByText("简化对白")).toBeInTheDocument();
    expect(screen.queryByText("歌曲歌词")).not.toBeInTheDocument();
  });

  it("requires explicit confirmation before calling the next-stage API", async () => {
    const act = vi.fn(async (operation) => operation());
    render(<Dashboard
      data={{
        selectedProject: { id: "project-1", title: "Episode", status: "awaiting_confirmation", current_stage: "probe", pending_confirmation_stage: "probe", pending_confirmation_revision: 1, checkpoint: { completed_stages: ["probe"], pending_confirmation_stage: "probe", pending_confirmation_revision: 1 } },
        jobs: [{ id: "job-1", project_id: "project-1", status: "awaiting_confirmation", stage: "probe" }],
        health: { connected: true, services: {} },
        projects: [], preview: { source_url: "/source.mp4" }, segments: [], anomalies: [], characters: [], act, refresh: vi.fn(),
      }}
      onNewSeries={() => {}}
      onImport={() => {}}
      onNavigate={() => {}}
    />);

    expect(document.querySelector(".stage-review-badge")).toHaveTextContent("待你确认");
    fireEvent.click(screen.getByRole("button", { name: "确认并进入下一步" }));
    await waitFor(() => expect(api.confirmStage).toHaveBeenCalledWith("project-1", "probe", { revision: 1, accepted_warning_ids: [] }));
    expect(api.enqueue).not.toHaveBeenCalled();
  });

  it("keeps a later-stage blocker from disabling an earlier review", () => {
    render(<Dashboard
      data={{
        selectedProject: { id: "project-1", title: "Episode", status: "awaiting_confirmation", current_stage: "probe", pending_confirmation_stage: "probe", pending_confirmation_revision: 1, checkpoint: { completed_stages: ["probe"], pending_confirmation_stage: "probe", pending_confirmation_revision: 1 } },
        jobs: [{ id: "job-1", project_id: "project-1", status: "awaiting_confirmation", stage: "probe" }],
        health: { connected: true, services: {} }, projects: [], preview: { source_url: "/source.mp4" }, segments: [], characters: [], act: vi.fn(),
        anomalies: [{ id: "synth-blocker", stage: "synthesize", blocking: true, severity: "blocking", message: "配音长度超出时间窗" }],
      }}
      onNewSeries={() => {}}
      onImport={() => {}}
      onNavigate={() => {}}
    />);

    expect(screen.getByRole("button", { name: "确认并进入下一步" })).toBeEnabled();
    expect(screen.getByText("需要处理 1 项")).toBeInTheDocument();
  });

  it("keeps candidate download locked until export is confirmed", () => {
    render(<Dashboard
      data={{
        selectedProject: { id: "project-1", title: "Episode", status: "awaiting_confirmation", current_stage: "export", pending_confirmation_stage: "export", pending_confirmation_revision: 1, output_sha256: "abc", checkpoint: { completed_stages: ["probe", "separate", "transcribe", "diarize", "characters", "rewrite", "synthesize", "mix", "subtitle", "export"], confirmed_stages: ["probe", "separate", "transcribe", "diarize", "characters", "rewrite", "synthesize", "mix", "subtitle"], pending_confirmation_stage: "export", pending_confirmation_revision: 1 } },
        jobs: [{ id: "job-1", project_id: "project-1", status: "awaiting_confirmation", stage: "export" }],
        health: { connected: true, services: {} }, projects: [], preview: { ready: true, output_url: "/candidate.mp4", output_sha256: "abc" }, segments: [], anomalies: [], characters: [], act: vi.fn(),
      }}
      onNewSeries={() => {}}
      onImport={() => {}}
      onNavigate={() => {}}
    />);

    expect(screen.getByRole("button", { name: "最终确认后可下载" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "确认成片并完成" })).toBeEnabled();
  });

  it("starts legacy checkpoint review from the earliest unconfirmed stage", () => {
    render(<Dashboard
      data={{
        selectedProject: { id: "project-1", title: "Episode", status: "blocked", current_stage: "synthesize", checkpoint: { completed_stages: ["probe", "separate", "rewrite"], confirmed_stages: [] } },
        jobs: [{ id: "job-1", project_id: "project-1", status: "blocked", stage: "synthesize" }],
        health: { connected: true, services: {} }, projects: [], preview: { source_url: "/source.mp4" }, segments: [], anomalies: [{ id: "synth-blocker", stage: "synthesize", blocking: true, severity: "blocking", message: "配音仍待处理" }], characters: [], act: vi.fn(),
      }}
      onNewSeries={() => {}}
      onImport={() => {}}
      onNavigate={() => {}}
    />);

    expect(screen.getByRole("heading", { name: "检查媒体" })).toBeInTheDocument();
    expect(screen.getByText("这是升级前已生成的阶段结果。开始复核后会从最早未确认项开始，复用有效缓存并逐关等待确认。")).toBeInTheDocument();
    expect(screen.getByText("旧版结果先逐项复核；后续异常会在所属阶段处理。")).toBeInTheDocument();
    expect(screen.queryByText("本阶段完成后会自动停住，等待你的确认。")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "开始逐项复核" })).toHaveLength(2);
  });
});
