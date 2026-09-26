import { MemoryReviewDetail } from "@/components/review/MemoryReviewDetail";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <MemoryReviewDetail id={id} />;
}
