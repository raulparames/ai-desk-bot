"""Wide scan: hundreds of Jupiter tokens are read and cut, the best two go on, the counter is honest."""

from datetime import datetime, timedelta, timezone


def test_wide_scan_reads_all_tokens_cuts_them_and_sends_the_best_two(monkeypatch, tmp_path):
    from deskbot import memetest, memedesk
    monkeypatch.setattr(memetest, "OUT", tmp_path)
    monkeypatch.setattr(memetest.MemeTest, "_eur_usd", lambda self: 1.0)
    desk = memedesk.MemesDesk(30, 110)
    desk.gmgn.key = ""
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()

    def tok(i, **kw):
        t = {"id": f"MINT{i}", "symbol": f"T{i}", "liquidity": 90_000, "mcap": 900_000, "organicScore": i,
             "stats1h": {"priceChange": 5}, "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True},
             "firstPool": {"createdAt": old}}
        t.update(kw)
        return t
    lists = {"toptrending/5m": [tok(i) for i in range(100)],
             "toptraded/5m": [tok(i, liquidity=5_000) for i in range(100, 200)],          # too thin
             "toporganicscore/5m": [tok(i, stats1h={"priceChange": 300}) for i in range(200, 250)],   # already ran
             "toptrending/1h": [tok(i, audit={"mintAuthorityDisabled": False}) for i in range(250, 260)],
             "recent": [tok(i, firstPool={"createdAt": new}) for i in range(260, 270)]}

    class R:
        ok = True

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data
    monkeypatch.setattr(desk.http, "get", lambda url, params=None, timeout=None: R(lists[url.split("/v2/")[1]]))
    looked_up = []

    def gecko(path, params=None):
        looked_up.append(path)
        mint = path.split("/tokens/")[1].split("/")[0]
        return {"data": [{"attributes": {"address": "POOL" + mint, "reserve_in_usd": "90000"},
                          "relationships": {"base_token": {"data": {"id": "solana_" + mint}}}}]}
    monkeypatch.setattr(desk, "_gecko", gecko)
    out = desk._wide(set())
    assert len(desk._mints_seen) == 270                      # every token read is counted
    assert set(out) == {"POOLMINT99", "POOLMINT98"}          # the two best organic scores that passed
    assert len(looked_up) == 2                               # only those cost a GeckoTerminal call
    cuts = desk.d.counts["trends"]
    assert cuts["liquidez"] == 100 and cuts["já correu"] == 50 and cuts["mint ativa"] == 10 and cuts["muito nova"] == 10
    assert len([e for e in desk.d.events if e["verdict"] == "kill"]) == 2   # only two shown on the card
    desk._wide(set())                                         # next minute: same tokens are not counted again
    assert desk.d.counts["trends"]["liquidez"] == 100 and len(desk._mints_seen) == 270
