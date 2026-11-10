"""The app has two desks: Memecoins on FOMO and Futuros on INVO (Hyperliquid). Nothing else is offered."""


def test_overview_lists_only_memes_and_futuros_and_futuros_is_the_invo_desk(monkeypatch, tmp_path):
    from deskbot import desks_api
    monkeypatch.setattr(desks_api, "MEME_DIR", tmp_path / "memetest")     # no sessions yet: both desks still show
    monkeypatch.setattr(desks_api, "PERP_DIR", tmp_path / "perpdesk")
    desks = desks_api.overview({})["desks"]
    assert [d["name"] for d in desks] == ["memes", "futuros"]
    assert "FOMO" in desks[0]["subtitle"] and "INVO" in desks[1]["subtitle"]
    assert desks_api.desk("memes", {})["venue"] == "fomo" and desks_api.desk("memes", {})["venues"] == ["fomo"]
    assert desks_api.desk("perps", {})["venue"] == "invo"


def test_memes_venue_is_always_fomo_even_when_an_old_client_asks_for_gmgn(monkeypatch, tmp_path):
    from deskbot import desks_api, memedesk, memetest
    monkeypatch.setattr(memetest, "OUT", tmp_path)
    monkeypatch.setattr(memetest.MemeTest, "_eur_usd", lambda self: 1.0)
    assert memedesk.MemesDesk(30, 110).d.venue == "fomo"
    assert memedesk.MemesDesk(30, 110, "gmgn").d.venue == "fomo"
    assert set(memedesk.VENUES) == {"fomo"} and desks_api.VENUES == ("fomo",)
    spawned = []
    monkeypatch.setattr(desks_api, "meme_running", lambda: False)
    monkeypatch.setattr(desks_api, "_spawn", lambda folder, args: spawned.append(args))
    desks_api.start_memes(30, 110, "gmgn", "mais")
    assert spawned and spawned[0][spawned[0].index("--venue") + 1] == "fomo"


def test_notifications_name_fomo_for_memes_and_invo_for_futures():
    import inspect
    from deskbot import push
    src = inspect.getsource(push)
    assert '"INVO" if desk == "perps" else "FOMO"' in src and "gmgn" not in src.lower()
