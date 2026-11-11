"""The "Gráfico" page (one screenshot of a futures chart read by the AI) and its phone build.

Text checks on the page the PC serves and on the docs/ build the phone opens, plus, when node is installed,
the page's own number formatting, "best option" card, leverage view and waiting rules, and the relay shim's two
chart calls run against a pretend relay. Nothing here opens a browser, the network or the AI, or writes into the repo.
"""

import importlib.util
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_pages", ROOT / "deploy" / "build_pages.py")
build_pages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_pages)

NOTE = ("Leitura educativa de uma só imagem, feita por IA: é uma leitura limitada, não uma garantia nem um conselho "
        "financeiro. Os dados de mercado ao vivo vêm da API pública da Hyperliquid e podem diferir da corretora do "
        "teu screenshot; também não garantem nada. «Melhor opção» quer dizer o melhor valor esperado entre as opções "
        "que a imagem mostra, depois de custos aproximados, nunca uma promessa de lucro. As probabilidades são a "
        "opinião do modelo e o valor esperado depende delas. Modo papel: nada é comprado ou vendido.")
node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def source(name="grafico.html"):
    return (build_pages.STATIC / name).read_text(encoding="utf-8")


def built(name="grafico.html"):
    return (build_pages.OUT / name).read_text(encoding="utf-8")


def run_node(tmp_path, name, code):
    script = tmp_path / name
    script.write_text(code, encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(tmp_path))
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


# ---------------------------------------------------------------- the page the PC serves

def test_the_page_has_one_plain_script_tag():
    """The build puts the relay shim before the first literal "<script>": a second tag, or one with attributes,
    would leave the phone without the shim or with it in the wrong place."""
    page = source()
    assert page.count("<script>") == 1
    assert len(re.findall(r"<script\b", page)) == 1
    assert page.count("</script>") == 1


def test_the_file_input_lets_the_phone_choose_from_the_gallery():
    """A screenshot is already in the gallery: a camera-only input (the capture attribute) could not reach it.
    Without it the phone offers the gallery, the files and the camera."""
    page = source()
    assert "capture" not in page.lower()
    inputs = re.findall(r"<input[^>]*type=\"file\"[^>]*>", page)
    assert len(inputs) == 1                                           # one screenshot per analysis
    assert 'accept="image/png,image/jpeg,image/webp"' in inputs[0] and "multiple" not in inputs[0]


def test_the_one_slot_the_timeframe_buttons_and_the_words_that_must_be_there():
    page = source()
    markup = page.split("<script>")[0]
    assert '<html lang="pt">' in page
    assert markup.count('class="slot"') == 1 and markup.count('class="pick"') == 1 and markup.count('class="rm"') == 1
    assert 'aria-label="Remover o screenshot">Remover</button>' in markup          # a way to take it out again
    assert '"Trocar"' in page                                                       # and to put another in its place
    # the timeframe is optional: "Automático" is the one pressed, and each button is big enough for a thumb
    tf = markup.split('id="tf"')[1].split("</div>")[0]
    assert re.findall(r'<button type="button" data-tf="([^"]+)" aria-pressed="([^"]+)">([^<]+)</button>', tf) == [
        ("auto", "true", "Automático"), ("1m", "false", "1 min"), ("5m", "false", "5 min"), ("15m", "false", "15 min")]
    assert markup.count("data-tf=") == 4
    assert re.search(r"\.seg button\{[^}]*min-height:44px", page)
    assert "Tempo do gráfico (opcional)" in markup and "Em Automático leio o tempo na própria imagem." in markup
    assert 'maxlength="200"' in page and "Ativo ou contexto (opcional)" in page
    assert ">Analisar</button>" in page
    assert 'href="/app" aria-label="Voltar às mesas"' in page
    for said in ("A IA está desligada neste computador",
                 "O computador está desligado ou a dormir: a análise precisa dele ligado.",
                 "Não consegui abrir esta imagem.", "À espera do computador…", "A analisar…", "Há uma análise a decorrer",
                 "O computador não respondeu. Confirma que está ligado e tenta outra vez.",
                 "Põe um screenshot para analisar.", "Só vai um screenshot por análise: fiquei com o primeiro.",
                 "Não consegui ler este gráfico", "Ficar de fora", "Comprar (long)", "Vender (short)",
                 "Probabilidade estimada", "Valor esperado", "depois de custos", "antes de custos", "custos ≈ ",
                 "Todas as opções", "Cenários", "Leitura do gráfico",
                 "Confluência", "Riscos", "O que não se vê na imagem", "Últimas análises"):
        assert said in page, said
    for gone in ("os três", "dois ou", "Leitura por tempo"):                        # the three-screenshot page is over
        assert gone not in page, gone


def test_the_page_says_it_is_for_futures_and_looks_at_both_sides():
    markup = source().split("<script>")[0]
    assert ("Envia um screenshot de um gráfico de futuros. Leio-o como um trader, avalio o long e o short e digo "
            "a melhor opção, ou que é melhor ficar de fora.") in markup
    assert "Um screenshot por análise, de um gráfico de futuros (perpétuos) em 1, 5 ou 15 min." in markup


def test_the_note_about_what_this_is_stays_under_every_result():
    """It is in the page itself, not drawn by the script, so no result can come without it. It comes after the
    result and after the last analyses (which also say "Comprar (long)" / "Vender (short)"), inside the page's main."""
    page = source()
    assert page.count(NOTE) == 1 and page.count('class="note"') == 1
    markup = page.split("<script>")[0]
    after = markup.split('id="hist-wrap"')[1]
    assert markup.index('id="result-wrap"') < markup.index('id="hist-wrap"')
    assert f'</section>\n  <p class="note">{NOTE}</p>\n</main>' in after
    for said in ("educativa", "uma só imagem", "leitura limitada", "não uma garantia", "nem um conselho financeiro",
                 "Os dados de mercado ao vivo vêm da API pública da Hyperliquid",       # where the live numbers are from
                 "podem diferir da corretora do teu screenshot", "também não garantem nada",
                 "«Melhor opção» quer dizer o melhor valor esperado entre as opções que a imagem mostra, depois de "
                 "custos aproximados, nunca uma promessa de lucro", "Modo papel: nada é comprado ou vendido."):
        assert said in NOTE, said
    assert "Mais rentável" not in page                                # the page says "Melhor opção", and so does the note
    assert '<div class="k">Melhor opção</div>' in page


class Hidden(HTMLParser):
    """Where each <p class="note"> sits: the tags around it, and whether any of them is hidden."""
    VOID = {"meta", "link", "img", "input", "br", "hr"}

    def __init__(self):
        super().__init__()
        self.open, self.notes = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "p" and "note" in (attrs.get("class") or "").split():
            self.notes.append({"path": [t for t, _ in self.open], "hidden": "hidden" in attrs or any(h for _, h in self.open)})
        if tag not in self.VOID:
            self.open.append((tag, "hidden" in attrs))

    def handle_endtag(self, tag):
        while self.open and self.open.pop()[0] != tag:
            pass


@pytest.mark.parametrize("read", [source, built])
def test_the_note_is_always_on_screen_never_inside_something_hidden(read):
    """The result and the last analyses are hidden until there is something in them: the note is in neither, so
    it is there from the first moment, whatever the page is showing."""
    seen = Hidden()
    seen.feed(read().split("<script>")[0])
    assert seen.notes == [{"path": ["html", "body", "main"], "hidden": False}]
    # and the check does see a hidden parent: the same note put back inside the result would fail it
    inside = Hidden()
    inside.feed('<main><section id="result-wrap" hidden tabindex="-1"><div></div><p class="note">x</p></section></main>')
    assert inside.notes == [{"path": ["main", "section"], "hidden": True}]


def test_the_leverage_only_changes_what_is_shown():
    """5x, 10x or 20x is a way of looking at the same result: it is kept on the device and never leaves it."""
    page = source()
    script = page.split("<script>")[1]
    assert "const LEVS = [5, 10, 20];" in script and 'LEV_KEY = "deskbot-grafico-lev"' in script
    assert "lev: levPick(store.get(LEV_KEY))" in script               # 10 when nothing was kept or nothing can be
    assert "store.set(LEV_KEY, String(lev));" in script
    # the storage is only ever touched inside a try: the page works with it blocked
    for call in re.findall(r"(?:localStorage|sessionStorage)\.\w+\(", script):
        assert re.search(r"try \{[^{}]*" + re.escape(call), script), call
    assert script.count("localStorage.") == 2 and script.count("sessionStorage.") == 3
    # the request is built from the images of the one screenshot, the timeframe and the note, and from nothing else
    assert "body: JSON.stringify(requestBody(S.shot.images, S.tf, $(\"#note\").value))" in script
    send = script[script.index("async function analyse()"):script.index('$("#go").addEventListener')]
    assert "lev" not in send.lower()
    # a new choice draws the result on screen again and asks nothing of the bot
    again = script[script.index('$("#result").addEventListener("click"'):script.index("function renderHistory()")]
    assert "renderResult();" in again and "api(" not in again and "load(" not in again and "fetch(" not in again
    for said in ("Alavancagem", " da margem</small>", "Só muda a forma de mostrar os números: nada é enviado nem aberto.",
                 "As percentagens «da margem» não contam comissões nem funding, e a distância à liquidação é aproximada "
                 "(margem isolada)."):
        assert said in script, said
    assert "Os valores ignoram" not in script              # it read as if the expected value ignored the costs too


def test_the_page_only_reads_and_asks_for_an_analysis():
    """Analysis only: the page knows the login and its own two calls, and none of the calls that start, stop or
    pause a desk. It signs in like the other pages."""
    page = source()
    assert set(re.findall(r'"(/api/[^"]*)"', page)) == {"/api/login", "/api/desk/grafico"}
    assert 'API + "/analyze"' in page
    for word in ("/start", "/stop", "/pause", "app_command"):
        assert word not in page
    assert '"deskbot-token"' in page and '"X-Desk-Token"' in page
    assert "localStorage.setItem(\"deskbot-grafico" not in page       # the wait is remembered for this tab only
    assert "sessionStorage" in page


def test_the_images_are_made_small_before_they_leave():
    """At most 1568 px on the long edge and 390 kB each (the bot refuses more than 400 kB), in the sharpest format
    that fits: PNG keeps every pixel of the thin digits of the price axis, JPEG is the last resort."""
    script = source().split("<script>")[1]
    assert "const MAX_EDGE = 1568, MAX_BYTES = 390000;" in script
    ladder = json.loads(re.search(r"^const ENCODINGS = (\[.*?\]);$", script, re.M | re.S).group(1))
    assert ladder == [["image/png"], ["image/webp", 0.95], ["image/webp", 0.9],
                      ["image/jpeg", 0.92], ["image/jpeg", 0.85], ["image/jpeg", 0.75], ["image/jpeg", 0.65]]
    # one place makes the files; what it got back is only used when it is the format that was asked for
    assert script.count(".toDataURL(") - script.count("canvas.toDataURL()") == 1 and "cv.toDataURL(type, q)" in script
    assert "readDataUrl(url, type)" in script and "bytes <= MAX_BYTES" in script
    make = script[script.index("function encode("):script.index("async function shrink(")]
    assert make.index('g.fillStyle = "#0b0c0f"; g.fillRect(0, 0, w, h);') < make.index("g.drawImage(")   # dark, then the image
    assert "w = Math.max(1, Math.round(w * 0.85)); h = Math.max(1, Math.round(h * 0.85));" in make       # too heavy: smaller
    # the preview says the format the whole view was really made in
    assert 'img.src = "data:" + shot.images[0].mime + ";base64," + shot.images[0].b64;' in script
    assert "data:image/jpeg;base64," not in script and "QUALITIES" not in script
    # an image is never kept anywhere: the only things written on the device are the key, the wait and the leverage
    assert set(re.findall(r"\b(?:store|tab)\.set\(([^,)]+)", script)) == {'"deskbot-token"', "WAIT_KEY", "LEV_KEY"}
    assert "indexedDB" not in script and "caches." not in script and "download" not in script
    assert "cv.width = cv.height = 1;" in script                      # and the drawing itself is let go
    # under the slot: what will leave, and a warning for a source too small to read
    markup = source().split("<script>")[0]
    assert '<p class="help" id="shot-info" role="status" hidden></p>' in markup
    assert '<p class="lw warn" id="shot-small" role="status" hidden></p>' in markup
    assert '$("#shot-info").textContent = said.envio;' in script and '$("#shot-small").textContent = said.aviso;' in script


# ---------------------------------------------------------------- the card on the home page

