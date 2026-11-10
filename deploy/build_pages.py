"""Builds docs/ (GitHub Pages): the phone app pages, talking to the Supabase relay instead of the PC.

The pages are the same files the PC serves (app.html, memes.html). A small script in front of them
answers their /api/... calls from the relay: login via the Supabase function, data via app_state
(only what changed since the last poll), buttons via app_command. Run: python deploy/build_pages.py
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "deskbot" / "dashboard_static"
OUT = ROOT / "docs"
URL = "https://kkawnjfowemyqkplkrma.supabase.co"
KEY = "sb_publishable_oL1nYjpN8GKzuG_RuhwGgQ_DAxM06qa"   # publishable key: public by design

SHIM = """<script>
/* Remote mode: /api/... answered by the Supabase relay (PC or GitHub, whichever is running the bot). */
(function () {
  const URL = %(url)s, KEY = %(key)s, H = {"apikey": KEY, "Content-Type": "application/json"};
  const tok = () => { try { return localStorage.getItem("deskbot-token") || ""; } catch (e) { return ""; } };
  const cache = {items: {}, since: null};
  const reply = (code, body) => new Response(JSON.stringify(body), {status: code, headers: {"Content-Type": "application/json"}});
  async function rpc(name, body) {
    const r = await fetch(URL + "/rest/v1/rpc/" + name, {method: "POST", headers: H, body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({}));
    if (r.status === 403 || r.status === 401 || (j && j.message === "login needed")) { const e = new Error("login"); e.code = 401; throw e; }
    if (!r.ok) throw new Error(j.message || r.statusText);
    return j;
  }
  async function state() {
    const j = await rpc("app_state", {p_token: tok(), p_since: cache.since});
    Object.assign(cache.items, j.items || {});
    cache.since = j.now;
    return cache.items;
  }
  const val = (items, k) => (items[k] || {}).value;
  /* How long ago the bot (PC or GitHub) last gave a sign, in seconds, by the SERVER's clock: the time of this
     answer (j.now) minus the heartbeat (the moment the server took it, or else the one the bot wrote in it).
     Never the phone's clock, which may be wrong. A computer asleep stops beating, and the pages say so. */
  const ms = s => Date.parse(String(s || "").replace(new RegExp("([.][0-9]{3})[0-9]+"), "$1"));
  function signal(items) {
    const runner = Object.assign({}, val(items, "app:runner") || {});
    const got = ms((items["app:runner"] || {}).updated_at), beat = isFinite(got) ? got : ms(runner.heartbeat), now = ms(cache.since);
    if (isFinite(beat) && isFinite(now)) runner.age_s = Math.max(0, Math.round((now - beat) / 1000));
    return runner;
  }
  const realFetch = window.fetch.bind(window);
  window.DESKBOT_REMOTE = true;
  window.fetch = async function (input, opts = {}) {
    const path = typeof input === "string" ? input : input.url;
    if (!path.startsWith("/api/")) return realFetch(input, opts);
    const body = opts.body ? JSON.parse(opts.body) : {};
    try {
      if (path === "/api/login" && (opts.method || "GET") === "GET") return reply(200, {password_set: true});
      if (path === "/api/login") {
        const r = await realFetch(URL + "/functions/v1/login", {method: "POST", headers: H, body: JSON.stringify({password: body.password})});
        const j = await r.json().catch(() => ({}));
        if (r.ok) { cache.items = {}; cache.since = null; }
        return reply(r.status, j);
      }
      if (path === "/api/push/subscribe") {   // lock-screen notifications: the Supabase function keeps the subscription
        const r = await realFetch(URL + "/functions/v1/push", {method: "POST", headers: H, body: JSON.stringify(Object.assign({}, body, {token: tok()}))});
        const j = await r.json().catch(() => ({}));
        if (r.status === 401) { const e = new Error("login"); e.code = 401; throw e; }
        return reply(r.status, j);
      }
      if (path === "/api/desks" || path.startsWith("/api/desk/")) {
        if (opts.method === "POST") {
          const ses = path.match(new RegExp("^/api/desk/(memes|perps)/(start|stop)$"));
          if (path === "/api/desk/pause") await rpc("app_command", {p_token: tok(), p_kind: "pause", p_payload: body});
          else if (ses) await rpc("app_command", {p_token: tok(), p_kind: ses[2] === "start" ? "start_session" : "stop_session",
                                                  p_payload: Object.assign({}, ses[2] === "start" ? body : {}, {desk: ses[1]})});
          else return reply(404, {error: "not found"});
          return reply(202, {queued: true});
        }
        const items = await state();
        const runner = signal(items);
        const data = path === "/api/desks" ? val(items, "app:overview") : val(items, "app:desk:" + path.split("/").pop());
        if (!data) return reply(503, {error: "Ainda sem dados do bot"});
        return reply(200, Object.assign({}, data, {runner}));
      }
      return reply(404, {error: "Só disponível no PC"});
    } catch (e) {
      if (e.code === 401) return reply(401, {error: "login"});
      throw e;
    }
  };
})();
</script>
"""


def rewrite(html: str) -> str:
    html = html.replace('href="/manifest.webmanifest"', 'href="manifest.webmanifest"')
    html = html.replace('"/icon-180.png"', '"icon-180.png"').replace("'/icon-180.png'", "'icon-180.png'")
    html = html.replace('href="/app"', 'href="index.html"').replace('"/memes"', '"memes.html"')
    html = html.replace('"/perps"', '"perps.html"').replace('href="/app#/futuros"', 'href="index.html#/futuros"')
    html = re.sub(r'<a class="mainbot" href="/m">.*?</a>',
                  '<p class="mainbot" style="justify-content:center">O bot principal só abre no PC</p>', html, flags=re.S)
    return html.replace("<script>", SHIM % {"url": json.dumps(URL), "key": json.dumps(KEY)} + "<script>", 1)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    (OUT / "index.html").write_text(rewrite((STATIC / "app.html").read_text(encoding="utf-8")), encoding="utf-8")
    terminal = rewrite((STATIC / "memes.html").read_text(encoding="utf-8"))
    (OUT / "memes.html").write_text(terminal, encoding="utf-8")
    (OUT / "perps.html").write_text(terminal, encoding="utf-8")   # same terminal; it picks the mode from its name
    for name in ("icon-180.png", "icon-512.png", "sw.js"):   # sw.js: lock-screen notifications
        shutil.copy(STATIC / name, OUT / name)
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    manifest.update(start_url="./index.html", scope="./")
    for i in manifest["icons"]:
        i["src"] = i["src"].lstrip("/")
    (OUT / "manifest.webmanifest").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    print("docs/ pronto:", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
