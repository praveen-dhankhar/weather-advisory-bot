# Decisions

## 1. What is code, what is the model, and why

| Job | Who does it | Why |
| --- | --- | --- |
| Fetching weather, choosing API fields, slicing time windows | code ([`weather.py`](backend/weather.py)) | numbers must be reproducible and auditable; a model that "remembers" a figure is a liability |
| Deciding whether a numeric threshold is crossed | code ([`conditions.py`](backend/conditions.py)) | a threshold is policy; it belongs in YAML and in an evaluator, not in a prompt |
| Deriving the situational signals | code ([`conditions.py::DERIVED_BUILDERS`](backend/conditions.py)) | arithmetic over a payload; no judgement needed |
| Validating SOP ids | code ([`loader.py`](backend/loader.py), [`matcher.py`](backend/nodes/matcher.py), [`guards.py`](backend/guards.py)) | the id set is the trust boundary; the model never widens it |
| Failure text, no-match text, clarifying question | code ([`nodes/fixed.py`](backend/nodes/fixed.py)) | a model asked to phrase an apology will eventually phrase advice with it |
| Tags, audience, activity and time window actually used | code ([`intake.py::normalise`](backend/nodes/intake.py)) | these drive routing, so they are re-derived from `_vocab.yaml` even though the model proposes them |
| Ranking matched SOPs | code ([`matcher.py::rank`](backend/nodes/matcher.py)) | "which risk leads" is policy |
| Extracting intent from free text | **model** | this is genuinely language work: paraphrase, ellipsis, follow-ups |
| Judging a plain-language rubric (fuzzy SOPs) | **model**, then code verifies | "is this pleasant for a walk" has no single threshold; but the values it used are re-checked |
| Phrasing the reply | **model** | tone and flow; the content is fixed, and the guard proves it |

The dividing line: the model may read language and choose among options the code
gave it. It may not originate a number, an id, a threshold or a recommendation.

## 2. Where each non-negotiable is enforced

| Rule | Enforced in | How |
| --- | --- | --- |
| Advice only from an SOP | `nodes/composer.py::build_prompt` | the prompt contains only matched SOPs' `advice`/`cite_as`; nothing else is supplied to phrase from |
| Every reply cites its SOP | `guards.py::check_reply` (check c) | a reply with no id and without the literal no-SOP sentence fails the guard |
| No invented SOP ids | `matcher.py::match_fuzzy` (drops non-candidate ids, logs it) + `guards.py::check_reply` (check a) | id must be in the candidate set to be matched and in the matched set to be cited |
| No invented numbers | `guards.py::check_reply` (check b) + `guards.py::allowed_numbers` | every figure in the reply must trace to the snapshot, the window values or the SOP text, within rounding tolerance |
| Numbers come from one place | `weather.py::snapshot_from_payload` → `WeatherSnapshot` | the only object carrying values; the composer sees a projection of it and nothing else |
| No guidance when none applies | `graph.py::route_after_match` → `nodes/fixed.py::no_match_node` | fixed text, no LLM call on that branch |
| No guessed location | `graph.py::route_after_intake` → `nodes/fixed.py::clarify_node` | a missing location is a fixed question, never a default city |
| Honest failure | `weather.py` typed errors → `graph.py::route_after_*` → `nodes/fixed.py::failure_node` | `LocationUnresolved` and `WeatherUnavailable` both land on fixed text with no figures |
| No claim of an official alert | SOP text in `sops/situational.yaml` + rule 5 of `COMPOSE_SYSTEM` + an assertion in `test_severe_fixture_triggers_situational_override` | the SOP itself says "this is a reading of forecast data" |
| User text is data | `llm.py::untrusted_block`, used by every prompt builder | the message is delimited, closing tags are defanged, and each prompt states it is untrusted |
| Loader fails loudly | `loader.py` (every raise names the file) | `main.py` calls `get_policy()` at import, so a bad SOP stops the process |

## 3. The conflict rule

```
1. situational SOPs with overrides: true  -> always first
2. then severity rank (critical > high > moderate > low > info)
3. then specificity: more matched leaf conditions, then more matched tags
4. then id, for a stable order
```

Implemented in [`matcher.py::rank`](backend/nodes/matcher.py). The top SOP is the
primary advice; up to **two** more are surfaced as short secondary notes
([`composer.py::MAX_SECONDARY`](backend/nodes/composer.py)); all of them are cited.

