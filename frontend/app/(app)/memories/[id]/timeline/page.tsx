import Link from "next/link";
import { MemoryTimeline } from "@/components/temporal/MemoryTimeline";

export default async function TimelinePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <div className="space-y-3">
      <nav aria-label="Breadcrumb" className="text-sm">
        <Link className="underline" href={`/memories/${id}`}>← Back to memory</Link>
      </nav>
      <h1 className="text-xl font-semibold">Memory timeline</h1>
      <MemoryTimeline id={id} />
    </div>
  );
}
