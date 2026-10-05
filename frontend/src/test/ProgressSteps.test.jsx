import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProgressSteps } from "../components/Ui";

describe("ProgressSteps", () => {
  it("prefers the latest job stage over a stale project snapshot", () => {
    render(<ProgressSteps
      project={{ id: "project-1", current_stage: "transcribe", progress: 0.3 }}
      jobs={[{ project_id: "project-1", stage: "synthesize", status: "running", progress: 0.6 }]}
    />);

    expect(screen.getByText("配音").closest("li")).toHaveClass("current");
    expect(screen.getByText("60%")).toBeInTheDocument();
    expect(screen.getByText("转写").closest("li")).toHaveClass("done");
  });
});
