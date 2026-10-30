# Diagnostics reference

How AnaTele turns records into findings and ranked hypotheses, and how to read each result.

- [Principles](#principles)
- [Pipeline](#pipeline)
- [Flight phases](#flight-phases)
- [Rule states](#rule-states)
- [Rule catalogue](#rule-catalogue)
- [Hypothesis ranking](#hypothesis-ranking)
- [Mission statuses](#mission-statuses)
- [Firmware semantic packs](#firmware-semantic-packs)
- [Data-quality diagnostics](#data-quality-diagnostics)
- [Writing a rule](#writing-a-rule)
- [Known limitations](#known-limitations)

## Principles

1. **Observations and interpretations stay separate.** A STATUSTEXT saying "Crash: Disarming" is an observation. "A motor failed" would be an interpretation, and AnaTele never states it as fact.
2. **Every finding carries evidence** (record ids, linked to raw bytes) and a list of what is *missing* to strengthen it.
3. **Missing data means unknown.** If a rule can't answer, it says `unassessable`. It never says "OK".
4. **No probabilities without labelled outcomes.** Hypotheses get a screening order and a label, not a percentage.
5. **Firmware meaning is versioned.** For example, Copter 4.7.2 `AngErr` is a thrust-vector tracking error, not a yaw error, and `ARMING_SKIPCHK` bit 7 skips the board-voltage check, not the compass check.

## Pipeline

```text
file ──► parser (CRC, resync, limits) ──► observations (Parquet, byte offsets)
     ──► sources & roles, clock epochs, STATUSTEXT reassembly
     ──► events (arm, mode, text classes, EKF flags, mission, parameter changes)
     ──► signals (normalised) ──► derived: motor mapping, tilt error, mixer axes
     ──► flight phases per armed episode
     ──► rule pack (rules/core.yaml) ──► findings + rule-coverage table
     ──► hypothesis ranking per crash episode
     ──► missions: transactions → plan versions → item execution
     ──► manifest (hashes, versions, limitations)
```

Diagnosis always uses full-resolution observations. Plot envelopes are never used.

## Flight phases

For each armed episode (from the vehicle HEARTBEAT armed flag, or DataFlash `ARM`):

| Status | Meaning |
|---|---|
| `airborne_reported` | EXTENDED_SYS_STATE reported *In air* or *Taking off*. |
| `airborne_inferred` | Home-relative altitude rose ≥ 1 m above the median of the first 2 s, **and** throttle ≥ 10 % or a motor output ≥ 25 % appeared within 5 s before the rise. Takeoff is backdated to the last sample ≤ 0.2 m above baseline. |
| `no_airborne_evidence` | Armed, but no qualifying rise. A very low hover can't be told apart from estimate noise. |
| `unassessable` | No home-relative altitude in the episode. |

Derived times:

- **Attitude excursion:** first |roll| or |pitch| > 60° after takeoff (a possible loss of control or impact).
- **Pre-event window:** from takeoff to the earlier of the excursion and *crash report − 3 s*. Rules that ask "was this happening **before** the upset?" use this window.
- **Event and after:** from the end of the pre-event window to disarm. It is used for contrast, e.g. "vibration 66 at impact, 24 before".

All thresholds are recorded in each phase's `config`. They are analysis choices, not firmware behaviour.

## Rule states

| State | Meaning |
|---|---|
| `triggered` | The condition was met. Becomes a finding with evidence. |
| `not_triggered` | The data was adequate and the condition was not met. This is **not** a health certificate. |
| `unassessable` | The data couldn't answer: signals missing, too few samples, or a sample interval too coarse for the persistence criterion. |
| `not_applicable` | The question doesn't apply (e.g. a ground-only episode, or no expected parameters in the profile). |

## Rule catalogue

Rules live in [`telemetry_platform/rules/core.yaml`](https://github.com/Avinashistoocool/AnaTele/blob/main/telemetry_platform/rules/core.yaml) (pack `core`, version 2).
Scopes: *event* (one result per anchor event), *source*, *episode* (per armed episode), *series*, *snapshot* (parameter set).

| Rule id | Scope | Detector | Triggers when |
|---|---|---|---|
| `crash-check-disarm` | event | event_present | A crash-check disarm message is recorded. Firmware note explains `AngErr`. |
| `compass-event-before-crash-check` | event | event_sequence | Compass selection, EKF lane switch or yaw reset ≤ 20 s before a crash report. Supported by compass ratio > 1 for ≥ 2 s before it; **contradicted** if tilt error > 30° for ≥ 0.5 s came first. |
| `prearm-radio-failsafe` | source | event_present | "PreArm: Radio failsafe". Adds the mapped throttle channel (`rc.ch{RCMAP_THROTTLE}`) near each report, compared with `FS_THR_VALUE`. |
| `arming-rejected-auto-mode` | source | event_present | "Auto mode not armable". |
| `arming-rejected-throttle-high` | source | event_present | "Throttle too high". |
| `gps-configuration-rejected` | source | event_present | "unable to configure band" or "still configuring", with the contemporaneous fix type and h_acc. |
| `compasses-inconsistent-reported` | source | event_present | "Compasses inconsistent". Post-incident reports don't establish pre-flight inconsistency. |
| `vibration-persistent-pre-event` | episode | persistence | Any IMU axis > profile threshold (default 30) for ≥ 2 s, or > 60 for ≥ 1 s, in the pre-event window. |
| `clipping-pre-event` | episode | counter_increment | Positive clipping-counter increments between consecutive observations ≤ 3 s apart in the pre-event window. Decreases count as resets. |
| `vibration-peak-sample` | series | peak | A single sample exceeds the threshold. This is a weaker, context-free signal. |
| `tracking-loss` | episode | persistence | Tilt tracking error > 30° for ≥ 0.5 s before the upset. |
| `ekf-compass-innovation-pre-event` | episode | persistence | EKF compass test ratio > 1.0 for ≥ 2 s pre-event. Also reports the armed-on-ground value: a rise only under thrust is consistent with motor-current interference. |
| `motor-ceiling-saturation` | episode | persistence | A motor at its maximum output (`MOT_SPIN_MAX`) for ≥ 1 s pre-event. |
| `motor-floor-saturation-pre-event` | episode | persistence | A motor at its minimum airborne output (`MOT_SPIN_MIN`) for ≥ 2 s pre-event (excluding the first 2 s after takeoff). |
| `motor-output-imbalance-pre-event` | episode | motor_balance | Spread of mean motor commands > 0.12 of the PWM range. Decomposed into roll/pitch/yaw using the frame geometry; flags *predominantly yaw* imbalance. |
| `power-monitoring-gap` | source | coverage | No battery-voltage samples while armed. Blocks the power hypothesis. |
| `batt-monitor-disabled` | snapshot | parameter | `BATT_MONITOR = 0` (instance 1 only). |
| `fs-gcs-disabled` | snapshot | parameter | `FS_GCS_ENABLE = 0`. Context, not a fault. |
| `arming-skipchk-board-voltage` | snapshot | parameter | `ARMING_SKIPCHK` bit 7 set. Only assessed under the Copter 4.7.2 semantic pack. |
| `profile-parameter-mismatch` | snapshot | profile_parameters | Observed values differ from the profile's expected parameters. |

### Derived indicators used by rules

- **Tilt tracking error:** the angle between the thrust vectors implied by desired and estimated roll/pitch, paired within 0.25 s. It matches the *meaning* of Copter's crash-check `AngErr`, but uses telemetry-rate samples.
- **Motor mapping:** `SERVOn_FUNCTION` 33–40 → motor 1–8. Floor and ceiling come from `MOT_PWM_MIN/MAX` and `MOT_SPIN_MIN/MAX`. Frames: QUAD/X and QUAD/+. Other frames are mapped but not decomposed.
- **Mixer decomposition:** a weighted difference of motor commands (right−left, front−back, CCW−CW props). A sustained negative `ccw_minus_cw` means the CW-prop motors work harder, which corresponds to sustained yaw torque demand.

## Hypothesis ranking

Hypotheses are defined in the same YAML and ranked only for episodes with a crash report:

| Hypothesis | Supported by (family, weight) | Contradicted by |
|---|---|---|
| Estimator/magnetic inconsistency | compass-event-before-crash (estimator, 1), ekf-compass-innovation (magnetic, 1), compasses-inconsistent (magnetic, 0.25) | tilt tracking error preceded the compass event (1) |
| Mechanical/thrust or frame configuration | tracking-loss (control, 1), motor-floor (propulsion, 1), motor-imbalance (propulsion, 0.75), motor-ceiling (propulsion, 0.5) | none |
| Vibration-driven estimation degradation | vibration-persistent (inertial, 1), clipping (inertial, 1) | vibration or clipping assessed and *not triggered* pre-event (0.5 each) |
| Electrical/power degradation | none | blocked by `power-monitoring-gap` |

```text
score = Σ_family min(family_cap = 1.5, Σ weights of triggered rules in that family) − Σ contradiction weights
label = unassessable      if blocked, or no supporting rule could be assessed
        mixed support     if both support and contradictions exist
        strong support    score ≥ 2
        some support      score ≥ 1
        weak support      score > 0
        limited or contradicting evidence otherwise
```

Capping by family stops repeated or correlated observations (e.g. three vibration axes) from piling up.
The weights are expert screening choices, **not** calibrated likelihoods. Each hypothesis lists the observation
that would best discriminate it, for example onboard XKF4 innovations, ESC telemetry or a props-off motor test.

## Mission statuses

Transactions (`upload`, `download`, `partial_write`, `clear`, `item_read`) are reconstructed with direction,
broadcast-ACK matching, retransmit counting and restart detection. **Plan versions** carry a confidence level:
`confirmed_by_ack`, `observed_onboard`, `derived_from_baseline` (a partial write onto a known plan) or `uncertain`
(an interrupted or rejected overwrite, since ArduPilot uploads are not atomic).

For each plan item in the episode's latest prior version:

| Status | Meaning |
|---|---|
| `reported_complete` | A `MISSION_ITEM_REACHED` for that sequence was received during the episode. |
| `geometry_consistent_with_completion` | The nearest recorded position is within `WP_RADIUS_M` plus GPS h_acc. This is *not* proof. |
| `completion_not_observed` | No report, and either no position coverage or a non-positional item (takeoff, RTL). |
| `not_visited_in_observed_trajectory` | Position coverage has no gaps over 5 s, and the nearest approach is outside the margin. |
| `unassessable` | Nothing to compare. |

## Firmware semantic packs

`telemetry_platform/semantics/*.yaml`:

- `ardupilot-common`: C-cast parameter encoding (validated for integrality and type range) and volatile counters (`STAT_*`) that are not reported as configuration changes.
- `arducopter-4.7.2`: matched by version string *and*, when present, the git hash in `ArduCopter V4.7.2 (fb773567)`. A different hash is reported as a probable custom build. The pack holds parameter descriptions, bitmasks and crash-check semantics.

## Data-quality diagnostics

Shown in the Limitations section of the manifest and in the web UI's diagnostics list:

| Kind | Meaning |
|---|---|
| `invalid_frame` | MAVLink CRC or decoding failure. The frame is excluded and the parser resyncs. |
| `recovered_span` / `unrecovered_tail` | Bytes skipped during resync. |
| `truncated` | Incomplete final frame. The dataset is marked *partial*. |
| `unknown_definition` / `unsupported_flags` | Excluded rather than trusted. |
| `capture_clock_regression` / `possible_boot_reset` / `boot_counter_wrap` | Clock discontinuities that split the data into epochs. |
| `incomplete_text` / `conflicting_text` | STATUSTEXT chunk reassembly problems. |
| `dataflash_tail` | DFReader stopped before the end of the file. |
| `series_limit` | The in-memory derived-indicator cap was reached, so later samples were not assessed. |

## Writing a rule

1. Add an entry to `rules/core.yaml` using an existing detector type: `event_present`, `event_sequence`, `persistence`,
   `counter_increment`, `peak`, `motor_balance`, `coverage`, `parameter` or `profile_parameters`.
2. Give it an `id`, an integer `version`, `severity`, `per` scope, `detail` / `detail_not_triggered` templates and `missing`.
3. Add a synthetic test in `test_engine.py`, and if possible a simulator scenario that exercises it.
4. Bump the pack `version`. Old analyses keep the pack hash they were produced with.

Rules can't execute code. The loader rejects unknown detector types and duplicate ids.

## Known limitations

- Telemetry is usually 2–10 Hz. Persistence below the sample interval is reported as unassessable.
- No cross-file clock alignment yet (a tlog and a `.bin` are analysed separately).
- No per-core EKF primary-lane tracking, ESC telemetry rules or PX4 ULog support.
- Desired attitude in a tlog comes from NAV_CONTROLLER_OUTPUT, which may differ from the rate controller's target.
- Hypothesis weights are uncalibrated. Treat the order as a prompt for which evidence to check first.
