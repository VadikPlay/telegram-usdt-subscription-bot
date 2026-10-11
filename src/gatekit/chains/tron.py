"""TronGrid client for incoming USDT (TRC20) transfers.

Endpoint (verified against the TronGrid OpenAPI spec, v4.8.0):
    GET /v1/accounts/{address}/transactions/trc20
Filters used: only_to, only_confirmed, contract_address, min_timestamp,
order_by=block_timestamp,asc, limit (max 200), pagination via meta.fingerprint.

No API key is required — the spec's security block is empty. A free key from
trongrid.io only raises the rate limit, so Gatekit stays inside the
"no paid services" rule.

Rate limiting, learned the hard way against the live API
────────────────────────────────────────────────────────
Without a key TronGrid allows roughly **one request per second**, and when you
exceed it the response is **HTTP 200 with an ``Error`` field** rather than a
429. Parsed naively that looks like "no incoming transfers", which would make
the bot silently miss payments. So this client:

* throttles itself to ``min_interval`` between requests (1.1s with no key);
* treats any payload carrying ``Error`` as a retryable failure;
* raises rather than returning an empty list, so the watcher logs it and tries
  again on the next tick instead of quietly concluding nobody paid.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from gatekit.chains.base import ChainTransfer
from gatekit.money import from_raw_units

log = logging.getLogger(__name__)

MAX_PAGE = 200
MAX_PAGES_PER_SWEEP = 25  # 5000 transfers per sweep is far beyond any real channel

# TronGrid's documented anonymous budget is ~1 request/second. Stay under it.
ANONYMOUS_MIN_INTERVAL = 1.1
KEYED_MIN_INTERVAL = 0.1


class TronGridError(RuntimeError):
    pass


class TronGridRateLimited(TronGridError):
    pass


class TronClient:
    currency = "USDT"

    def __init__(
        self,
        *,
        base_url: str,
        wallet: str,
        contract: str,
        decimals: int = 6,
        api_key: str = "",
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
        min_interval: float | None = None,
    ) -> None:
        if not wallet:
            raise ValueError("TRON_WALLET is empty — nothing to watch")
        self.base_url = base_url.rstrip("/")
        self.wallet = wallet
        self.contract = contract
        self.decimals = decimals
        headers = {"Accept": "application/json", "User-Agent": "gatekit/1.0"}
        if api_key:
            headers["TRON-PRO-API-KEY"] = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout, headers=headers)
        self._owns_client = client is None
        self.min_interval = (
            min_interval
            if min_interval is not None
            else (KEYED_MIN_INTERVAL if api_key else ANONYMOUS_MIN_INTERVAL)
        )
        self._last_request_at = 0.0
        self._request_lock = asyncio.Lock()
        if not api_key:
            log.info(
                "TronGrid without an API key: throttling to one request per %.1fs. "
                "A free key from trongrid.io raises this limit.",
                self.min_interval,
            )

    # ── public API ──────────────────────────────────────────────────────────
    async def fetch_incoming(
        self, since_ts_ms: int, limit: int = MAX_PAGE
    ) -> tuple[list[ChainTransfer], int]:
        params: dict[str, str | int | bool] = {
            "only_to": "true",
            "only_confirmed": "true",
            "contract_address": self.contract,
            "min_timestamp": max(0, int(since_ts_ms)),
            "order_by": "block_timestamp,asc",
            "limit": min(int(limit), MAX_PAGE),
        }
        url = f"{self.base_url}/v1/accounts/{self.wallet}/transactions/trc20"

        transfers: list[ChainTransfer] = []
        cursor = int(since_ts_ms)
        fingerprint: str | None = None

        for page in range(MAX_PAGES_PER_SWEEP):
            page_params = dict(params)
            if fingerprint:
                # When paginating, every other parameter must stay identical.
                page_params["fingerprint"] = fingerprint
            payload = await self._get(url, page_params)

            rows = payload.get("data") or []
            for row in rows:
                parsed = self._parse_row(row)
                if parsed is None:
                    continue
                transfers.append(parsed)
                cursor = max(cursor, int(row.get("block_timestamp") or 0))

            meta = payload.get("meta") or {}
            fingerprint = meta.get("fingerprint")
            if not fingerprint or len(rows) < page_params["limit"]:
                break
            if page == MAX_PAGES_PER_SWEEP - 1:
                log.warning("TronGrid sweep hit the page cap; will continue next tick")

        return transfers, cursor

    async def fetch_by_tx_id(self, tx_id: str) -> ChainTransfer | None:
        """Find a specific transaction among recent transfers to our wallet.

        TronGrid has no "transfer by hash" endpoint on the v1 account API, so we
        scan a recent window. That is enough for the manual "here is my hash"
        flow, where the payment is minutes old.
        """
        tx_id = tx_id.strip().lower()
        if not tx_id:
            return None
        window_start_ms = int((datetime.now(tz=timezone.utc).timestamp() - 7 * 24 * 3600) * 1000)
        transfers, _ = await self.fetch_incoming(window_start_ms)
        for transfer in transfers:
            if transfer.tx_id.lower() == tx_id:
                return transfer
        return None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── internals ───────────────────────────────────────────────────────────
    async def _throttle(self) -> None:
        """Serialise requests and keep at least ``min_interval`` between them."""
        async with self._request_lock:
            elapsed = asyncio.get_running_loop().time() - self._last_request_at
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last_request_at = asyncio.get_running_loop().time()

    async def _get(self, url: str, params: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                await self._throttle()
                response = await self._client.get(url, params=params)

                if response.status_code == 429:
                    raise TronGridRateLimited("HTTP 429")
                response.raise_for_status()
                payload = response.json()

                # A rate-limited TronGrid answers 200 with an "Error" field. If we
                # treated that as a valid empty page, the bot would decide nobody
                # had paid — the worst possible silent failure.
                if isinstance(payload, dict) and payload.get("Error"):
                    message = str(payload["Error"])
                    if "rate exceeded" in message or "suspended" in message:
                        raise TronGridRateLimited(message)
                    raise TronGridError(f"TronGrid error: {message}")
                if payload.get("success") is False:
                    raise TronGridError(f"TronGrid returned an error: {payload.get('error')}")
                if "data" not in payload:
                    raise TronGridError(f"Unexpected TronGrid response shape: {payload}")
                return payload

            except TronGridRateLimited as exc:
                last_error = exc
                # TronGrid suspends the caller for ~5s, so start the backoff there.
                wait = 5 * (attempt + 1)
                log.warning("TronGrid rate limit (%s); waiting %ss", exc, wait)
                await asyncio.sleep(wait)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                wait = 2 ** attempt
                log.warning("TronGrid request failed (%s), retrying in %ss", exc, wait)
                await asyncio.sleep(wait)

        # Raising (rather than returning nothing) is deliberate: the watcher logs
        # it and retries next tick instead of concluding that no payments arrived.
        raise TronGridError(f"TronGrid unusable after retries: {last_error}")

    def _parse_row(self, row: dict) -> ChainTransfer | None:
        # Defensive: only count confirmed inbound Transfer events of our token.
        if (row.get("type") or "Transfer") != "Transfer":
            return None
        token = row.get("token_info") or {}
        contract = token.get("address") or ""
        if contract and contract != self.contract:
            return None
        to_address = row.get("to") or ""
        if to_address != self.wallet:
            return None

        raw_value = row.get("value")
        if raw_value is None:
            return None
        decimals = int(token.get("decimals") or self.decimals)
        try:
            amount = from_raw_units(raw_value, decimals)
        except Exception:  # noqa: BLE001 - malformed row, skip it
            log.warning("Skipping TRC20 row with unparsable value: %r", raw_value)
            return None
        if amount <= Decimal(0):
            return None

        ts_ms = int(row.get("block_timestamp") or 0)
        return ChainTransfer(
            tx_id=str(row.get("transaction_id") or ""),
            currency=self.currency,
            amount=amount,
            to_address=to_address,
            from_address=row.get("from"),
            ts=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
            comment=None,  # TRC20 has no memo — this is why invoices use unique amounts
        )
