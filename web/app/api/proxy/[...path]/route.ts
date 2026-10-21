// Server-side proxy for the Dispatcher demo: only the lead and approval endpoints are reachable.
const API_URL = process.env.API_URL ?? "http://localhost:8000";
const ALLOWED = [/^agents\/lead(\/|$)/, /^approvals(\/|$)/];

async function forward(req: Request, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const path = (await ctx.params).path.join("/");
  if (!ALLOWED.some((rx) => rx.test(path))) return Response.json({ detail: "not found" }, { status: 404 });
  try {
    const r = await fetch(`${API_URL}/${path}`, {
      method: req.method,
      headers: { "content-type": "application/json" },
      body: req.method === "GET" ? undefined : await req.text(),
      cache: "no-store",
    });
    return new Response(await r.text(), {
      status: r.status,
      headers: { "content-type": r.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ detail: "The API isn't reachable. Is `make up` running?" }, { status: 502 });
  }
}

export { forward as GET, forward as POST };
