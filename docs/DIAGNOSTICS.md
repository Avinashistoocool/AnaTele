# Diagnostics reference

This document tells how AnaTele changes records into findings and hypotheses in sequence. It also tells how to read each result.

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

1. **AnaTele keeps observations and interpretations separate.** A STATUSTEXT with the text "Crash: Disarming" is an observation. "A motor failed" is an interpretation. AnaTele never gives an interpretation as a fact.
2. **Each finding has evidence** (record ids, with links to the raw bytes). Each finding also has a list of the *missing* data that can make it stronger.
3. **Missing data means unknown.** If a rule cannot answer, it gives `unassessable`. It never gives "OK".
4. **AnaTele gives no probabilities without labelled outcomes.** Each hypothesis gets a screening order and a label, not a percentage.
5. **Firmware meaning has versions.** For example, in Copter 4.7.2, `AngErr` is a tracking error of the thrust vector, not a yaw error. Also, `ARMING_SKIPCHK` bit 7 skips the board-voltage check, not the compass check.

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

The diagnosis always uses the observations at full resolution. It never uses the plot envelopes.

## Flight phases

AnaTele finds each armed episode from the armed flag in the vehicle HEARTBEAT, or from DataFlash `ARM`. For each armed episode, it gives one of these statuses:

| Status | Meaning |
|---|---|
| `airborne_reported` | EXTENDED_SYS_STATE reported *In air* or *Taking off*. |
| `airborne_inferred` | The altitude above home increased by ≥ 1 m above the median of the first 2 s. **Also**, throttle ≥ 10 % or a motor output ≥ 25 % occurred in the 5 s before the increase. The takeoff time is set back to the last sample that is ≤ 0.2 m above the baseline. |
| `no_airborne_evidence` | Armed, but no applicable increase in altitude. AnaTele cannot tell the difference between a hover at a low altitude and noise in the estimate. |
| `unassessable` | The episode has no altitude above home. |

Derived times:

- **Attitude excursion:** the first time that |roll| or |pitch| > 60° after takeoff (a possible loss of control or impact).
- **Pre-event window:** from takeoff to the earlier of the excursion and *crash report − 3 s*. The rules that ask "did this occur **before** the upset?" use this window.
- **Event and after:** from the end of the pre-event window to disarm. AnaTele uses it for contrast, for example "vibration 66 at impact, 24 before".

Each phase records all thresholds in its `config`. The thresholds are analysis decisions. They are not firmware behaviour.

## Rule states

| State | Meaning |
|---|---|
| `triggered` | The condition occurred. The result becomes a finding with evidence. |
| `not_triggered` | The data was sufficient, and the condition did not occur. This is **not** a health certificate. |
| `unassessable` | The data could not answer. Possible causes: missing signals, too few samples, or a sample interval that is too long for the persistence criterion. |
| `not_applicable` | The question does not apply (for example, an episode on the ground only, or a profile with no expected parameters). |

## Rule catalogue

