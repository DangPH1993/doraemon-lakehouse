"""Standalone Telegram digest pipeline; never invokes RSS or GenAI classification."""
from __future__ import annotations

import logging
import sys

from .config import settings
from .telegram_notifications import cleanup_old_delivery_markers, send_telegram_digests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s | %(message)s')
log = logging.getLogger('mini-lakehouse.telegram-pipeline')


def main() -> int:
    if not settings.telegram_bot_token:
        log.error('TELEGRAM_BOT_TOKEN is missing; no messages were sent.')
        return 2
    result = send_telegram_digests()
    log.info('Telegram pipeline finished: %s', result)
    try:
        purged = cleanup_old_delivery_markers()
        log.info('Old Telegram delivery markers purged: %s', purged)
    except Exception:
        log.exception('Could not clean old Telegram delivery markers.')
    # Successful users have a delivery marker. A later scheduled retry only sees articles
    # not marked as delivered, rather than repeating the complete digest.
    return 1 if int(result.get('failed_users', 0)) > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
