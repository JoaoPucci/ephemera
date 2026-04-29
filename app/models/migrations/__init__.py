"""Schema migrations applied by app.models._core.init_db.

Each `migrations.v<N>` module owns the schema bump from version (N-1) to N.
The `MIGRATIONS` dict maps target version to its `migrate(conn)` callable;
init_db iterates over it in ascending order and applies the ones whose
target version is greater than the DB's stamped version.

## Bumping the schema

1. Add `app/models/migrations/v<N>.py` with a `migrate(conn)` function.
2. Bump `CURRENT_SCHEMA_VERSION` in `app/models/_core.py`.
3. Add the new entry to `MIGRATIONS` below.

## Helpers

Migrations import `_cols` and `SchemaVersionError` from `app.models._core`
when they need them. The import is safe even though `_core` itself
imports this package (at the bottom, after defining those helpers) --
Python's module-import machinery resolves the partial-module case
correctly because the helpers are defined before the migrations import.
"""

import sqlite3
from collections.abc import Callable

from .v2 import migrate as _migrate_to_v2
from .v3 import migrate as _migrate_to_v3
from .v4 import migrate as _migrate_to_v4
from .v5 import migrate as _migrate_to_v5
from .v6 import migrate as _migrate_to_v6

MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
    6: _migrate_to_v6,
}

__all__ = ["MIGRATIONS"]
