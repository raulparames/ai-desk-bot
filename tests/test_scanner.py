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


def test_more_entries_loosens_whales_votes_and_top10_but_never_the_rug_guard(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from deskbot import memetest, memedesk
    monkeypatch.setattr(memetest, "OUT", tmp_path)
    monkeypatch.setattr(memetest.MemeTest, "_eur_usd", lambda self: 1.0)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    whales = [{"attributes": {"block_timestamp": now, "kind": "buy", "tx_from_address": f"w{i}", "volume_in_usd": "350"}}
              for i in range(2)]                                        # 2 buyers of $350
    info = {"data": {"attributes": {"holders": {"distribution_percentage": {"top_10": "33"}}}}}   # top 10 at 33%
    pool = {"attributes": {"reserve_in_usd": "250000", "transactions": {"h1": {"buyers": 300, "buys": 900, "sells": 400}},
                           "volume_usd": {"m5": 9000, "h1": 90000}, "price_change_percentage": {"m5": 2, "h1": 10}}}
    results = {}
    for style in ("poucas", "mais"):
        desk = memedesk.MemesDesk(30, 110, "fomo", style)
        desk.gmgn.key = ""
        monkeypatch.setattr(desk, "_gecko", lambda path, params=None: {"data": whales} if "/trades" in path else info)
        monkeypatch.setattr(desk, "_mint_info", lambda mint: {"mintAuthority": None, "freezeAuthority": None, "decimals": 6})
        monkeypatch.setattr(desk, "_pairs", lambda pools, session=None, **kw: {})
        results[style] = (desk.smarts("P", "$X", "M")["ok"], desk.debate("P", pool, "$X", "M")["ok"])
        if style == "mais":   # the rug guard stays on: an active mint is still a no
            monkeypatch.setattr(desk, "_mint_info", lambda mint: {"mintAuthority": "DEV", "freezeAuthority": None, "decimals": 6})
            assert desk.debate("P", pool, "$X", "M")["ok"] is False
            assert desk.d.style == "mais"
    assert results == {"poucas": (False, False), "mais": (True, True)}   # 2 whales and a 33% top 10: only "mais"


# ------------------------------------------------ the wide scan's first rug filter (never the last one)
def _wide_desk(monkeypatch, tmp_path):
    from deskbot import memetest, memedesk
    monkeypatch.setattr(memetest, "OUT", tmp_path)
    monkeypatch.setattr(memetest, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(memetest.MemeTest, "_eur_usd", lambda self: 1.0)
    desk = memedesk.MemesDesk(30, 110)
    desk.gmgn.key = ""
    return desk


def _token(**kw):
    """A token that passes every other cut of the wide scan."""
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    t = {"id": "MINT1", "symbol": "T1", "liquidity": 90_000, "mcap": 900_000, "organicScore": 1,
         "stats1h": {"priceChange": 5}, "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True},
         "firstPool": {"createdAt": old}}
    t.update(kw)
    return t


def test_a_token_whose_audit_shows_both_authorities_revoked_goes_on(monkeypatch, tmp_path):
    assert _wide_desk(monkeypatch, tmp_path)._wide_cut(_token()) == ""


def test_an_audit_that_does_not_say_the_mint_is_revoked_is_a_cut(monkeypatch, tmp_path):
    desk = _wide_desk(monkeypatch, tmp_path)
    assert desk._wide_cut(_token(audit={"freezeAuthorityDisabled": True})) == "mint ativa"          # key absent
    assert desk._wide_cut(_token(audit={"mintAuthorityDisabled": None, "freezeAuthorityDisabled": True})) == "mint ativa"
    assert desk._wide_cut(_token(audit={"mintAuthorityDisabled": False, "freezeAuthorityDisabled": True})) == "mint ativa"
    assert desk._wide_cut(_token(audit={"mintAuthorityDisabled": "true", "freezeAuthorityDisabled": True})) == "mint ativa"


def test_an_audit_that_does_not_say_the_freeze_is_revoked_is_a_cut(monkeypatch, tmp_path):
    desk = _wide_desk(monkeypatch, tmp_path)
    assert desk._wide_cut(_token(audit={"mintAuthorityDisabled": True})) == "freeze ativo"          # key absent
    assert desk._wide_cut(_token(audit={"mintAuthorityDisabled": True, "freezeAuthorityDisabled": False})) == "freeze ativo"
    assert desk._wide_cut(_token(audit={})) == "mint ativa"                 # an empty audit proves nothing


def test_a_token_with_no_audit_at_all_is_forwarded_to_the_chain_check(monkeypatch, tmp_path):
    desk = _wide_desk(monkeypatch, tmp_path)
    tok = _token()
    del tok["audit"]
    assert desk._wide_cut(tok) == ""          # DEBATE reads the mint on chain before any buy
    assert desk._wide_cut(_token(audit=None)) == ""


def test_a_top_level_mint_or_freeze_authority_is_a_cut_whatever_the_audit_says(monkeypatch, tmp_path):
    desk = _wide_desk(monkeypatch, tmp_path)
    assert desk._wide_cut(_token(mintAuthority="DEV")) == "mint ativa"
    assert desk._wide_cut(_token(freezeAuthority="DEV")) == "freeze ativo"
    no_audit = _token(mintAuthority="DEV")
    del no_audit["audit"]
    assert desk._wide_cut(no_audit) == "mint ativa"
    assert desk._wide_cut(_token(mintAuthority=None, freezeAuthority="")) == ""     # empty means revoked


def test_the_new_rug_cuts_are_counted_like_the_others(monkeypatch, tmp_path):
    desk = _wide_desk(monkeypatch, tmp_path)
    toks = [_token(id="M1", audit={"mintAuthorityDisabled": True}), _token(id="M2", freezeAuthority="DEV"),
            _token(id="M3", mintAuthority="DEV")]

    class R:
        ok = True

        def json(self):
            return toks
    seen = []
    monkeypatch.setattr(desk.http, "get", lambda url, params=None, timeout=None: R())
    monkeypatch.setattr(desk, "_gecko", lambda path, params=None: seen.append(path))
    assert desk._wide(set()) == {} and seen == []            # none of them cost a GeckoTerminal call
    cuts = desk.d.counts["trends"]
    assert cuts["freeze ativo"] == 2 and cuts["mint ativa"] == 1
