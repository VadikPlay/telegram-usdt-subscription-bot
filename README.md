# Gatekit — Telegram paid-subscription bot with direct USDT (TRC20) & TON payments

Self-hosted bot that sells access to a private Telegram channel and takes payment
**straight into your own wallet**. No payment processor, no middleman account, no
revenue share. You run it, you hold the keys, you keep the money.

```
subscriber pays USDT ──► your wallet          (Gatekit never touches the funds)
                          │
Gatekit reads the chain ──┘──► issues a single-use invite link
                              tracks the expiry date
                              reminds before renewal
                              removes lapsed members (carefully — see below)
```

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-53%20unit%20%2B%2056%20end--to--end-brightgreen)

---

## What it does

- **Sells access** to a private channel or supergroup, with multiple tiers.
- **Takes USDT on TRON (TRC20)** directly to your wallet. TON is supported as a
  second rail.
- **Issues single-use, short-lived invite links** — a leaked link cannot be resold.
- **Tracks every subscription period** and extends it correctly when someone
  renews early (remaining days are never stolen).
- **Sends renewal reminders** 3 days and 1 day before expiry.
- **Removes lapsed members** — after a grace period, and only when every safety
  guard agrees (see below).
- **Admin panel inside the bot**: revenue, active subscribers, who is lapsing,
  manual grant/extend/revoke, CSV export, broadcast, per-user audit trail.
- **Runs on one command**: `docker compose up -d`. SQLite, no database to install,
  no inbound ports, no webhooks.

## Non-custodial, and why that is the whole point

Gatekit holds **no private keys** and never moves funds. It only *reads* the
public blockchain to learn that a payment landed. There is no wallet to drain, no
operator who can freeze your payout, and nothing to trust beyond the code in
front of you.

That also means: if you hand this to a client, **they** own the wallet, **they**
own the server, and **they** own the kill switch. Nobody can hold their channel
hostage — including whoever installed it.

## Will it kick a paying member?

This is the question every channel owner actually asks, so it gets a real answer.
Removal is not an `if expired: kick` branch. **Five independent guards** must all
agree before anyone loses access:

| Guard | What it does |
|---|---|
| `SAFE_MODE` | On by default. Nobody is ever removed — you just get told what *would* have happened. Run it like this for a week before enforcing. |
| Grace period | Access continues for `GRACE_PERIOD_DAYS` after expiry. |
| Payment protection | Anyone who paid within `PAYMENT_PROTECTION_HOURS` is never removed, **whatever the dates say**. A stale expiry date cannot beat real money. |
| Idempotency | A member already removed is never touched again. Restarts and overlapping sweeps cannot double-act. |
| Manual confirmation | Optional: every removal waits for an admin's click in `/pending`. |

Every access decision — granted, reminded, entered grace, removed, removal
refused — is written to an append-only audit log with a timestamp, the reason,
and whether the scheduler or a human did it. When a subscriber claims they were
removed unfairly, `/audit <user_id>` shows exactly what happened.

The removal itself is a ban immediately followed by an unban: Telegram's only
"kick" primitive is a ban, and leaving it in place would stop a lapsed subscriber
from ever coming back.

## How payments are matched without a memo

A TRC20 transfer carries **no memo field**. If two subscribers each send 39.00
USDT to the same wallet, the chain cannot tell you who is who.

Gatekit solves this without custody: **every invoice gets a unique amount.**

```
advertised price   39.00 USDT
invoice GK-7F3A9   39.004271 USDT   ← the last digits identify the invoice
```

- Variance is under one cent, and there are 9999 slots — far more than the
  number of invoices open at once.
- Matching is **exact**: `39.00` does **not** satisfy `39.004271`. A rounded
  payment is never auto-credited; it is flagged to you in `/unmatched` instead.
- A transaction id can only ever be credited once, so a replayed chain page
  cannot grant two subscriptions.
- Money that matches nothing is reported, never guessed at.

TON *does* have a comment field, so TON invoices are matched on the invoice code
instead — a stronger guarantee than TRC20 allows.

## Quick start

