"""TON client (optional second rail) via the public toncenter API.

TON transfers carry a text comment, so TON invoices are matched by their code
instead of the unique-amount trick — a stronger guarantee than TRC20 allows.

Status: secondary rail. TRON/USDT is the production path in this release; test
TON on testnet before enabling it for real subscribers.
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

TONCENTER_MAINNET = "https://toncenter.com/api/v2"
NANOTON_DECIMALS = 9


class TonClientError(RuntimeError):
    pass


class TonClient:
    currency = "TON"

    def __init__(
        self,
        *,
        wallet: str,
        base_url: str = TONCENTER_MAINNET,
        api_key: str = "",
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not wallet:
            raise ValueError("TON_WALLET is empty — nothing to watch")
        self.wallet = wallet
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json", "User-Agent": "gatekit/1.0"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout, headers=headers)
        self._owns_client = client is None

    async def fetch_incoming(
        self, since_ts_ms: int, limit: int = 100
    ) -> tuple[list[ChainTransfer], int]:
        payload = await self._get(
            f"{self.base_url}/getTransactions",
            {"address": self.wallet, "limit": min(int(limit), 100), "archival": "true"},
        )
        rows = (payload.get("result") or []) if isinstance(payload, dict) else []

        transfers: list[ChainTransfer] = []
        cursor = int(since_ts_ms)
        for row in rows:
            parsed = self._parse_row(row)
            if parsed is None:
                continue
            ts_ms = int(parsed.ts.timestamp() * 1000)
            if ts_ms < int(since_ts_ms):
                continue
            transfers.append(parsed)
            cursor = max(cursor, ts_ms)

        transfers.sort(key=lambda t: t.ts)
        return transfers, cursor

    async def fetch_by_tx_id(self, tx_id: str) -> ChainTransfer | None:
        tx_id = tx_id.strip()
        if not tx_id:
            return None
        window_start = int((datetime.now(tz=timezone.utc).timestamp() - 7 * 24 * 3600) * 1000)
        transfers, _ = await self.fetch_incoming(window_start)
        for transfer in transfers:
            if transfer.tx_id == tx_id:
                return transfer
        return None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, url: str, params: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = await self._client.get(url, params=params)
                if response.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(2 ** attempt)
        raise TonClientError(f"toncenter unreachable after retries: {last_error}")

    def _parse_row(self, row: dict) -> ChainTransfer | None:
        in_msg = row.get("in_msg") or {}
        destination = in_msg.get("destination") or ""
        if not destination:
            return None
        raw_value = in_msg.get("value")
        if raw_value in (None, "0"):
            return None
        try:
            amount = from_raw_units(raw_value, NANOTON_DECIMALS)
        except Exception:  # noqa: BLE001
            return None
        if amount <= Decimal(0):
            return None

        tx_id = row.get("transaction_id") or {}
        ts = int(row.get("utime") or 0)
        return ChainTransfer(
            tx_id=str(tx_id.get("hash") or ""),
            currency=self.currency,
            amount=amount,
            to_address=destination,
            from_address=in_msg.get("source") or None,
            ts=datetime.fromtimestamp(ts, tz=timezone.utc),
            comment=(in_msg.get("message") or "").strip() or None,
        )
