# ADR-006: Exhaustive enumeration with bounded search instead of a solver

**Status:** accepted

**Context.** At most one offer per kind and one payment instrument per checkout. Real product pages
show few offers.

**Decision.** Enumerate combinations per instrument. Prune to the best 3 offers per kind (by
confidence-weighted benefit) and cap 2,000 combinations per instrument. Resolve unknown bases with
four bounding readings instead of 2ⁿ.

**Evidence.** Before pruning, 60 offers took 3.5 s; after, about 0.1 s. A property test checks the
bounding readings against brute force over 400 random offer sets; a two-reading shortcut fails it.

**Consequences.** Exact and explainable. Pathological inputs are pruned and the result says so.
A CP-SAT/ILP model becomes worthwhile only if real offer sets routinely exceed the limits.
