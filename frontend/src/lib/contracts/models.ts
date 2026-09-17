
export interface ModelCatalogEntryInfo {
  name: string;
  base_model?: string | null;
  context_window: number;
  max_output_tokens: number;
  input_cost_per_token: number;
  output_cost_per_token: number;
}

export interface ModelCatalogResponse {
  backend: string;
  models: ModelCatalogEntryInfo[];
  role_aliases: Record<string, string>;
  stale: boolean;
  error?: string | null;
}

