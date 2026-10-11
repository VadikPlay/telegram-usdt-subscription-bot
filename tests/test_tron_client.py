"""TronGrid client tests, including a regression test for a real silent failure.

The rate-limit test exists because the live API taught us something the docs do
not say: when you exceed the anonymous quota, TronGrid answers **HTTP 200 with
an ``Error`` field**, not 429. An earlier version of this client parsed that as
"no incoming transfers", which would have made the bot quietly miss payments.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from gatekit.chains.tron import TronClient, TronGridError

WALLET = "TWatchedWallet00000000000000000000"
CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

RATE_LIMIT_BODY = {
    "Error": (
        "request rate exceeded the allowed_rps(1), and the query server is "
        "suspended for 5 s."
    )
}

REAL_SHAPE_ROW = {
    "transaction_id": "a" * 64,
    "token_info": {
        "symbol": "USDT",
        "address": CONTRACT,
        "decimals": 6,
        "name": "Tether USD",
    },
    "block_timestamp": 1_789_000_000_000,
    "from": "TPayerAddress0000000000000000000000",
    "to": WALLET,
    "type": "Transfer",
    "value": "39004271",
}


def make_client(handler, **kwargs) -> TronClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return TronClient(
        base_url="https://api.trongrid.io",
        wallet=WALLET,
        contract=CONTRACT,
        api_key="",
        client=http,
        min_interval=0.0,  # no real waiting in tests
        **kwargs,
    )


async def test_parses_a_documented_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": [REAL_SHAPE_ROW], "meta": {}})

    client = make_client(handler)
    transfers, cursor = await client.fetch_incoming(0)
    await client.close()

    assert len(transfers) == 1
    transfer = transfers[0]
    assert transfer.amount == Decimal("39.004271")
    assert transfer.currency == "USDT"
    assert transfer.to_address == WALLET
    assert transfer.comment is None  # TRC20 has no memo — hence unique amounts
    assert transfer.ts.tzinfo is not None
    assert cursor == 1_789_000_000_000


async def test_rate_limit_disguised_as_http_200_is_never_read_as_empty(monkeypatch):
    """The regression test for the bug the live API exposed."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=RATE_LIMIT_BODY)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("gatekit.chains.tron.asyncio.sleep", no_sleep)

    client = make_client(handler)
    with pytest.raises(TronGridError):
        await client.fetch_incoming(0)
    await client.close()

    # It must retry and then fail loudly, never return "no payments arrived".
    assert calls["n"] > 1


async def test_unexpected_shape_raises_rather_than_returning_nothing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"weird": True})

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("gatekit.chains.tron.asyncio.sleep", no_sleep)

    client = make_client(handler)
    with pytest.raises(TronGridError):
        await client.fetch_incoming(0)
    await client.close()


async def test_recovers_after_a_transient_rate_limit(monkeypatch):
    responses = [
        httpx.Response(200, json=RATE_LIMIT_BODY),
        httpx.Response(200, json={"success": True, "data": [REAL_SHAPE_ROW], "meta": {}}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("gatekit.chains.tron.asyncio.sleep", no_sleep)

    client = make_client(handler)
    transfers, _ = await client.fetch_incoming(0)
    await client.close()
    assert len(transfers) == 1


async def test_transfer_to_another_wallet_is_dropped():
    row = dict(REAL_SHAPE_ROW, to="TSomeoneElse00000000000000000000000")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": [row], "meta": {}})

    client = make_client(handler)
    transfers, _ = await client.fetch_incoming(0)
    await client.close()
    assert transfers == []


async def test_other_token_contract_is_dropped():
    row = dict(REAL_SHAPE_ROW)
    row["token_info"] = dict(
        REAL_SHAPE_ROW["token_info"], address="TFakeUSDT000000000000000000000000"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": [row], "meta": {}})

    client = make_client(handler)
    transfers, _ = await client.fetch_incoming(0)
    await client.close()
    assert transfers == []


async def test_zero_and_malformed_values_are_dropped():
    rows = [
        dict(REAL_SHAPE_ROW, transaction_id="b" * 64, value="0"),
        dict(REAL_SHAPE_ROW, transaction_id="c" * 64, value=None),
        dict(REAL_SHAPE_ROW, transaction_id="d" * 64, value="not-a-number"),
        dict(REAL_SHAPE_ROW, transaction_id="e" * 64, type="Approval"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": rows, "meta": {}})

    client = make_client(handler)
    transfers, _ = await client.fetch_incoming(0)
    await client.close()
    assert transfers == []


async def test_paginates_with_fingerprint():
    page_one = {
        "success": True,
        "data": [
            dict(REAL_SHAPE_ROW, transaction_id=f"{i:064x}", block_timestamp=1_789_000_000_000 + i)
            for i in range(200)
        ],
        "meta": {"fingerprint": "PAGE2"},
    }
    page_two = {
        "success": True,
        "data": [dict(REAL_SHAPE_ROW, transaction_id="f" * 64)],
        "meta": {},
    }
    seen_fingerprints: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fingerprint = request.url.params.get("fingerprint")
        seen_fingerprints.append(fingerprint)
        return httpx.Response(200, json=page_one if fingerprint is None else page_two)

    client = make_client(handler)
    transfers, _ = await client.fetch_incoming(0)
    await client.close()

    assert len(transfers) == 201
    assert seen_fingerprints == [None, "PAGE2"]


async def test_query_asks_only_for_confirmed_incoming_usdt():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"success": True, "data": [], "meta": {}})

    client = make_client(handler)
    await client.fetch_incoming(1_700_000_000_000)
    await client.close()

    assert captured["only_to"] == "true"
    assert captured["only_confirmed"] == "true"
    assert captured["contract_address"] == CONTRACT
    assert captured["min_timestamp"] == "1700000000000"
    assert captured["order_by"] == "block_timestamp,asc"