Rationale: safety-first (the worst applicable risk is what the user must hear
first), nothing hidden (a lesser matched rule is still shown, so the user is not
silently denied information that fired), and bounded length (three SOPs is a reply
somebody reads; eight is a reply nobody reads). Specificity breaks ties because
between two equally severe rules, the one that matched on more conditions is the
better-targeted one.

Alternatives rejected:

- **Show every matched SOP.** Honest but unusable: the severe fixture matches six
  SOPs, and a wall of text buries the one that matters.
- **Show only the highest-severity SOP.** Shortest, but it hides real matched
  guidance - a user told about the storm and not about the child heat threshold has
  been under-informed by the system's own policy.
- **Let the LLM pick the primary.** Rejected outright: "which risk leads" is exactly
  the judgement the brief forbids the model from making.
- **Severity sum / risk score.** Tempting, but it invents a number nobody authored
  and makes two moderate rules outrank one critical one. Not defensible.

## 4. Fuzzy SOPs

`kind: fuzzy` carries a `fuzzy_criteria` rubric in plain language instead of a
condition tree, because "is it pleasant for a walk" is a judgement, not a threshold.
The flow ([`matcher.py::match_fuzzy`](backend/nodes/matcher.py)):

1. the tag filter picks the fuzzy SOPs that could apply at all;
2. the model is given **only** those ids, their rubrics, and the window's values from
   the snapshot. It returns `apply` plus the field values it relied on;
3. code drops any id that was not a candidate, and drops any match whose cited
   values disagree with the snapshot (`_value_agrees`, 2% or 0.51 absolute
   tolerance). A match that cites no fields at all is also dropped - citing is the
   price of being believed;
4. survivors enter the same ranking as everything else.

So a fuzzy SOP can be wrong about taste, but it cannot be wrong about facts, and it
cannot smuggle in an id. `test_fuzzy_match_requires_verifiable_field_values` drives
exactly this path with a model that lies about a value.

## 5. Situational SOPs and the override

Open-Meteo is a forecast model. It has no IMD feed, no civic alerts, no official
warnings, so the bot does not pretend to have them. Instead
[`conditions.py`](backend/conditions.py) derives signals from the payload -
`precip_next_24h`, `precip_next_6h`, `pressure_now`, `pressure_change_3h`,
`gust_ratio`, `heavy_rain_hours_next_12h`, `min_visibility_next_6h`,
`max_precip_prob_next_12h` - and a `kind: situational` SOP declares which
combination counts as a system, in YAML:

```yaml
signals:
  all_of:
    - precip_next_24h: {gte: 50}
    - any_of:
        - pressure_now: {lte: 1002}
        - pressure_change_3h: {lte: -2.5}
        - heavy_rain_hours_next_12h: {gte: 2}
```

That fires when no single reading looks extreme - 2 mm in the current hour with a
falling barometer and 50 mm banked over a day is a situation, not a drizzle. With
`overrides: true` the SOP sorts above everything, the graph routes to the `override`
node, and the reply leads with the system before the activity advice. No event name,
city or date appears anywhere in the code or the YAML, and the SOP text itself says
it is a reading of forecast data rather than an authority's warning.

## 6. What is data-driven, honestly

**Pure data changes, zero code:**

- a new SOP of any existing `kind`, using any already-declared field or derived
  signal (`test_new_sop_needs_no_code_change` proves it)
- retuning any threshold, severity, title, advice text or citation string
- new nesting of `all_of` / `any_of` / `not` with the existing operators
- a new activity, alias or tag (`sops/_vocab.yaml`) - including the tags an SOP
  targets
- a new relative time window such as `late_afternoon` (`sops/_vocab.yaml`)
- a new raw Open-Meteo variable: add a block to `sops/_fields.yaml` and
  `weather.py` fetches it, aggregates it per that block's `aggregate`, and exposes
  it to conditions. No Python edit.

**Needs code:**

- a new *operator* (e.g. `regex`, `rising_over`): add it to `conditions.OPERATORS`.
- a new *kind* of derived signal: add a builder to `conditions.DERIVED_BUILDERS`.
  The loader refuses to start if `_fields.yaml` declares a derived signal with no
  builder, so this fails loudly rather than silently.
- a new aggregation rule beyond `max/min/sum/mean/set`.
- a fifth routing branch, or a new `kind` of SOP.
- the stand-in LLM's fuzzy thresholds in `fake_llm.py` mirror the rubric text by
  hand. With a real model the rubric is the only source; with `LLM_PROVIDER=fake`
  the rubric and the stand-in can drift apart. It is a test double, not policy.

