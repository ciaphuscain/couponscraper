# ADR-005: Unknown is a first-class value; separate evidence dimensions

**Status:** accepted

**Decision.** Eligibility and stacking are three-valued (YES/NO/UNKNOWN, Kleene logic).
Authenticity, lifecycle, the user's eligibility, redemption evidence and cashback settlement are
separate fields, not one confidence state. Offers with unknown terms are never dropped and never
assumed: scenarios that depend on them are marked `rules_confirmed = false`. The UI shows a
"confirmed best" next to a "possible best" with the exact assumptions.

**Consequences.** The UI carries more text, and the answer is honest. Owner-confirmed stacking
rules (Settings) turn unknowns into knowns.
