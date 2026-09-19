import type { CapabilitiesResponse } from "../contracts/research";
import type { ModelCatalogResponse } from "../contracts/models";
import { apiFetch } from "./http";

export const configurationApi = {
  capabilities: () => apiFetch<CapabilitiesResponse>("/capabilities"),
  models: () => apiFetch<ModelCatalogResponse>("/models"),
};
