import Link from "next/link";

export const dynamic = "force-dynamic";

type Health = { status: string; pgvector?: string | null; integrations?: string; outbound_mode?: string };

async function getHealth(): Promise<Health | null> {
  const base = process.env.API_URL ?? "http://localhost:8000";
  try {
    const res = await fetch(`${base}/health`, { cache: "no-store" });
    return (await res.json()) as Health;
  } catch {
    return null;
  }
}

const agents = [
  { href: "/support", name: "Concierge", what: "Customer support — hand-built ReAct loop + memory" },
  { href: "/lead", name: "Dispatcher", what: "Lead qualification — LangGraph + n8n intake" },
  { href: "/voice", name: "Operator", what: "Voice receptionist — Pipecat + Twilio" },
];

export default async function Home() {
  const h = await getHealth();
  const up = h?.status === "ok";
  return (
    <>
      <h1>Switchboard</h1>
      <p className="muted">Business AI agents on a shared Python core.</p>
      <p>
        API: <strong className={up ? "ok" : "bad"}>{up ? "healthy" : "unreachable"}</strong>
        {up && (
          <span className="muted">
            {" "}· pgvector {h?.pgvector} · integrations {h?.integrations} · outbound {h?.outbound_mode}
          </span>
        )}
      </p>
      <ul>
        {agents.map((a) => (
          <li key={a.href}>
            <Link href={a.href}>{a.name}</Link> <span className="muted">— {a.what}</span>
          </li>
        ))}
      </ul>
    </>
  );
}
