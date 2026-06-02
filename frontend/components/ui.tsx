import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-lg border border-border bg-card/50 p-4 ${className}`}
    >
      {children}
    </div>
  );
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "danger";
};

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonProps) {
  const base =
    "inline-flex items-center justify-center rounded-md px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-40";
  const variants: Record<string, string> = {
    primary: "bg-primary text-primary-foreground hover:bg-primary/90",
    ghost: "border border-border text-foreground hover:bg-muted",
    danger: "bg-red-600 text-white hover:bg-red-500",
  };
  return <button className={`${base} ${variants[variant]} ${className}`} {...props} />;
}

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-secondary text-foreground",
  planning: "bg-indigo-900 text-indigo-200",
  planned: "bg-indigo-800 text-indigo-100",
  queued: "bg-secondary text-foreground",
  running: "bg-blue-900 text-blue-200",
  review: "bg-amber-900 text-amber-200",
  awaiting_approval: "bg-amber-900 text-amber-200",
  merged: "bg-green-900 text-green-200",
  rejected: "bg-muted text-muted-foreground",
  failed: "bg-red-900 text-red-200",
  succeeded: "bg-green-900 text-green-200",
  // review verdicts
  approve: "bg-green-900 text-green-200",
  request_changes: "bg-orange-900 text-orange-200",
  // finding severities
  blocker: "bg-red-900 text-red-200",
  warning: "bg-amber-900 text-amber-200",
  nit: "bg-secondary text-foreground",
};

export function Badge({ status }: { status: string }) {
  const color = STATUS_COLORS[status] ?? "bg-secondary text-foreground";
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${color}`}
    >
      {status}
    </span>
  );
}

export function Input(
  props: React.InputHTMLAttributes<HTMLInputElement>,
) {
  return (
    <input
      {...props}
      className={`w-full rounded-md border border-border bg-card px-3 py-1.5 text-sm outline-none focus:border-ring ${props.className ?? ""}`}
    />
  );
}
