"""Loads and validates everything in sops/ - the policy, the vocabulary and the
weather-field map.

Every failure raises :class:`SOPConfigError` naming the offending file, and the
FastAPI app calls :func:`get_policy` at import time so a bad SOP stops the
process instead of silently disabling a rule.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import ValidationError

from backend.conditions import DERIVED_BUILDERS, collect_fields
from backend.models import SOP, SOPConfigError

SOP_DIR = Path(__file__).resolve().parent.parent / "sops"
VALID_AGGREGATES = {"max", "min", "sum", "mean", "set"}
VALID_BLOCKS = {"current", "hourly"}
GROUP_OPERATORS = {"in_group": "in", "not_in_group": "not_in"}


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys.

    Plain YAML silently keeps the last of two identical keys. For a policy file that
    means a second `sops:` block discards every SOP above it - the exact mistake
    someone makes when appending a new SOP - with no error at all.
    """


def _no_duplicate_keys(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None,
                f"duplicate key {key!r} - YAML would silently keep only the last one",
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


StrictLoader.construct_mapping = _no_duplicate_keys  # type: ignore[method-assign]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    variable: str
    blocks: tuple[str, ...]
    unit: str
    aggregate: str


@dataclass
class TimeWindowSpec:
    name: str
    label: str
    day_offset: int = 0
    use_current: bool = False
    start_hour: Optional[int] = None
    end_hour: Optional[int] = None


@dataclass
class Policy:
    """The loaded, validated policy. Read-only at runtime."""

    sops: dict[str, SOP]
    fields: dict[str, FieldSpec]
    derived: dict[str, dict[str, Any]]
    daily: list[str]
    tags: dict[str, str]
    audiences: list[str]
    activities: dict[str, dict[str, Any]]
    time_windows: dict[str, TimeWindowSpec]
    code_groups: dict[str, list[int]] = field(default_factory=dict)
    alias_to_activity: list[tuple[str, str]] = field(default_factory=list)

    @property
    def known_fields(self) -> set[str]:
        return set(self.fields) | set(self.derived)

    def unit(self, field_name: str) -> str:
        if field_name in self.fields:
            return self.fields[field_name].unit
        return str(self.derived.get(field_name, {}).get("unit", ""))

    def sop(self, sop_id: str) -> SOP:
        return self.sops[sop_id]


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(), Loader=StrictLoader)
    except FileNotFoundError as exc:
        raise SOPConfigError(f"{path.name}: required policy file is missing ({path})") from exc
    except yaml.YAMLError as exc:
        raise SOPConfigError(f"{path.name}: not valid YAML - {exc}") from exc


def _load_fields(
    path: Path,
) -> tuple[dict[str, FieldSpec], dict[str, dict[str, Any]], list[str], dict[str, list[int]]]:
    raw = _read_yaml(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), dict):
        raise SOPConfigError(f"{path.name}: expected a top-level `fields` mapping")

    specs: dict[str, FieldSpec] = {}
    for name, body in raw["fields"].items():
        if not isinstance(body, dict):
            raise SOPConfigError(f"{path.name}: field `{name}` must be a mapping")
        blocks = tuple(body.get("blocks") or [])
        if not blocks or set(blocks) - VALID_BLOCKS:
            raise SOPConfigError(
                f"{path.name}: field `{name}` has blocks {list(blocks)}; allowed: {sorted(VALID_BLOCKS)}"
            )
        how = body.get("aggregate")
        if how not in VALID_AGGREGATES:
            raise SOPConfigError(
                f"{path.name}: field `{name}` has aggregate {how!r}; allowed: {sorted(VALID_AGGREGATES)}"
            )
        if not body.get("variable"):
            raise SOPConfigError(f"{path.name}: field `{name}` is missing `variable`")
        specs[name] = FieldSpec(name, str(body["variable"]), blocks, str(body.get("unit", "")), how)

    derived = raw.get("derived") or {}
    if not isinstance(derived, dict):
        raise SOPConfigError(f"{path.name}: `derived` must be a mapping")
    unknown = sorted(set(derived) - set(DERIVED_BUILDERS))
    if unknown:
        raise SOPConfigError(
            f"{path.name}: derived signals {unknown} have no builder in "
            f"backend/conditions.py::DERIVED_BUILDERS (a new KIND of signal needs code)"
        )
    overlap = sorted(set(derived) & set(specs))
    if overlap:
        raise SOPConfigError(f"{path.name}: {overlap} declared as both a raw field and a derived signal")

    groups_raw = raw.get("code_groups") or {}
    if not isinstance(groups_raw, dict):
        raise SOPConfigError(f"{path.name}: `code_groups` must be a mapping")
    groups: dict[str, list[int]] = {}
    for name, codes in groups_raw.items():
        if not isinstance(codes, list) or not codes or not all(isinstance(c, int) for c in codes):
            raise SOPConfigError(f"{path.name}: code group `{name}` must be a non-empty list of ints")
        groups[str(name)] = [int(c) for c in codes]

    # Resolve each derived signal's `codes_group` into the literal codes its builder
    # reads, so the group is defined once and cannot drift.
    for name, cfg in derived.items():
        if not isinstance(cfg, dict):
            raise SOPConfigError(f"{path.name}: derived signal `{name}` must be a mapping")
        group = cfg.get("codes_group")
        if group is None:
            continue
        if group not in groups:
            raise SOPConfigError(
                f"{path.name}: derived signal `{name}` names unknown code group {group!r}; "
                f"known groups: {sorted(groups)}"
            )
        cfg["codes"] = groups[group]

    daily = raw.get("daily") or []
    if not isinstance(daily, list):
        raise SOPConfigError(f"{path.name}: `daily` must be a list")
    return specs, derived, [str(d) for d in daily], groups