def test_the_home_page_has_the_card_outside_the_desks_it_redraws():
    app = source("app.html")
    empty = '<div class="desks" id="desks"></div>'                    # loadHome() fills this one from /api/desks
    card = '<a class="dcard" href="/grafico" aria-label="Abrir a mesa Gráfico"'
    assert app.count(empty) == 1 and app.count(card) == 1 and app.count('"/grafico"') == 1
    assert app.index(empty) < app.index(card) < app.index('<a class="mainbot"')
    text = app[app.index(card):app.index('<a class="mainbot"')]
    assert "<b>Gráfico</b>" in text and "1 screenshot · futuros · 1/5/15 min" in text
    assert ("Envia um screenshot de um gráfico de futuros. A IA lê-o como um trader, avalia o long e o short e diz "
            "a melhor opção, ou que é melhor ficar de fora.") in text
    assert "screenshots" not in text and "três" not in text
    assert "terminal · 2 mesas + gráfico · papel" in app
    assert "grafico" not in app.split("<script>")[1]                  # not a desk: the script never asks for it


# ---------------------------------------------------------------- the phone build

def test_the_phone_build_has_the_shim_and_the_chart_calls():
    page = built()
    assert page.count("<script>") == 2
    shim = page.split("<script>")[1]
    assert "window.DESKBOT_REMOTE = true" in shim
    assert 'rpc("app_chart", {p_token: tok(), p_shots: body.shots, p_note: body.note || ""})' in shim
    assert 'rpc("app_chart_get", {p_token: tok()})' in shim
    assert NOTE in page and "Mais rentável" not in page


def test_the_phone_build_has_no_link_to_the_pcs_own_addresses():
    page = build_pages.rewrite(source())
    assert re.findall(r'(?:href|src)=["\']/[^"\']*["\']', page) == []
    assert 'href="index.html" aria-label="Voltar às mesas"' in page
    home = built("index.html")
    assert 'href="grafico.html" aria-label="Abrir a mesa Gráfico"' in home
    for html in (page, home):
        assert 'href="/grafico"' not in html and 'href="/app"' not in html


def test_the_shim_answers_the_chart_before_the_desks_and_keeps_off_the_phones_clock():
    shim = build_pages.rewrite("<html><script>var page = 1;</script></html>").split("<script>")[1]
    assert "Date.now" not in shim and "new Date" not in shim
    desks = shim.index('path.startsWith("/api/desk/")')                # the generic branch would answer 404 or 503
    assert shim.index('path === "/api/desk/grafico/analyze"') < shim.index('path === "/api/desk/grafico")') < desks


SHIM_HARNESS = """
const QUEUE = __ANSWERS__, ASKS = __ASKS__, calls = [];
globalThis.window = globalThis;
globalThis.localStorage = {getItem: () => "token"};
globalThis.fetch = async (url, opts) => {
  calls.push({url: String(url), body: JSON.parse(opts.body)});
  const a = QUEUE.shift();
  if (a.status === 0) throw new TypeError("Failed to fetch");      // no network: the request never left
  return new Response(JSON.stringify(a.body), {status: a.status});
};
Date.now = () => 0;                                   // a phone whose clock is decades wrong
__SHIM__
(async () => {
  const out = [];
  for (const a of ASKS) {
    try { const r = await window.fetch(a.path, a.opts); out.push({status: r.status, body: await r.json()}); }
    catch (e) { out.push({rejected: String(e.message)}); }         // what the page reads as "no connection"
  }
  console.log(JSON.stringify({out, calls}));
})();
"""


@node
def test_the_shim_sends_the_screenshots_and_reads_the_answer_back(tmp_path):
    shim = re.search(r"<script>(.*?)</script>", build_pages.rewrite("<script></script>"), re.S).group(1)
    shots = [{"tf": "5m", "image": "QUJD"}]
    post = {"method": "POST", "headers": {"Content-Type": "application/json"}}
    send = {"path": "/api/desk/grafico/analyze", "opts": dict(post, body=json.dumps({"shots": shots, "note": "BTC"}))}
    bare = {"path": "/api/desk/grafico/analyze", "opts": dict(post, body=json.dumps({"shots": shots}))}
    read = {"path": "/api/desk/grafico", "opts": {}}
    job = {"id": "41", "status": "analysing", "started": "2026-11-10T12:09:50+00:00", "ended": None, "secs": None,
           "tfs": ["5m"], "note": "BTC", "error": None, "result": None}
    answers = [
        {"status": 200, "body": 41},                                                   # 0 queued: the command's number
        {"status": 400, "body": {"message": "too many commands"}},                     # 1
        {"status": 400, "body": {"message": "too large"}},                             # 2
        {"status": 400, "body": {"message": "bad shots"}},                             # 3
        {"status": 400, "body": {"message": "login needed"}},                          # 4
        # 5 the computer beat at 12:05:00.654 by the relay's date; the relay answers at 12:10:00.123
        {"status": 200, "body": {"now": "2026-11-10T12:10:00.123456+00:00", "chart": {"job": job, "history": []},
                                 "runner": {"value": {"owner": "pc", "heartbeat": "2026-11-10T12:04:58+00:00"},
                                            "updated_at": "2026-11-10T12:05:00.654321+00:00"}}},
        {"status": 200, "body": {"now": "2026-11-10T12:10:00+00:00", "chart": None, "runner": None}},   # 6 nothing yet
        {"status": 401, "body": {}},                                                   # 7 the key is not good any more
    ]
    asks = [send, bare, send, send, send, read, read, read]
    code = (SHIM_HARNESS.replace("__ANSWERS__", json.dumps(answers)).replace("__ASKS__", json.dumps(asks))
            .replace("__SHIM__", shim))
    got = run_node(tmp_path, "chart_shim.js", code)
    out, calls = got["out"], got["calls"]

    assert [c["url"].rsplit("/", 1)[1] for c in calls] == ["app_chart"] * 5 + ["app_chart_get"] * 3
    assert calls[0]["body"] == {"p_token": "token", "p_shots": shots, "p_note": "BTC"}
    assert calls[1]["body"]["p_note"] == ""                                            # no note: an empty one
    assert calls[5]["body"] == {"p_token": "token"}

    assert (out[0]["status"], out[0]["body"]) == (202, {"id": "41", "status": "queued"})
    assert (out[1]["status"], out[1]["body"]) == (429, {"error": "Demasiados pedidos: espera um minuto"})
    assert (out[2]["status"], out[2]["body"]) == (413, {"error": "As imagens são demasiado grandes"})
    assert (out[3]["status"], out[3]["body"]) == (400, {"error": "Pedido inválido"})
    assert out[4]["status"] == 401 and out[7]["status"] == 401                         # the page shows the login
    assert out[5]["status"] == 200
    assert out[5]["body"] == {"ai": {"enabled": True, "backend": "pc"}, "job": job, "history": [],
                              "runner": {"owner": "pc", "heartbeat": "2026-11-10T12:04:58+00:00", "age_s": 299}}
    assert out[6]["body"] == {"ai": {"enabled": True, "backend": "pc"}, "job": None, "history": [], "runner": {}}


@node
def test_the_shim_says_why_a_chart_call_failed_and_only_no_network_is_no_connection(tmp_path):
    """An error answer of the relay to one of the two chart calls reaches the page as an answer with words in it.
    Only a request that never left is a rejected fetch (the page's "Sem ligação"). The other desks stay as they were."""
    shim = re.search(r"<script>(.*?)</script>", build_pages.rewrite("<script></script>"), re.S).group(1)
    post = {"method": "POST", "headers": {"Content-Type": "application/json"}}
    send = {"path": "/api/desk/grafico/analyze",
            "opts": dict(post, body=json.dumps({"shots": [{"tf": "auto", "image": "QUJD"}], "note": ""}))}
    read = {"path": "/api/desk/grafico", "opts": {}}
    missing = {"status": 404, "body": {"code": "PGRST202", "message": "Could not find the function public.app_chart"}}
    broken = {"status": 500, "body": {"message": "internal error"}}
    refused = {"id": "42", "error": "Já há uma análise a decorrer"}
    job = {"id": "41", "status": "analysing"}
    answers = [
        {"status": 400, "body": {"message": "too many charts"}},                       # 0 the brake of 8 in 10 minutes
        {"status": 400, "body": {"message": "too many commands"}},                     # 1 the one per minute, as before
        missing, broken,                                                               # 2-3 app_chart
        {"status": 0}, {"status": 401, "body": {}},                                    # 4-5 no network; the key is bad
        {"status": 400, "body": {"message": "something new"}},                         # 6 a refusal with no words for it
        missing, broken,                                                               # 7-8 app_chart_get
        {"status": 0}, {"status": 400, "body": {"message": "login needed"}},           # 9-10
        {"status": 200, "body": {"now": "2026-11-10T12:10:00+00:00", "runner": None,   # 11 the PC refused request 42
                                 "chart": {"job": job, "history": [], "refused": refused}}},
        {"status": 200, "body": {"now": "2026-11-10T12:10:00+00:00", "runner": None,   # 12 and nothing refused
                                 "chart": {"job": job, "history": [], "refused": None}}},
        broken, missing,                                                               # 13-14 another desk: app_state
        broken,                                                                        # 15 another desk: app_command
    ]
    asks = [send] * 7 + [read] * 6 + [{"path": "/api/desks", "opts": {}}, {"path": "/api/desk/memes", "opts": {}},
                                      {"path": "/api/desk/pause", "opts": dict(post, body="{}")}]
    code = (SHIM_HARNESS.replace("__ANSWERS__", json.dumps(answers)).replace("__ASKS__", json.dumps(asks))
            .replace("__SHIM__", shim))
    got = run_node(tmp_path, "chart_fail.js", code)
    out, calls = got["out"], got["calls"]
    assert [c["url"].rsplit("/", 1)[1] for c in calls] == (["app_chart"] * 7 + ["app_chart_get"] * 6 +
                                                           ["app_state"] * 2 + ["app_command"])
    late = {"status": 503, "body": {"error": "A mesa Gráfico ainda não está disponível no telemóvel"}}
    failed = {"status": 502, "body": {"error": "O servidor não aceitou o pedido. Tenta mais tarde."}}
    gone = {"rejected": "Failed to fetch"}
    assert out[0] == {"status": 429, "body": {"error": "Demasiadas análises seguidas: espera uns 10 minutos"}}
    assert out[1] == {"status": 429, "body": {"error": "Demasiados pedidos: espera um minuto"}}
    assert out[2:7] == [late, failed, gone, {"status": 401, "body": {"error": "login"}}, failed]
    assert out[7:11] == [late, failed, gone, {"status": 401, "body": {"error": "login"}}]
    # "refused" is one more field of chart:out: it reaches the page as the PC wrote it
    assert out[11]["status"] == 200 and out[11]["body"]["refused"] == refused and out[11]["body"]["job"] == job
    assert out[12]["body"]["refused"] is None
    # the desks that are not the chart: an error of the relay is still a rejected fetch, with the relay's words
    assert out[13:] == [{"rejected": "internal error"}, {"rejected": "Could not find the function public.app_chart"},
                        {"rejected": "internal error"}]
    # the brake of the charts is looked for before the one of the commands, inside the chart branch only
    chart = shim[shim.index('path === "/api/desk/grafico/analyze"'):shim.index('path === "/api/desks"')]
    assert chart.index('"too many charts"') < chart.index('"too many commands"')
    assert shim.count("chartFail(e)") == 3 and chart.count("chartFail(e)") == 2       # defined once, used twice


# ---------------------------------------------------------------- the page's own rules, run by node

PURE = ("fmt", "num", "fmtPrice", "fmtPct", "fmtRR", "fmtEV", "tfLabel", "trendWord", "actionWord", "b64Bytes",
        "fitSize", "tilePlan", "readDataUrl", "shotInfo", "fmtSigned", "fmtUsd", "marketModel", "optionModel",
        "heroModel", "levModel", "fmtMargem", "levWarning", "levPick", "requestBody", "waitState", "waitKind")


def page_functions():
    page = source()
    code = [re.search(r"^const NFC = .*$", page, re.M).group(0),
            re.search(r"^const MAX_EDGE = .*$", page, re.M).group(0)]      # and MAX_BYTES, on the same line
    for name in PURE:
        found = re.search(rf"^function {name}\(.*?^\}}", page, re.M | re.S)
        assert found, f"{name} must stay a plain top-level function"
        code.append(found.group(0))
    return "\n".join(code)


def plain(value):
    """pt-PT groups thousands with a no-break space: compare with ordinary ones."""
    if isinstance(value, str):
        return re.sub(r"\s", " ", value)
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


