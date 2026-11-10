"""The phone opens docs/ (GitHub Pages), not the files the PC serves. A page edited and not rebuilt would
leave the phone on the old rules and texts: this fails until `python deploy/build_pages.py` is run again."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_pages", ROOT / "deploy" / "build_pages.py")
build_pages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_pages)


@pytest.mark.parametrize("built, source", [("index.html", "app.html"), ("memes.html", "memes.html"),
                                           ("perps.html", "memes.html"), ("grafico.html", "grafico.html")])
def test_the_phone_pages_are_the_ones_on_disk(built, source):
    want = build_pages.rewrite((build_pages.STATIC / source).read_text(encoding="utf-8"))
    assert (build_pages.OUT / built).read_text(encoding="utf-8") == want, "run: python deploy/build_pages.py"


def test_the_phone_gets_the_notification_worker_of_the_pc():
    assert (build_pages.OUT / "sw.js").read_bytes() == (build_pages.STATIC / "sw.js").read_bytes()
