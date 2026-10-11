"""Gatekit — self-hosted Telegram paid-subscription bot with direct crypto payments.

Design rule #1: Gatekit is non-custodial. It never stores a private key, never
holds subscriber money and never moves funds. Payments go straight from the
subscriber to the channel owner's wallet; Gatekit only *reads* the public chain
to learn that a payment landed.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
