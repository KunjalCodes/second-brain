import { EntityReviewDetail } from "@/components/review/EntityReviewDetail";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <EntityReviewDetail id={id} />;
}
