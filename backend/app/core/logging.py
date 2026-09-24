"""Logging setup.

Managed hosts collect stdout, so plain stream logging is sufficient and avoids any
dependency on local filesystem state.
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
    # The driver logs one INFO line per connection, and emits a verbose INFO
    # notification for every "IF NOT EXISTS" schema statement that was a no-op.
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)
