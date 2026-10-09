"use client";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { setMemorySpace } from "@/lib/api/space";

/**
 * A link into a project's own memory (memory pages, graph, entities). Those pages read the
 * SELECTED memory space, so following the link selects the project first. The server still
 * verifies ownership on every request; this is a selection, never an authority.
 */
export function SpaceLink({ projectId, href, className, children }: { projectId: string; href: string; className?: string; children: ReactNode }) {
  const client = useQueryClient();
  return (
    <Link
      href={href}
      className={className ?? "underline"}
      onClick={() => {
        setMemorySpace(projectId);
        client.clear();
      }}
    >
      {children}
    </Link>
  );
}
