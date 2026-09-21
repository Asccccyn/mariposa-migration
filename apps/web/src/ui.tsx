import type { ReactNode } from "react";

export function Tag({ kind, children }: { kind?: string; children: ReactNode }) {
  return <span className={`tag ${kind ?? ""}`}>{children}</span>;
}

export function Item({ children }: { children: ReactNode }) {
  return <div className="item">{children}</div>;
}

export function Meta({ children }: { children: ReactNode }) {
  return <div className="meta">{children}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Err({ e }: { e: unknown }) {
  const msg = e instanceof Error ? e.message : String(e);
  return <div className="err">{msg}</div>;
}

export function fmtRep(rep: string): string {
  return rep === "forgotten_summary" ? "遗忘摘要" : "full";
}
