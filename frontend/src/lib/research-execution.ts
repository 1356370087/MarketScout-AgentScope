export type ResearchExecutionMode = "adaptive" | "sync" | "collaborator" | "teams";

export const researchExecutionLabels: Record<ResearchExecutionMode, string> = {
  adaptive: "自适应研究", sync: "同步委派", collaborator: "异步委派", teams: "团队协作",
};

export function researchExecutionMode(settings: Record<string, unknown>): ResearchExecutionMode {
  if (settings.enable_async_research && settings.async_research_mode === "teams") return "teams";
  if (settings.research_execution_strategy !== "delegated") return "adaptive";
  return settings.enable_async_research ? "collaborator" : "sync";
}

export function researchExecutionConfig(mode: ResearchExecutionMode) {
  return {
    research_execution_strategy: mode === "adaptive" ? "adaptive" : "delegated",
    enable_async_research: mode === "collaborator" || mode === "teams",
    async_research_mode: mode === "teams" ? "teams" : "collaborator",
  };
}
