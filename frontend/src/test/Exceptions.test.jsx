import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Exceptions } from "../views/Exceptions";

describe("Exceptions", () => {
  it("labels project-level processing failures without a character as project-level", () => {
    render(<Exceptions data={{
      anomalies: [{ id: "tts-failure", kind: "OMNIVOICE_TTS_FAILED", severity: "blocking", blocking: true, resolved: false, message: "合成失败" }],
      selectedProject: { id: "project-1" },
    }} />);

    expect(screen.getByRole("heading", { name: "项目级异常" })).toBeInTheDocument();
    expect(screen.queryByText("未匹配角色")).not.toBeInTheDocument();
  });
});
