import ConfigurationEditorPage from "@/features/platform/configuration-editor";
export default async function ConfigurationPage({ params }: { params: Promise<{ id: string }> }) { const { id } = await params; return <ConfigurationEditorPage id={id} />; }
