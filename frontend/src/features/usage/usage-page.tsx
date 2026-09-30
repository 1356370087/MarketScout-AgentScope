import { AppShell } from "@/components/app-shell";
import { UsageAnalyticsDashboard } from "@/components/usage-analytics-dashboard";

export default function UsagePage() {
  return <AppShell><div className="analytics-page"><UsageAnalyticsDashboard /></div></AppShell>;
}
