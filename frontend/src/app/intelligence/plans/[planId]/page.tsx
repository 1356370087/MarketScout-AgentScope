import { PlanPage } from "@/features/intelligence/intelligence-page";
export default async function Page({ params }: { params: Promise<{ planId: string }> }) {
  const { planId } = await params;
  return <PlanPage id={planId} />;
}
