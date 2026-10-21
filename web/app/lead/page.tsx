"use client";

import { useEffect, useRef, useState } from "react";

type Lead = {
  id: string;
  stage: string;
  owner: string | null;
  crm_id: string | null;
  approval_id: string | null;
  contact: { email: string | null; phone: string | null; issues: string[] } | null;
  assessment: {
    intent: string; tier: number; tier_name: string; guests: number | null; check_in: string | null;
    check_out: string | null; villa: string | null; days_ahead: number | null; fit: boolean;
    rationale: string; missing: string[];
  } | null;
  draft: { channel: string; subject: string; body: string } | null;
};
type Detail = {
  lead: Lead;
  events: { kind: string; data: Record<string, unknown>; at: string }[];
  messages: { channel: string; recipient: string; body: string; template: string | null; status: string }[];
};

const EXAMPLES: Record<string, { name: string; channel: string; message: string }> = {
  "Hot lead": { name: "Asha Rao", channel: "web_form",
    message: "Hi! We are 6 friends looking at Villa Sol from 20 to 23 Nov. Is it free?" },
  "Warm (no dates)": { name: "Rohan Fernandes", channel: "email",
    message: "We're a family of 4 thinking about Casa Kokum sometime this winter. What are the options?" },
  "Event": { name: "Meera Iyer", channel: "web_form",
    message: "Can we host a small birthday dinner for 12 at Villa Sol on 5 Dec, with 6 of us staying 2 nights?" },
  "Spam": { name: "Promo Bot", channel: "web_form",
    message: "CONGRATULATIONS!!! You won an iPhone. Click here to claim your prize now!!!" },
};
const SETTLED = new Set(["with_owner", "nurturing", "held", "closed", "awaiting_approval", "opted_out", "replied", "failed"]);

function demoContact(name: string) {
  const tag = Math.random().toString(36).slice(2, 7);
  const slug = name.toLowerCase().replace(/[^a-z]+/g, ".");
  const digits = String(Math.floor(Math.random() * 1e5)).padStart(5, "0");
  return { email: `${slug}+${tag}@example.org`, phone: `98765 ${digits}` };
}

async function api(path: string, body?: unknown) {
  const r = await fetch(`/api/proxy/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Error ${r.status}`);
  return data;
}

export default function LeadPage() {
  const [form, setForm] = useState({ name: "", channel: "web_form", message: "", ...demoContact("guest") });
  const [leadId, setLeadId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const polls = useRef(0);

  useEffect(() => {
    if (!leadId) return;
    polls.current = 0;
    const t = setInterval(async () => {
      try {
        const d = (await api(`agents/lead/${leadId}`)) as Detail;
        setDetail(d);
        polls.current = SETTLED.has(d.lead.stage) ? polls.current + 1 : 0;
        if (polls.current > 6) clearInterval(t);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Polling failed");
        clearInterval(t);
      }
    }, 1500);
    return () => clearInterval(t);
  }, [leadId]);

  function pick(label: string) {
    const ex = EXAMPLES[label];
    setForm({ ...ex, ...demoContact(ex.name) });
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setDetail(null);
    try {
      const body = { ...form, email: form.email || null, phone: form.phone || null, source: "demo" };
      const r = await api("agents/lead/intake", body);
      setLeadId(r.lead_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  async function act(path: string, body: unknown) {
    setError(null);
    try {
      await api(path, body);
      const id = leadId;
      setLeadId(null);
      setTimeout(() => setLeadId(id), 0); // restart polling
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed");
    }
  }

  const L = detail?.lead;
  const a = L?.assessment;
  return (
    <>
      <div className="row">
        <h1>Dispatcher</h1>
        <span className="muted small">Lead qualification · Palmgrove Stays (fictional)</span>
      </div>
      <p className="muted small">
        Submit an enquiry. The model extracts the facts; code computes the tier, decides whether a
        reply may be sent (outbound mode, allowlist, opt-out, WhatsApp 24h window) and routes hot
        leads to a person in Slack.
      </p>

      <div className="examples">
        {Object.keys(EXAMPLES).map((k) => (
          <button key={k} onClick={() => pick(k)}>{k}</button>
        ))}
      </div>

      <form className="leadform" onSubmit={submit}>
        <input placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <select value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })}>
          <option value="web_form">Web form</option>
          <option value="email">Email</option>
          <option value="whatsapp">WhatsApp</option>
        </select>
        <input placeholder="Email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <input placeholder="Phone" value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} />
        <textarea placeholder="Their message" rows={3} value={form.message}
          onChange={(e) => setForm({ ...form, message: e.target.value })} />
        <button type="submit" disabled={busy || !form.message.trim() || !(form.email || form.phone)}>
          {busy ? "Sending…" : "Submit enquiry"}
        </button>
      </form>

      {error && <p className="bad">{error}</p>}

      {L && (
        <section className="lead">
          <div className="row">
            <h2>Stage: <code>{L.stage}</code></h2>
            {L.owner && <span>Owner: <b>{L.owner}</b></span>}
          </div>

          {a && (
            <div className="msg">
              <b>Tier {a.tier} · {a.tier_name}</b> — {a.intent.replace(/_/g, " ")}
              <div className="small muted">
                guests {a.guests ?? "?"} · {a.check_in ?? "no dates"}{a.check_out ? ` → ${a.check_out}` : ""}
                {a.villa ? ` · ${a.villa}` : ""}{a.days_ahead !== null ? ` · ${a.days_ahead} days ahead` : ""}
                {a.fit ? "" : " · not a fit"}
              </div>
              <div className="text">{a.rationale}</div>
              {a.missing.length > 0 && <div className="small">Still needed: {a.missing.join(", ")}</div>}
              <div className="small muted meta">Tier computed in code from these facts, not by the model.</div>
            </div>
          )}

          {L.draft && (
            <div className="msg">
              <b>Draft reply ({L.draft.channel})</b>
              {L.draft.subject && <div className="small">Subject: {L.draft.subject}</div>}
              <div className="text">{L.draft.body}</div>
            </div>
          )}

          {detail.messages.length > 0 && (
            <div className="msg">
              <b>Outbox</b>
              <ul className="cites">
                {detail.messages.map((m, i) => (
                  <li key={i}>
                    <code>{m.status}</code> · {m.channel} → {m.recipient}
                    {m.template ? ` · template ${m.template}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="examples">
            {L.stage === "awaiting_approval" && L.approval_id && (
              <>
                <button onClick={() => act(`approvals/${L.approval_id}/decision`, { decision: "approved" })}>Approve reply</button>
                <button onClick={() => act(`approvals/${L.approval_id}/decision`, { decision: "rejected" })}>Reject</button>
              </>
            )}
            <button onClick={() => act(`agents/lead/${L.id}/events`, { kind: "lead_replied", text: "Thanks! Can you hold the dates?" })}>Lead replies</button>
            <button onClick={() => act(`agents/lead/${L.id}/events`, { kind: "human_takeover" })}>Human takes over</button>
            <button onClick={() => act(`agents/lead/${L.id}/events`, { kind: "lead_replied", text: "Please stop messaging me" })}>Lead opts out</button>
          </div>

          <details>
            <summary>Timeline ({detail.events.length} events)</summary>
            <ol className="cites small">
              {detail.events.map((e, i) => (
                <li key={i}><code>{e.kind}</code> <span className="muted">{new Date(e.at).toLocaleTimeString()}</span></li>
              ))}
            </ol>
          </details>
        </section>
      )}
    </>
  );
}
