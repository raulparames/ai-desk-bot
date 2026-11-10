// AI Desk Bot: edge function `watch` (deploy/supabase_watch.sql calls it; see deskbot/watch.py).
// The runner of a session went quiet and Supabase claimed the silence: post the notifications the runner
// had left ready (encrypted and signed there, so no key lives here; copied into `watch:fired:<runner>` at
// the claim), once. Anyone may call it: it only sends what a claim made due (this gen, claimed by Supabase,
// not delivered, not taken by another call), and only to the browsers' push services. Deploy with JWT
// verification OFF: pg_net calls it without a token.
import { createClient } from "jsr:@supabase/supabase-js@2";

const HOSTS = ["web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com"];
const SUFFIXES = [".push.apple.com", ".notify.windows.com", ".push.services.mozilla.com"];
const HEADERS = ["authorization", "ttl", "content-encoding", "urgency", "topic", "crypto-key", "encryption"];
const SUB_KEY = /^push:sub:[0-9a-f]{24}$/;
const MAX_SENDS = 20;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function pushHostOk(endpoint: string): boolean {
  try {
    const u = new URL(endpoint);
    const h = u.hostname.toLowerCase();
    return u.protocol === "https:" && (HOSTS.includes(h) || SUFFIXES.some((s) => h.endsWith(s)));
  } catch {
    return false;
  }
}

function bytes(b64: string): Uint8Array {
  const raw = atob(b64);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

type Send = { key?: unknown; endpoint?: unknown; headers?: Record<string, unknown>; body?: unknown };

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  let ask: { runner?: unknown; gen?: unknown } = {};
  try { ask = await req.json(); } catch { return json({ error: "pedido inválido" }, 400); }
  const runner = String(ask?.runner ?? "");
  const gen = String(ask?.gen ?? "");
  if (!["pc", "github"].includes(runner) || !gen || gen.length > 80) return json({ error: "pedido inválido" }, 400);
  const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
    auth: { persistSession: false },
  });
  // the copy taken when the silence was claimed, not the switch the runner may have rewritten since
  const { data: row, error } = await db.from("kv").select("value").eq("key", `watch:fired:${runner}`).maybeSingle();
  if (error) return json({ error: "relay" }, 500);
  const fired = row?.value ?? {};
  if (fired?.gen !== gen || fired?.by !== "supabase" || fired?.delivered || fired?.failed) return json({ ok: true, sent: 0 });
  const { data: mine } = await db.rpc("_watch_sending", { p_runner: runner, p_gen: gen });
  if (mine !== true) return json({ ok: true, sent: 0 });   // another call took it

  const result: Record<string, number | string> = {};
  const sends: Send[] = Array.isArray(fired.sends) ? fired.sends.slice(0, MAX_SENDS) : [];
  for (const s of sends) {
    const key = String(s?.key ?? "");
    const endpoint = String(s?.endpoint ?? "");
    const id = SUB_KEY.test(key) ? key.slice(-6) : "?";
    if (!pushHostOk(endpoint) || endpoint.length > 1000 || typeof s?.body !== "string" || s.body.length > 8000) {
      result[id] = "refused";
      continue;
    }
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries(s.headers ?? {})) {
      const name = k.toLowerCase();
      if (HEADERS.includes(name) && typeof v === "string" && v.length < 2000) headers[name] = v;
    }
    if (!headers.authorization?.startsWith("vapid ")) {
      result[id] = "refused";
      continue;
    }
    headers["content-type"] = "application/octet-stream";
    try {
      const r = await fetch(endpoint, { method: "POST", headers, body: bytes(s.body), signal: AbortSignal.timeout(10000) });
      result[id] = r.status;
      await r.body?.cancel();
      if ((r.status === 404 || r.status === 410) && SUB_KEY.test(key)) {   // the phone turned them off
        await db.from("kv").upsert({ key, value: { gone: true, at: Date.now() / 1000 }, updated_at: new Date().toISOString() });
      }
    } catch (e) {
      result[id] = e instanceof Error ? e.name : "error";
    }
  }
  const sent = Object.values(result).filter((v) => typeof v === "number" && v >= 200 && v < 300).length;
  await db.rpc("_watch_sent", { p_runner: runner, p_gen: gen, p_result: result, p_delivered: sent > 0 });
  return json({ ok: true, sent });
});
