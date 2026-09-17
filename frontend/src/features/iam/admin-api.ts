import { authFetch } from "@/lib/auth";

export type AdminUser = { id: string; email: string; display_name: string | null; status: string; role_codes: string[]; created_at: string };
export type Role = { id: string; code: string; name: string; description: string | null; is_system: boolean; permission_codes: string[] };
export type Permission = { code: string; name: string; description: string; domain: string };
export type Audit = { id: string; action: string; actor_email: string | null; target_user_id: string | null; created_at: string; detail: Record<string, unknown> | null };

export const adminApi = {
  users: () => authFetch<AdminUser[]>("/api/iam/admin/users"),
  roles: () => authFetch<Role[]>("/api/iam/admin/roles"),
  permissions: () => authFetch<Permission[]>("/api/iam/admin/permissions"),
  audit: () => authFetch<Audit[]>("/api/iam/admin/audit-events?limit=100"),
  mutate: (path: string, method: string, body?: unknown) => authFetch(`/api/iam/admin${path}`, { method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined }),
};

