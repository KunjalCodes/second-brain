import "./navigation.mock";
import { fireEvent, render, screen } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import RouteError from "@/app/error";
import GlobalError from "@/app/global-error";
import { documentErrorText } from "@/lib/format/documents";

const secret = new Error("SELECT * FROM users -- /Users/x/secret stack detail") as Error & { digest?: string };
secret.digest = "abc123";

describe("error boundaries (Phase 11)", () => {
  it("route boundary shows a recoverable message and the digest, never the error text", () => {
    const retry = vi.fn();
    const { container } = render(<RouteError error={secret} retry={retry} />);
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(container.textContent).toContain("abc123");
    expect(container.textContent).not.toContain("SELECT");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("global boundary never renders the error text either", () => {
    // It renders its own <html>/<body>, so check the markup rather than mounting it.
    const html = renderToStaticMarkup(<GlobalError error={secret} retry={() => undefined} />);
    expect(html).not.toContain("/Users/x/secret");
    expect(html).toContain("could not load");
    expect(html).toContain("abc123");
  });
});

describe("document error vocabulary covers Phase 11 codes", () => {
  it.each(["document_missing", "text_too_large", "extraction_failed", "request_too_large"])("%s", (code) => {
    expect(documentErrorText(code)).not.toMatch(/^Processin/);
  });
});
