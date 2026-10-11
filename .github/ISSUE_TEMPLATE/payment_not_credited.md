---
name: A payment was not credited
about: Money arrived but the subscriber did not get access
labels: payment
---

Before filing: check `/unmatched` in the admin panel. The most common cause is a
**rounded amount** — Gatekit matches the invoice amount exactly, so `39.00` does
not satisfy an invoice for `39.004271`. That is deliberate, and the fix is
`/grant <user_id> <plan_code>`.

**Invoice code** (e.g. `GK-7F3A9`)

**Amount the invoice asked for**

**Amount actually sent**

**Transaction id**

**Did it appear in `/unmatched`?** yes / no

**Time between the transfer confirming and now**

**Relevant log lines** (redact the token and wallet)

```
paste here
```
