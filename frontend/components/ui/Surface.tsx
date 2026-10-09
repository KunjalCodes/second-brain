import type { ElementType, HTMLAttributes, ReactNode } from "react";
import { TONE_VAR, kindOf } from "@/lib/format/glyphs";

/** A solid content surface (`panel`) or, sparingly, a glass one (side panels, overlays). */
export function Surface({
  glass = false,
  as: Tag = "section",
  className = "",
  ...props
}: HTMLAttributes<HTMLElement> & { glass?: boolean; as?: ElementType }) {
  return <Tag {...props} className={`${glass ? "glass" : "panel"} ${className}`} />;
}

/** A titled block with an optional right-hand action; the page's basic unit of structure. */
export function Section({
  title,
  id,
  action,
  children,
  className = "",
}: {
  title: string;
  id: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Surface aria-labelledby={id} className={`p-4 ${className}`}>
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h2 id={id} className="text-sm font-semibold tracking-wide text-[var(--color-fg)]">
          {title}
        </h2>
        {action}
      </div>
      {children}
    </Surface>
  );
}

/** Glyph + label for a kind of knowledge. Text is always present (never glyph/colour alone). */
export function KindPill({ kind, entityType }: { kind?: string | null; entityType?: string | null }) {
  const k = kindOf(kind, entityType);
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium"
      style={{ color: TONE_VAR[k.tone], borderColor: TONE_VAR[k.tone] }}
    >
      <span aria-hidden="true">{k.glyph}</span>
      {k.label}
    </span>
  );
}
