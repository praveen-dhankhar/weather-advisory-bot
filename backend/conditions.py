"""Generic, declarative condition evaluator plus the derived-signal registry.

The evaluator knows about operators and nesting; it knows nothing about weather
or policy. Thresholds, fields and nesting all come from the SOP YAML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

# WMO code groups are POLICY and live in sops/_fields.yaml::code_groups. A derived
# signal names the group it counts; SOP conditions reference the same group through
# the `in_group` operator, so "heavy rain" is defined exactly once, as data.


# --------------------------------------------------------------------------- #
# Operators
# --------------------------------------------------------------------------- #
def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _op_gt(left: Any, right: Any) -> bool:
    lv = _num(left)
    return lv is not None and lv > float(right)


def _op_gte(left: Any, right: Any) -> bool:
    lv = _num(left)
    return lv is not None and lv >= float(right)


def _op_lt(left: Any, right: Any) -> bool:
    lv = _num(left)
    return lv is not None and lv < float(right)


def _op_lte(left: Any, right: Any) -> bool:
    lv = _num(left)
    return lv is not None and lv <= float(right)


def _op_eq(left: Any, right: Any) -> bool:
    return left == right


def _op_between(left: Any, right: Any) -> bool:
    lo, hi = right
    lv = _num(left)
    return lv is not None and float(lo) <= lv <= float(hi)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _op_in(left: Any, right: Any) -> bool:
    """Membership. A list-valued field (e.g. the weather codes seen in a window)
    matches if ANY of its values is in the operand list."""
    wanted = set(_as_list(right))
    return any(v in wanted for v in _as_list(left))


def _op_not_in(left: Any, right: Any) -> bool:
    return not _op_in(left, right)


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "gt": _op_gt,
    "gte": _op_gte,
    "lt": _op_lt,
    "lte": _op_lte,
    "eq": _op_eq,
    "between": _op_between,
    "in": _op_in,
    "not_in": _op_not_in,
}

COMBINATORS = ("all_of", "any_of", "not")


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
@dataclass
class Eval:
    ok: bool
    matched: int = 0  # number of leaf conditions that held - used for specificity
    evidence: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    def merge(self, other: "Eval") -> None:
        self.matched += other.matched
        self.evidence.update(other.evidence)
        self.missing.extend(other.missing)


def evaluate(node: Any, values: dict[str, Any]) -> Eval:
    """Evaluate a condition tree against resolved field values.

    A node is either a combinator (``all_of`` / ``any_of`` / ``not``) or a mapping
    of ``field -> {operator: operand}``. Several fields in one mapping are an
    implicit ``all_of``. A field with no value is a failed leaf, never a pass.
    """
    if not isinstance(node, dict) or not node:
        raise ValueError(f"condition node must be a non-empty mapping, got {node!r}")

    if "all_of" in node or "any_of" in node or "not" in node:
        if len(node) != 1:
            raise ValueError(f"combinator node must hold exactly one key, got {sorted(node)}")
        key, payload = next(iter(node.items()))
        if key == "not":
            inner = evaluate(payload, values)
            return Eval(not inner.ok, 1 if not inner.ok else 0, inner.evidence, inner.missing)
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"{key} must be a non-empty list")
        children = [evaluate(child, values) for child in payload]
        out = Eval(ok=False)
        if key == "all_of":
            out.ok = all(c.ok for c in children)
            for c in children:
                out.merge(c)
            if not out.ok:
                out.matched = 0
        else:  # any_of
            out.ok = any(c.ok for c in children)
            for c in children:
                out.evidence.update(c.evidence)
                out.missing.extend(c.missing)
                if c.ok:
                    out.matched += c.matched
        return out

    out = Eval(ok=True)
    for fname, test in node.items():
        if not isinstance(test, dict) or len(test) != 1:
            raise ValueError(f"field `{fname}` needs exactly one operator, got {test!r}")
        op_name, operand = next(iter(test.items()))
        if op_name not in OPERATORS:
            raise ValueError(f"unknown operator `{op_name}` on field `{fname}`")
        value = values.get(fname)
        out.evidence[fname] = value
        if value is None:
            out.missing.append(fname)
            out.ok = False
            continue
        if OPERATORS[op_name](value, operand):
            out.matched += 1
        else:
            out.ok = False
    if not out.ok:
        out.matched = 0
    return out


def collect_fields(node: Any) -> set[str]:
    """Every field name a condition tree reads. Used to validate SOPs at startup."""
    found: set[str] = set()
    if not isinstance(node, dict):
        return found
    for key, payload in node.items():
        if key in COMBINATORS:
            items: Iterable[Any] = payload if isinstance(payload, list) else [payload]
            for child in items:
                found |= collect_fields(child)
        else:
            found.add(key)
    return found


# --------------------------------------------------------------------------- #
# Derived signals
# --------------------------------------------------------------------------- #
@dataclass
class DerivedContext:
    """Raw arrays needed to compute derived signals, before the snapshot exists."""

    current: dict[str, Optional[float]]
    hourly: dict[str, list[Optional[float]]]
    times: list[str]
    now_index: int

    def ahead(self, fname: str, hours: int) -> list[float]:
        series = self.hourly.get(fname) or []
        window = series[self.now_index : self.now_index + hours]
        return [v for v in window if isinstance(v, (int, float))]

    def at(self, fname: str, offset: int) -> Optional[float]:
        series = self.hourly.get(fname) or []
        idx = self.now_index + offset
        if 0 <= idx < len(series) and isinstance(series[idx], (int, float)):
            return float(series[idx])
        return None


# A builder reads the hourly arrays plus its own config block from _fields.yaml.
Builder = Callable[[DerivedContext, dict[str, Any]], Optional[float]]


def _window(cfg: dict[str, Any], default: int) -> int:
    return int(cfg.get("hours", default))


def _sum_ahead(fname: str, hours: int) -> Builder:
    def build(ctx: DerivedContext, cfg: dict[str, Any]) -> Optional[float]:
        values = ctx.ahead(fname, _window(cfg, hours))
        return round(sum(values), 1) if values else None

    return build


def _max_ahead(fname: str, hours: int) -> Builder:
    def build(ctx: DerivedContext, cfg: dict[str, Any]) -> Optional[float]:
        values = ctx.ahead(fname, _window(cfg, hours))
        return max(values) if values else None

    return build


def _min_ahead(fname: str, hours: int) -> Builder:
    def build(ctx: DerivedContext, cfg: dict[str, Any]) -> Optional[float]:
        values = ctx.ahead(fname, _window(cfg, hours))
        return min(values) if values else None

    return build


def _pressure_now(ctx: DerivedContext, cfg: dict[str, Any]) -> Optional[float]:
    return ctx.current.get("pressure_msl") or ctx.at("pressure_msl", 0)


def _pressure_change(ctx: DerivedContext, cfg: dict[str, Any]) -> Optional[float]:
    now = _pressure_now(ctx, cfg)
    before = ctx.at("pressure_msl", -_window(cfg, 3))
    return round(now - before, 1) if now is not None and before is not None else None


def _gust_ratio(ctx: DerivedContext, cfg: dict[str, Any]) -> Optional[float]:
    gust = ctx.current.get("wind_gusts_10m")
    mean = ctx.current.get("wind_speed_10m")
    if gust is None or mean is None:
        return None
    return round(gust / max(float(mean), 1.0), 2)


def _code_hours_ahead(ctx: DerivedContext, cfg: dict[str, Any]) -> Optional[float]:
    """Hours whose weather code falls in the code group named by this signal."""
    codes = ctx.ahead("weather_code", _window(cfg, 12))
    if not codes:
        return None
    wanted = {int(c) for c in cfg.get("codes", ())}
    return float(sum(1 for c in codes if int(c) in wanted))


DERIVED_BUILDERS: dict[str, Builder] = {
    "precip_next_6h": _sum_ahead("precipitation", 6),
    "precip_next_24h": _sum_ahead("precipitation", 24),
    "max_precip_prob_next_12h": _max_ahead("precipitation_probability", 12),
    "pressure_now": _pressure_now,
    "pressure_change_3h": _pressure_change,
    "gust_ratio": _gust_ratio,
    "heavy_rain_hours_next_12h": _code_hours_ahead,
    "min_visibility_next_6h": _min_ahead("visibility", 6),
}


def compute_derived(
    ctx: DerivedContext, configs: "dict[str, dict[str, Any]] | Iterable[str]"
) -> dict[str, Optional[float]]:
    """Run the registered builder for each declared derived signal.

    `configs` is the `derived:` mapping from _fields.yaml (name -> config block).
    A bare iterable of names is accepted so unit tests can stay terse.
    """
    if not isinstance(configs, dict):
        configs = {name: {} for name in configs}
    return {name: DERIVED_BUILDERS[name](ctx, cfg or {}) for name, cfg in configs.items()}


# --------------------------------------------------------------------------- #
# Hourly aggregation (policy for how a multi-hour window collapses to one value)
# --------------------------------------------------------------------------- #
def aggregate(values: list[Any], how: str) -> Any:
    """Collapse a list of hourly values. `how` comes from sops/_fields.yaml."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    if how == "set":
        return sorted({int(v) for v in clean})
    if how == "max":
        return max(clean)
    if how == "min":
        return min(clean)
    if how == "sum":
        return round(sum(clean), 1)
    if how == "mean":
        return round(sum(clean) / len(clean), 1)
    raise ValueError(f"unknown aggregate `{how}`")


