import { SessionView } from "@/components/projects/SessionView";

export default async function Page({ params }: { params: Promise<{ id: string; sessionId: string }> }) {
  const { id, sessionId } = await params;
  return <SessionView projectId={id} sessionId={sessionId} />;
}
