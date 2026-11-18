import { Suspense } from "react";
import { FeishuCallbackPage } from "@/features/intelligence/intelligence-page";
export default function Page() { return <Suspense fallback={<p>正在读取飞书授权…</p>}><FeishuCallbackPage /></Suspense>; }
