"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getProject, projectHistory, projectResume, projectSessions } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { ResumeContext, TimelineItem } from "@/lib/api/types";
import { formatDate, formatDateTime } from "@/lib/format/dates";
import { EVIDENCE_TEXT, NOISY_EVENT_TYPES, eventStyle } from "@/lib/format/projectEvents";
import { Tag } from "@/components/ui/Chip";
import { Section } from "@/components/ui/Surface";
import { Timeline } from "@/components/ui/Timeline";
import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/ui/States";
import { SpaceLink } from "./SpaceLink";

const UNFINISHED_BASIS: Record<string, string> = {
  explicit_unfinished: "recorded as unfinished",
  task_blocked: "recorded as blocked",
  unclosed_user_request: "a request with no recorded completion",
};

function Standing({ r, projectId }: { r: ResumeContext; projectId: string }) {
  return (
    <div className="space-y-4 text-sm">
      {r.active_task ? (
        <div>
          <h3 className="text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]">Current task</h3>
          <p className="content-text reading">{r.active_task.text}</p>
          <p className="text-xs text-[var(--color-fg-muted)]">
            {r.active_task.basis === "session_goal" ? "From the session goal" : "The latest request with no recorded completion"} · {formatDateTime(r.active_task.occurred_at)}
          </p>
        </div>
      ) : null}
      <div>
        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]">Unfinished work</h3>
        {r.unfinished_work.length === 0 ? (
          <p className="text-[var(--color-fg-muted)]">Nothing is recorded as unfinished. (The system never guesses: this lists only what was explicitly recorded.)</p>
        ) : (
          <ul className="space-y-1">
            {r.unfinished_work.map((u, i) => (
              <li key={i}>
                <span className="content-text">{u.text}</span>{" "}
                <span className="text-xs text-[var(--color-fg-muted)]">({UNFINISHED_BASIS[u.basis] ?? u.basis})</span>{" "}
                <Link className="text-xs underline" href={`/projects/${projectId}/sessions/${u.session_id}`}>session</Link>
              </li>
            ))}
          </ul>
        )}
      </div>
      {r.recent_decisions.length ? (
        <div>
          <h3 className="text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]">Decisions</h3>
          <ul className="space-y-2">
            {r.recent_decisions.map((d, i) => (
              <li key={i}>
                <p className="content-text font-medium">
                  ⚖ {d.title} {d.status === "superseded" ? <Tag>superseded</Tag> : null}
                </p>
                {d.rationale ? <p className="content-text text-[var(--color-fg-muted)]">{d.rationale}</p> : null}
                <p className="text-xs text-[var(--color-fg-muted)]">{formatDate(d.occurred_at)}{d.files.length ? ` · ${d.files.slice(0, 3).join(", ")}` : ""}</p>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {r.known_bugs.length ? (
        <div>
          <h3 className="text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]">Bugs</h3>
          <ul className="space-y-1">
            {r.known_bugs.map((b, i) => (
              <li key={i} className="content-text">⚠ {b.title} <Tag>{b.status}</Tag> <span className="text-xs text-[var(--color-fg-muted)]">{b.fix_attempts} fix attempt{b.fix_attempts === 1 ? "" : "s"}</span></li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function historyEntries(items: TimelineItem[], projectId: string) {
  return items
    .filter((i) => !NOISY_EVENT_TYPES.has(i.type))
    .map((i) => {
      const st = eventStyle(i.type);
      return {
        key: `${i.kind}:${i.source.id}`,
        at: i.occurred_at,
        glyph: st.glyph,
        label: st.label,
        tone: st.tone,
        body: (
          <>
            {i.title}{" "}
            <span className="text-xs text-[var(--color-fg-muted)]">
              {EVIDENCE_TEXT[i.evidence] ?? i.evidence}
              {i.session_id ? (
                <> · <Link className="underline" href={`/projects/${projectId}/sessions/${i.session_id}`}>session</Link></>
              ) : null}
              {i.memory_id ? (
                <> · <SpaceLink projectId={projectId} href={`/memories/${i.memory_id}`}>why remembered</SpaceLink></>
              ) : null}
            </span>
          </>
        ),
      };
    });
}

export function ProjectOverview({ id }: { id: string }) {
  const project = useQuery({ queryKey: qk.project(id), queryFn: ({ signal }) => getProject(id, signal) });
  const resume = useQuery({ queryKey: qk.projectResume(id), queryFn: ({ signal }) => projectResume(id, signal) });
  const history = useQuery({ queryKey: qk.projectHistory(id), queryFn: ({ signal }) => projectHistory(id, 30, signal) });
  const sessions = useQuery({ queryKey: qk.projectSessions(id), queryFn: ({ signal }) => projectSessions(id, signal) });

  if (project.isPending) return <LoadingRows rows={4} label="Loading project" />;
  if (project.isError) return <ErrorState error={project.error} onRetry={() => void project.refetch()} notFound="This project doesn't exist, or it isn't yours." />;
  const p = project.data;
  const r = resume.data;
  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="text-sm"><Link className="underline" href="/projects">← Projects</Link></nav>
      <PageHeader title={p.name}>
        <SpaceLink projectId={id} href="/graph" className="rounded-lg border border-[var(--color-border)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--color-surface-2)]">Explore its graph</SpaceLink>
        <SpaceLink projectId={id} href="/memories" className="rounded-lg border border-[var(--color-border)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--color-surface-2)]">Its memories</SpaceLink>
      </PageHeader>
      <p className="text-xs text-[var(--color-fg-muted)]">
        {p.status === "archived" ? "Archived · " : ""}Created {formatDate(p.created_at)}{p.default_branch ? ` · branch ${p.default_branch}` : ""}
      </p>

      <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
        <div className="space-y-4">
          <Section id="standing-h" title="Where this project stands">
            {resume.isPending ? (
              <LoadingRows rows={3} label="Loading project state" />
            ) : resume.isError ? (
              <ErrorState error={resume.error} onRetry={() => void resume.refetch()} />
            ) : !r!.has_history ? (
              <EmptyState title="No history recorded yet">Once a coding agent reports events or Git history is collected, decisions, bugs and unfinished work appear here.</EmptyState>
            ) : (
              <Standing r={r!} projectId={id} />
            )}
          </Section>
          <Section id="activity-h" title="Recent activity">
            {history.isPending ? (
              <LoadingRows rows={4} label="Loading activity" />
            ) : history.isError ? (
              <ErrorState error={history.error} onRetry={() => void history.refetch()} />
            ) : historyEntries(history.data.items, id).length === 0 ? (
              <p className="text-sm text-[var(--color-fg-muted)]">No activity recorded yet.</p>
            ) : (
              <Timeline label="Project activity, newest first" entries={historyEntries(history.data.items, id)} />
            )}
          </Section>
        </div>
        <div className="space-y-4">
          <Section id="sessions-h" title="Agent sessions">
            {sessions.isPending ? (
              <LoadingRows rows={2} label="Loading sessions" />
            ) : sessions.isError ? (
              <ErrorState error={sessions.error} onRetry={() => void sessions.refetch()} />
            ) : sessions.data.sessions.length === 0 ? (
              <p className="text-sm text-[var(--color-fg-muted)]">No agent sessions yet.</p>
            ) : (
              <ul className="space-y-2">
                {sessions.data.sessions.map((s) => (
                  <li key={s.session_id} className="text-sm">
                    <Link className="underline" href={`/projects/${id}/sessions/${s.session_id}`}>
                      {s.summary ?? `Session ${formatDateTime(s.started_at)}`}
                    </Link>
                    <p className="text-xs text-[var(--color-fg-muted)]">
                      {s.status === "active" ? "Active" : s.outcome ?? "Ended"} · {s.event_count} events · {formatDateTime(s.last_event_at)}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </Section>
          {r && r.has_history ? (
            <>
              <Section id="files-h" title="Files">
                <p className="text-sm">
                  {r.files_modified.length ? <>Changed: <span className="font-mono text-xs">{r.files_modified.slice(0, 8).join(", ")}</span></> : "No file changes recorded."}
                </p>
                {r.files_inspected.length ? <p className="mt-1 text-xs text-[var(--color-fg-muted)]">Inspected: <span className="font-mono">{r.files_inspected.slice(0, 8).join(", ")}</span></p> : null}
              </Section>
              <Section id="commits-h" title="Recent commits">
                {r.recent_commits.length === 0 ? (
                  <p className="text-sm text-[var(--color-fg-muted)]">No commits collected yet.</p>
                ) : (
                  <ul className="space-y-1 text-sm">
                    {r.recent_commits.map((c) => (
                      <li key={c.sha} className="content-text">
                        <span className="font-mono text-xs">⦿ {c.sha.slice(0, 8)}</span> {c.subject}
                      </li>
                    ))}
                  </ul>
                )}
              </Section>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
