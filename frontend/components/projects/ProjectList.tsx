"use client";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { listProjects } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { setMemorySpace } from "@/lib/api/space";
import { formatDate } from "@/lib/format/dates";
import { Button } from "@/components/ui/Button";
import { Surface } from "@/components/ui/Surface";
import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/ui/States";

/** Projects are separate, isolated memory spaces: one per codebase a coding agent works on. */
export function ProjectList() {
  const client = useQueryClient();
  const router = useRouter();
  const q = useQuery({ queryKey: qk.projects, queryFn: ({ signal }) => listProjects(signal) });
  return (
    <div className="space-y-4">
      <PageHeader title="Projects" />
      <p className="max-w-prose text-sm text-[var(--color-fg-muted)]">
        Each project is its own memory: what a coding agent decided, tried, fixed and committed in one codebase. Projects never mix with each other or with your personal memory.
      </p>
      {q.isPending ? (
        <LoadingRows rows={3} label="Loading projects" />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : q.data.items.length === 0 ? (
        <EmptyState title="No projects yet">
          A project is created through the API (<code>POST /projects</code>), then a coding agent is connected with a scoped token. See the MCP adapter in <code>integrations/mcp</code>.
        </EmptyState>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {q.data.items.map((p) => (
            <Surface as="li" key={p.id} className="p-4">
              <Link href={`/projects/${p.id}`} className="reading text-lg font-semibold underline-offset-2 hover:underline">
                {p.name}
              </Link>
              <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
                {p.status === "archived" ? "Archived · " : ""}Created {formatDate(p.created_at)}
                {p.default_branch ? ` · branch ${p.default_branch}` : ""}
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button onClick={() => router.push(`/projects/${p.id}`)}>Open</Button>
                <Button
                  onClick={() => {
                    // Browse this project's memories, entities and graph (a memory-space switch).
                    setMemorySpace(p.id);
                    client.clear();
                    router.push("/graph");
                  }}
                >
                  Explore its graph
                </Button>
              </div>
            </Surface>
          ))}
        </ul>
      )}
    </div>
  );
}
