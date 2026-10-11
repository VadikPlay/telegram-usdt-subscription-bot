"""Configuration, loaded from environment / .env.

Everything tunable lives here so that a customer can reconfigure the bot without
touching Python. See .env.example for the annotated list.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# TRON networks that TronGrid exposes. Testnets let you rehearse the whole flow
# with worthless tokens before touching real money.
TRON_ENDPOINTS: dict[str, str] = {
    "mainnet": "https://api.trongrid.io",
    "nile": "https://nile.trongrid.io",
    "shasta": "https://api.shasta.trongrid.io",
}

USDT_DECIMALS = 6
TON_DECIMALS = 9


@dataclass(frozen=True, slots=True)
class Plan:
    """One purchasable subscription tier."""

    code: str
    days: int
    price_usdt: Decimal
    label: str
    price_ton: Decimal | None = None

    def price_for(self, currency: str) -> Decimal | None:
        if currency == "USDT":
            return self.price_usdt
        if currency == "TON":
            return self.price_ton
        return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ── Telegram ────────────────────────────────────────────────────────────
    bot_token: str = Field(min_length=20)
    channel_id: int
    admin_ids: str = ""
    support_contact: str = ""
    default_lang: str = "en"

    # ── Plans ───────────────────────────────────────────────────────────────
    plans: str = "month:30:39.00"
    plan_labels: str = ""

    # ── TRON ────────────────────────────────────────────────────────────────
    tron_enabled: bool = True
    tron_wallet: str = ""
    tron_network: str = "mainnet"
    tron_usdt_contract: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    tron_api_key: str = ""

    # ── TON ─────────────────────────────────────────────────────────────────
    ton_enabled: bool = False
    ton_wallet: str = ""
    ton_prices: str = ""
    ton_api_key: str = ""

    # ── Matching ────────────────────────────────────────────────────────────
    invoice_ttl_minutes: int = 60
    payment_late_tolerance_minutes: int = 180
    clock_skew_seconds: int = 120
    watcher_interval_seconds: int = 30

    # ── Access safety ───────────────────────────────────────────────────────
    safe_mode: bool = True
    grace_period_days: int = 2
    payment_protection_hours: int = 48
    reminder_days: str = "3,1"
    invite_link_ttl_minutes: int = 30
    require_manual_kick_confirmation: bool = False
    scheduler_interval_seconds: int = 300

    # ── Storage ─────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./data/gatekit.db"
    log_level: str = "INFO"

    # ── Validators ──────────────────────────────────────────────────────────
    @field_validator("tron_network")
    @classmethod
    def _known_network(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in TRON_ENDPOINTS:
            raise ValueError(f"TRON_NETWORK must be one of {sorted(TRON_ENDPOINTS)}, got {v!r}")
        return v

    @field_validator("default_lang")
    @classmethod
    def _known_lang(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"en", "ru"}:
            raise ValueError("DEFAULT_LANG must be 'en' or 'ru'")
        return v

    # ── Derived helpers ─────────────────────────────────────────────────────
    @property
    def admin_id_set(self) -> set[int]:
        out: set[int] = set()
        for chunk in self.admin_ids.split(","):
            chunk = chunk.strip()
            if chunk:
                out.add(int(chunk))
        return out

    @property
    def reminder_day_list(self) -> list[int]:
        days = []
        for chunk in self.reminder_days.split(","):
            chunk = chunk.strip()
            if chunk:
                days.append(int(chunk))
        return sorted(set(days), reverse=True)

    @property
    def tron_base_url(self) -> str:
        return TRON_ENDPOINTS[self.tron_network]

    @property
    def plan_list(self) -> list[Plan]:
        return parse_plans(self.plans, self.plan_labels, self.ton_prices)

    def plan_by_code(self, code: str) -> Plan | None:
        for plan in self.plan_list:
            if plan.code == code:
                return plan
        return None

    @property
    def enabled_currencies(self) -> list[str]:
        out: list[str] = []
        if self.tron_enabled and self.tron_wallet:
            out.append("USDT")
        if self.ton_enabled and self.ton_wallet:
            out.append("TON")
        return out

    def wallet_for(self, currency: str) -> str:
        return {"USDT": self.tron_wallet, "TON": self.ton_wallet}.get(currency, "")

    def decimals_for(self, currency: str) -> int:
        return {"USDT": USDT_DECIMALS, "TON": TON_DECIMALS}.get(currency, USDT_DECIMALS)

    def sanity_check(self) -> list[str]:
        """Human-readable problems to shout about at startup instead of failing later."""
        problems: list[str] = []
        if not self.plan_list:
            problems.append("PLANS is empty — nothing to sell.")
        if not self.enabled_currencies:
            problems.append(
                "No payment rail is usable: enable TRON (with TRON_WALLET) "
                "and/or TON (with TON_WALLET)."
            )
        if not self.admin_id_set:
            problems.append("ADMIN_IDS is empty — you would lock yourself out of the admin panel.")
        if self.channel_id >= 0:
            problems.append(
                f"CHANNEL_ID looks wrong ({self.channel_id}): channel/supergroup IDs are negative "
                "and usually start with -100."
            )
        if self.tron_enabled and self.tron_network != "mainnet":
            problems.append(
                f"TRON_NETWORK={self.tron_network} — testnet mode. Real USDT will NOT be seen. "
                "Switch to mainnet when you go live."
            )
        if self.ton_enabled and self.ton_wallet:
            missing = [p.code for p in self.plan_list if p.price_ton is None]
            if missing:
                problems.append(f"TON is on but TON_PRICES has no price for plans: {missing}")
        return problems


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{field}: {raw!r} is not a number") from exc
    if value <= 0:
        raise ValueError(f"{field}: price must be positive, got {value}")
    return value


def parse_plans(plans_raw: str, labels_raw: str = "", ton_prices_raw: str = "") -> list[Plan]:
    """Parse ``code:days:price`` triples into Plan objects.

    >>> [p.code for p in parse_plans("month:30:39.00,year:365:349")]
    ['month', 'year']
    """
    labels: dict[str, str] = {}
    for chunk in labels_raw.split("|"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        code, _, label = chunk.partition("=")
        labels[code.strip()] = label.strip()

    ton_prices: dict[str, Decimal] = {}
    for chunk in ton_prices_raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        code, _, amount = chunk.partition(":")
        ton_prices[code.strip()] = _parse_decimal(amount, "TON_PRICES")

    out: list[Plan] = []
    seen: set[str] = set()
    for chunk in plans_raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(":")]
        if len(parts) != 3:
            raise ValueError(f"PLANS entry {chunk!r} must look like code:days:price")
        code, days_raw, price_raw = parts
        if not code:
            raise ValueError("PLANS: plan code cannot be empty")
        if code in seen:
            raise ValueError(f"PLANS: duplicate plan code {code!r}")
        seen.add(code)
        days = int(days_raw)
        if days <= 0:
            raise ValueError(f"PLANS: {code} has non-positive days ({days})")
        out.append(
            Plan(
                code=code,
                days=days,
                price_usdt=_parse_decimal(price_raw, "PLANS"),
                label=labels.get(code, code),
                price_ton=ton_prices.get(code),
            )
        )
    return out


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
