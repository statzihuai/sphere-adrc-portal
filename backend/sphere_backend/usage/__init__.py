"""AI-request usage lifecycle: reserve → finalize/cancel, plus stale-reclaim.

Wraps the wallet hold and the ``api_usage_log`` row together so a reservation's
hold is released exactly once — by ``finalize`` (settle + charge), ``cancel``
(release without charge), or ``reclaim_stale`` (sweep crashed requests).
"""

from .service import cancel, finalize, open_reservation, reclaim_stale

__all__ = ["open_reservation", "finalize", "cancel", "reclaim_stale"]
