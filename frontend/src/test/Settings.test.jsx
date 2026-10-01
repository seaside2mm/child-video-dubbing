import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Settings } from "../views/Settings";

describe("settings view", () => {
  it("renders level rules returned as an object by the backend", () => {
    render(<Settings data={{
      health: { connected: true, overall: "healthy", services: {}, capabilities: {}, app: {} },
      settings: { levels: { L1: "只保留最常用的短句", L2: { rule: "可加入简单连接句", default_speed: 0.86 } } },
      recheck: () => {},
    }} />);

    expect(screen.getByText("L1")).toBeInTheDocument();
    expect(screen.getByText("只保留最常用的短句")).toBeInTheDocument();
    expect(screen.getByText("L2")).toBeInTheDocument();
    expect(screen.getByText("可加入简单连接句 · 0.86×")).toBeInTheDocument();
  });
});
