import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "FORGE — Autonomous Software Production",
  description: "AI-native autonomous engineering infrastructure platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-forge-bg text-forge-text antialiased">
        <Providers>
          <div className="flex min-h-screen flex-col">
            <nav className="border-b border-forge-border bg-forge-surface px-6 py-3">
              <div className="mx-auto flex max-w-7xl items-center justify-between">
                <a href="/" className="flex items-center gap-2">
                  <span className="text-lg font-bold tracking-tight text-forge-accent">FORGE</span>
                  <span className="hidden text-xs text-forge-muted sm:block">
                    Autonomous Software Production
                  </span>
                </a>
                <div className="flex items-center gap-6 text-sm">
                  <a href="/" className="text-forge-muted hover:text-forge-text transition-colors">
                    Dashboard
                  </a>
                  <a
                    href="/pipeline"
                    className="text-forge-muted hover:text-forge-text transition-colors"
                  >
                    Pipeline
                  </a>
                  <a
                    href="/agents"
                    className="text-forge-muted hover:text-forge-text transition-colors"
                  >
                    Agents
                  </a>
                </div>
              </div>
            </nav>
            <main className="flex-1">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