def _load_vocab(path: Path) -> tuple[dict[str, str], list[str], dict[str, Any], dict[str, TimeWindowSpec]]:
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise SOPConfigError(f"{path.name}: expected a top-level mapping")

    tags = raw.get("tags") or {}
    audiences = raw.get("audiences") or []
    activities = raw.get("activities") or {}
    windows_raw = raw.get("time_windows") or {}
    for key, value, kind in (("tags", tags, dict), ("activities", activities, dict),
                             ("time_windows", windows_raw, dict), ("audiences", audiences, list)):
        if not isinstance(value, kind):
            raise SOPConfigError(f"{path.name}: `{key}` must be a {kind.__name__}")

    for name, body in activities.items():
        if not isinstance(body, dict) or not body.get("aliases") or not body.get("tags"):
            raise SOPConfigError(f"{path.name}: activity `{name}` needs both `aliases` and `tags`")
        bad = sorted(set(body["tags"]) - set(tags))
        if bad:
            raise SOPConfigError(f"{path.name}: activity `{name}` uses undefined tags {bad}")

    windows: dict[str, TimeWindowSpec] = {}
    for name, body in windows_raw.items():
        if not isinstance(body, dict):
            raise SOPConfigError(f"{path.name}: time window `{name}` must be a mapping")
        spec = TimeWindowSpec(
            name=name,
            label=str(body.get("label", name.replace("_", " "))),
            day_offset=int(body.get("day_offset", 0)),
            use_current=bool(body.get("use_current", False)),
            start_hour=body.get("start_hour"),
            end_hour=body.get("end_hour"),
        )
        if not spec.use_current and (spec.start_hour is None or spec.end_hour is None):
            raise SOPConfigError(
                f"{path.name}: time window `{name}` needs start_hour and end_hour unless use_current is true"
            )
        windows[name] = spec
    if "now" not in windows:
        raise SOPConfigError(f"{path.name}: a `now` time window is required")
    return {str(k): str(v) for k, v in tags.items()}, [str(a) for a in audiences], activities, windows


def _expand_groups(node: Any, groups: dict[str, list[int]], where: str) -> None:
    """Rewrite `{field: {in_group: name}}` into `{field: {in: [codes]}}` in place."""
    if not isinstance(node, dict):
        return
    for key, payload in node.items():
        if key in ("all_of", "any_of"):
            for child in payload if isinstance(payload, list) else [payload]:
                _expand_groups(child, groups, where)
        elif key == "not":
            _expand_groups(payload, groups, where)
        elif isinstance(payload, dict):
            for op, plain in GROUP_OPERATORS.items():
                if op in payload:
                    name = payload.pop(op)
                    if name not in groups:
                        raise SOPConfigError(
                            f"{where}: field `{key}` names unknown code group {name!r}; "
                            f"known groups: {sorted(groups)}"
                        )
                    payload[plain] = list(groups[name])


