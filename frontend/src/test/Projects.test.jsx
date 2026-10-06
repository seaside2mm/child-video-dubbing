import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Projects } from "../views/Projects";

vi.mock("../api", () => ({
  api: {
    mediaUrl: (_projectId, path) => `/api/projects/project-1/media/${path}`,
    regenerateSegment: vi.fn(),
    updateSegment: vi.fn().mockResolvedValue({ id: "line-1" }),
  },
  resolveUrl: (path) => path || "",
}));
import { api } from "../api";

function projectData({ status = "awaiting_confirmation", pendingStage = "subtitle" } = {}) {
  const project = {
    id: "project-1",
    title: "Episode",
    status,
    speed: 0.82,
    pending_confirmation_stage: pendingStage,
    checkpoint: { pending_confirmation_stage: pendingStage, confirmed_stages: Array(10).fill("confirmed") },
  };
  return {
    selectedProject: project,
    selectedProjectId: project.id,
    setSelectedProjectId: vi.fn(),
    projects: [project],
    jobs: [],
    characters: [],
    health: { connected: true },
    segments: [
      { id: "line-1", kind: "dialogue", status: "synthesized", start: 1, end: 2, source_text: "Hello.", target_text: "你好。", speed: 0.82, audio_url: "/line-1.wav" },
      { id: "song-1", kind: "song", status: "synthesized", start: 2, end: 3, source_text: "Song", target_text: "歌词", speed: 0.82, audio_url: "/song.wav" },
    ],
    act: async (operation) => operation(),
  };
}

describe("Projects stage review editing", () => {
  it("lets the user correct dialogue text or rate from subtitle review, but not edit songs", async () => {
    render(<Projects data={projectData()} onImport={() => {}}/>);

    const targetText = screen.getByLabelText("片段 line-1 目标文本");
    const rate = screen.getAllByLabelText(/语速/)[0];
    const songText = screen.getByLabelText("片段 song-1 目标文本");
    expect(targetText).toBeEnabled();
    expect(rate).toBeEnabled();
    expect(songText).toBeDisabled();

    fireEvent.change(targetText, { target: { value: "你好！" } });
    const saveButtons = screen.getAllByRole("button", { name: "保存" });
    fireEvent.click(saveButtons[0]);
    await waitFor(() => expect(api.updateSegment).toHaveBeenCalledWith("project-1", "line-1", { target_text: "你好！" }));
  });

  it("allows post-completion dialogue regeneration with warnings, never song regeneration", () => {
    render(<Projects data={projectData({ status: "completed_with_warnings", pendingStage: null })} onImport={() => {}}/>);

    const regenerateButtons = screen.getAllByRole("button", { name: "重生成" });
    expect(regenerateButtons[0]).toBeEnabled();
    expect(regenerateButtons[1]).toBeDisabled();
  });
});