You need: a VPS with Docker, a Telegram bot token, a private channel, and a TRON
wallet address.

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/telegram-usdt-subscription-bot.git
cd telegram-usdt-subscription-bot
cp .env.example .env
nano .env                 # fill in BOT_TOKEN, CHANNEL_ID, ADMIN_IDS, TRON_WALLET
docker compose up -d
docker compose logs -f
```

1. **Create the bot** with [@BotFather](https://t.me/BotFather) → copy the token
   into `BOT_TOKEN`.
2. **Add the bot to your private channel as an administrator** with *Invite users
   via link* and *Ban users* enabled. Gatekit refuses to start otherwise and tells
   you exactly what is missing.
3. **Get the channel id**: post a message in the channel, forward it to
   [@RawDataBot](https://t.me/RawDataBot), copy `forward_from_chat.id` (it starts
   with `-100`) into `CHANNEL_ID`.
4. **Get your user id** from [@userinfobot](https://t.me/userinfobot) → `ADMIN_IDS`.
5. **Set `TRON_WALLET`** to the address you want to be paid at, and your prices in
   `PLANS`.

Then send `/start` to your bot as a subscriber would, and `/admin` as yourself.

### Without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install .
python -m gatekit
```

A systemd unit is in [`deploy/gatekit.service`](deploy/gatekit.service).

## Rehearse it before touching real money

**Run the self-test.** It walks the entire money path — invoice → on-chain
payment → access granted → reminder → expiry → removal — against a temporary
database, a fake TronGrid and a fake Telegram. No token, no channel, no money:

```bash
python scripts/selftest.py
```

It asserts the things that matter, including that a rounded payment does **not**
unlock access, that a replayed transaction is ignored, and that a recent payer is
never removed.

