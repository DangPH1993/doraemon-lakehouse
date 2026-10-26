"""Subscription expiry helpers for News Radar Pro plans."""
from __future__ import annotations

from .db import get_conn


def expire_pro_plans(user_id: int | None = None) -> int:
    """Downgrade expired Pro accounts to Free and disable Telegram delivery.

    Supplying user_id performs a low-cost lazy expiry check for the current user.
    Omitting it expires every account and is used by the scheduled workflow/admin list.
    """
    predicate = "AND user_id=%s" if user_id is not None else ""
    params = (user_id,) if user_id is not None else ()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""UPDATE app_users
                SET plan='Free',
                    plan_expires_at=NULL,
                    plan_updated_at=now(),
                    plan_updated_by='subscription-expiry',
                    telegram_enabled=false,
                    telegram_notifications_since=NULL
                WHERE plan='Pro'
                  AND plan_expires_at IS NOT NULL
                  AND plan_expires_at <= now()
                  {predicate}
                RETURNING user_id""",
            params,
        )
        return len(cur.fetchall())
