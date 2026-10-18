// Server-side proxy to the FastAPI service, so the browser never needs the API's address.
const API_URL = process.env.API_URL ?? "http://localhost:8000";

export async function POST(req: Request): Promise<Response> {
  const body = await req.text();
  try {
    const r = await fetch(`${API_URL}/concierge/chat`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
      cache: "no-store",
    });
    return new Response(await r.text(), {
      status: r.status,
      headers: { "content-type": r.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ detail: "The Concierge API isn't reachable. Is `make up` running?" }, { status: 502 });
  }
}
