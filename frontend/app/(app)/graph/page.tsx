import { Suspense } from "react";
import { GraphExplorer } from "@/components/graph/GraphExplorer";
import { LoadingRows } from "@/components/ui/States";

export default function GraphPage() {
  return (
    <Suspense fallback={<LoadingRows />}>
      <GraphExplorer />
    </Suspense>
  );
}
