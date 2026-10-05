import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProgressSteps } from "../components/Ui";

describe("ProgressSteps", () => {
  it("prefers the latest job stage over a stale project snapshot", () => {
    render(<ProgressSteps
      project={{ id: "project-1", current_stage: "transcribe", progress: 0.3, checkpoint: { completed_stages: ["probe", "separate", "transcribe", "diarize"], confirmed_stages: ["probe", "separate", "transcribe", "diarize"] } }}
      jobs={[{ project_id: "project-1", stage: "synthesize", status: "running", progress: 0.6 }]}
    />);

    expect(screen.getByText("配音").closest("li")).toHaveClass("current");
    expect(screen.getByText("60%")).toBeInTheDocument();
    expect(screen.getByText("转写").closest("li")).toHaveClass("done");
  });

  it("does not mark a completed checkpoint as done until a person confirms it", () => {
    render(<ProgressSteps
      project={{ id: "project-1", status: "awaiting_confirmation", current_stage: "separate", checkpoint: { completed_stages: ["probe", "separate"], confirmed_stages: ["probe"], pending_confirmation_stage: "separate" } }}
      jobs={[{ project_id: "project-1", stage: "separate", status: "awaiting_confirmation", progress: 0.2 }]}
    />);

    expect(screen.getByText("检查").closest("li")).toHaveClass("done");
    expect(screen.getByText("分离").closest("li")).toHaveClass("current", "awaiting");
    expect(screen.getByText("待确认")).toBeInTheDocument();
  });

  it("marks legacy completed stages as awaiting review", () => {
    render(<ProgressSteps
      project={{ id: "project-1", status: "blocked", current_stage: "synthesize", checkpoint: { completed_stages: ["probe", "separate", "rewrite"], confirmed_stages: [] } }}
      jobs={[{ project_id: "project-1", stage: "synthesize", status: "blocked", progress: 0.6 }]}
    />);

    expect(screen.getByText("检查").closest("li")).toHaveClass("current");
    expect(screen.getAllByText("待复核")).toHaveLength(3);
    expect(screen.getByText("输出").closest("li")).toHaveTextContent("未开始");
  });
});
