import { execFileSync } from "node:child_process";

function docker(...args: string[]) {
  return execFileSync("docker", args, { encoding: "utf8", timeout: 60_000 }).trim();
}

/** Fault injection is restricted to a run in the runner's isolated Compose project. */
export function interruptNativeRun(runId: string, scenario: string) {
  if (process.env.E2E_MANAGED_STACK !== "true" || !process.env.COMPOSE_PROJECT_NAME) {
    throw new Error("故障注入仅允许 run_native_e2e.py 创建的专用项目");
  }
  const project = process.env.COMPOSE_PROJECT_NAME;
  let ids: string[];
  if (scenario === "worker-kill") {
    ids = docker("ps", "-q", "--filter", `label=insightforge.run_id=${runId}`, "--filter", "label=insightforge.role=native-team").split(/\s+/).filter(Boolean);
  } else if (scenario === "api-gateway-kill") {
    ids = ["api", "sandbox-gateway"].flatMap(service => docker("ps", "-q", "--filter", `label=com.docker.compose.project=${project}`, "--filter", `label=com.docker.compose.service=${service}`).split(/\s+/).filter(Boolean));
    if (ids.length !== 2) throw new Error("专用 API/Gateway 容器不完整");
  } else {
    throw new Error(`不支持的故障场景 ${scenario}`);
  }
  if (!ids.length) return false;
  try {
    for (const id of ids) docker("kill", "--signal=KILL", id);
  } finally {
    // 团队 Worker 由正式 Controller 重建；仅恢复刚被本测试停止的 API/Gateway。
    if (scenario === "api-gateway-kill") for (const id of ids) docker("start", id);
  }
  return true;
}