@node
def test_the_numbers_are_written_the_portuguese_way_with_the_precision_the_price_needs(tmp_path):
    asks = """[
      [67432.5, 185.2, 1.0845, 0.05123, 0.00001234, 0, null, "abc", true].map(fmtPrice),
      [fmtRR(2.3), fmtRR(1), fmtRR(null)],
      [fmtEV(0.42), fmtEV(-0.3), fmtEV(0), fmtEV(undefined)],
      [fmtPct(30.3, 0), fmtPct(2), fmtPct(0.45), fmtPct("x")],
      [b64Bytes("QUJD"), b64Bytes("QUI="), b64Bytes("QQ=="), b64Bytes("")],
      [fitSize(3136, 1000, 1568), fitSize(800, 600, 1568), fitSize(1000, 4000, 1568)],
      ["1m", "5m", "15m", "outro", "auto", null].map(tfLabel),
    ]"""
    got = plain(run_node(tmp_path, "numbers.js", page_functions() + f"\nconsole.log(JSON.stringify({asks}));\n"))
    assert got[0] == ["67 432,5", "185,20", "1,0845", "0,05123", "0,00001234", "0", "—", "—", "—"]
    assert got[1] == ["1 : 2,3", "1 : 1,0", "—"]
    assert got[2] == ["+0,42 R", "−0,30 R", "0,00 R", "—"]
    assert got[3] == ["30 %", "2,0 %", "0,45 %", "—"]
    assert got[4] == [3, 2, 1, 0]
    assert got[5] == [[1568, 500], [800, 600], [392, 1568]]
    assert got[6] == ["1 min", "5 min", "15 min", "outro", "automático", ""]


def option(**over):
    """A valid option as the bot sends it: the stop at 2 % of the price, so the rough costs (0.1 % of the price for
    the round trip) are 0.05 R, and the expected value after them 0.42 - 0.05."""
    base = {"nome": "Rompimento", "direcao": "long", "tipo": "breakout", "tf": "5m", "entrada": 100, "stop": 98,
            "alvo": 104.6, "prob_alvo": 80, "gatilho": "fecho acima de 100", "porque": "tendência de alta",
            "valida": True, "problema": "", "rr": 2.3, "prob_usada": 70, "ev_r": 0.42, "equilibrio_pct": 30.3,
            "stop_pct": 2.0, "alvo_pct": 4.6, "custo_r": 0.05, "ev_liq_r": 0.37}
    return dict(base, **over)


def before_costs(**over):
    """The same option as an analysis kept from before the costs: neither of the two numbers is in it."""
    old = option(**over)
    del old["custo_r"], old["ev_liq_r"]
    return old


@node
def test_the_best_option_card_shows_what_the_code_chose(tmp_path):
    bad = option(nome="Sem stop", direcao="short", valida=False, problema="stop do lado errado", rr=None, ev_r=None,
                 custo_r=None, ev_liq_r=None)
    short = option(nome="Rejeição", direcao="short", prob_alvo=40, prob_usada=40, ev_r=-0.1, rr=1.5, ev_liq_r=-0.15)
    cases = [
        {"melhor": {"acao": "long", "opcao": 0, "porque": "melhor valor esperado"}, "opcoes": [option(), short]},   # 0
        {"melhor": {"acao": "fora", "opcao": None, "porque": "nenhuma compensa"}, "opcoes": [short]},               # 1
        {"melhor": {"acao": "short", "opcao": 5, "porque": "x"}, "opcoes": [option(), bad, short]},                 # 2
        {"melhor": {"acao": "short", "opcao": 0, "porque": "x"}, "opcoes": [bad]},                                  # 3
        {},                                                                                                         # 4
    ]
    got = plain(run_node(tmp_path, "hero.js", page_functions() + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(heroModel)));\n"))
    best = got[0]
    assert (best["fora"], best["index"], best["dir"]) == (False, 0, "long")
    assert best["titulo"] == "Comprar (long)" and best["nome"] == "Rompimento" and best["tipo"] == "breakout"
    assert (best["entrada"], best["stop"], best["alvo"]) == ("100,00", "98,00", "104,60")
    assert best["rr"] == "1 : 2,3" and best["prob"] == "80 %" and best["contada"] == "contada como 70 %"
    # the number the card leads with is the one after costs; the one before them and the costs come as a note
    assert best["ev"] == "+0,37 R" and best["evSign"] == 1 and best["evQuando"] == "depois de custos"
    assert best["evNota"] == "+0,42 R antes de custos · custos ≈ 0,05 R"
    assert best["equilibrio"] == "Precisa de acertar mais de 30 % das vezes para compensar"
    assert (best["stopPct"], best["alvoPct"]) == ("2,0 %", "4,6 %")
    assert best["motivo"] == "melhor valor esperado"
    assert got[1] == {"fora": True, "index": -1, "titulo": "Ficar de fora", "motivo": "nenhuma compensa"}
    # a number that points nowhere: the first valid option of the side the code chose, never an invalid one
    assert (got[2]["fora"], got[2]["index"], got[2]["titulo"]) == (False, 2, "Vender (short)")
    assert got[2]["contada"] == "" and got[2]["ev"] == "−0,15 R" and got[2]["evSign"] == -1
    assert got[2]["evNota"] == "−0,10 R antes de custos · custos ≈ 0,05 R"
    assert got[3]["fora"] is True and got[4]["fora"] is True and got[4]["titulo"] == "Ficar de fora"


@node
def test_the_expected_value_is_shown_after_costs_and_an_old_analysis_says_before(tmp_path):
    cases = [
        option(ev_r=0.25, custo_r=0.1, ev_liq_r=0.15),                     # 0 as the page is asked to show it
        option(ev_r=0.05, custo_r=0.1, ev_liq_r=-0.05),                    # 1 the costs eat it: red, not green
        option(ev_r=0.1, custo_r=0.1, ev_liq_r=0),                         # 2 exactly nothing left
        before_costs(),                                                    # 3 kept from before the costs
        before_costs(ev_r=-0.3),                                           # 4
        option(custo_r=None, ev_liq_r=None),                               # 5 the two are there and empty
        option(custo_r="abc", ev_liq_r="abc"),                             # 6 not numbers
        option(custo_r=None),                                              # 7 only the net one
        option(ev_r=None),                                                 # 8 only the net one and the cost
        option(valida=False, problema="stop a menos de 0,1% do preço", rr=None, ev_r=None, custo_r=None, ev_liq_r=None,
               stop_pct=None, alvo_pct=None),                              # 9 does not count: nothing to say
        {}, None,                                                          # 10-11
    ]
    code = page_functions() + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(optionModel)));\n"
    got = plain(run_node(tmp_path, "costs.js", code))
    view = [(g["ev"], g["evSign"], g["evQuando"], g["evNota"]) for g in got]
    assert view[0] == ("+0,15 R", 1, "depois de custos", "+0,25 R antes de custos · custos ≈ 0,10 R")
    assert view[1] == ("−0,05 R", -1, "depois de custos", "+0,05 R antes de custos · custos ≈ 0,10 R")
    assert view[2] == ("0,00 R", 0, "depois de custos", "+0,10 R antes de custos · custos ≈ 0,10 R")
    assert view[3] == ("+0,42 R", 1, "antes de custos", "")
    assert view[4] == ("−0,30 R", -1, "antes de custos", "")
    assert view[5] == view[6] == view[3]
    assert view[7] == ("+0,37 R", 1, "depois de custos", "+0,42 R antes de custos")
    assert view[8] == ("+0,37 R", 1, "depois de custos", "custos ≈ 0,05 R")
    assert view[9] == view[10] == view[11] == ("—", 0, "antes de custos", "")
    assert got[9]["valida"] is False and got[9]["problema"] == "stop a menos de 0,1% do preço"
    said = json.dumps(got, ensure_ascii=False)
    assert "NaN" not in said and "undefined" not in said and "null" not in said and "Infinity" not in said


# ---------------------------------------------------------------- futures: the same option seen with leverage

