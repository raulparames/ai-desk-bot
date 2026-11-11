"""Guards every test in this folder shares."""
import pytest

from deskbot import chartdata


@pytest.fixture(autouse=True)
def no_market_data_network(monkeypatch):
    """No test reaches Hyperliquid through the Gráfico desk: chartdata's one HTTP function fails loudly, so
    chartdata.snapshot() answers None (no market data) unless a test puts its own fake in the function's place."""
    def refuse(payload, timeout):
        raise AssertionError("a test tried to reach the network through deskbot.chartdata")
    monkeypatch.setattr(chartdata, "_post", refuse)
