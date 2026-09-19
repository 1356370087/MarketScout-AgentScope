import type { RunUsageResponse, UsageAnalyticsResponse } from "../contracts/usage";
import { apiFetch } from "./http";

export const usageApi = {
  runUsage: (id: string) => apiFetch<RunUsageResponse>(`/runs/${encodeURIComponent(id)}/usage`),
  usageAnalytics: (params: Record<string, string | number | undefined> = {}) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); });
    return apiFetch<UsageAnalyticsResponse>(`/usage/analytics${query.size ? `?${query}` : ""}`);
  },
};