def lev_view(tmp_path, cases):
    """levModel() of each [option, leverage], with the two numbers as the page writes them and the warning."""
    code = (page_functions() + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(c => {{ const m = levModel(c[0], c[1]);"
            " return Object.assign({stop: fmtMargem(m.stopMargem), alvo: fmtMargem(m.alvoMargem), said: levWarning(m)}, m); })));\n")
    return plain(run_node(tmp_path, "lev.js", code))


@node
def test_stop_and_target_as_a_share_of_the_margin_with_5x_10x_and_20x(tmp_path):
    got = lev_view(tmp_path, [[option(), 5], [option(), 10], [option(), 20],            # stop at 2 %, target at 4.6 %
                              [option(stop_pct=0.35, alvo_pct=0.07), 20],               # 3 no 7.000000000000001
                              [option(stop_pct=-2, alvo_pct=-4.6), 10]])                # 4 a sign on the wire
    keys = {"lev", "stopMargem", "alvoMargem", "liqPct", "aviso", "stop", "alvo", "said"}
    assert all(set(g) == keys for g in got)
    assert [(g["lev"], g["stopMargem"], g["alvoMargem"], g["liqPct"], g["aviso"]) for g in got] == [
        (5, -10, 23, 20, ""), (10, -20, 46, 10, ""), (20, -40, 92, 5, ""), (20, -7, 1.4, 5, ""), (10, -20, 46, 10, "")]
    # the stop is a loss and the target a gain, whatever the side of the trade
    assert [(g["stop"], g["alvo"]) for g in got] == [("−10 %", "+23 %"), ("−20 %", "+46 %"), ("−40 %", "+92 %"),
                                                     ("−7 %", "+1,4 %"), ("−20 %", "+46 %")]
    assert [g["said"] for g in got] == [""] * 5


@node
def test_a_stop_near_or_past_the_liquidation_is_said(tmp_path):
    """The liquidation is about 100 / leverage % away; the exchange gets there a little earlier (hence 0.5)."""
    stops = {5: [13.99, 14, 19.49, 19.5, 25], 10: [6.99, 7, 9.49, 9.5, 12], 20: [3.49, 3.5, 4.49, 4.5, 6]}
    cases = [[option(stop_pct=s), lev] for lev, group in stops.items() for s in group]
    got = lev_view(tmp_path, cases)
    assert [g["aviso"] for g in got] == ["", "perto", "perto", "depois", "depois"] * 3
    assert [g["liqPct"] for g in got] == [20] * 5 + [10] * 5 + [5] * 5
    assert got[14]["said"] == "Com 20x a liquidação chega antes do stop (cerca de 5% do preço): este stop não te protege."
    assert got[8]["said"] == "Com 10x a liquidação chega antes do stop (cerca de 10% do preço): este stop não te protege."
    assert got[3]["said"] == "Com 5x a liquidação chega antes do stop (cerca de 20% do preço): este stop não te protege."
    assert got[11]["said"] == ("Com 20x o stop fica perto da liquidação (cerca de 5% do preço): um movimento brusco "
                               "pode liquidar-te antes de o stop atuar.")
    assert got[0]["said"] == got[5]["said"] == got[10]["said"] == ""
    assert got[14]["stopMargem"] == -120                     # the plain multiplication: the warning is what explains it


@node
def test_an_option_that_does_not_count_or_has_no_numbers_shows_nothing(tmp_path):
    blank = {"stopMargem": None, "alvoMargem": None, "aviso": "", "stop": "", "alvo": "", "said": ""}
    cases = [
        [option(valida=False, problema="stop do lado errado", stop_pct=None, alvo_pct=None), 10],   # 0 as the bot sends it
        [option(valida=False, stop_pct=6, alvo_pct=9), 20],                                         # 1 never counted
        [{}, 10], [None, 10], ["abc", 10],                                                          # 2-4
        [option(stop_pct="abc", alvo_pct=True), 20],                                                # 5 not numbers
        [option(stop_pct=None), 10],                                                                # 6 only the target
        [option(alvo_pct=None, stop_pct=8), 10],                                                    # 7 only the stop
        [option(), None], [option(), 0], [option(), -5], [option(), "abc"],                         # 8-11 no leverage
    ]
    got = lev_view(tmp_path, cases)
    for g in got[:6]:
        assert {k: g[k] for k in blank} == blank
    assert [(g["lev"], g["liqPct"]) for g in got[:6]] == [(10, 10), (20, 5), (10, 10), (10, 10), (10, 10), (20, 5)]
    assert (got[6]["stop"], got[6]["alvo"], got[6]["aviso"]) == ("", "+46 %", "")
    assert (got[7]["stop"], got[7]["alvo"], got[7]["aviso"]) == ("−80 %", "", "perto")
    for g in got[8:]:
        assert dict({k: g[k] for k in blank}, lev=g["lev"], liqPct=g["liqPct"]) == dict(blank, lev=None, liqPct=None)
    assert "NaN" not in json.dumps(got) and "Infinity" not in json.dumps(got) and "undefined" not in json.dumps(got)


@node
def test_the_leverage_is_5_10_or_20_and_10_when_in_doubt(tmp_path):
    asks = """[[5, 10, 20, "20", "5", null, undefined, "abc", 3, 100, true, ""].map(levPick),
               [-20, 46, -7.5, 0, null, "x", -120, 1e9].map(fmtMargem),
               [null, {}, {lev: 20, liqPct: 5, aviso: ""}, {lev: null, liqPct: null, aviso: "depois"}].map(levWarning)]"""
    got = plain(run_node(tmp_path, "pick.js", page_functions() + f"\nconsole.log(JSON.stringify({asks}));\n"))
    assert got[0] == [5, 10, 20, 20, 5, 10, 10, 10, 10, 10, 10, 10]
    assert got[1] == ["−20 %", "+46 %", "−7,5 %", "0 %", "", "", "−120 %", "+1 000 000 000 %"]
    assert got[2] == ["", "", "", ""]


@node
def test_the_page_sends_exactly_one_screenshot(tmp_path):
    """One screenshot is its whole view ("full") and, when that view lost detail, up to two pieces of it ("tile"):
    the whole view always goes first, all with the one timeframe, and nothing but tf, image and part leaves."""
    asks = """(() => {
      const F = {b64: "RlVM", part: "full", mime: "image/png", bytes: 3, w: 1568, h: 882, nativo: false};
      const T1 = {b64: "VDE=", part: "tile", mime: "image/webp", bytes: 2, nativo: true}, T2 = Object.assign({}, T1, {b64: "VDI="});
      const T3 = Object.assign({}, T1, {b64: "VDM="});
      return [requestBody([F], "5m", "  BTC/USDT perp  "), requestBody([F, T1, T2], "auto", ""), requestBody([F]),
              requestBody([F, T1], "1h", null), requestBody([F], ["1m", "5m"], "x".repeat(300)),
              requestBody([F, T1, T2], "1m", 7), requestBody([F], "15m", "a"),
              requestBody([T1, F, T2], "15m", ""),                           // 7 the whole view goes first, whatever
              requestBody([F, T1, T2, T3], "5m", ""),                        // 8 never more than three
              requestBody([F, Object.assign({}, F, {b64: "T1RIRVI="}), T1], "5m", ""),     // 9 one screenshot: one whole view
              requestBody([T1, T2], "5m", "a"),                              // 10 pieces of nothing: nothing to send
              requestBody([null, "x", 7, {part: "full"}, {b64: 7, part: "full"}, {b64: "", part: "full"},
                           {b64: "RVZJTA==", part: "evil"}, {b64: "RVZJTA=="}, F], "5m", ""),   // 11 only what is whole
              requestBody("RlVM", "5m", ""), requestBody(null, "5m", ""), requestBody(undefined), requestBody({0: F, length: 1})];
    })()"""
    got = run_node(tmp_path, "body.js", page_functions() + f"\nconsole.log(JSON.stringify({asks}));\n")
    full = {"image": "RlVM", "part": "full"}
    t1, t2 = {"image": "VDE=", "part": "tile"}, {"image": "VDI=", "part": "tile"}
    assert got[0] == {"shots": [dict(full, tf="5m")], "note": "BTC/USDT perp"}
    assert got[1] == {"shots": [dict(full, tf="auto"), dict(t1, tf="auto"), dict(t2, tf="auto")], "note": ""}
    for body in got:
        assert set(body) == {"shots", "note"}                         # no leverage, nothing else
        assert len(body["shots"]) <= 3
        for i, shot in enumerate(body["shots"]):
            assert set(shot) == {"tf", "image", "part"}               # not the size, the format or anything else
            assert shot["part"] == ("full" if i == 0 else "tile")     # the first is the whole view, and only the first
            assert shot["tf"] == body["shots"][0]["tf"]               # a piece has the timeframe of its screenshot
    assert [[s["image"] for s in b["shots"]] for b in got] == [
        ["RlVM"], ["RlVM", "VDE=", "VDI="], ["RlVM"], ["RlVM", "VDE="], ["RlVM"], ["RlVM", "VDE=", "VDI="], ["RlVM"],
        ["RlVM", "VDE=", "VDI="], ["RlVM", "VDE=", "VDI="], ["RlVM", "VDE="], [], ["RlVM"], [], [], [], []]
    assert [b["shots"][0]["tf"] for b in got[:10]] == ["5m", "auto", "auto", "auto", "auto", "1m", "15m", "15m", "5m", "5m"]
    assert [b["note"] for b in got[:8]] == ["BTC/USDT perp", "", "", "", "x" * 200, "7", "a", ""]
    # and that is the only body the page builds: one place for one screenshot, one call that sends it
    script = source().split("<script>")[1]
    assert script.count("shots:") == 1 and script.count("requestBody(") == 2 and script.count('API + "/analyze"') == 1
    assert "shot: null," in script and "S.slots" not in script and "S.shots" not in script


# ---------------------------------------------------------------- a sharper image: the whole view and two pieces

SIZES = [[1, 1], [10000, 300], [300, 10000], [1170, 2532], [1920, 1080], [3840, 2160], [1568, 900],
         [1844, 1000], [1845, 1000], [800, 600], [1568, 1568], [2000, 2000], [2000, 1900], [2000, 1500], [1500, 2000],
         [1569, 1], [0, 0], [-5, "abc"], [None, 800]]


@node
def test_a_big_screenshot_also_goes_as_two_pieces_at_the_size_it_came_in(tmp_path):
    """The whole view fitted to 1568 px loses the small numbers of a big screenshot. When that fitting scaled it
    below 0.85, two halves along the long side go with it, overlapping, each fitted to 1568 px on its own."""
    code = page_functions() + f"\nconsole.log(JSON.stringify({json.dumps(SIZES)}.map(s => tilePlan(s[0], s[1]))));\n"
    got = dict(zip([f"{w}x{h}" for w, h in SIZES], run_node(tmp_path, "tiles.js", code)))

    def tile(sx, sy, sw, sh, w, h):
        return {"sx": sx, "sy": sy, "sw": sw, "sh": sh, "w": w, "h": h}

    assert got["1x1"] == {"full": {"w": 1, "h": 1}, "tiles": []}
    assert got["1568x900"] == {"full": {"w": 1568, "h": 900}, "tiles": []}             # nothing was lost: one image
    assert got["800x600"] == {"full": {"w": 800, "h": 600}, "tiles": []}               # never made bigger
    assert got["1568x1568"] == {"full": {"w": 1568, "h": 1568}, "tiles": []}
    assert got["1844x1000"] == {"full": {"w": 1568, "h": 850}, "tiles": []}            # scaled to 0.8503: still fine
    assert got["1845x1000"] == {"full": {"w": 1568, "h": 850},                         # 0.8499: two pieces, as they came
                                "tiles": [tile(0, 0, 1034, 1000, 1034, 1000), tile(811, 0, 1034, 1000, 1034, 1000)]}
    # a laptop screen: the two halves go pixel for pixel
    assert got["1920x1080"] == {"full": {"w": 1568, "h": 882},
                                "tiles": [tile(0, 0, 1076, 1080, 1076, 1080), tile(844, 0, 1076, 1080, 1076, 1080)]}
    # a phone held upright: cut along its height
    assert got["1170x2532"] == {"full": {"w": 725, "h": 1568},
                                "tiles": [tile(0, 0, 1170, 1418, 1170, 1418), tile(0, 1114, 1170, 1418, 1170, 1418)]}
    # 4K: a half is still over 1568 px, so it is fitted too (0.73 of the source, against 0.41 for the whole view)
    assert got["3840x2160"] == {"full": {"w": 1568, "h": 882},
                                "tiles": [tile(0, 0, 2151, 2160, 1561, 1568), tile(1689, 0, 2151, 2160, 1561, 1568)]}
    assert got["10000x300"] == {"full": {"w": 1568, "h": 47},
                                "tiles": [tile(0, 0, 5600, 300, 1568, 84), tile(4400, 0, 5600, 300, 1568, 84)]}
    assert got["300x10000"] == {"full": {"w": 47, "h": 1568},
                                "tiles": [tile(0, 0, 300, 5600, 84, 1568), tile(0, 4400, 300, 5600, 84, 1568)]}
    assert got["1569x1"] == {"full": {"w": 1568, "h": 1}, "tiles": []}
    # a square one: a half of it is as tall as the whole, so it would come out no sharper. Not sent, not paid for
    assert got["2000x2000"] == {"full": {"w": 1568, "h": 1568}, "tiles": []}
    assert got["2000x1900"] == {"full": {"w": 1568, "h": 1490}, "tiles": []}           # 5 % sharper is not worth two images
    assert got["2000x1500"] == {"full": {"w": 1568, "h": 1176},
                                "tiles": [tile(0, 0, 1120, 1500, 1120, 1500), tile(880, 0, 1120, 1500, 1120, 1500)]}
    assert got["1500x2000"] == {"full": {"w": 1176, "h": 1568},
                                "tiles": [tile(0, 0, 1500, 1120, 1500, 1120), tile(0, 880, 1500, 1120, 1500, 1120)]}
    # sizes that are not sizes: one pixel, never NaN, never a crash
    assert got["0x0"] == got["-5xabc"] == {"full": {"w": 1, "h": 1}, "tiles": []}
    assert got["Nonex800"] == {"full": {"w": 1, "h": 800}, "tiles": []}

    for (w, h), plan in zip(SIZES, got.values()):
        w, h = (v if isinstance(v, int) and v >= 1 else 1 for v in (w, h))
        side, full, tiles = max(w, h), plan["full"], plan["tiles"]
        numbers = [full["w"], full["h"]] + [v for t in tiles for v in t.values()]
        assert all(isinstance(v, int) for v in numbers) and min(full["w"], full["h"]) >= 1
        assert max(full["w"], full["h"]) == min(side, 1568) and full["w"] <= w and full["h"] <= h
        length = -(-side * 56 // 100)                                  # 56 % of the long side, rounded up
        gain = min(1, 1568 / max(length, min(w, h))) / (1568 / side)   # how much sharper a piece is than the whole
        assert len(tiles) == (2 if 1568 / side < 0.85 and gain >= 1.1 else 0)       # one image, or the whole and two pieces
        if not tiles:
            continue
        along, across, size, short = ("sx", "sy", "sw", "sh") if w >= h else ("sy", "sx", "sh", "sw")
        first, second = tiles
        assert first[size] == second[size] == length
        assert first[along] == 0 and second[along] + second[size] == side              # nothing left out at the ends
        assert first[size] - second[along] >= 0.12 * side - 1                         # and 12 % seen twice in the middle
        for t in tiles:
            assert t[across] == 0 and t[short] == min(w, h)                            # the short side goes whole
            assert t["w"] >= 1 and t["h"] >= 1 and t["w"] <= t["sw"] and t["h"] <= t["sh"]       # never made bigger
            assert max(t["w"], t["h"]) <= 1568
            assert max(t["w"], t["h"]) / max(t["sw"], t["sh"]) >= 1.09 * 1568 / side            # sharper than the whole
    # the page takes what the plan says: the whole view first, then each piece, and a piece no sharper is left out
    script = source().split("<script>")[1]
    make = script[script.index("async function shrink("):script.index("function paintSlot()")]
    assert "plan = tilePlan(srcW, srcH)" in make
    assert "encode(img, cv, g, Object.assign({sx: 0, sy: 0, sw: srcW, sh: srcH}, plan.full))" in make
    assert 'const images = [Object.assign(full, {part: "full"})];' in make
    assert 'if (made && made.w / t.sw > full.w / srcW) images.push(Object.assign(made, {part: "tile"}));' in make
    assert "return {images, srcW, srcH};" in make


ENCODE_HARNESS = """
const CASES = __CASES__;
function run(c) {
  const calls = [], drawn = [];
  const cv = {width: 0, height: 0, toDataURL(type, q) {
    calls.push([type, q === undefined ? null : q, cv.width, cv.height]);
    const a = c.answers[Math.min(calls.length, c.answers.length) - 1];
    if (a === "throw") throw new Error("SecurityError");
    if (a == null) return "data:,";                                   // what a canvas too big to draw gives back
    return "data:" + a[0] + ";base64," + "A".repeat(a[1]);
  }};
  const g = {fillRect(...a) { drawn.push(["fill", g.fillStyle].concat(a)); }, drawImage(img, ...a) { drawn.push(["draw"].concat(a)); }};
  const made = encode({}, cv, g, c.piece);
  return {made: made && Object.assign({}, made, {b64: made.b64.length}), calls, drawn, quality: g.imageSmoothingQuality};
}
console.log(JSON.stringify(CASES.map(run)));
"""


@node
def test_each_image_is_the_sharpest_file_that_fits(tmp_path):
    """encode() with a pretend canvas: PNG when it fits, then WebP only when the browser really made one (Safari
    answers a WebP request with a PNG), then JPEG, and only when nothing fits the same piece drawn smaller."""
    page = source()
    code = "\n".join([page_functions(), re.search(r"^const ENCODINGS = \[.*?\];$", page, re.M | re.S).group(0),
                      re.search(r"^function encode\(.*?^\}", page, re.M | re.S).group(0)])
    piece = {"sx": 844, "sy": 0, "sw": 1076, "sh": 1080, "w": 1076, "h": 1080}
    fits, heavy = 520000, 520004                    # base64 characters: 390 000 bytes, and 3 bytes more
    png, webp, jpeg = "image/png", "image/webp", "image/jpeg"
    cases = [
        {"piece": piece, "answers": [[png, 4000]]},                                    # 0 a PNG that fits: the end
        {"piece": piece, "answers": [[png, fits]]},                                    # 1 exactly the limit
        {"piece": piece, "answers": [[png, heavy], [webp, 4000]]},                     # 2 too heavy: a real WebP
        {"piece": piece, "answers": [[png, heavy], [png, 4000], [png, 4000], [jpeg, 4000]]},   # 3 Safari: no WebP there
        {"piece": piece, "answers": [[png, heavy], "throw", None, [jpeg, heavy], [jpeg, 8000]]},     # 4 it goes on
        {"piece": piece, "answers": [[png, heavy]] * 7 + [[png, 4000]]},               # 5 nothing fits: drawn smaller
        {"piece": piece, "answers": [[png, heavy]]},                                   # 6 never fits: given up
        {"piece": piece, "answers": [None]},                                           # 7 a canvas that draws nothing
        {"piece": piece, "answers": [["image/gif", 4000]]},                            # 8 a format nobody asked for
        {"piece": {"sx": 0, "sy": 0, "sw": 3840, "sh": 2160, "w": 1568, "h": 882}, "answers": [[png, 4000]]},     # 9
    ]
    got = run_node(tmp_path, "encode.js", code + ENCODE_HARNESS.replace("__CASES__", json.dumps(cases)))
    asked = [[[c[0], c[1]] for c in g["calls"]] for g in got]
    ladder = [[png, None], [webp, 0.95], [webp, 0.9], [jpeg, 0.92], [jpeg, 0.85], [jpeg, 0.75], [jpeg, 0.65]]

    assert got[0]["made"] == {"b64": 4000, "bytes": 3000, "mime": png, "w": 1076, "h": 1080, "nativo": True}
    assert asked[0] == [[png, None]]                                                   # no quality asked of a PNG
    assert got[1]["made"]["bytes"] == 390000 and asked[1] == [[png, None]]
    assert (got[2]["made"]["mime"], got[2]["made"]["nativo"]) == (webp, True) and asked[2] == ladder[:2]
    assert (got[3]["made"]["mime"], got[3]["made"]["bytes"]) == (jpeg, 3000) and asked[3] == ladder[:4]
    assert (got[4]["made"]["mime"], got[4]["made"]["bytes"]) == (jpeg, 6000) and asked[4] == ladder[:5]
    # the same piece of the source, drawn at 0.85 of the size: said as it is, no longer "pixel for pixel"
    assert got[5]["made"] == {"b64": 4000, "bytes": 3000, "mime": png, "w": 915, "h": 918, "nativo": False}
    assert asked[5] == ladder + [[png, None]]
    assert [c[2:] for c in got[5]["calls"]] == [[1076, 1080]] * 7 + [[915, 918]]
    assert got[5]["drawn"] == [["fill", "#0b0c0f", 0, 0, 1076, 1080], ["draw", 844, 0, 1076, 1080, 0, 0, 1076, 1080],
                               ["fill", "#0b0c0f", 0, 0, 915, 918], ["draw", 844, 0, 1076, 1080, 0, 0, 915, 918]]
    # nothing ever fits: it stops when the drawing would be too small to read, with no image at all
    for g in got[6:9]:
        assert g["made"] is None and len(g["calls"]) % 7 == 0 and 7 <= len(g["calls"]) <= 24 * 7
        sizes = [c[2:] for c in g["calls"][::7]]
        assert sizes[0] == [1076, 1080] and min(sizes[-1]) < 200 and all(min(s) >= 200 for s in sizes[:-1])
    assert got[9]["made"]["nativo"] is False and (got[9]["made"]["w"], got[9]["made"]["h"]) == (1568, 882)
    assert got[9]["drawn"] == [["fill", "#0b0c0f", 0, 0, 1568, 882], ["draw", 0, 0, 3840, 2160, 0, 0, 1568, 882]]
    assert all(g["quality"] == "high" for g in got)
    for g in got:                                                      # the dark fill always comes before the image
        assert [d[0] for d in g["drawn"]] == ["fill", "draw"] * (len(g["drawn"]) // 2)


@node
def test_only_the_format_that_was_asked_for_is_taken_from_the_canvas(tmp_path):
    asks = """[readDataUrl("data:image/png;base64,QUJD", "image/png"), readDataUrl("data:image/webp;base64,QUJD", "image/webp"),
               readDataUrl("data:image/jpeg;base64,QQ==", "image/jpeg"),
               readDataUrl("data:image/png;base64,QUJD", "image/webp"),      // Safari: asked for WebP, got a PNG
               readDataUrl("data:image/webp;base64,", "image/webp"), readDataUrl("data:,", "image/png"),
               readDataUrl("data:image/gif;base64,QQ==", "image/gif"), readDataUrl("data:image/svg+xml;base64,QQ==", "image/svg+xml"),
               readDataUrl(" data:image/png;base64,QUJD", "image/png"), readDataUrl("data:image/png;base64,QUJD"),
               readDataUrl(null, "image/png"), readDataUrl(undefined, "image/png"), readDataUrl(7, "image/png"),
               readDataUrl("", "image/png")]"""
    got = run_node(tmp_path, "dataurl.js", page_functions() + f"\nconsole.log(JSON.stringify({asks}));\n")
    assert got[:3] == [{"mime": "image/png", "b64": "QUJD"}, {"mime": "image/webp", "b64": "QUJD"},
                       {"mime": "image/jpeg", "b64": "QQ=="}]
    assert got[3:] == [None] * 11


@node
def test_under_the_screenshot_the_page_says_what_will_be_sent(tmp_path):
    asks = """(() => {
      const F = {part: "full", nativo: false}, T = {part: "tile", nativo: true}, S = {part: "tile", nativo: false};
      return [{images: [F], srcW: 1568, srcH: 900}, {images: [F, T, T], srcW: 1170, srcH: 2532},
              {images: [F, S, S], srcW: 3840, srcH: 2160}, {images: [F, T, S], srcW: 3840, srcH: 2160},
              {images: [F, T], srcW: 1920, srcH: 1080}, {images: [F, {part: "tile"}], srcW: 1920, srcH: 1080},
              {images: [F], srcW: 500, srcH: 300}, {images: [F], srcW: 600, srcH: 2000}, {images: [F], srcW: 2000, srcH: 599},
              {images: [F, T, T], srcW: 400, srcH: 5000}, {images: [F], srcW: "abc", srcH: null}, {images: [F]},
              {images: [null, F]}, null, undefined, {}, {images: []}, {images: "abc"}, "abc"].map(shotInfo);
    })()"""
    got = run_node(tmp_path, "shot_info.js", page_functions() + f"\nconsole.log(JSON.stringify({asks}));\n")
    small = "Imagem pequena ({}): os números do eixo podem não se ler bem. Usa o screenshot original, em ecrã inteiro."
    assert [g["envio"] for g in got] == [
        "Vista inteira", "Vista inteira + 2 recortes em resolução original",
        "Vista inteira + 2 recortes com mais detalhe",                # 4K: the pieces were fitted too, and it says so
        "Vista inteira + 2 recortes com mais detalhe", "Vista inteira + 1 recorte em resolução original",
        "Vista inteira + 1 recorte com mais detalhe", "Vista inteira", "Vista inteira", "Vista inteira",
        "Vista inteira + 2 recortes em resolução original", "Vista inteira", "Vista inteira", "Vista inteira",
        "", "", "", "", "", ""]
    assert [g["aviso"] for g in got] == ["", "", "", "", "", "", small.format("500x300"), "", small.format("2000x599"),
                                         small.format("400x5000"), "", "", "", "", "", "", "", "", ""]
    assert all(set(g) == {"envio", "aviso"} for g in got)


# ---------------------------------------------------------------- what the result draws, run by node

def result_code(cases):
    """The part of the script that turns a RESULT into HTML (no DOM in it), run for each [leverage, shown]."""
    page = source()
    script = page.split("<script>")[1]
    draw = script[script.index("/* ---------------------------------------------------------------- result */"):
                  script.index("function renderResult()")]
    assert "document" not in draw and "$(" not in draw
    consts = [re.search(rf"^const {name} = .*$", page, re.M).group(0) for name in ("esc", "LEVS")]
    return (page_functions() + "\n" + "\n".join(consts) + "\nconst S = {lev: 10};\n" + draw +
            f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(c => {{ S.lev = c[0]; return resultHtml(c[1]); }})));\n")


def shown(result, tfs=("auto",)):
    return {"id": "9", "at": "2026-11-10T12:00:00+00:00", "tfs": list(tfs), "result": result}


def reading(tf="5m", **over):
    return dict({"tf": tf, "tendencia": "alta", "estrutura": "topos e fundos ascendentes", "suportes": [98, 96.5],
                 "resistencias": [104.6], "padroes": ["bandeira de alta"], "volume_indicadores": "volume a subir"}, **over)


def answer(**over):
    base = {"legivel": True, "motivo_ilegivel": "", "ativo": "BTC/USDT perp", "preco_atual": 100.4, "resumo": "Alta em 5 min.",
            "leituras": [reading()], "confluencia": "", "cenarios": [{"direcao": "alta", "prob": 55, "gatilho": "acima de 100",
                                                                      "descricao": "continua a subir"}],
            "opcoes": [option()], "melhor": {"acao": "long", "opcao": 0, "porque": "melhor valor esperado"},
            "riscos": ["notícia às 14h"], "nao_se_ve": ["o livro de ordens"]}
    return dict(base, **over)


@node
def test_the_result_is_drawn_again_with_the_leverage_chosen(tmp_path):
    wide = option(nome="Stop largo", direcao="short", stop_pct=6.0, alvo_pct=9.0, rr=1.5, ev_r=0.05, custo_r=0.02, ev_liq_r=0.03)
    bad = option(nome="Sem stop", valida=False, problema="stop do lado errado", rr=None, ev_r=None, stop_pct=None, alvo_pct=None,
                 custo_r=None, ev_liq_r=None)
    full = answer(opcoes=[option(), wide, bad])
    out = answer(opcoes=[bad], melhor={"acao": "fora", "opcao": None, "porque": "nada compensa"})
    got = [plain(h) for h in run_node(tmp_path, "draw.js", result_code(
        [[5, shown(full)], [10, shown(full)], [20, shown(full)], [20, shown(out)], ["abc", shown(full)]]))]

    for lev, html in zip((5, 10, 20), got):
        assert html.count("<button") == 3 and f'data-lev="{lev}" aria-pressed="true">{lev}x</button>' in html
        assert html.count('aria-pressed="false"') == 2
        assert f"Com {lev}x a liquidação fica a cerca de {100 // lev}% do preço." in html
        assert ("As percentagens «da margem» não contam comissões nem funding, e a distância à liquidação é aproximada "
                "(margem isolada).") in html
        assert f"<span>Com {lev}x, da margem</span>" in html                        # the "Melhor opção" card
    # the price distances stay as they were; next to them, what they are of the margin
    assert all("<small>a 2,0 %</small>" in h and "<small>a 4,6 %</small>" in h for h in got[:3])
    assert '<small class="down">−10 % da margem</small>' in got[0] and '<small class="up">+23 % da margem</small>' in got[0]
    assert '<small class="down">−20 % da margem</small>' in got[1] and '<small class="up">+46 % da margem</small>' in got[1]
    assert '<small class="down">−40 % da margem</small>' in got[2] and '<small class="up">+92 % da margem</small>' in got[2]
    assert 'stop&nbsp;<b class="down">−20 %</b> · alvo&nbsp;<b class="up">+46 %</b>' in got[1]
    assert "stop&nbsp;2,0 % · alvo&nbsp;4,6 %</span>" in got[1]              # "alvo" never parts from its number
    # a stop at 6 % of the price: fine with 5x, close with 10x is not yet (7 %), past the liquidation with 20x
    assert 'class="lw' not in got[0] and 'class="lw' not in got[1]
    assert got[2].count('class="lw bad"') == 1 and 'class="lw warn"' not in got[2]
    assert ("Com 20x a liquidação chega antes do stop (cerca de 5% do preço): este stop não te protege. "
            "Perdias a margem toda antes de o preço lá chegar.</p>") in got[2]
    # the option that does not count gets no margin line and no warning; the valid ones get two lines each
    assert all(h.count(" da margem</small>") == 4 for h in got[:3])
    tail = got[2][got[2].index("Sem stop"):]
    assert "Não conta:" in tail and "da margem" not in tail.split("Cenários")[0]
    # nothing to show with leverage: no chooser at all
    assert "Alavancagem" not in got[3] and "<button" not in got[3] and "da margem" not in got[3]
    assert "Ficar de fora" in got[3] and "nada compensa" in got[3]
    assert 'data-lev="10" aria-pressed="true"' in got[4]                           # a bad value: 10x
    for html in got:
        assert "NaN" not in html and "undefined" not in html and "null" not in html and "Infinity" not in html


@node
def test_the_result_leads_with_the_expected_value_after_costs(tmp_path):
    """In the "Melhor opção" card and in each of the other options; an analysis kept from before the costs shows
    the only number it has and says it is before them. An option that does not count shows why, and no number."""
    second = option(nome="Recuo", ev_r=0.05, custo_r=0.1, ev_liq_r=-0.05, rr=1.3)
    near = option(nome="Stop colado", valida=False, problema="stop a menos de 0,1% do preço", rr=None, ev_r=None,
                  stop_pct=None, alvo_pct=None, custo_r=None, ev_liq_r=None)
    far = dict(near, nome="Entrada longe", problema="entrada a mais de 10% do preço atual")
    now = answer(opcoes=[option(ev_r=0.25, custo_r=0.1, ev_liq_r=0.15), second, near, far])
    old = answer(opcoes=[before_costs(), before_costs(nome="Recuo", ev_r=-0.3)])
    got = [plain(h) for h in run_node(tmp_path, "costs_draw.js", result_code([[10, shown(now)], [10, shown(old)]]))]

    hero, rest = got[0].split("Todas as opções")
    assert ('<div class="ev"><span>Valor esperado depois de custos</span><span class="mono up"><b>+0,15 R</b></span>'
            "<small>+0,25 R antes de custos · custos ≈ 0,10 R</small></div>") in hero
    assert hero.count("Valor esperado") == 1
    # the option the costs turn into a loss is red, with the gain it had before them said next to it
    assert ('· valor esperado depois de custos <b class="down">−0,05 R</b><br>+0,05 R antes de custos · custos ≈ 0,10 R</p>') in rest
    # said twice after the card: by the heading, as the order of the list, and by the one other option that counts
    assert rest.startswith("</h2><span>por valor esperado depois de custos</span></div>")
    assert rest.count("valor esperado depois de custos") == 2 and rest.count('class="opt bad"') == 2
    assert "<b>Não conta:</b> stop a menos de 0,1% do preço</p>" in rest
    assert "<b>Não conta:</b> entrada a mais de 10% do preço atual</p>" in rest
    for tail in (rest[rest.index("Stop colado"):rest.index("Entrada longe")], rest[rest.index("Entrada longe"):].split("Cenários")[0]):
        assert "valor esperado" not in tail and "custos" not in tail and " R<" not in tail

    hero, rest = got[1].split("Todas as opções")
    assert ('<div class="ev"><span>Valor esperado antes de custos</span><span class="mono up"><b>+0,42 R</b></span></div>') in hero
    assert '· valor esperado antes de custos <b class="down">−0,30 R</b></p>' in rest
    assert rest.startswith("</h2><span>por valor esperado</span></div>")      # the order it was given in, back then
    assert "depois de custos" not in got[1] and "custos ≈" not in got[1] and "<small>+" not in got[1]
    for html in got:
        assert "NaN" not in html and "undefined" not in html and "null" not in html and "Infinity" not in html
    # the row of the card may break in two on a narrow phone, with the note on a line of its own
    page = source()
    assert ".kv div.ev{flex-wrap:wrap" in page and ".kv div.ev small{flex-basis:100%" in page


@node
def test_a_stop_close_to_the_liquidation_gets_the_amber_line(tmp_path):
    near = answer(opcoes=[option(stop_pct=8.0, alvo_pct=16.0)])
    got = [plain(h) for h in run_node(tmp_path, "near.js", result_code([[5, shown(near)], [10, shown(near)], [20, shown(near)]]))]
    assert 'class="lw' not in got[0]
    assert got[1].count('class="lw warn"') == 1 and 'class="lw bad"' not in got[1]
    assert ("Com 10x o stop fica perto da liquidação (cerca de 10% do preço): um movimento brusco pode liquidar-te "
            "antes de o stop atuar.</p>") in got[1]
    assert got[2].count('class="lw bad"') == 1


# ---------------------------------------------------------------- the live market the bot's code looked up

def market(**over):
    """result.mercado as the bot sends it: BTC on Hyperliquid, read on a 5 min chart, the image's price 0.12 % away."""
    trends = ["alta", "baixa", "lateral", "alta", "baixa", "alta"]
    base = {"fonte": "Hyperliquid", "moeda": "BTC", "tf": "5m", "preco": 67432.5, "em": "2026-11-10T12:00:05",
            "funding_h_pct": 0.00125, "oi_usd": 2.5e9, "vol_24h_usd": 345.6e6, "var_24h_pct": -1.234, "atr_pct": 0.35,
            "tfs": [{"tf": tf, "tendencia": trend, "var_pct": 0.4, "max": 68000.0, "min": 67000.0, "atr_pct": 0.3}
                    for tf, trend in zip(["1m", "5m", "15m", "1h", "4h", "1d"], trends)],
            "desvio_pct": 0.12, "fresco": True, "aviso": ""}
    return dict(base, **over)


FAR = "O preço do screenshot está a 4,2% do preço ao vivo: pode ser antigo ou de outro mercado."
NO_MARKET = "Sem dados de mercado ao vivo: análise feita só pela imagem."


@node
def test_the_live_market_is_put_into_the_words_the_page_shows(tmp_path):
    evil = "<b>x</b>"
    cases = [
        market(),                                                                                  # 0
        market(desvio_pct=4.2, fresco=False, aviso=FAR, funding_h_pct=-0.002, var_24h_pct=2),      # 1 not the same price
        market(fresco=False, aviso="", desvio_pct=None),                                           # 2 far, with no words
        market(fresco=None, desvio_pct=None, funding_h_pct=None, oi_usd=None, vol_24h_usd=None, var_24h_pct=None,
               atr_pct=None, tf=None, tfs=[]),                                                     # 3 only the price
        market(funding_h_pct=0, var_24h_pct=0, oi_usd=12345, vol_24h_usd=950, preco="0.00001234"),   # 4
        market(fonte={}, moeda=[], em=None, aviso=7, tf="2h", tfs="abc", oi_usd="x", atr_pct={}, desvio_pct="abc",
               funding_h_pct=True, var_24h_pct=[], vol_24h_usd=-1),                                # 5 not what was agreed
        market(tfs=[{"tf": "2h", "tendencia": "alta"}, {"tf": "1m", "tendencia": "forte"}, None, "x", 7,
                    {"tf": "1m", "tendencia": "constructor"}, {"tf": ["1m"], "tendencia": "alta"},
                    {"tf": evil, "tendencia": evil}, {"tf": "1h", "tendencia": "lateral", "var_pct": evil}]),   # 6
        market(tfs=[{"tf": "1m", "tendencia": "alta"}] * 9),                                       # 7 never more than six
        None, "abc", 7, [], {}, {"preco": "abc"}, {"preco": 0}, {"preco": -1}, {"preco": None}, {"preco": True},
        [market()],                                                                                # 8-18 no market
    ]
    code = page_functions() + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(marketModel).concat([marketModel()])));\n"
    got = plain(run_node(tmp_path, "market.js", code))
    assert got[0] == {
        "titulo": "Hyperliquid · BTC", "em": "2026-11-10T12:00:05", "preco": "67 432,5", "longe": False,
        "desvio": "0,12 % · bate certo", "var24": "−1,2 %", "var24Sign": -1, "funding": "+0,0013 %/h · longs pagam",
        "oi": "2,50 mil M $", "vol24": "345,6 M $", "atr": "0,35 %", "atrTf": "5 min", "aviso": "",
        "tfs": [{"tf": "1 min", "palavra": "alta", "chip": "done"}, {"tf": "5 min", "palavra": "baixa", "chip": "armed"},
                {"tf": "15 min", "palavra": "lateral", "chip": ""}, {"tf": "1 h", "palavra": "alta", "chip": "done"},
                {"tf": "4 h", "palavra": "baixa", "chip": "armed"}, {"tf": "1 dia", "palavra": "alta", "chip": "done"}]}
    # the screenshot is not of this price: said by the bot, and the page keeps its sentence
    assert (got[1]["longe"], got[1]["desvio"], got[1]["aviso"]) == (True, "4,20 % · não bate certo", FAR)
    assert (got[1]["funding"], got[1]["var24"], got[1]["var24Sign"]) == ("−0,0020 %/h · shorts pagam", "+2,0 %", 1)
    # said to be far with no sentence: the page has one of its own, so it is never silent about it
    assert got[2]["longe"] is True and got[2]["desvio"] == "" and got[2]["aviso"] == (
        "O preço do screenshot está longe do preço ao vivo: pode ser um screenshot antigo ou de outro mercado, "
        "e os níveis podem já não valer.")
    blank = {"desvio": "", "var24": "", "var24Sign": 0, "funding": "", "oi": "", "vol24": "", "atr": "", "atrTf": "",
             "tfs": [], "aviso": "", "longe": False}
    assert {k: got[3][k] for k in blank} == blank and got[3]["preco"] == "67 432,5"
    assert (got[4]["funding"], got[4]["var24"], got[4]["var24Sign"]) == ("0,0000 %/h", "0,00 %", 0)
    assert (got[4]["oi"], got[4]["vol24"], got[4]["preco"]) == ("12,3 mil $", "950 $", "0,00001234")
    # anything that is not what was agreed is left out, never shown as it came
    assert {k: got[5][k] for k in blank} == blank and (got[5]["titulo"], got[5]["em"]) == ("", "")
    assert got[6]["tfs"] == [{"tf": "1 h", "palavra": "lateral", "chip": ""}]
    assert len(got[7]["tfs"]) == 6
    assert got[8:] == [None] * 12                                      # and marketModel() with nothing at all
    said = json.dumps(got, ensure_ascii=False)
    assert "NaN" not in said and "undefined" not in said and "Infinity" not in said and "<b>" not in said
    # every model has the same fields, so the drawing never meets one that is missing
    assert all(set(g) == set(got[0]) for g in got[:8])


@node
def test_the_money_and_the_changes_of_the_market_are_written_short(tmp_path):
    asks = """[[2.5e9, 345.6e6, 12345, 950, 0, 999.4, 999.5, 999949, 999950, 999.94e6, 999.95e6, 1e12].map(fmtUsd),
               [-1, null, undefined, "abc", true, NaN, Infinity, ""].map(fmtUsd),
               [fmtSigned(1.234), fmtSigned(-0.35), fmtSigned(0), fmtSigned(12.345, 2), fmtSigned(-12.345, 0),
                fmtSigned(null), fmtSigned("abc"), fmtSigned(true), fmtSigned(undefined)],
               ["1h", "4h", "1d"].map(tfLabel),
               [{stop_atr: 0.84, alertas: ["a", " ", 7, null, "b", "c", "d", "e"], contra: ["1h"]}, {stop_atr: null},
                {stop_atr: "abc", alertas: "abc", contra: "abc"}, {stop_atr: -1.25, alertas: []}, {stop_atr: 0}, {},
                null].map(optionModel).map(m => [m.stopAtr, m.alertas]),
               [0.46, 0.45, 0.49, 0.5, 0.96, 1, 0.04].map(a => optionModel({stop_atr: a}).stopAtr)]"""
    got = plain(run_node(tmp_path, "short.js", page_functions() + f"\nconsole.log(JSON.stringify({asks}));\n"))
    assert got[0] == ["2,50 mil M $", "345,6 M $", "12,3 mil $", "950 $", "0 $", "999 $", "1,0 mil $", "999,9 mil $",
                      "1,0 M $", "999,9 M $", "1,00 mil M $", "1 000,00 mil M $"]
    assert got[1] == [""] * 8
    assert got[2] == ["+1,2 %", "−0,35 %", "0,00 %", "+12,35 %", "−12 %", "", "", "", ""]
    assert got[3] == ["1 h", "4 h", "1 dia"]
    # the stop in ATRs and the alerts of the bot's code: at most four, only sentences; nothing without market data
    assert got[4] == [["0,84 ATR", ["a", "b", "c", "d"]], ["", []], ["", []], ["1,25 ATR", []], ["0,00 ATR", []],
                      ["", []], ["", []]]
    # two decimals, as the bot's code decides and words it: a stop refused at 0,46 ATR ("menos de 0,5") must not
    # read "0,5 ATR" on its own card, nor one called tight at 0,96 read "1,0 ATR"
    assert got[5] == ["0,46 ATR", "0,45 ATR", "0,49 ATR", "0,50 ATR", "0,96 ATR", "1,00 ATR", "0,04 ATR"]


@node
def test_the_result_shows_the_live_market_and_what_it_says_about_each_option(tmp_path):
    against = ["Contra a tendência de 1 h (baixa).", "Stop a menos de 1 ATR: o ruído normal pode tocá-lo."]
    best = option(stop_atr=0.84, contra=["1h"], alertas=against)
    other = option(nome="Recuo", direcao="short", stop_atr=2.31, contra=[], alertas=["Funding contra o short."])
    plain_op = option(nome="Simples", stop_atr=None, contra=[], alertas=[])
    live = answer(mercado=market(), opcoes=[best, other, plain_op])
    far = answer(mercado=market(desvio_pct=4.2, fresco=False, aviso=FAR), opcoes=[best, other])
    silent = answer(mercado=market(fresco=False, aviso="", desvio_pct=None))
    unread = answer(legivel=False, motivo_ilegivel="imagem desfocada", opcoes=[], leituras=[], cenarios=[], mercado=None,
                    melhor={"acao": "fora", "opcao": None, "porque": "não se lê"})
    gone = answer()
    del gone["opcoes"]
    cases = [[10, shown(live)], [10, shown(far)], [10, shown(silent)],                             # 0-2
             [10, shown(answer())], [10, shown(answer(mercado=None))], [10, shown(answer(mercado="abc"))],   # 3-5 none
             [10, shown(answer(mercado={"preco": 0}))], [10, shown(answer(mercado=[market()]))],   # 6-7 none either
             [10, shown(unread)], [10, shown(dict(unread, mercado=market()))],                     # 8-9 nothing was read
             [10, shown(answer(mercado=market(), opcoes=[before_costs()]))], [10, shown(dict(gone, mercado=market()))]]
    got = [plain(h) for h in run_node(tmp_path, "market_draw.js", result_code(cases))]

    html = got[0]
    assert html.count('<div class="panel"><h3>Mercado ao vivo</h3>') == 1 and html.count("Mercado ao vivo") == 1
    assert re.search(r'<h3>Mercado ao vivo</h3><p class="rcap">Hyperliquid · BTC · [^<]*\d\d:\d\d</p><div class="kv">', html)
    card = html[html.index("<h3>Mercado ao vivo</h3>"):html.index("Todas as opções")]
    assert re.findall(r'<div><span>([^<]+)</span><span class="mono ([a-z]*)">([^<]+)</span></div>', card) == [
        ("Preço ao vivo", "", "67 432,5"), ("Diferença para a imagem", "", "0,12 % · bate certo"),
        ("Variação em 24 h", "down", "−1,2 %"), ("Funding", "", "+0,0013 %/h · longs pagam"),
        ("Open interest", "", "2,50 mil M $"), ("Volume em 24 h", "", "345,6 M $"), ("ATR (5 min)", "", "0,35 %")]
    assert ('<div class="chips"><span class="chip done">1 min · alta</span><span class="chip armed">5 min · baixa</span>'
            '<span class="chip ">15 min · lateral</span><span class="chip done">1 h · alta</span>'
            '<span class="chip armed">4 h · baixa</span><span class="chip done">1 dia · alta</span></div>') in card
    assert ('<p class="help">Dados da API pública da Hyperliquid, tirados quando a análise foi feita. Podem diferir da '
            "corretora do teu screenshot e já ter mudado entretanto.</p></div>") in card
    # the card of the best option first, then the market it was checked against, then the other options
    assert html.index("Melhor opção") < html.index("Mercado ao vivo") < html.index("Todas as opções")
    assert NO_MARKET not in html and "Atenção:" not in html
    # the stop in ATRs sits by the stop, between its distance in the price and in the margin
    hero, rest = html.split("Mercado ao vivo")[0], html.split("Todas as opções")[1]
    assert '<small>a 2,0 %</small><small>0,84 ATR</small><small class="down">−20 % da margem</small>' in hero
    assert '<small>a 2,0 %</small><small>2,31 ATR</small><small class="down">−20 % da margem</small>' in rest
    assert html.count(" ATR</small>") == 2                             # the option without the number shows none
    # the alerts of the bot's code, each a line of the amber list, under the option they are about
    assert ('<ul class="lw warn al"><li>Contra a tendência de 1 h (baixa).</li>'
            "<li>Stop a menos de 1 ATR: o ruído normal pode tocá-lo.</li></ul>") in hero
    assert '<ul class="lw warn al"><li>Funding contra o short.</li></ul>' in rest
    assert html.count('<ul class="lw warn al">') == 2 and html.count("<li>Contra a tendência") == 1
    simple = rest[rest.index("Simples"):].split("Cenários")[0]
    assert "ATR" not in simple and 'class="lw' not in simple

    # a screenshot whose price is not the live one: said loudly before anything else is proposed
    for html, words in ((got[1], FAR), (got[2], "O preço do screenshot está longe do preço ao vivo: pode ser um "
                                                "screenshot antigo ou de outro mercado, e os níveis podem já não valer.")):
        loud = f'<p class="lw warn"><b>Atenção:</b> {words}</p>'
        assert html.count(loud) == 1 and html.count("Atenção:") == 1
        assert html.index(loud) < html.index("Melhor opção") < html.index("Mercado ao vivo")
    assert '<span>Diferença para a imagem</span><span class="mono down">4,20 % · não bate certo</span>' in got[1]
    assert "Diferença para a imagem" not in got[2]                     # no number for it: no row, never a dash

    # no market data (an analysis from before it, the coin not found, the API down): one quiet line says so
    for html in got[3:8]:
        assert html.count(f'<p class="rcap">{NO_MARKET}</p>') == 1 and html.count("Sem dados de mercado") == 1
        assert "Mercado ao vivo" not in html and "Atenção:" not in html and " ATR" not in html
        assert 'class="lw' not in html and "<ul class=\"lw" not in html
        assert html.index(NO_MARKET) < html.index("Melhor opção")
    # a chart that could not be read: that is what is said, not that the market is missing
    assert "Não consegui ler este gráfico" in got[8] and NO_MARKET not in got[8] and "Mercado ao vivo" not in got[8]
    assert "Não consegui ler este gráfico" in got[9] and NO_MARKET not in got[9] and got[9].count("Mercado ao vivo") == 1
    # the market with an analysis that has one option only, or none: still drawn, and no list of others
    assert got[10].count("Mercado ao vivo") == 1 and "Todas as opções" not in got[10]
    assert got[11].count("Mercado ao vivo") == 1 and "Ficar de fora" in got[11]
    for html in got:
        assert "NaN" not in html and "undefined" not in html and "null" not in html and "Infinity" not in html
        assert set(re.findall(r"<([a-zA-Z0-9]+)", html)) <= {"div", "p", "span", "b", "small", "h2", "h3", "button", "ul", "li", "i", "br"}
    # the new rules of the layout: a long sentence breaks inside its box and the chips wrap, so a phone never scrolls sideways
    page = source()
    assert re.search(r"\.lw\{[^}]*overflow-wrap:anywhere", page) and re.search(r"\.chips\{[^}]*flex-wrap:wrap", page)
    assert re.search(r"\.sh\.two\{[^}]*flex-wrap:wrap", page)


@node
def test_what_the_model_or_the_user_wrote_never_becomes_markup(tmp_path):
    """Every string of a result, the asset and the timeframes included, is text on the page: no tag, no attribute.
    The same for what came from the market API and for the alerts the bot's code wrote about each option."""
    evil = '<img src=x onerror=alert(1)>" onmouseover="alert(2)\'&'
    op = option(nome=evil, tipo=evil, tf=evil, gatilho=evil, porque=evil, problema=evil, stop_atr=evil, contra=[evil],
                alertas=[evil, evil])
    bad_market = {"fonte": evil, "moeda": evil, "tf": evil, "preco": 100, "em": evil, "funding_h_pct": evil, "oi_usd": evil,
                  "vol_24h_usd": evil, "var_24h_pct": evil, "atr_pct": evil, "desvio_pct": evil, "fresco": False, "aviso": evil,
                  "tfs": [{"tf": evil, "tendencia": evil, "var_pct": evil, "max": evil, "min": evil, "atr_pct": evil},
                          {"tf": "1h", "tendencia": evil}, {"tf": evil, "tendencia": "alta"}, evil, None,
                          {"tf": "5m", "tendencia": "alta", "var_pct": evil}]}
    hostile = answer(
        mercado=bad_market,
        ativo=evil, resumo=evil, motivo_ilegivel=evil, confluencia=evil, riscos=[evil], nao_se_ve=[evil],
        leituras=[reading(tf=evil, tendencia=evil, estrutura=evil, padroes=[evil], volume_indicadores=evil,
                          suportes=[evil], resistencias=[evil])],
        cenarios=[{"direcao": evil, "prob": evil, "gatilho": evil, "descricao": evil}],
        opcoes=[op, dict(op, direcao=evil), dict(op, valida=False, entrada=evil, stop=evil, alvo=evil, rr=evil, ev_r=evil,
                                               prob_alvo=evil, prob_usada=evil, stop_pct=evil, alvo_pct=evil,
                                               equilibrio_pct=evil)],
        melhor={"acao": "long", "opcao": 0, "porque": evil})
    unread = answer(legivel=False, motivo_ilegivel=evil, ativo=evil, opcoes=[], leituras=[], cenarios=[],
                    melhor={"acao": "fora", "opcao": None, "porque": evil})
    no_price = answer(mercado=dict(bad_market, preco=evil), resumo=evil)           # a market with no price is no market
    cases = [[20, dict(shown(hostile, tfs=[evil, "5m"]), at=evil)], [evil, shown(hostile)], [10, shown(unread)],
             [10, shown(dict(unread, mercado=bad_market))], [10, shown(no_price)]]
    got = run_node(tmp_path, "evil.js", result_code(cases))
    for html in got:
        assert "<img" not in html and "onerror=alert(1)>" not in html
        assert 'onmouseover="' not in html and "&lt;img src=x onerror=alert(1)&gt;&quot; onmouseover=&quot;" in html
        assert set(re.findall(r"<([a-zA-Z0-9]+)", html)) <= {"div", "p", "span", "b", "small", "h2", "h3", "button", "ul", "li", "i", "br"}
        assert set(re.findall(r'\s([a-z-]+)="', html)) <= {"class", "style", "role", "aria-label", "aria-labelledby",
                                                             "aria-hidden", "aria-pressed", "id", "type", "data-lev"}
        assert "NaN" not in html and "undefined" not in html and "Infinity" not in html
    # the hostile market is drawn as far as it can be trusted: its price, its warning as text, and one known timeframe
    for html in (got[0], got[1], got[3]):
        assert html.count("<h3>Mercado ao vivo</h3>") == 1 and html.count('<p class="lw warn"><b>Atenção:</b> &lt;img') == 1
        card = plain(html).split("<h3>Mercado ao vivo</h3>")[1].split("Dados da API pública")[0]
        assert card.count('<span class="chip done">5 min · alta</span>') == 1 and card.count('<span class="chip') == 1
        assert re.findall(r"<span>([^<]+)</span><span class=\"mono [a-z]*\">", card) == ["Preço ao vivo"]
    for html in got[:2]:
        assert html.count('<ul class="lw warn al"><li>&lt;img') >= 1 and " ATR</small>" not in html
    assert "Mercado ao vivo" not in got[2] and "Mercado ao vivo" not in got[4] and NO_MARKET in got[4]
    # and in the page itself no ${...} goes into the markup of a result or of the list without esc() or a number
    script = source().split("<script>")[1]
    for name in ("alertList", "marketCard"):                       # the new blocks: every hole of theirs goes through esc()
        body = re.search(rf"^function {name}\(.*?^\}}", script, re.M | re.S).group(0)
        assert body.count("${") == body.count("${esc(") > 0, name
    assert "${esc(mk.aviso)}" in script and "${esc(order)}" in script and "${esc(m.stopAtr)}" in script
    assert "innerHTML = x ? resultHtml(x) : \"\"" in script
    assert script.count("innerHTML") == 4 and "insertAdjacentHTML" not in script and "outerHTML" not in script and "document.write" not in script


@node
def test_an_old_analysis_of_one_two_or_three_screenshots_still_opens(tmp_path):
    three = answer(leituras=[reading("1m"), reading("5m", tendencia="baixa"), reading("15m", tendencia="lateral")],
                   confluencia="Os três tempos apontam para cima.")
    cases = [[10, shown(three, tfs=["1m", "5m", "15m"])], [10, shown(answer(leituras=[reading("5m"), reading("15m")]), tfs=["5m", "15m"])],
             [10, shown(answer(), tfs=["5m"])], [10, shown(answer(leituras=[reading("outro")]), tfs=["auto"])],
             [10, dict(shown(answer()), tfs=None)], [10, dict(shown(answer()), tfs="5m")], [10, {"id": "1", "result": None}]]
    got = [plain(h) for h in run_node(tmp_path, "old.js", result_code(cases))]
    assert got[0].count('class="rd"') == 4 and "Confluência" in got[0] and "Os três tempos apontam para cima." in got[0]
    assert "· 1 min · 5 min · 15 min</p>" in got[0]
    assert got[1].count('class="rd"') == 2 and "· 5 min · 15 min</p>" in got[1]
    assert got[2].count('class="rd"') == 1 and "· 5 min</p>" in got[2]
    assert "· automático</p>" in got[3] and "<b>outro</b>" in got[3]
    for html in got[:6]:
        assert "Leitura do gráfico" in html and "Melhor opção" in html and "undefined" not in html and "NaN" not in html
    assert "Resultado" in got[6] and "Ficar de fora" in got[6]            # nothing in it: still a page, not an error


@node
def test_the_wait_ends_with_an_answer_an_error_or_a_computer_that_never_came(tmp_path):
    mine = {"id": "9", "status": "analysing"}
    result = {"legivel": True}
    cases = [
        [None, None, 0, []],                                                          # 0 nothing asked, nothing running
        [{"id": "7", "status": "analysing"}, None, 0, []],                            # 1 somebody else's is running
        [{"id": "7", "status": "done", "result": result}, None, 0, []],               # 2 an old one, over
        [None, "9", 5, []],                                                           # 3 the computer has not seen mine
        [{"id": "8", "status": "done", "result": result}, "9", 39, []],               # 4 still the one before mine
        [None, "9", 41, []],                                                          # 5 40 s and nothing
        [{"id": 9, "status": "analysing"}, "9", 300, []],                             # 6 mine, a number on the wire
        [dict(mine, status="error", error="A IA não respondeu"), "9", 12, []],        # 7
        [dict(mine, status="offline", error="Sem IA"), "9", 12, []],                  # 8
        [dict(mine, status="done", result=result, ended="2026-11-10T12:00:00+00:00", tfs=["5m"]), "9", 30, []],   # 9
        [{"id": "10", "status": "analysing"}, "9", 90, [{"id": 9, "at": "t", "tfs": ["1m"], "result": result}]],  # 10
        [dict(mine, status="done", result=None), "9", 30, []],                        # 11 done with nothing in it
    ]
    code = page_functions() + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(c => waitState(c[0], c[1], c[2], c[3]))));\n"
    got = run_node(tmp_path, "wait.js", code)
    assert [g["kind"] for g in got] == ["idle", "busy", "idle", "pending", "pending", "timeout", "analysing",
                                        "error", "error", "done", "done", "error"]
    assert got[7]["error"] == "A IA não respondeu" and got[8]["error"] == "Sem IA"
    assert got[9] == {"kind": "done", "result": result, "at": "2026-11-10T12:00:00+00:00", "tfs": ["5m"]}
    assert got[10] == {"kind": "done", "result": result, "at": "t", "tfs": ["1m"]}
    # the fifth argument left out, null, or about another request: exactly the same answers
    for refused in ("null", "undefined", '{"id": "77", "error": "x"}', "{}"):
        code = page_functions() + (f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(c => "
                                   f"waitState(c[0], c[1], c[2], c[3], {refused}))));\n")
        assert run_node(tmp_path, "wait_same.js", code) == got


@node
def test_a_request_the_computer_refused_ends_the_wait_with_its_reason(tmp_path):
    """The phone's request went into the relay while another analysis was running: the computer says so in
    "refused", and the page stops waiting at once instead of 40 s later with "O computador não respondeu"."""
    theirs = {"id": "7", "status": "analysing"}
    result = {"legivel": True}
    no = {"id": "9", "error": "Já há uma análise a decorrer. Espera que termine."}
    cases = [
        [theirs, "9", 3, [], no],                                                     # 0 refused: said at once
        [theirs, 9, 3, [], {"id": "9", "error": no["error"]}],                        # 1 a number on one side
        [theirs, "9", 3, [], {"id": 9}],                                              # 2 no words: the page's own
        [None, "9", 90, [], no],                                                      # 3 long after, still the reason
        [theirs, "9", 3, [], {"id": "8", "error": "x"}],                              # 4 somebody else's was refused
        [theirs, "9", 41, [], {"id": "8", "error": "x"}],                             # 5
        [theirs, "9", 3, [], None],                                                   # 6 nothing refused
        [theirs, None, 0, [], no],                                                    # 7 this page asked for nothing
        [None, None, 0, [], no],                                                      # 8
        [theirs, "9", 3, [{"id": "9", "at": "t", "tfs": ["5m"], "result": result}], no],          # 9 it has an answer
        [{"id": "9", "status": "analysing"}, "9", 3, [], no],                         # 10 it is being made after all
        [{"id": "9", "status": "done", "result": result, "ended": "t", "tfs": []}, "9", 3, [], no],   # 11
    ]
    code = page_functions() + (f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(c => "
                               "waitState(c[0], c[1], c[2], c[3], c[4]))));\n")
    got = run_node(tmp_path, "refused.js", code)
    assert got[0] == got[1] == got[3] == {"kind": "error", "error": "Já há uma análise a decorrer. Espera que termine."}
    assert got[2] == {"kind": "error", "error": "Já há uma análise a decorrer"}
    assert [g["kind"] for g in got[4:]] == ["pending", "timeout", "pending", "busy", "idle", "done", "analysing", "done"]
    script = source().split("<script>")[1]
    assert "waitState(job, w.id, (Date.now() - w.since) / 1000, st.history, st.refused)" in script


@node
def test_the_wait_is_only_given_up_on_an_answer_asked_for_after_the_40_seconds(tmp_path):
    """The clock alone says nothing: the state in memory may be from before the page was left. A wait that began
    at `since` ends as "the computer did not answer" only when the GET behind the state was asked for 40 s later."""
    since = 1_800_000_000_000
    cases = [
        # [the job in the state, seconds waited, when that state was asked for (ms after `since`; None: no state)]
        [None, 300, 10_000],                              # 0 back after 5 min: the tick comes before the fresh GET
        [{"id": "8", "status": "done"}, 300, 39_999],     # 1 the same, with the job before mine still in the state
        [None, 300, None],                                # 2 the page opened again: the wait resumed, no state yet
        [None, 41, 38_000],                               # 3 40 s on the clock, the last answer is from before them
        [None, 41, 40_000],                               # 4 the computer is off: an answer asked for at 40 s says so
        [None, 300, 298_000],                             # 5 back after 5 min and the fresh GET has landed: off
        [None, 39, 38_000],                               # 6 not 40 s yet
        [{"id": "9", "status": "analysing"}, 300, 10_000],                                # 7 an old state, mine in it
        [{"id": "9", "status": "error", "error": "A IA não respondeu"}, 300, 10_000],     # 8 a real error stays one
        [{"id": "9", "status": "done", "result": {"legivel": True}}, 300, None],          # 9
    ]
    code = page_functions() + f"""
const since = {since};
console.log(JSON.stringify({json.dumps(cases)}.map(c => {{
  const ws = waitState(c[0], "9", c[1], []);
  return [ws.kind, waitKind(ws.kind, c[2] == null ? 0 : since + c[2], since)];
}}).concat([["idle", "busy", "pending", "analysing", "done", "error"].map(k => waitKind(k, 0, since)),
           [waitKind("timeout", undefined, since), waitKind("timeout", NaN, since), waitKind("timeout", since, NaN)]])));
"""
    got = run_node(tmp_path, "wait_kind.js", code)
    assert got[:10] == [["timeout", "pending"], ["timeout", "pending"], ["timeout", "pending"], ["timeout", "pending"],
                        ["timeout", "timeout"], ["timeout", "timeout"], ["pending", "pending"],
                        ["analysing", "analysing"], ["error", "error"], ["done", "done"]]
    assert got[10] == ["idle", "busy", "pending", "analysing", "done", "error"]      # only "timeout" is ever changed
    assert got[11] == ["pending", "pending", "pending"]                              # in doubt, the wait goes on
    # the page keeps when each state was asked for, and step() is the one place that ends the wait
    script = source().split("<script>")[1]
    load = script[script.index("async function load()"):script.index("function step()")]
    assert load.index("const asked = Date.now();") < load.index("S.state = await api(API); S.stateAt = asked; S.failAt = 0;")
    assert "stateAt: 0," in script and script.count("S.stateAt") == 2
    step = script[script.index("function step()"):script.index("let pollTimer")]
    assert "const kind = waitKind(ws.kind, S.stateAt, w.since, S.failAt, Date.now());" in step and "ws.kind ===" not in step
    assert 'kind === "timeout") { endWait();' in step and script.count("endWait();") == 4


@node
def test_a_wait_with_no_answer_at_all_ends_a_minute_into_the_failures(tmp_path):
    """With no network (or the relay refusing every GET) no fresh state ever lands, so the 40 s rule alone would
    leave the button locked for ever: after a minute of nothing but failed GETs the wait is given up as "lost"."""
    since = 1_000_000
    cases = [                                    # [kind, stateAt, failAt, now] -> kind (offsets from `since`, None: 0)
        ["pending", None, 5_000, 64_000],        # 0 failing for 59 s: still waiting
        ["pending", None, 5_000, 65_000],        # 1 a minute of failures: lost
        ["timeout", None, 5_000, 70_000],        # 2 the same, when the old state already says timeout
        ["analysing", 4_000, 6_000, 90_000],     # 3 it had started, then the line went: lost too (the history keeps it)
        ["pending", None, None, 500_000],        # 4 no failure at all: the 40 s rule decides, not this one
        ["timeout", 45_000, None, 500_000],      # 5 a fresh state says the PC is off: timeout, as before
        ["done", None, 5_000, 500_000],          # 6 an answer in hand is never thrown away
        ["error", None, 5_000, 500_000],         # 7
        ["pending", None, -30_000, 35_000],      # 8 failing for 65 s, but the 40 s are not over yet
    ]
    code = page_functions() + f"""
const since = {since};
console.log(JSON.stringify({json.dumps(cases)}.map(c =>
  waitKind(c[0], c[1] == null ? 0 : since + c[1], since, c[2] == null ? 0 : since + c[2], since + c[3]))));
"""
    assert run_node(tmp_path, "wait_lost.js", code) == ["pending", "lost", "lost", "lost", "pending", "timeout",
                                                       "done", "error", "pending"]
    script = source().split("<script>")[1]
    load = script[script.index("async function load()"):script.index("function step()")]
    assert "if (!S.failAt) S.failAt = asked;" in load and "failAt: 0," in script
    assert 'kind === "lost") { endWait();' in script
    assert "if (!document.hidden) { S.failAt = 0; load()" in script     # failures from before the page was hidden do not count


@node
def test_the_script_of_the_page_is_valid_javascript(tmp_path):
    script = tmp_path / "grafico_page.js"
    script.write_text(re.search(r"<script>(.*?)</script>", source(), re.S).group(1), encoding="utf-8")
    run = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60,
                         cwd=str(tmp_path))
    assert run.returncode == 0, run.stderr
