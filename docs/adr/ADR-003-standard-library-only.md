# ADR-003: Standard library only at runtime

**Status:** accepted

**Context.** The target is Windows, with ₹0 cost and low maintenance. Each dependency adds
install friction, supply-chain risk and upgrade work.

**Decision.** No runtime dependencies: `http.server`, `sqlite3`, `urllib`, `xml.etree`,
`unittest`. Playwright is used only by the optional extension E2E test.

**Consequences.** More hand-written code for routing and validation (~600 lines), covered by
`tests/test_web.py`. No OpenAPI generation. Revisit if the API grows past ~30 endpoints or needs
async.
