# ADR-001: Modular monolith (one process) over services

**Status:** accepted · **Date:** 2026-10-25

**Context.** One user on one Windows machine. A handful of sources and at most hundreds of
watched products. The extension needs a local API.

**Decision.** One Python process runs the HTTP server (API and dashboard) and a scheduler thread.
Modules have one-way dependencies; domain logic is I/O-free.

**Consequences.** Simple to install, debug (one log, one `job_runs` table) and back up (one file).
A crash stops everything, so mitigations are restart at logon, transactional writes, lease expiry
and single catch-up. Splitting the scheduler into a worker later needs no domain changes.

**Rejected.** Event-driven workers + queue (Candidate C): solves throughput problems this workload
doesn't have. Script-only (Candidate A): cannot serve the extension.
