"""Database schema migrations for octodot.

Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

from octodot.migrations.runner import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    get_schema_version,
    migrate_database,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "Migration",
    "get_schema_version",
    "migrate_database",
]
