"""Scheduled maintenance: downgrade expired Pro subscriptions to Free."""
from __future__ import annotations

import logging

from app.subscriptions import expire_pro_plans

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mini-lakehouse.subscription-expiry")


def main() -> int:
    expired = expire_pro_plans()
    log.info("Subscription expiry complete: downgraded_users=%s", expired)
    return expired


if __name__ == "__main__":
    main()