**Then rehearse on a testnet.** Set `TRON_NETWORK=nile` and point
`TRON_USDT_CONTRACT` at the USDT-equivalent contract on Nile (find it on
[nile.tronscan.org](https://nile.tronscan.org)), fund a test wallet from the Nile
faucet, and run the flow end to end with worthless tokens. Gatekit warns you in
the logs whenever it is pointed at a testnet, so you cannot forget to switch back.

**Live checklist before you take real subscribers:**

- [ ] `python scripts/selftest.py` passes
- [ ] Testnet run: paid an invoice, got the invite, joined the channel
- [ ] `/config` shows the wallet, network and prices you expect
- [ ] `/stats` reports the test payment
- [ ] Sent a deliberately rounded amount and confirmed it landed in `/unmatched`
- [ ] Left `SAFE_MODE=true` for the first week and watched the warnings
- [ ] Only then set `SAFE_MODE=false`

## Configuration

Every setting is documented in [`.env.example`](.env.example). The ones that
matter most:

| Variable | Default | What it controls |
|---|---|---|
| `PLANS` | `month:30:39.00` | `code:days:price`, comma-separated |
| `TRON_WALLET` | — | The address you get paid at |
| `TRON_NETWORK` | `mainnet` | `mainnet`, `nile`, `shasta` |
| `TRON_API_KEY` | empty | Optional free key; see the rate-limit note below |
| `SAFE_MODE` | `true` | `true` = never remove anyone, only warn |
| `GRACE_PERIOD_DAYS` | `2` | Extra access after expiry |
| `PAYMENT_PROTECTION_HOURS` | `48` | Never remove someone who just paid |
| `REMINDER_DAYS` | `3,1` | When to nudge about renewal |
| `INVOICE_TTL_MINUTES` | `60` | How long an invoice's unique amount is held |
| `PAYMENT_LATE_TOLERANCE_MINUTES` | `180` | How late a payment is still honoured |
| `REQUIRE_MANUAL_KICK_CONFIRMATION` | `false` | Admin must confirm every removal |

### A note on the TronGrid rate limit

Without an API key, TronGrid allows roughly **one request per second**, and when
you exceed it the response is **HTTP 200 with an `Error` field** — not a 429.
Parsed naively, that looks like "no incoming transfers", which would make a bot
silently miss payments.

Gatekit throttles itself to stay under the limit, treats that response as a
retryable error, and raises rather than reporting an empty result, so a rate limit
can never be mistaken for "nobody paid". A free key from
[trongrid.io](https://www.trongrid.io/) raises the limit and is worth setting.

## Admin commands

| Command | What it does |
|---|---|
| `/stats` | Paying members, revenue (30d and all time), conversion, unmatched money |
| `/subs` | Most recent subscribers with plan, state and expiry |
| `/expiring` | Who lapses within 7 days |
| `/export` | Subscriber CSV |
| `/unmatched` | Payments that matched no invoice — usually a rounded amount |
| `/pending` | Removals awaiting your confirmation |
| `/audit <user_id>` | Everything that ever happened to one member's access |
| `/grant <user_id> <plan>` | Give access manually (does not inflate revenue) |
| `/extend <user_id> <days>` | Add days |
| `/revoke <user_id>` | Remove now |
| `/broadcast <text>` | Message every paying member |
| `/config` | What this instance is actually doing right now |

## Honest comparison

You have three options, and this README is not going to pretend one of them is
always right.

**1. Telegram's own Star Subscriptions** (built in since August 2024) already do
paid invite links, automatic renewal and automatic removal on non-payment. They
are free, native and require no server. The cost is the cut: a subscriber who buys
Stars in the mobile app pays roughly 30% to Apple or Google before Telegram's own
withdrawal rate, leaving the owner around 65–68% — though buying Stars through
Fragment on desktop lands closer to 93–96%. Payouts go to a TON wallet through
Fragment with a 21-day hold and a 1000-Star minimum. **If your audience is not
crypto-native, this is probably the right choice** and you do not need Gatekit.

**2. A hosted service** — InviteMember (from ~$19/month flat), Subscriby (free
tier at 10% per transaction), MyMembers (5%), Whop (~2.7% + $0.30). These are
mature, supported products that work out of the box and several accept crypto.
**If you want someone else to be on call, use one of these.**

**3. Gatekit.** Worth it when: your subscribers already hold USDT, you want the
money to land in your own wallet with no percentage and no hold period, you want
the subscriber data in a database you control, or you need logic a hosted product
will not build for you.

Requires: a server, and being comfortable that you are the support team.

## Telegram's rules — read this

Telegram's Bot Payments documentation states that digital goods sold **through the
Bot Payments API** must use Telegram Stars (`XTR`). Gatekit does not use that API
at all: payment happens entirely on-chain, outside Telegram, and the bot only
manages channel access.

There is **no explicit prohibition** in Telegram's Terms of Service or Bot
Developer Terms on accepting external or crypto payments for channel access, and
services that do exactly this have operated for years without being blocked. It
is nevertheless a **grey area**: Telegram reserves the right, "at our sole
discretion", to terminate a bot, its account, and affiliated channels.

Decide with that in your eyes open. If your channel is your livelihood and you
want zero platform risk, use native Star Subscriptions.

**Also**: do not mass-DM strangers to advertise your channel. Telegram's own spam
policy says unsolicited commercial messages get you limited and, on repetition,
permanently blocked from messaging non-contacts.

## Security notes

- The bot stores **no private keys**. There is nothing in the database worth
  stealing except subscriber ids and expiry dates.
- `.env` holds your bot token — keep it out of git (it is in `.gitignore`).
- No inbound ports are opened; Gatekit polls outbound only.
- The systemd unit runs with `NoNewPrivileges`, `ProtectSystem=strict` and a
  read-write allowlist.
- Invite links are single-use and short-lived, so a link leaked into a screenshot
  is already dead.
- Back up `data/gatekit.db` — it is your subscriber list.

## FAQ

**A subscriber sent the wrong amount. What now?**
It lands in `/unmatched` with the sender address and transaction id. Work out who
it was, then `/grant <user_id> <plan>`. Nothing is auto-credited on a guess.

**What if the bot is offline when someone pays?**
Nothing is lost. On startup the watcher looks back three days, and
`PAYMENT_LATE_TOLERANCE_MINUTES` keeps the invoice claimable after it expires.

**Can someone pay once and stay forever?**
No. Every membership has a stored expiry, and the sweep enforces it (once
`SAFE_MODE` is off).

**Can I run several channels?**
One channel per instance in this release. Run a second container with its own
`.env` and database, or ask about the multi-channel build.

**Does it work with a group instead of a channel?**
Yes — any supergroup where the bot is an admin with invite and ban rights.

**Where is the money held?**
Nowhere. It goes from the subscriber to your wallet. Gatekit only reads.

## Development

```bash
pip install -e ".[dev]"
pytest -q                     # 53 unit tests
python scripts/selftest.py    # 56 end-to-end checks
ruff check src tests scripts
```

The matching engine (`services/matcher.py`) and the access rules
(`services/access_rules.py`) are deliberately **pure functions** — no database,
no network, no clock. That is why they can be tested exhaustively, and it is where
you should look first when reading the code.

## Hire the author

I build and customise Telegram payment bots. If you want this installed on your
server, adapted to your monetisation, or extended with something it does not do
yet:

- **Installation on your server**, configured, tested, handed over with the source
- **Custom logic**: trials, discounts, referral programmes, tiered access,
  multiple channels in one bot
- **Integrations**: CRM, Google Sheets, analytics, your existing stack
- **Migration** from a hosted platform to self-hosted

Contact: **[@YOUR_TELEGRAM](https://t.me/YOUR_TELEGRAM)** · fixed price, escrow
welcome, async by default — no calls required.

## License

MIT — see [LICENSE](LICENSE). Use it commercially, fork it, resell installations
of it. Attribution appreciated, not required.