def demo() -> None:
    """Self-check for the evaluator, the operators and the derived builders."""
    vals = {"wind_speed_10m": 45, "wind_gusts_10m": 30, "weather_code": [3, 95], "uv_index": None}

    assert evaluate({"wind_speed_10m": {"gte": 40}}, vals).ok
    assert not evaluate({"wind_speed_10m": {"gte": 50}}, vals).ok
    # any_of passes on one leaf; matched counts only the passing branch
    res = evaluate({"any_of": [{"wind_speed_10m": {"gte": 40}}, {"wind_gusts_10m": {"gte": 55}}]}, vals)
    assert res.ok and res.matched == 1, res
    # all_of needs every leaf; a failed tree reports zero matched conditions
    res = evaluate({"all_of": [{"wind_speed_10m": {"gte": 40}}, {"wind_gusts_10m": {"gte": 55}}]}, vals)
    assert not res.ok and res.matched == 0
    # list-valued field + `in` = any overlap
    assert evaluate({"weather_code": {"in": [95, 96, 99]}}, vals).ok
    assert not evaluate({"weather_code": {"in": [45, 48]}}, vals).ok
    # a missing value is a failed leaf, never a pass, and is reported
    res = evaluate({"uv_index": {"gte": 8}}, vals)
    assert not res.ok and res.missing == ["uv_index"]
    res = evaluate({"uv_index": {"lt": 8}}, vals)
    assert not res.ok, "missing values must not satisfy `lt` either"
    assert evaluate({"wind_speed_10m": {"between": [40, 50]}}, vals).ok
    assert evaluate({"not": {"wind_speed_10m": {"gte": 50}}}, vals).ok
    assert collect_fields({"all_of": [{"a": {"gt": 1}}, {"any_of": [{"b": {"lt": 2}}]}]}) == {"a", "b"}

    ctx = DerivedContext(
        current={"pressure_msl": 998.0, "wind_gusts_10m": 60.0, "wind_speed_10m": 25.0},
        hourly={
            "precipitation": [1.0] * 30,
            "pressure_msl": [1004.0, 1003.0, 1002.0, 1001.0, 1000.0, 999.0, 998.0],
            "weather_code": [95, 65, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3],
            "precipitation_probability": [10.0, 80.0, 20.0],
            "visibility": [5000.0, 800.0, 9000.0],
        },
        times=[f"2026-01-01T{h:02d}:00" for h in range(30)],
        now_index=0,
    )
    assert compute_derived(ctx, ["precip_next_6h"])["precip_next_6h"] == 6.0
    assert compute_derived(ctx, ["precip_next_24h"])["precip_next_24h"] == 24.0
    assert compute_derived(ctx, ["gust_ratio"])["gust_ratio"] == 2.4
    heavy = {"heavy_rain_hours_next_12h": {"codes": [65, 67, 82, 95, 96, 99]}}
    assert compute_derived(ctx, heavy)["heavy_rain_hours_next_12h"] == 2.0
    # the code list is config, not code: an empty group counts nothing
    assert compute_derived(ctx, {"heavy_rain_hours_next_12h": {"codes": []}})[
        "heavy_rain_hours_next_12h"] == 0.0
    assert compute_derived(ctx, ["max_precip_prob_next_12h"])["max_precip_prob_next_12h"] == 80.0
    assert compute_derived(ctx, ["min_visibility_next_6h"])["min_visibility_next_6h"] == 800.0
    # no history available at index 0 -> honest None rather than a guess
    assert compute_derived(ctx, ["pressure_change_3h"])["pressure_change_3h"] is None
    ctx.now_index = 6
    assert compute_derived(ctx, ["pressure_change_3h"])["pressure_change_3h"] == -3.0

    assert aggregate([1, 5, 3], "max") == 5
    assert aggregate([1, 5, 3], "min") == 1
    assert aggregate([1.1, 2.2], "sum") == 3.3
    assert aggregate([3, 95, 3], "set") == [3, 95]
    assert aggregate([None, None], "max") is None
    print("conditions.py self-check OK")


if __name__ == "__main__":
    demo()
