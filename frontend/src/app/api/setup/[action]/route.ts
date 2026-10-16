import { NextRequest, NextResponse } from "next/server";
import { backendOrigin, sameOriginValid } from "@/lib/server-auth";

async function forward(request: NextRequest, context: { params: Promise<{ action: string }> }) {
  const { action } = await context.params;
  if (!((request.method === "GET" && action === "status") || (request.method === "POST" && action === "bootstrap"))) return NextResponse.json({ detail: "setup_action_not_allowed" }, { status: 404 });
  if (!sameOriginValid(request)) return NextResponse.json({ detail: "csrf_validation_failed" }, { status: 403 });
  const response = await fetch(new URL(`/setup/${action}`, backendOrigin), { method: request.method, headers: { "Content-Type": "application/json" }, cache: "no-store", ...(request.method === "POST" ? { body: await request.text() } : {}) });
  return NextResponse.json(await response.json(), { status: response.status });
}
export const GET = forward;
export const POST = forward;
