import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Switchboard",
  description: "Business AI agents on a shared core",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header>
          <Link href="/" className="brand">Switchboard</Link>
          <nav>
            <Link href="/support">Concierge</Link>
            <Link href="/lead">Dispatcher</Link>
            <Link href="/voice">Operator</Link>
          </nav>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