## 7. Honest gaps

- **Geocoding takes the first hit.** "Bhopal" has three Open-Meteo matches; the bot
  takes the first and tells you which place it used (`ResolvedLocation.label` is in
  every reply and in `facts.place`). It does not disambiguate, and a user who means
  the smaller Bhopal in Uttar Pradesh will get the wrong forecast.
- **Open-Meteo is not an alert source.** Everything the bot says about "a system" is
  inference from a forecast model. Nobody should evacuate on its say-so, and the
  situational SOP text says as much.
- **The fuzzy pass is non-deterministic.** Same numbers, different day, possibly a
  different verdict on a picnic. It is confined to `low` severity SOPs about comfort
  for exactly that reason; no `high` or `critical` SOP is fuzzy.
- **UV has no `current` value.** Open-Meteo only exposes `uv_index` hourly, so the
  "now" window reads the current hour's hourly value. Documented in `_fields.yaml`
  and implemented in `resolve_window`.
- **`weather_code` aggregation is `set`**, and `in`/`not_in` test for any overlap -
  so a window containing one thunderstorm hour counts as a thunderstorm window. That
  is deliberately conservative and will sometimes be over-cautious for a 15-hour
  "today" window.
- **`gust_ratio` is noisy in calm air** (a 7 km/h wind gusting to 20 is a ratio of
  2.8 and means nothing), which is why every SOP using it also demands an absolute
  gust threshold. A ratio-only SOP would misfire.
- **Derived signals are anchored to "now", not to the asked-about window.** Ask
  about tomorrow evening and `precip_next_6h` still means the next 6 hours from now.
  Honest consequence: the situational override is a statement about the current
  system, not about an arbitrary future window.
- **Severity is authored, not computed.** Two SOP authors could disagree; nothing in
  the system detects an inconsistent severity ladder across files.
- **Session memory is a process dict.** Restart and the conversation is gone. No
  eviction beyond the last 8 turns, no cross-process sharing, no auth - so this is
  single-instance only.
- **No real-LLM verification in this run.** The `OPENAI_API_KEY` available in the
  build environment is rejected with HTTP 401, so every result in
  [`evals/RESULTS.md`](evals/RESULTS.md) comes from the deterministic stand-in. The
  live-LLM test exists, skips with the provider's own error text, and has not been
  observed passing. See RESULTS.md for what that does and does not prove.

## 8. A severe-weather test that still works after the storm passes

Two complementary halves, both in [`evals/test_cases.py`](evals/test_cases.py):

1. **Recorded fixtures** (`test_severe_fixture_triggers_situational_override`). A
   real Open-Meteo payload with specific arrays edited to represent a heavy-rain
   system; `evals/fixtures/record.py` documents every edit, and each fixture file
   carries a `_provenance` string. Self-consistent (its "now" is inside its own
   timeline), so it passes identically in a year.
2. **A dynamic live scan** (`test_live_severe_scan_picks_the_worst_city`). Scans a
   candidate list, takes the city with the highest 24h accumulation (tie-break:
   lowest pressure), and asserts the override path on whatever it finds. If nothing
   qualifies it **skips with the figures it saw**, so "no storm today" can never be
   read as "the override works". One unreachable candidate does not fail the scan -
   `Cherrapunji` is deliberately in the list and is unknown to Open-Meteo's
   geocoder, which exercises that path on every run.

What this deliberately does **not** do: hardcode a city, a date, an event name, or a
"known severe" location. The only severe-weather knowledge in the repo is the
threshold combination in `sops/situational.yaml`.

## 9. Smaller calls worth naming

- **An activity with no `_vocab.yaml` entry routes to `no_match`.** Scuba diving gets
  "no SOP applies" rather than generic outdoor advice, because the bot has no policy
  about water. The cost is that a new activity needs a vocabulary line before it can
  be answered - the right trade for a system whose whole promise is not improvising.
- **Past hours are never advised on.** For a same-day window, `resolve_window` drops
  slots earlier than now, so "today" at 18:00 does not warn about noon's UV.
- **The guard gets exactly one retry**, then a deterministic template. A model that
  has broken the rules twice gets no third attempt at creativity.
- **`matched_sop_ids` holds what was surfaced** (primary + up to two secondaries),
  not everything that matched; the full match list stays in the graph state and in
  `trace`, so nothing is lost for debugging.
- **`trace` is returned by the API.** Not a product feature - it is how a reviewer
  checks that the claims above are true without reading the code.
