import { ItemPage } from "@/features/intelligence/intelligence-page";
export default async function Page({ params }: { params: Promise<{ itemId: string }> }) {
  const { itemId } = await params;
  return <ItemPage id={itemId} />;
}
