import { Suspense } from "react";
import { MemoryExplorer } from "@/components/memory/MemoryExplorer";
import { LoadingRows } from "@/components/ui/States";

export default function MemoriesPage() {
  return (
    <Suspense fallback={<LoadingRows />}>
      <MemoryExplorer />
    </Suspense>
  );
}
