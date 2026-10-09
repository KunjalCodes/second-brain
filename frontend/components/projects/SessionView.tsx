"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { projectSession } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { formatDateTime } from "@/lib/format/dates";
import { EVIDENCE_TEXT, NOISY_EVENT_TYPES, eventStyle } from "@/lib/format/projectEvents";
import { Section } from "@/components/ui/Surface";
import { Timeline } from "@/components/ui/Timeline";
import { ErrorState, LoadingRows, PageHeader } from "@/components/ui/States";
import { SpaceLink } from "./SpaceLink";

function List({ title, items, mono = false }: { title: string; items: string[]; mono?: boolean }) {
  if (!items.length) return null;
  return (
    <div>
      <h3 className="text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]">{title}</h3>
      <ul className="mt-1 space-y-0.5 text-sm">
        {items.slice(0, 12).map((t, i) => (
          <li key={i} className={`content-text ${mono ? "font-mono text-xs" : ""}`}>{t}</li>
        ))}
      </ul>
    </div>
  );
}

/** "What did the coding agent do?": the meaningful events of one session, not tool-call noise. */
export function SessionView({ projectId, sessionId }: { projectId: string; sessionId: string }) {
  const q = useQuery({ queryKey: qk.projectSession(projectId, sessionId), queryFn: ({ signal }) => projectSession(projectId, sessionId, signal) });
  if (q.isPending) return <LoadingRows rows={4} label="Loading session" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} notFound="This session doesn't exist, or it isn't yours." />;
  const d = q.data;
  const s = d.session;
  const events = d.events
    .filter((e) => !NOISY_EVENT_TYPES.has(e.type))
    .map((e) => {
      const st = eventStyle(e.type);
      return {
        key: `${e.kind}:${e.source.id}`,
        at: e.occurred_at,
        glyph: st.glyph,
        label: st.label,
        tone: st.tone,
        body: (
          <>
            {e.title}{" "}
            <span className="text-xs text-[var(--color-fg-muted)]">
              {EVIDENCE_TEXT[e.evidence] ?? e.evidence}
              {e.memory_id ? <> · <SpaceLink projectId={projectId} href={`/memories/${e.memory_id}`}>why remembered</SpaceLink></> : null}
            </span>
          </>
        ),
      };
    });
  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="text-sm"><Link className="underline" href={`/projects/${projectId}`}>← Project</Link></nav>
      <PageHeader title={s.summary ?? d.goal ?? "Agent session"} />
      <p className="text-xs text-[var(--color-fg-muted)]">
        {s.status === "active" ? "Active" : s.outcome ?? "Ended"} · started {formatDateTime(s.started_at)} · {s.event_count} events
        {d.continues_session ? <> · continues <Link className="underline" href={`/projects/${projectId}/sessions/${d.continues_session}`}>an earlier session</Link></> : null}
      </p>
      <div className="grid gap-4 xl:grid-cols-[1.4fr_1fr]">
        <Section id="did-h" title="What the agent did">
          {events.length === 0 ? <p className="text-sm text-[var(--color-fg-muted)]">No meaningful events recorded.</p> : <Timeline label="Session events in order" entries={events} />}
          {d.next_after_seq != null ? <p className="mt-2 text-xs text-[var(--color-warn)]">Showing the first part of a longer session.</p> : null}
        </Section>
        <Section id="summary-h" title="Summary">
          <div className="space-y-3">
            <List title="Requests" items={d.requests} />
            <List title="Files changed" items={d.files_modified} mono />
            <List title="Files inspected" items={d.files_inspected} mono />
            <List title="Decisions" items={d.decisions} />
            <List title="Errors" items={d.errors} />
            <List title="Fixes" items={d.fixes} />
            <List title="Tests" items={d.tests} />
            <List title="Commits" items={d.commits} mono />
            <List title="Unfinished (explicitly recorded)" items={d.unfinished} />
          </div>
        </Section>
      </div>
    </div>
  );
}
