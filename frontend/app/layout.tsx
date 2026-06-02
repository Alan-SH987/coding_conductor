import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { ThemeToggle } from "@/components/ThemeToggle";

// Set the theme class on <html> before paint to avoid a flash of the wrong theme.
const NO_FLASH = `(function(){try{var t=localStorage.getItem('theme');var d=t==='dark'||((!t||t==='system')&&window.matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.classList.toggle('dark',d);}catch(e){}})();`;

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
    <html lang="en" suppressHydrationWarning>
      <body>
        <script dangerouslySetInnerHTML={{ __html: NO_FLASH }} />
        <header className="border-b border-border">
          <div className="flex items-center gap-3 px-6 py-4">
            <Link href="/" className="text-lg font-semibold tracking-tight">
              Coding Conductor
            </Link>
            <span className="text-xs text-muted-foreground">
              orchestrate AI coding agents
            </span>
            <div className="ml-auto">
              <ThemeToggle />
            </div>
          </div>
        </header>
        <main className="px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
