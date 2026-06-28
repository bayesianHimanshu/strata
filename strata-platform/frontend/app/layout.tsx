import "./globals.css";
import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "STRATA — Agentic IEG",
  description: "Auditable, leakage-controlled Integrated Evidence Generation.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <div className="topbar">
          <div className="shell">
            <Link href="/" className="wordmark">
              STRA<span>TA</span>
            </Link>
            <span className="tag">Integrated Evidence Generation</span>
            <nav>
              <Link href="/">Capabilities</Link>
              <Link href="/hta">HTA Archaeology</Link>
              <Link href="/synthesis">Evidence Synthesis</Link>
              <Link href="/landscape">Landscape</Link>
              <Link href="/surveillance">Surveillance</Link>
            </nav>
          </div>
        </div>
        <main className="shell">{children}</main>
        <div className="shell">
          <footer>
            STRATA platform · public-data IEG · every grounded claim is traceable to its
            sources, under an enforced leakage boundary.
          </footer>
        </div>
      </body>
    </html>
  );
}
