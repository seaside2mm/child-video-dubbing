import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Dashboard } from "../views/Dashboard";

describe("Dashboard", () => {
  it("does not preview preserved song lyrics as dialogue", () => {
    render(<Dashboard
      data={{
        selectedProject: { id: "project-1", title: "Episode", status: "processing" },
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
});
