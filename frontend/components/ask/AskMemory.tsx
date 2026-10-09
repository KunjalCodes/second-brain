"use client";
/**
 * Ask My Memory (optional M9). Answers are plain text; citations come ONLY from
 * the server's validated `cited_memory_ids` -- never parsed out of the answer.
 */
import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { agentTurn, createConversation } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { AgentTurnResponse } from "@/lib/api/types";
import { ANSWER_CONFIDENCE, TERMINATION_REASON } from "@/lib/format/labels";
import { LinkedMemory } from "@/components/memory/LinkedMemory";
import { Button } from "@/components/ui/Button";
import { Tag } from "@/components/ui/Chip";
import { ErrorState, PageHeader } from "@/components/ui/States";
import { useTimeLens } from "@/lib/time/lens";
import { formatDateTime } from "@/lib/format/dates";

interface Turn {
  question: string;
  answer: AgentTurnResponse;
}

export function terminationNotice(reason: string): string | null {
  return reason in TERMINATION_REASON ? TERMINATION_REASON[reason]! : `The assistant stopped early (${reason}); the answer may be incomplete.`;
}

export function AskMemory() {
  const queryClient = useQueryClient();
  const { asOf } = useTimeLens();
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const m = useMutation({
    mutationFn: async (question: string) => {
      let id = conversationId;
      if (!id) {
        id = (await createConversation(null)).id;
        setConversationId(id);
      }
      return { question, answer: await agentTurn(id, question) };
    },
    onSuccess: (turn) => {
      setTurns((t) => [...t, turn]);
      setText("");
      void queryClient.invalidateQueries({ queryKey: qk.conversations });
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const q = text.trim();
    if (q && !m.isPending) m.mutate(q);
  };
  return (
    <div className="space-y-4">
      <PageHeader title="Ask your memory">
        {conversationId ? <Link className="text-sm underline" href={`/conversations/${conversationId}`}>Open transcript</Link> : null}
        {turns.length ? <Button onClick={() => { setConversationId(null); setTurns([]); m.reset(); }}>New conversation</Button> : null}
      </PageHeader>
      <p className="text-sm text-[var(--color-fg-muted)]">
        Questions are saved to a conversation, and what you write may be turned into new memories. The assistant&apos;s answers are never stored as memories.
      </p>
      <p className="text-xs text-[var(--color-fg-muted)]">
        <span aria-hidden="true">◷ </span>
        The assistant works out the time from your question (for example “in March”). The time lens at the top applies to the pages you browse, not to Ask.
        {asOf ? <> (Lens is set to {formatDateTime(asOf)}.)</> : null}
      </p>
      <ol className="space-y-4" aria-live="polite">
        {turns.map((t, i) => {
          const notice = terminationNotice(t.answer.termination_reason);
          return (
            <li key={i} className="space-y-2 panel p-3">
              <p className="content-text text-sm font-medium">{t.question}</p>
              <p className="content-text reading">{t.answer.message.content}</p>
              <div className="flex flex-wrap gap-2 text-xs">
                <Tag>{ANSWER_CONFIDENCE[t.answer.confidence] ?? t.answer.confidence}</Tag>
                <span>{t.answer.tool_call_count} memory lookups</span>
              </div>
              {notice ? <p role="note" className="text-xs text-[var(--color-warn)]">{notice}</p> : null}
              {t.answer.cited_memory_ids.length ? (
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]">Evidence — memories this answer is based on</p>
                  <ul className="mt-1 space-y-1">
                    {t.answer.cited_memory_ids.map((id) => <li key={id}><LinkedMemory id={id} /></li>)}
                  </ul>
                </div>
              ) : (
                <p className="text-xs text-[var(--color-fg-muted)]">No memory was cited for this answer.</p>
              )}
            </li>
          );
        })}
      </ol>
      {m.isError ? <ErrorState error={m.error} /> : null}
      <form onSubmit={submit} className="flex flex-col gap-2 sm:flex-row">
        <label htmlFor="ask-input" className="sr-only">Your question</label>
        <textarea id="ask-input" value={text} onChange={(e) => setText(e.target.value)} rows={2} maxLength={4000} placeholder="What was I using for OCR in March?" className="flex-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1" />
        <Button type="submit" variant="primary" disabled={m.isPending || !text.trim()}>{m.isPending ? "Thinking… (up to 90 s)" : "Ask"}</Button>
      </form>
    </div>
  );
}
