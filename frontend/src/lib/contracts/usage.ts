
export interface TokenVector {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens: number;
  cache_creation_input_tokens: number;
  reasoning_tokens: number;
}

export type UsageAccountingStatus = "complete" | "partial" | "unavailable";
export type UsageCostSource = "provider_reported" | "configured_estimate" | "unavailable";

export interface UsageBucket {
  key: string;
  label: string;
  reported: TokenVector;
  estimated: TokenVector;
  call_count: number;
  estimated_cost_micro_usd: number | null;
  cost_source: UsageCostSource;
  average_latency_ms?: number | null;
  completeness?: UsageAccountingStatus;
}

export interface RunUsageResponse {
  schema_version: 1;
  run_id: string;
  status: string;
  duration_ms: number | null;
  revision: number;
  updated_at: number | null;
  accounting_status: UsageAccountingStatus;
  unavailable_reason?: "no_usage_events" | "run_not_observed" | "accounting_disabled" | "storage_unavailable";
  totals: {
    reported: TokenVector;
    estimated: TokenVector;
    calls: {
      attempts: number;
      successful_responses: number;
      provider_reported: number;
      provider_partial: number;
      estimated: number;
      missing: number;
      unknown_failed_attempts: number;
      legacy_unclassified: number;
      coverage_ratio: number;
    };
    cost: {
      estimated_cost_micro_usd: number | null;
      cost_source: UsageCostSource;
      price_table_hash: string | null;
    };
    budgets: Record<string, { settled: number | null; estimated: number; reserved: number; limit: number | null }>;
  };
  breakdowns: {
    by_stage: UsageBucket[];
    by_agent_role: UsageBucket[];
    by_model: UsageBucket[];
    by_task: UsageBucket[];
    by_stage_gateway?: Array<{
      stage: string;
      calls: number;
      total_tokens: number;
      spend_micro_usd: number;
    }>;
  };
  timeline: Array<{
    timestamp: number;
    reported_tokens: number;
    estimated_tokens: number;
    reported_cumulative: number;
    estimated_cumulative: number;
    call_count: number;
    retry_count: number;
  }>;
  task_operations?: Record<string, { model_call_count: number; tool_call_count: number; tool_success_count: number; tool_failed_count: number }>;
  operations: {
    llm_call_count: number;
    retry_count: number;
    rate_limited_count: number;
    rate_429: number;
    cache_hit_rate: number;
    cache_input_ratio: number;
    reasoning_output_ratio: number;
    output_tokens_per_second: number;
    tool_call_count: number;
    tool_success_rate: number | null;
    empty_tool_result_count: number;
    zero_source_search_count: number;
  };
}

export interface UsageAnalyticsResponse {
  schema_version: 1;
  range: "7d" | "30d" | "retained";
  timezone: string;
  retention_days: number;
  actual_range_days: number;
  summary: {
    run_count: number;
    reported: TokenVector;
    estimated: TokenVector;
    estimated_cost_micro_usd: number | null;
    coverage_ratio: number;
    gateway_spend_micro_usd?: number | null;
    gateway_attributed_runs?: number | null;
    gateway_status?: "ok" | "unavailable";
  };
  daily: Array<{
    date: string;
    reported_tokens: number;
    estimated_tokens: number;
    run_count: number;
    coverage_ratio: number;
    rate_429: number;
    cache_hit_rate: number;
    output_tokens_per_second: number;
  }>;
  distributions: {
    provider: Array<{ key: string; reported_tokens: number; estimated_tokens: number; call_count: number }>;
    model: Array<{ key: string; reported_tokens: number; estimated_tokens: number; call_count: number }>;
    status: Array<{ key: string; reported_tokens: number; estimated_tokens: number; run_count: number }>;
  };
  runs: Array<{
    run_id: string;
    title: string;
    status: string;
    started_at: number;
    ended_at: number | null;
    duration_ms: number | null;
    accounting_status: UsageAccountingStatus;
    reported: TokenVector;
    estimated: TokenVector;
    calls: RunUsageResponse["totals"]["calls"];
    cost: RunUsageResponse["totals"]["cost"];
    operations: RunUsageResponse["operations"];
  }>;
  next_cursor: string | null;
}
