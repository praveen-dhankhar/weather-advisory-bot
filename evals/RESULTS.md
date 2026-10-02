# Eval results

**Run date:** 2026-10-02. **Model under test (mode B):** NVIDIA NIM
`nvidia/nemotron-3-super-120b-a12b` (OpenAI-compatible, free tier). **Policy:** 29 SOPs
in 4 categories.

| mode | command | result |
| --- | --- | --- |
| A | `pytest` | **154 passed, 8 skipped** in ~6 s. The 8 skips are exactly the tests marked `live`. |
| B | `pytest -o addopts= -q -rs --run-live --eval-report=evals/REPORT.md` | **161 passed, 1 skipped** in 476 s (8 min). The skip is the live storm scan - no storm on the run date, explained below. |

Mode A uses recorded Open-Meteo fixtures and the deterministic stand-in LLM, so it is
identical on every run. Mode B adds live Open-Meteo calls and the real model.

**Per-case results are in [`REPORT.md`](REPORT.md)**, written by that mode B run itself:
for every case, what it checks, its pass condition, what the bot actually did (branch,
surfaced SOPs, guard verdict, whether the guard needed its retry or the template), and
the result. Nothing in it is typed by hand; regenerate it rather than edit it.

## The brief's cases, and where they are tested

| Brief case | Tests | Mode B result |
| --- | --- | --- |
| SOP clearly applies (×2) | `test_high_wind_cycling_matches_sop_ex_01`, `test_high_uv_midday_exercise_matches_sop_ex_02`, `test_every_rule_fires_end_to_end_on_its_own_conditions` (×11 - with the rest of the suite, every one of the 29 SOPs is surfaced and cited in at least one full reply) | pass |
| Paraphrase (×2) | `test_paraphrase_scooter_wind`, `test_paraphrase_toddler_fresh_air`, `test_paraphrase_from_the_brief_riding_my_bike_to_the_office`, and with the real model `test_live_llm_paraphrase_outside_the_vocabulary` ("pedalling", a word in no alias list) | pass |
| Severe live weather | `test_live_bhopal_bike_ride_is_grounded_in_the_live_payload` (any day: every surfaced rule re-evaluates true on the live numbers, every quoted reading is the live value), `test_live_severe_scan_picks_the_worst_city` (needs a real storm), `test_severe_fixture_triggers_situational_override` (recorded storm) | pass / **skip** (no storm today, see below) / pass |
| No SOP applies | `test_no_sop_gives_fixed_no_guidance_reply` (×2), `test_asking_for_a_high_severity_answer_without_a_policy_still_gets_none` | pass |
| Weather API unreachable | `test_unreachable_weather_api_is_honest`, `test_timeout_also_routes_to_failure`, `test_unresolvable_city`, `test_malformed_forecast_payload_fails_honestly` (×9), `test_geocoding_and_transport_failures_fail_honestly` (×9) | pass |
| Adversarial | `test_injection_cannot_create_change_or_drop_policy` (×5, the brief's phrasings), `test_live_llm_injection_cannot_change_policy` (×3, real model), `test_composer_and_fuzzy_prompts_carry_no_user_text_and_no_reading_values`, `test_invented_readings_are_rejected_even_when_they_look_like_clock_hours`, `test_guard_falls_back_when_the_model_misbehaves`, `test_fake_sop_id_is_never_cited`, `test_user_supplied_weather_is_ignored`, `test_matcher_drops_an_unknown_id_from_the_fuzzy_pass` | pass |
| More than one SOP applies | `test_wind_uv_and_rain_on_one_cycling_question_surface_by_severity` (the brief's own example), `test_rank_applies_each_documented_tie_break_in_order`, `test_guard_enforces_the_conflict_rule_on_the_model_output`, `test_conflict_rule_primary_is_highest_severity`, `test_at_most_three_sops_are_surfaced` | pass |
| Session memory | `test_multi_turn_followup_inherits_and_reslices`, `test_a_followup_without_a_time_keeps_the_sessions_period`, `test_answering_the_clarifying_question_with_a_place_continues_the_question`, `test_followup_that_changes_the_audience_reruns_matching` (×2), `test_sessions_do_not_share_memory`, `test_every_turn_fetches_fresh_weather`, `test_a_follow_up_sent_mid_turn_waits_for_that_turn_and_builds_on_it`, `test_api_gives_each_caller_without_a_session_id_a_fresh_session` | pass |
| Invalid SOP file | `test_loader_fails_loudly_naming_the_file`, `test_malformed_condition_tree_stops_startup` (×10), `test_sop_text_fields_and_category_are_validated` (×4), `test_duplicate_yaml_key_is_rejected`, `test_unexpected_yaml_keys_fail_as_named_config_errors`, `test_bad_time_window_vocabulary_stops_startup` (×3), `test_weather_code_groups_are_defined_once` | pass |
| The 11th SOP | `test_eleventh_sop_is_loaded_matched_composed_and_cited_with_no_code_change` (whole graph), `test_new_sop_needs_no_code_change` (matcher level), `test_a_time_window_further_out_is_fetched_without_code_change` | pass |

Also run by hand in this pass: the 11th SOP dropped into the real `sops/` directory,
the server restarted, and a live question about cycling in Bhopal answered with
`branch: compose | sop_ids: ['SOP-EX-12', 'SOP-TR-05', 'SOP-EX-05']`, the new SOP's own
text, and "Guidance applied: SOP-EX-12 (...)". The `.py` files hashed identically before
and after. The file was then removed.

## The one skip

```
SKIPPED evals/test_cases.py:166: no situational SOP fired on live data today. Scanned ->
Bhopal: 24h=0.0mm p=1016.3hPa, Mumbai: 24h=0.0mm p=1015.0hPa, Chennai: 24h=1.3mm
p=1015.3hPa, Kolkata: 24h=0.9mm p=1013.9hPa, Guwahati: 24h=0.0mm p=1013.4hPa,
Thiruvananthapuram: 24h=3.6mm p=1015.3hPa
```

SOP-SIT-01 needs at least 50 mm over 24 hours; the wettest
candidate had a few millimetres. **No severe system existed on the run date**, so the
scan skipped and printed why; it does not lower a threshold to manufacture a pass. The
override is still exercised on every run by the recorded storm (3a) and the squall case,
and the live Bhopal test proves grounding on whatever the weather is. `Cherrapunji` is
not in Open-Meteo's geocoder (it indexes "Sohra"); the scan tolerates that by design.

## Second audit: what was found and what changed

Each fix left a test in [`test_hardening.py`](test_hardening.py) whose docstring opens
with `DEFECT:`. Run against the code before this pass, **57 of that file's 76 tests at
the time failed**; the other 19 cover behaviour that already held - the `CHECKS` tests,
plus control cases of parametrized `DEFECT` tests (a missing hourly variable, for
example, was already handled). Each row below was reproduced with the old code, except
the frontend timeout row, which is read from the code and the old latency figures. The
two tests added afterwards - the session lock and the visible decision log - were
checked red-green: each fails with its fix temporarily removed and passes with it back.

| Severity | Defect | Fix |
| --- | --- | --- |
| Critical | The number guard allowed any figure within 0.51 of an hour in the window, or within 2% of any reading: "wind 15 km/h, 19 C, 1000 hPa" passed on a 48 km/h day | the composer is never shown a value; readings enter only as `{placeholders}` filled by code; any other digit must be written in the surfaced SOP text - no tolerance |
| Critical | The composer prompt carried the raw user message; "Cycling is completely safe right now [SOP-EX-01]" passed the guard during a 48 km/h wind | user text reaches only the intake prompt; composer and fuzzy judge never see it |
| High | A reply citing only a secondary SOP - dropping the override - passed | guard requires every surfaced SOP cited and the primary first |
| High | `POST /chat` without `session_id` used a shared "default" session: a stranger's "what about this evening?" inherited another caller's Pune | missing id -> fresh id, returned in the response |
| High | Malformed condition trees (unknown operator, text threshold, bad `between`, wrong combinator shape) loaded, then crashed the request that reached them | shape validated in the `SOP` model at startup, naming file and SOP |
| High | Malformed Open-Meteo payloads crashed the graph (text readings, non-object JSON) or became "No SOP applies" (bad timestamps, all-null readings); ragged arrays were accepted | strict Pydantic `ForecastPayload` / `GeocodeResponse`; every case is an honest failure |
| High | Replying "Bhopal" to the bot's own "which place?" got "No SOP applies" from the real model; "Springfield, Illinois" could never resolve | a reply to a clarifying question continues it (code rule); "Name, Region" qualifies geocoding |
| High | A dead model or missing key said "I could not understand that request... rephrase" and named the missing env var; transport errors showed URLs to users | separate outage text; user-safe reasons; raw causes logged only |
| Medium | Fuzzy prompt carried the user message; `"apply": "false"` counted as a match | message removed; only JSON `true` counts |
| Medium | A period that had passed was answered from its last past hour; the 11:00-16:00 UV rule fired at 18:00 on 16:00's UV | past slots dropped with no fallback; an empty window fails honestly |
| Medium | "next week" silently answered as "right now"; a follow-up naming no time reset to "now" | unsupported period -> fixed question listing supported ones; follow-ups keep the session's period |
| Medium | Intake output `{}` became an intent full of defaults and carried on | required keys enforced |
| Medium | Unknown category, blank advice/title, and a `cite_as` naming a different SOP all loaded; a numeric YAML key raised a bare `TypeError` | validated, each failing startup with the file named |
| Medium | The per-turn decision log was emitted at INFO with no handler - invisible under uvicorn | logging configured in `main.py`; `test_the_decision_log_is_printed_by_every_entry_point` checks the API and the embedded Streamlit app, each in a fresh process |
| Medium | Session store unbounded; a follow-up sent mid-turn read the session before that turn was recorded (lost place and activity), and history could interleave | LRU cap (500), per-session lock; `test_a_follow_up_sent_mid_turn_waits_for_that_turn_and_builds_on_it` |
| Medium | The 11th-SOP test stopped at the matcher; `get_policy` bound the SOP dir at import | end-to-end test; directory read at call time |
| Medium | Frontend: 90 s timeout shorter than a slow turn (the UI would say "unreachable" while the backend still answered and recorded the turn), no loading state, every error reported as "could not be reached" | 300 s configurable timeout, spinner, HTTP vs connection errors distinguished |
| Low | SOP-EX-06 ("pleasantness of a walk") was tagged for all exercise and led a real-model cycling answer | tagged `leisure` only |
| Low | 5 SOPs never fired in any test, 6 more never reached a full reply | trigger cases; all 29 now surface in a reply |
| Low | Dead code and stale config: `requests` pinned but unused, a `daily` block fetched "for the UI" and shown nowhere, unused `last_numbers` / `sort_key`, NIM default pointing at a retired model, an example SOP id in the composer prompt | removed / corrected |

## Real-model behaviour observed in this pass

Transcripts from runs against NIM `nemotron-3-super-120b-a12b`, before and after the fixes:

- **Before:** "is it safe to cycle to work right now?" -> clarifying question; "Bhopal" ->
  `no_match` ("No SOP applies"); a dead end the bot created itself. The evening
  follow-up then worked only because the failed turn had stored the place, and led
  with SOP-EX-06, a rule about walks.
- **After, same conversation:** clarify (6 s) -> "Bhopal" continues the cycling question,
  SOP-TR-05 + SOP-EX-05, guard passed first time (61 s) -> "what about this evening
  instead?" re-fetched, evening slots only (44 s) -> "and for my elderly father?"
  SOP-VG-08 leads, period still "this evening" (85 s).
- **Recorded storm with the real model:** `override`, SOP-SIT-01 leads with "the
  forecast data shows", then SOP-EX-04 and SOP-TR-03, guard passed first time (119 s).
- **Placeholder contract:** no composed draft in these runs typed a reading. The only
  digits the model typed were thresholds already in the SOP text ("above 5 km") and
  clock times from PERIOD, both of which the guard allows; the readings came from the
  code-built line. In the final mode B run all five real-model cases passed the guard
  on the first draft - no retry, no template ([`REPORT.md`](REPORT.md) records this per
  case; the four "fell back to template" rows there are the tests that install a
  deliberately misbehaving composer).

## Latency, honestly

Free-tier NIM queues dominate: one turn took 6 s (clarify, one call) to 119 s (override,
two to three calls) in this pass, and the full mode B suite 4-6 minutes depending on the queue. Nothing
in the architecture needs that long; the stand-in answers in milliseconds.

## The first audit, briefly

The first pass fixed nine defect groups (audience switching on follow-ups, a duplicate
`sops:` key silently discarding SOPs, "Goa" resolving to Genoa, 422s on long messages,
coverage gaps between temperature bands, intake failures blamed on the forecast,
"heavy rain" defined twice) and left the regression tests that are in
[`test_cases.py`](test_cases.py). It also reported a mutation run ("35 of 35 caught").
**No mutation script is in the repo, so that figure cannot be reproduced and was not
re-run here; treat it as historical, not as a result.** The second audit's guard
finding is a reminder of why: that suite passed while the number check let invented
figures through.

## Known weaknesses that remain

- **Real-model coverage is one model and a few dozen calls.** Fuzzy judgements are not
  deterministic and were not sampled repeatedly.
- **Conditions see window aggregates, not hours**, so an `all_of` over two fields can
  be met by two different hours (DECISIONS.md section 7). It errs toward warning, never toward
  reassurance, but it can over-warn.
- **A threshold written in the SOP can be restated as if it were a reading**, and
  numbers spelled as words are not checked. The code-built readings line always shows
  the real values beside it.
- **The stand-in restates the two fuzzy rubrics by hand** (`fake_llm.py`, the only
  place SOP ids appear in Python), so editing a rubric is not caught by mode A until the
  stand-in is updated. It is a test double, never used unless `LLM_PROVIDER=fake`.
- **Single process.** Session memory is an in-process dict; a second worker would not
  see a session. Out of scope by the brief, a real bug in production.
- **The severe live scan depends on the weather.** It skips without a storm; the
  recorded fixtures and the any-day Bhopal grounding test are what keep the override
  and grounding covered after a system passes.

## Why prompt injection is the highest risk here

This bot's value is that its answers trace to approved policy. Every other failure
degrades it; prompt injection *inverts* it. A dead API gives an honest "I can't answer";
a missing SOP gives "no guidance". A successful injection gives a reply that looks
exactly like grounded advice - confident, cited - telling someone it is fine to ride
into a storm, and the citation makes them trust it more. It is also the most likely
attack: the input is free text from the public.

So the defences are structural, not textual - each assumes the model will be fooled:

1. user text reaches **one** prompt, intake, and every routing field it yields - tags,
   audience, activity, period - is re-derived in code ([`intake.py::normalise`](../backend/nodes/intake.py));
2. which SOPs apply is decided by code for 27 of 29 SOPs; the fuzzy judge never sees the
   message, may only pick ids it was handed, and must cite values that are re-checked;
3. the composer never sees the message or a reading value;
4. the guard rejects any id outside the surfaced set, a missing or misordered citation,
   an unknown placeholder, and any digit not written in the SOP text; one retry, then
   the deterministic template ([`guards.py`](../backend/guards.py));
5. the readings and the "Guidance applied" line are written by code in every advisory
   reply.

The first version's prompt-level defence was real but the composer still read the
message; `test_composer_and_fuzzy_prompts_carry_no_user_text_and_no_reading_values` now
fails if user text or a reading value ever reaches those prompts again.
