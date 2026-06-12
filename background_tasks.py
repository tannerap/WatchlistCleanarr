"""Run watchlist cleanup outside the webhook request thread."""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="watchlist-cleanup")
atexit.register(lambda: _executor.shutdown(wait=True, cancel_futures=False))


def submit_watchlist_cleanup(
    task_name: str,
    cleanup: Callable[..., int],
    **kwargs: Any,
) -> None:
    """Queue Plex watchlist cleanup and return immediately to the webhook caller."""

    def _run() -> None:
        title = kwargs.get("title", "unknown")
        try:
            removed_count = cleanup(**kwargs)
            logger.info(
                "Background %s finished for '%s': removedFromWatchlists=%d",
                task_name,
                title,
                removed_count,
            )
        except Exception as exc:
            logger.error(
                "Background %s failed for '%s': %s",
                task_name,
                title,
                exc,
                exc_info=True,
            )

    _executor.submit(_run)
    logger.info("Queued background %s for '%s'", task_name, kwargs.get("title", "unknown"))
