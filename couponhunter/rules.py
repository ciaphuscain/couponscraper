"""Explicit, testable eligibility predicates with three-valued logic.

A rule is a JSON-serialisable tree:

    {"all": [rule, ...]}      every child true
    {"any": [rule, ...]}      at least one child true
    {"not": rule}
    {"field": "product.category", "op": "in", "value": ["electronics"]}

Evaluation returns True, False or None (unknown). Unknown arises whenever a
field the rule depends on is missing from the context, e.g. we do not know if
the user is a new customer. Kleene logic keeps "unknown" from silently turning
into "eligible": all(True, None) is None, any(False, None) is None.
"""

from __future__ import annotations

from typing import Any

OPS = {"eq", "ne", "in", "not_in", "gte", "lte", "gt", "lt", "contains"}
MAX_DEPTH = 16


class RuleError(ValueError):
    pass


def validate(rule: Any, depth: int = 0) -> None:
    """Reject malformed rules at ingest time rather than at evaluation time."""
    if depth > MAX_DEPTH:
        raise RuleError("rule nested too deeply")
    if not isinstance(rule, dict) or len(rule) == 0:
        raise RuleError("rule must be a non-empty object")
    if "all" in rule or "any" in rule:
        key = "all" if "all" in rule else "any"
        children = rule[key]
        if not isinstance(children, list) or not children:
            raise RuleError(f"'{key}' needs a non-empty list")
        for c in children:
            validate(c, depth + 1)
        return
    if "not" in rule:
        validate(rule["not"], depth + 1)
        return
    if set(rule) - {"field", "op", "value"}:
        raise RuleError(f"unexpected keys: {sorted(set(rule) - {'field', 'op', 'value'})}")
    if not isinstance(rule.get("field"), str) or rule.get("op") not in OPS:
        raise RuleError(f"bad leaf rule: {rule!r}")
    if rule["op"] in ("in", "not_in") and not isinstance(rule.get("value"), list):
        raise RuleError("'in'/'not_in' need a list value")


def _lookup(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _norm(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


def _leaf(ctx: dict[str, Any], rule: dict[str, Any]) -> bool | None:
    actual = _lookup(ctx, rule["field"])
    if actual is None:
        return None
    op, expected = rule["op"], rule.get("value")
    a = _norm(actual)
    if op in ("in", "not_in"):
        hit = a in {_norm(x) for x in expected}
        return hit if op == "in" else not hit
    e = _norm(expected)
    if op == "eq":
        return a == e
    if op == "ne":
        return a != e
    if op == "contains":
        return isinstance(a, str) and isinstance(e, str) and e in a
    try:
        if op == "gte":
            return a >= e
        if op == "lte":
            return a <= e
        if op == "gt":
            return a > e
        if op == "lt":
            return a < e
    except TypeError:
        return None
    raise RuleError(f"unknown op {op}")


def evaluate(rule: dict[str, Any] | None, ctx: dict[str, Any]) -> bool | None:
    if rule is None:
        return True
    if "all" in rule:
        results = [evaluate(r, ctx) for r in rule["all"]]
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True
    if "any" in rule:
        results = [evaluate(r, ctx) for r in rule["any"]]
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False
    if "not" in rule:
        r = evaluate(rule["not"], ctx)
        return None if r is None else not r
    return _leaf(ctx, rule)


def explain(rule: dict[str, Any] | None) -> str:
    """Human-readable rendering for the UI."""
    if rule is None:
        return "no restriction"
    if "all" in rule:
        return " AND ".join(f"({explain(r)})" for r in rule["all"])
    if "any" in rule:
        return " OR ".join(f"({explain(r)})" for r in rule["any"])
    if "not" in rule:
        return f"NOT ({explain(rule['not'])})"
    v = rule.get("value")
    if isinstance(v, list):
        v = ", ".join(map(str, v))
    return f"{rule['field']} {rule['op']} {v}"