def _load_sops(
    sop_dir: Path,
    policy_fields: set[str],
    tags: set[str],
    audiences: set[str],
    groups: dict[str, list[int]],
) -> dict[str, SOP]:
    sops: dict[str, SOP] = {}
    files = sorted(p for p in sop_dir.glob("*.yaml") if not p.name.startswith("_"))
    if not files:
        raise SOPConfigError(f"{sop_dir}: no SOP files found (expected *.yaml)")

    for path in files:
        raw = _read_yaml(path)
        if not isinstance(raw, dict) or not isinstance(raw.get("sops"), list):
            raise SOPConfigError(f"{path.name}: expected a top-level `sops:` list")
        for index, body in enumerate(raw["sops"]):
            where = f"{path.name}[{index}]"
            if not isinstance(body, dict):
                raise SOPConfigError(f"{where}: each SOP must be a mapping")
            _expand_groups(body.get("conditions"), groups, f"{where} (id={body.get('id', '?')})")
            _expand_groups(body.get("signals"), groups, f"{where} (id={body.get('id', '?')})")
            try:
                sop = SOP(**body, source_file=path.name)
            except ValidationError as exc:
                raise SOPConfigError(f"{where} (id={body.get('id', '?')}): {exc}") from exc
            if sop.id in sops:
                raise SOPConfigError(
                    f"{path.name}: duplicate SOP id {sop.id} (already defined in {sops[sop.id].source_file})"
                )
            if sop.applies_to:
                bad_tags = sorted(set(sop.applies_to.activity_tags_any or []) - tags)
                if bad_tags:
                    raise SOPConfigError(f"{path.name} ({sop.id}): undefined activity tags {bad_tags}")
                bad_aud = sorted(set(sop.applies_to.audience_any or []) - audiences)
                if bad_aud:
                    raise SOPConfigError(f"{path.name} ({sop.id}): undefined audiences {bad_aud}")
            used = collect_fields(sop.rule)
            bad_fields = sorted(used - policy_fields)
            if bad_fields:
                raise SOPConfigError(
                    f"{path.name} ({sop.id}): conditions use unknown weather fields {bad_fields}. "
                    f"Add them to sops/_fields.yaml."
                )
            sops[sop.id] = sop
    return sops


def load_policy(sop_dir: Path | str = SOP_DIR) -> Policy:
    """Load sops/ into a validated :class:`Policy`, or raise ``SOPConfigError``."""
    sop_dir = Path(sop_dir)
    fields, derived, daily, groups = _load_fields(sop_dir / "_fields.yaml")
    tags, audiences, activities, windows = _load_vocab(sop_dir / "_vocab.yaml")
    known = set(fields) | set(derived)
    sops = _load_sops(sop_dir, known, set(tags), set(audiences), groups)

    aliases: list[tuple[str, str]] = []
    for name, body in activities.items():
        for alias in [name, *body["aliases"]]:
            aliases.append((str(alias).lower(), name))
    aliases.sort(key=lambda pair: len(pair[0]), reverse=True)  # longest phrase wins

    return Policy(
        sops=sops, fields=fields, derived=derived, daily=daily, tags=tags, audiences=audiences,
        activities=activities, time_windows=windows, alias_to_activity=aliases,
        code_groups=groups,
    )


@functools.lru_cache(maxsize=4)
def get_policy(sop_dir: Path | str = SOP_DIR) -> Policy:
    """Process-wide cached policy. Call at startup so bad SOPs fail loudly."""
    return load_policy(sop_dir)


if __name__ == "__main__":
    p = get_policy()
    print(f"{len(p.sops)} SOPs loaded from {SOP_DIR}")
    for sop in sorted(p.sops.values(), key=lambda s: s.id):
        print(f"  {sop.id:<12} {sop.severity.value:<9} {sop.kind:<12} {sop.title}")
    print(f"fields: {len(p.fields)}  derived: {len(p.derived)}  "
          f"tags: {len(p.tags)}  activities: {len(p.activities)}  windows: {len(p.time_windows)}")