The rules are in [`telemetry_platform/rules/core.yaml`](https://github.com/Avinashistoocool/AnaTele/blob/main/telemetry_platform/rules/core.yaml) (pack `core`, version 2).
Scopes: *event* (one result for each anchor event), *source*, *episode* (for each armed episode), *series*, *snapshot* (parameter set).

| Rule id | Scope | Detector | Triggers when |
|---|---|---|---|
| `crash-check-disarm` | event | event_present | The log has a crash-check disarm message. A firmware note explains `AngErr`. |
| `compass-event-before-crash-check` | event | event_sequence | A compass selection, an EKF lane switch or a yaw reset occurred ≤ 20 s before a crash report. A compass ratio > 1 for ≥ 2 s before the event supports it. A tilt error > 30° for ≥ 0.5 s that occurred first **contradicts** it. |
| `prearm-radio-failsafe` | source | event_present | "PreArm: Radio failsafe". The result adds the mapped throttle channel (`rc.ch{RCMAP_THROTTLE}`) near each report, compared with `FS_THR_VALUE`. |
| `arming-rejected-auto-mode` | source | event_present | "Auto mode not armable". |
| `arming-rejected-throttle-high` | source | event_present | "Throttle too high". |
| `gps-configuration-rejected` | source | event_present | "unable to configure band" or "still configuring", with the fix type and h_acc at the same time. |
| `compasses-inconsistent-reported` | source | event_present | "Compasses inconsistent". A report after the incident does not prove that the compasses were inconsistent before the flight. |
| `vibration-persistent-pre-event` | episode | persistence | An IMU axis > the profile threshold (default 30) for ≥ 2 s, or > 60 for ≥ 1 s, in the pre-event window. |
| `clipping-pre-event` | episode | counter_increment | Positive increments of the clipping counter between sequential observations ≤ 3 s apart, in the pre-event window. A decrease counts as a reset. |
| `vibration-peak-sample` | series | peak | One sample is more than the threshold. This signal is weaker and has no context. |
| `tracking-loss` | episode | persistence | Tilt tracking error > 30° for ≥ 0.5 s before the upset. |
| `ekf-compass-innovation-pre-event` | episode | persistence | EKF compass test ratio > 1.0 for ≥ 2 s before the event. The result also gives the value when the vehicle was armed on the ground. An increase only under thrust agrees with interference from the motor current. |
| `motor-ceiling-saturation` | episode | persistence | A motor at its maximum output (`MOT_SPIN_MAX`) for ≥ 1 s before the event. |
| `motor-floor-saturation-pre-event` | episode | persistence | A motor at its minimum airborne output (`MOT_SPIN_MIN`) for ≥ 2 s before the event (the first 2 s after takeoff are not included). |
| `motor-output-imbalance-pre-event` | episode | motor_balance | The spread of the mean motor commands is > 0.12 of the PWM range. AnaTele decomposes the spread into roll/pitch/yaw with the frame geometry. It flags an imbalance that is *predominantly yaw*. |
| `power-monitoring-gap` | source | coverage | No battery-voltage samples while the vehicle was armed. This rule blocks the power hypothesis. |
| `batt-monitor-disabled` | snapshot | parameter | `BATT_MONITOR = 0` (instance 1 only). |
| `fs-gcs-disabled` | snapshot | parameter | `FS_GCS_ENABLE = 0`. This is context, not a fault. |
| `arming-skipchk-board-voltage` | snapshot | parameter | `ARMING_SKIPCHK` bit 7 is set. AnaTele examines this rule only with the Copter 4.7.2 semantic pack. |
| `profile-parameter-mismatch` | snapshot | profile_parameters | The observed values are different from the expected parameters of the profile. |

### Derived indicators used by rules

- **Tilt tracking error:** the angle between the two thrust vectors from the desired and the estimated roll/pitch. AnaTele pairs the samples if they are within 0.25 s. The *meaning* is the same as the crash-check `AngErr` of Copter, but AnaTele uses samples at the telemetry rate.
- **Motor mapping:** `SERVOn_FUNCTION` 33–40 → motor 1–8. The floor and the ceiling come from `MOT_PWM_MIN/MAX` and `MOT_SPIN_MIN/MAX`. Frames: QUAD/X and QUAD/+. AnaTele maps other frames, but it does not decompose them.
- **Mixer decomposition:** a weighted difference of the motor commands (right−left, front−back, CCW−CW props). A persistent negative `ccw_minus_cw` means that the motors with CW props work harder. This agrees with a persistent demand for yaw torque.

## Hypothesis ranking

The same YAML file defines the hypotheses. AnaTele puts them in sequence only for the episodes that have a crash report:

| Hypothesis | Supported by (family, weight) | Contradicted by |
|---|---|---|
| Estimator/magnetic inconsistency | compass-event-before-crash (estimator, 1), ekf-compass-innovation (magnetic, 1), compasses-inconsistent (magnetic, 0.25) | tilt tracking error before the compass event (1) |
| Mechanical/thrust or frame configuration | tracking-loss (control, 1), motor-floor (propulsion, 1), motor-imbalance (propulsion, 0.75), motor-ceiling (propulsion, 0.5) | none |
| Vibration-driven estimation degradation | vibration-persistent (inertial, 1), clipping (inertial, 1) | vibration or clipping examined and *not triggered* before the event (0.5 each) |
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

The limit for each family prevents an increase in the score from repeated or correlated observations (for example, three vibration axes).
The weights are screening decisions from experts. They are **not** calibrated likelihoods. Each hypothesis shows the observation
that can best tell it apart from the others. Examples are the onboard XKF4 innovations, ESC telemetry or a motor test with the propellers removed.

## Mission statuses

AnaTele reconstructs the transactions (`upload`, `download`, `partial_write`, `clear`, `item_read`). It finds the direction,
matches the broadcast ACKs, counts the retransmits and finds the restarts. Each **plan version** has a confidence level:

- `confirmed_by_ack`;
- `observed_onboard`;
- `derived_from_baseline` (a partial write onto a known plan);
- `uncertain` (an interrupted or rejected overwrite, because ArduPilot uploads are not atomic).

AnaTele uses the latest plan version before the episode. For each plan item in that version, it gives one of these statuses:

| Status | Meaning |
|---|---|
| `reported_complete` | AnaTele received a `MISSION_ITEM_REACHED` for that sequence during the episode. |
| `geometry_consistent_with_completion` | The nearest recorded position is within `WP_RADIUS_M` plus the GPS h_acc. This is *not* proof. |
| `completion_not_observed` | No report. Also, there is no position coverage, or the item has no position (takeoff, RTL). |
| `not_visited_in_observed_trajectory` | The position coverage has no gaps of more than 5 s, and the nearest approach is outside the margin. |
| `unassessable` | There is no data for the comparison. |

## Firmware semantic packs

`telemetry_platform/semantics/*.yaml`:

- `ardupilot-common`: the C-cast encoding of parameters (validated for integer values and for the range of the type). It also has volatile counters (`STAT_*`). AnaTele does not report changes to these counters as configuration changes.
- `arducopter-4.7.2`: AnaTele selects this pack from the version string. If the git hash is available, the hash must also match, for example `ArduCopter V4.7.2 (fb773567)`. AnaTele reports a different hash as a probable custom build. The pack contains parameter descriptions, bitmasks and crash-check semantics.

## Data-quality diagnostics

The Limitations section of the manifest and the diagnostics list in the web UI show these items:

| Kind | Meaning |
|---|---|
| `invalid_frame` | MAVLink CRC or decoding failure. AnaTele excludes the frame, and the parser resyncs. |
| `recovered_span` / `unrecovered_tail` | Bytes that AnaTele skipped during the resync. |
| `truncated` | The final frame is incomplete. AnaTele marks the dataset as *partial*. |
| `unknown_definition` / `unsupported_flags` | AnaTele excludes these items. It does not trust them. |
| `capture_clock_regression` / `possible_boot_reset` / `boot_counter_wrap` | Clock discontinuities. They divide the data into epochs. |
| `incomplete_text` / `conflicting_text` | Problems with the reassembly of STATUSTEXT chunks. |
| `dataflash_tail` | DFReader stopped before the end of the file. |
| `series_limit` | The data reached the memory limit for derived indicators. AnaTele did not examine the later samples. |

## Writing a rule

1. Add an entry to `rules/core.yaml`. Use an existing detector type: `event_present`, `event_sequence`, `persistence`,
   `counter_increment`, `peak`, `motor_balance`, `coverage`, `parameter` or `profile_parameters`.
2. Give the entry an `id`, an integer `version`, `severity`, `per` scope, `detail` / `detail_not_triggered` templates and `missing`.
3. Add a synthetic test in `test_engine.py`.
4. If possible, add a simulator scenario that tests the rule.
5. Increase the `version` of the pack. Old analyses keep the pack hash that made them.

Rules cannot execute code. The loader rejects unknown detector types and duplicate ids.

## Known limitations

- Telemetry is usually 2–10 Hz. If the persistence is shorter than the sample interval, AnaTele reports it as unassessable.
- AnaTele does not yet align the clocks of different files. It analyses a tlog and a `.bin` separately.
- AnaTele does not track the primary EKF lane for each core. It has no ESC telemetry rules and no PX4 ULog support.
- In a tlog, the desired attitude comes from NAV_CONTROLLER_OUTPUT. This value can be different from the target of the rate controller.
- The hypothesis weights are not calibrated. Use the sequence only to decide which evidence to examine first.
