# ADR-004: No automated access to amazon.in; user-initiated page capture instead

**Status:** accepted

**Context.** Amazon's Conditions of Use exclude robots and data extraction from the licence, and
the Creators API is eligibility-gated (docs/02).

**Decision.** The backend never fetches amazon.in. The extension reads product facts from the
page the user is viewing, when the user clicks Save (auto-capture is an opt-in setting), and sends
them only to the loopback backend. No proxies, header spoofing, CAPTCHA handling or scheduled
page loads.

**Consequences.** Price alerts only cover products the user visits or prices they enter: the main
functional limitation. Residual legal ambiguity about in-browser capture is documented for the
owner to decide (docs/06). If the owner becomes Creators API eligible, an API adapter can fill the
gap without changing anything downstream.
