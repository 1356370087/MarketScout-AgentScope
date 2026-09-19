// Compatibility entrypoint; request and stream implementations live in domain clients.
import { configurationApi } from "./api/configuration";
import { researchApi as runsApi } from "./api/research";
import { documentsApi } from "./api/documents";
import { securityApi } from "./api/security";
import { publicationsApi } from "./api/publications";
import { usageApi } from "./api/usage";
import { activityApi } from "./api/activity";

export { apiFetch } from "./api/http";
export { listAllDocuments } from "./api/documents";
export { subscribeToRun } from "./api/research";
export { subscribeToPublications } from "./api/publications";
export { subscribeToTaskActivity } from "./api/activity";

export const researchApi = {
  ...configurationApi,
  ...runsApi,
  ...documentsApi,
  ...securityApi,
  ...publicationsApi,
  ...usageApi,
  ...activityApi,
};
