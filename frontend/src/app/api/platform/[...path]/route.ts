import { NextRequest, NextResponse } from "next/server";
import { authenticatedProxy } from "@/lib/server-auth";

const roots = new Set(["status", "catalog", "configurations", "import", "import-preview", "configuration-preview", "preferences", "models", "credentials", "overview", "operations", "usage", "quotas", "audit"]);
async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  if (!roots.has(path[0]) || path.some((part) => part === "." || part === ".." || part.includes("/") || part.includes("\\"))) {
    return NextResponse.json({ detail: "proxy_path_not_allowed" }, { status: 404 });
  }
  return authenticatedProxy(request, `/platform/${path.map(encodeURIComponent).join("/")}`);
}
export const dynamic = "force-dynamic";
export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
