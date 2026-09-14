import { readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { emptyRunState, hydrateSnapshot, reducePublicEvent } from "@/lib/run-reducer";
import type { PublicEvent, RunSnapshot } from "@/lib/types";
import { RunQualityStatus } from "./run-quality-status";

const root = process.env.QUALITY_GATE_REPLAY_DIR ?? path.resolve(process.cwd(), "../output/playwright/quality-gate-20260906");

describe.skipIf(!existsSync(path.join(root, "run-response.json")))("recorded failed run integration", () => {
  afterEach(cleanup);

  it("keeps the recorded SSE terminal outcome consistent with the API snapshot", () => {
    const snapshot: RunSnapshot = JSON.parse(readFileSync(path.join(root, "run-response.json"), "utf8"));
    const events: PublicEvent[] = readFileSync(path.join(root, "public_events.jsonl"), "utf8")
      .split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
    const streamed = events.reduce(reducePublicEvent, emptyRunState(snapshot.run_id));
    const hydrated = hydrateSnapshot(emptyRunState(snapshot.run_id), snapshot);
    expect(streamed.status).toBe("failed");
    expect(streamed.resultStatus).toBe("failed");
    expect(streamed.terminationReason).toBe("max_turns_drained");
    expect(streamed.report).toBe("");
    expect(hydrated.status).toBe(streamed.status);
    expect(hydrated.resultStatus).toBe(streamed.resultStatus);
    expect(hydrated.report).toBe(streamed.report);
    const resumed = hydrateSnapshot(streamed, snapshot);
    render(<RunQualityStatus state={resumed} />);
    expect(screen.getByText("研究失败")).toBeInTheDocument();
    expect(screen.getByText(/本次运行未生成报告/)).toBeInTheDocument();
    expect(screen.queryByText(/报告已生成/)).not.toBeInTheDocument();
    expect(screen.queryByText(/以部分结果结束/)).not.toBeInTheDocument();
  });
});
