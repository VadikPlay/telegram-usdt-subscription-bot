"""Entry point: ``python -m gatekit``."""

from __future__ import annotations

import asyncio
import sys


def run() -> None:
    from gatekit.bot.main import run_bot

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("\nStopped.")
    except SystemExit as exc:
        # Startup problems are raised as SystemExit carrying a human-readable fix.
        # Printed rather than logged so it is readable before logging is set up.
        if exc.code and not isinstance(exc.code, int):
            print(f"\n{exc.code}\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()
