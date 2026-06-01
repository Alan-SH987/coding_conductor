import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Coding Conductor",
  description: "Orchestrate AI coding agents through one interface",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <header className="border-b border-zinc-800">
          <div className="mx-auto flex max-w-5xl items-center gap-3 px-6 py-4">
            <Link href="/" className="text-lg font-semibold tracking-tight">
              Coding Conductor
            </Link>
            <span className="text-xs text-zinc-500">
              orchestrate AI coding agents
            </span>
          </div>
        </header>
        <main className="mx-auto max-w-5xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
