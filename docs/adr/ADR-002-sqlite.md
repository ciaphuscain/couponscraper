# ADR-002: SQLite in WAL mode

**Status:** accepted

**Context.** ≤10⁴ offers, ≤10⁶ price observations, one writer process, financial records need
atomicity.

**Decision.** SQLite with WAL, `foreign_keys=ON`, `busy_timeout=5000`, writes through
`Database.tx()` (BEGIN IMMEDIATE). Money is INTEGER paise; times are ISO-8601 UTC text.
Migrations are numbered SQL files applied in one transaction each.

**Consequences.** A SIGKILL mid-transaction leaves a consistent database (tested). Backups use
the online backup API plus `integrity_check`. Multi-machine writers would need PostgreSQL. The
schema has no SQLite-only types, so the move is mechanical.
