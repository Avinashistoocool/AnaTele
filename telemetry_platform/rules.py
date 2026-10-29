"""Declarative, versioned rule evaluation.

Rules live in telemetry_platform/rules/*.yaml and use a bounded set of detector types.
No rule can execute code. Every rule result has one of four states and keeps its evidence.
"""
import hashlib
import re
from pathlib import Path

import yaml

from .derived import phase_windows

RULE_DIR = Path(__file__).parent / "rules"
STATES = ("triggered", "not_triggered", "unassessable", "not_applicable")
SEVERITIES = {"critical", "error", "warning", "notice", "info"}
SCOPES = {"event", "source", "episode", "series", "snapshot"}
MAX_EVIDENCE = 100


class RuleError(ValueError):
    pass


def load_pack(path=None):
    path = Path(path or RULE_DIR / "core.yaml")
    raw = path.read_bytes()
    pack = yaml.safe_load(raw)
    validate(pack)
    pack["sha256"] = hashlib.sha256(raw).hexdigest()
    return pack


def validate(pack):
    if not isinstance(pack, dict) or not isinstance(pack.get("rules"), list):
        raise RuleError("Rule pack needs a 'rules' list")
    seen = set()
    for rule in pack["rules"]:
        rid = rule.get("id")
        if not rid or rid in seen:
            raise RuleError(f"Missing or duplicate rule id: {rid}")
        seen.add(rid)
        if not isinstance(rule.get("version"), int):
            raise RuleError(f"{rid}: integer version required")
        if rule.get("severity") not in SEVERITIES or rule.get("per") not in SCOPES:
            raise RuleError(f"{rid}: invalid severity or scope")
        if (rule.get("detector") or {}).get("type") not in DETECTORS:
            raise RuleError(f"{rid}: unknown detector type")
    for hypothesis in pack.get("hypotheses", []):
        for item in hypothesis.get("support", []) + hypothesis.get("contradict", []):
            if item["rule"] not in seen:
                raise RuleError(f"Hypothesis {hypothesis['id']} references unknown rule {item['rule']}")


class _Values(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def fmt(template, values):
    try:
        return template.format_map(_Values(values))
    except (ValueError, KeyError, IndexError, TypeError):
        return template


def threshold(spec, profile):
    if isinstance(spec, dict):
        return float(profile.get(spec["profile"], spec.get("default")))
    return float(spec)


def seconds(us):
    return us / 1e6


class Context:
    def __init__(self, analysis, parameter_sets):
        self.a = analysis
        self.events = analysis.events
        self.series = analysis.series
        self.profile = analysis.profile
        self.parameter_sets = parameter_sets
        self.vehicle_sources = [key for key, source in analysis.sources.items() if source["role"] in ("vehicle", "onboard_log")]
        self.episodes = [(index, episode, analysis.phases[index]) for index, episode in enumerate(analysis.episodes)
                         if episode["source"] in self.vehicle_sources]

    def param(self, source, name):
        return self.a.param(source, name)

    def semantics(self, source):
        return self.a.sources[source].get("semantic_pack")

    def has_messages(self, source, names):
        counts = self.a.sources[source]["message_types"]
        return any(counts.get(name) for name in names)


def result(rule, state, scope, detail, evidence=(), measurements=None, **extra):
    return {
        "rule_id": rule["id"], "rule_version": rule["version"], "title": rule["title"], "state": state,
        "severity": rule["severity"], "kind": rule.get("kind", "observation"), "scope": scope,
        "detail": detail, "evidence": list(dict.fromkeys(evidence))[:MAX_EVIDENCE],
        "missing": list(rule.get("missing", [])), "measurements": measurements or {}, **extra,
    }


def episode_scope(index, episode, phase):
    return {"type": "episode", "source": episode["source"], "epoch": episode["epoch"], "episode": index,
            "start_us": episode["start_us"], "end_us": episode["end_us"], "phase_status": phase["status"]}


def episode_gate(rule, ctx, index, episode, phase):
    """Return (windows, early_result)."""
    scope = episode_scope(index, episode, phase)
    if phase["status"] == "no_airborne_evidence":
        return None, result(rule, "not_applicable", scope, "No airborne evidence in this armed episode. " + " ".join(phase["notes"]))
    if phase["status"] == "unassessable":
        return None, result(rule, "unassessable", scope, "Flight phase could not be established. " + " ".join(phase["notes"]))
    name = rule["detector"]["phase"]
    windows = phase_windows(phase, name)
    trim = int(rule["detector"].get("trim_start_s", 0) * 1e6)
    windows = [(start + trim, end) for start, end in windows if end - (start + trim) > 0]
    if not windows:
        return None, result(rule, "unassessable", scope, f"No usable '{name}' window (too short or begins at the event).")
    return windows, None


def window_text(phase, windows, label):
    start, end = windows[0]
    return (f"{label}: T+{seconds(start - phase['takeoff_us']):.1f}–{seconds(end - phase['takeoff_us']):.1f} s after "
            f"{'reported' if phase['status'] == 'airborne_reported' else 'inferred'} takeoff ({seconds(end - start):.1f} s).")


def runs_above(series, windows, limit, max_gap_us):
    runs = []
    for start, end in windows:
        current = None
        previous_t = None
        for i in series.window(start, end):
            t, v = series.t[i], series.v[i]
            gap_ok = previous_t is None or t - previous_t <= max_gap_us
            if v > limit and gap_ok and current is not None:
                current["end"], current["last_id"], current["n"] = t, series.ids[i], current["n"] + 1
                current["max"] = max(current["max"], v)
            elif v > limit:
                if current:
                    runs.append(current)
                current = {"start": t, "end": t, "first_id": series.ids[i], "last_id": series.ids[i], "n": 1, "max": v}
            else:
                if current:
                    runs.append(current)
                current = None
            previous_t = t
        if current:
            runs.append(current)
    return runs


def series_label(name):
    match = re.match(r"imu\.(\d+)\.vibration\.(\w)", name)
    if match:
        return f"IMU {match.group(1)}, {match.group(2).upper()}"
    return name


# ---------------------------------------------------------------- detectors

def d_event_present(rule, ctx):
    det = rule["detector"]
    kinds = set(det.get("kinds", []))
    pattern = re.compile(det["text_regex"], re.I) if det.get("text_regex") else None

    def matches(event):
        return (event["kind"] in kinds) or (pattern is not None and pattern.search(event["text"]))

    results = []
    for source in ctx.vehicle_sources:
        hits = [e for e in ctx.events if e["source"] == source and matches(e)]
        text_capable = ctx.has_messages(source, det.get("requires_messages", ["STATUSTEXT", "MSG"]))
        if not hits:
            state = "not_triggered" if text_capable else "unassessable"
            detail = ("No matching report in the recorded text messages (reports can be missing from a link capture)."
                      if text_capable else "Source recorded no text messages; the report cannot be assessed.")
            results.append(result(rule, state, {"type": "source", "source": source}, detail))
            continue
        groups = [[hit] for hit in hits] if rule["per"] == "event" else [hits]
        for group in groups:
            values = {"count": len(group), "context_text": ""}
            evidence = [rid for e in group for rid in e["evidence"]]
            measurements = {}
            context = []
            for spec in det.get("context_signals", []):
                text, ids, measure = context_signal(ctx, source, group, spec)
                context.append(text)
                evidence += ids
                measurements[spec["label"]] = measure
            values["context_text"] = " ".join(context)
            firmware = ctx.semantics(source)
            notes = rule.get("firmware_notes", {})
            values["firmware_note"] = notes.get(firmware, notes.get("default", ""))
            first = group[0]
            scope = ({"type": "event", "source": source, "epoch": first["epoch"], "t_us": first["t_us"], "event_id": first["id"]}
                     if rule["per"] == "event" else {"type": "source", "source": source})
            results.append(result(rule, "triggered", scope, fmt(rule["detail"], values), evidence, measurements,
                                  t_us=first["t_us"]))
    return results


def context_signal(ctx, source, events, spec):
    name = spec["signal"]
    for param in re.findall(r"\{(\w+)\}", name):
        value = ctx.param(source, param)
        if value is None:
            return f"{spec['label']}: unknown ({param} not observed or encoding unverified).", [], None
        name = name.replace("{" + param + "}", str(int(value)))
    window = int(spec.get("window_s", 2) * 1e6)
    values, ids = [], []
    for event in events:
        series = ctx.series.get(source, event["epoch"], name)
        if series is None:
            continue
        for i in series.window(event["t_us"] - window, event["t_us"] + window):
            values.append(series.v[i])
            ids.append(series.ids[i])
    if not values:
        return f"{spec['label']} ({name}): no samples within ±{spec.get('window_s', 2)} s of the reports.", [], None
    text = f"{spec['label']} ({name}) near the reports: {min(values):g}–{max(values):g} ({len(values)} samples)."
    measure = {"signal": name, "min": min(values), "max": max(values), "n": len(values)}
    if spec.get("compare_param"):
        limit = ctx.param(source, spec["compare_param"])
        if limit is None:
            text += f" {spec['compare_param']} unknown."
        else:
            measure["threshold"] = limit
            text += (f" {spec['compare_param']}={limit:g}: all samples are above it, so these values alone do not "
                     "establish the below-threshold mechanism." if min(values) > limit else
                     f" {spec['compare_param']}={limit:g}: some samples are at or below it.")
    return text, [ids[0], ids[-1]], measure


def d_event_sequence(rule, ctx):
    det = rule["detector"]
    results = []
    window = int(det["window_s"] * 1e6)
    anchors = [e for e in ctx.events if e["source"] in ctx.vehicle_sources and e["kind"] in det["anchor_kinds"]]
    for anchor in anchors:
        scope = {"type": "event", "source": anchor["source"], "epoch": anchor["epoch"], "t_us": anchor["t_us"], "event_id": anchor["id"]}
        precursors = [e for e in ctx.events if e["source"] == anchor["source"] and e["epoch"] == anchor["epoch"]
                      and e["kind"] in det["precursor_kinds"] and 0 <= anchor["t_us"] - e["t_us"] <= window
                      and e["id"] < anchor["id"]]
        if not precursors:
            results.append(result(rule, "not_triggered", scope, rule.get("detail_not_triggered", ""), anchor["evidence"]))
            continue
        first = precursors[0]
        support, contradictions, evidence = [], [], first["evidence"] + anchor["evidence"]
        spec = det.get("support_signal")
        if spec:
            for key, series in ctx.series.match(spec["signal"], anchor["source"], anchor["epoch"]):
                runs = [r for r in runs_above(series, [(first["t_us"] - int(spec["lookback_s"] * 1e6), first["t_us"])],
                                              spec["above"], 2_500_000) if seconds(r["end"] - r["start"]) >= spec["for_s"]]
                if runs:
                    best = max(runs, key=lambda r: r["end"] - r["start"])
                    support.append(f"{spec['text']} ({key[2]}: {seconds(best['end'] - best['start']):.1f} s, max {best['max']:.2f}).")
                    evidence += [best["first_id"], best["last_id"]]
        spec = det.get("contradict_onset")
        if spec:
            series = ctx.series.get(anchor["source"], anchor["epoch"], spec["signal"])
            phase = next((p for _, ep, p in ctx.episodes if ep["source"] == anchor["source"]
                          and ep["start_us"] <= anchor["t_us"] <= (ep["end_us"] or anchor["t_us"]) + 2_000_000), None)
            start = phase["takeoff_us"] if phase and phase["takeoff_us"] else first["t_us"] - window
            if series is not None:
                runs = [r for r in runs_above(series, [(start, first["t_us"])], spec["above"], 1_000_000)
                        if seconds(r["end"] - r["start"]) >= spec["for_s"]]
                if runs:
                    contradictions.append(spec["text"] + f" (from {seconds(first['t_us'] - runs[0]['start']):.1f} s before).")
                    evidence += [runs[0]["first_id"]]
        values = {"precursor_text": f"'{first['text']}'", "lead_s": seconds(anchor["t_us"] - first["t_us"])}
        detail = fmt(rule["detail"], values)
        if support:
            detail += " Supporting: " + " ".join(support)
        if contradictions:
            detail += " Contradicting: " + " ".join(contradictions)
        results.append(result(rule, "triggered", scope, detail, evidence, {"lead_s": values["lead_s"]},
                              support=support, contradictions=contradictions, t_us=first["t_us"]))
    if not anchors:
        for source in ctx.vehicle_sources:
            results.append(result(rule, "not_applicable", {"type": "source", "source": source}, "No anchor event recorded."))
    return results


def d_persistence(rule, ctx):
    det = rule["detector"]
    results = []
    max_gap = int(det.get("max_gap_s", 1.5) * 1e6)
    for index, episode, phase in ctx.episodes:
        windows, early = episode_gate(rule, ctx, index, episode, phase)
        if early:
            results.append(early)
            continue
        scope = episode_scope(index, episode, phase)
        matched = [(key, series) for key, series in ctx.series.match(det["signal"], episode["source"], episode["epoch"])
                   if any(len(series.window(s, e)) for s, e in windows)]
        if not matched:
            results.append(result(rule, "unassessable", scope, f"No '{det['signal']}' samples in the window; not a healthy result."))
            continue
        lines, evidence, measurements, triggered = [window_text(phase, windows, "Window")], [], {}, False
        for key, series in matched:
            values = [series.v[i] for s, e in windows for i in series.window(s, e)]
            measure = {"n": len(values), "max": max(values), "mean": sum(values) / len(values)}
            parts = [f"{key[2]}: max {measure['max']:.2f}, mean {measure['mean']:.2f} (n={len(values)})"]
            for criterion in det["criteria"]:
                limit = threshold(criterion["above"], ctx.profile)
                runs = runs_above(series, windows, limit, max_gap)
                longest = max(runs, key=lambda r: r["end"] - r["start"]) if runs else None
                duration = seconds(longest["end"] - longest["start"]) if longest else 0.0
                measure[f"longest_above_{limit:g}_s"] = duration
                parts.append(f"longest run >{limit:g}: {duration:.1f} s")
                if longest and duration >= criterion["for_s"]:
                    triggered = True
                    parts[-1] += f" (>= {criterion['for_s']:g} s)"
                    evidence += [longest["first_id"], longest["last_id"]]
            for extra in det.get("also_measure", []):
                other = [(s, e) for s, e in phase_windows(phase, extra) if e > s]
                after = [series.v[i] for s, e in other for i in series.window(s, e)]
                if after:
                    measure[f"{extra}_max"] = max(after)
                    parts.append(f"{extra.replace('_', ' ')} max {max(after):.2f}")
            measurements[key[2]] = measure
            lines.append("; ".join(parts) + ".")
        summary = " ".join(lines)
        if not any(m["n"] >= 3 for m in measurements.values()):
            results.append(result(rule, "unassessable", scope, "Fewer than 3 samples in the window. " + summary, evidence, measurements))
            continue
        intervals = sorted(seconds(series.t[i + 1] - series.t[i]) for _, series in matched
                           for s, e in windows for i in list(series.window(s, e))[:-1])
        median_interval = intervals[len(intervals) // 2] if intervals else None
        coarse = [c["for_s"] for c in det["criteria"] if median_interval is not None and median_interval >= c["for_s"]]
        if not triggered and len(coarse) == len(det["criteria"]):
            results.append(result(rule, "unassessable", scope,
                                  f"Median sample interval {median_interval:.2f} s is not shorter than the persistence "
                                  f"criteria, so absence of a qualifying run is not evidence. {summary}",
                                  evidence, measurements))
            continue
        if not triggered and coarse:
            summary += (f" The {'/'.join(f'{c:g} s' for c in coarse)} criterion cannot be resolved at the median "
                        f"{median_interval:.2f} s sample interval.")
        template = rule["detail"] if triggered else rule.get("detail_not_triggered", "{summary}")
        results.append(result(rule, "triggered" if triggered else "not_triggered", scope,
                              fmt(template, {"summary": summary}), evidence, measurements, t_us=windows[0][0]))
    return results


def d_counter_increment(rule, ctx):
    det = rule["detector"]
    results = []
    max_gap = int(det.get("max_gap_s", 3) * 1e6)
    for index, episode, phase in ctx.episodes:
        windows, early = episode_gate(rule, ctx, index, episode, phase)
        if early:
            results.append(early)
            continue
        scope = episode_scope(index, episode, phase)
        lines, evidence, measurements, total, assessable = [window_text(phase, windows, "Window")], [], {}, 0, False

        def increments(series, wins):
            added, resets, pairs, ids = 0, 0, 0, []
            for s, e in wins:
                indices = list(series.window(s, e))
                for a, b in zip(indices, indices[1:]):
                    if series.t[b] - series.t[a] > max_gap:
                        continue
                    pairs += 1
                    delta = series.v[b] - series.v[a]
                    if delta > 0:
                        added += delta
                        ids += [series.ids[a], series.ids[b]]
                    elif delta < 0:
                        resets += 1
            return added, resets, pairs, ids

        for key, series in ctx.series.match(det["signal"], episode["source"], episode["epoch"]):
            added, resets, pairs, ids = increments(series, windows)
            if not pairs:
                continue
            assessable = True
            total += added
            evidence += ids[:2] + ids[-2:]
            text = f"{key[2]}: +{added:g} over {pairs} observed intervals" + (f", {resets} reset(s) excluded" if resets else "")
            after = [(s, e) for extra in det.get("also_measure", []) for s, e in phase_windows(phase, extra) if e > s]
            later = increments(series, after)[0] if after else None
            if later is not None:
                text += f"; at/after event +{later:g}"
            measurements[key[2]] = {"pre_event_increment": added, "resets": resets, "after_event_increment": later}
            lines.append(text + ".")
        if not assessable:
            results.append(result(rule, "unassessable", scope, "No consecutive counter observations in the window."))
            continue
        summary = " ".join(lines)
        template = rule["detail"] if total > 0 else rule.get("detail_not_triggered", "{summary}")
        results.append(result(rule, "triggered" if total > 0 else "not_triggered", scope,
                              fmt(template, {"summary": summary}), evidence, measurements, t_us=windows[0][0]))
    return results


def d_peak(rule, ctx):
    det = rule["detector"]
    limit = threshold(det["above"], ctx.profile)
    results = []
    for source in ctx.vehicle_sources:
        matched = ctx.series.match(det["signal"], source)
        if not matched:
            results.append(result(rule, "unassessable", {"type": "source", "source": source}, f"No '{det['signal']}' samples recorded."))
        for (src, epoch, name), series in matched:
            if not len(series.v):
                continue
            index = max(range(len(series.v)), key=series.v.__getitem__)
            peak = series.v[index]
            scope = {"type": "series", "source": src, "epoch": epoch, "signal": name}
            state = "triggered" if peak > limit else "not_triggered"
            template = rule["detail"] if state == "triggered" else rule.get("detail_not_triggered", rule["detail"])
            entry = result(rule, state, scope, fmt(template, {"peak": peak, "threshold": limit}),
                           [series.ids[index]], {"peak": peak, "threshold": limit}, t_us=series.t[index])
            entry["title"] = fmt(rule["title"], {"series": series_label(name)})
            results.append(entry)
    return results


def position_label(layout, motor):
    if not layout or motor not in layout:
        return f"motor {motor}"
    x, y, spin = layout[motor]
    where = ("front" if y > 0 else "back" if y < 0 else "") + ("-" if x and y else "") + ("right" if x > 0 else "left" if x < 0 else "")
    return f"motor {motor} ({where}, {'CCW' if spin > 0 else 'CW'})"


def d_motor_balance(rule, ctx):
    det = rule["detector"]
    results = []
    for index, episode, phase in ctx.episodes:
        windows, early = episode_gate(rule, ctx, index, episode, phase)
        if early:
            results.append(early)
            continue
        scope = episode_scope(index, episode, phase)
        mapping = ctx.a.motor_maps.get(episode["source"])
        means, evidence = {}, []
        for (src, epoch, name), series in ctx.series.match("motor.*.output_frac", episode["source"], episode["epoch"]):
            values = [series.v[i] for s, e in windows for i in series.window(s, e)]
            if len(values) >= 3:
                means[int(name.split(".")[1])] = sum(values) / len(values)
                indices = [i for s, e in windows for i in series.window(s, e)]
                evidence += [series.ids[indices[0]], series.ids[indices[-1]]]
        if len(means) < 2:
            limits = "; ".join(mapping["limitations"]) if mapping else "No motor mapping."
            results.append(result(rule, "unassessable", scope, f"Motor outputs could not be mapped/assessed. {limits}"))
            continue
        layout = mapping.get("layout") if mapping else None
        low, high = min(means, key=means.get), max(means, key=means.get)
        spread = means[high] - means[low]
        axes = {}
        if layout and set(layout) == set(means):
            for axis, column in (("roll (right minus left)", 0), ("pitch (front minus back)", 1), ("yaw (CCW minus CW props)", 2)):
                weights = [layout[m][column] for m in means]
                if any(weights):
                    axes[axis] = sum(layout[m][column] * means[m] for m in means) / sum(abs(w) for w in weights)
        summary = (window_text(phase, windows, "Window") + " Mean command (fraction of PWM range): " +
                   ", ".join(f"{position_label(layout, m)} {v:.2f}" for m, v in sorted(means.items())) +
                   f". Highest {position_label(layout, high)}, lowest {position_label(layout, low)}; spread {spread:.2f}.")
        dominant = None
        if axes:
            summary += " Mixer decomposition: " + ", ".join(f"{k} {v:+.3f}" for k, v in axes.items()) + "."
            dominant = max(axes, key=lambda k: abs(axes[k]))
            if dominant.startswith("yaw") and abs(axes[dominant]) > 2 * max(abs(v) for k, v in axes.items() if k != dominant):
                summary += (" The imbalance is predominantly yaw-axis (one propeller direction working harder): consistent "
                            "with sustained yaw torque demand from commanded heading changes, twisted/tilted motor mounts, "
                            "or a propeller/direction mismatch, rather than a single weak motor.")
        state = "triggered" if spread > det["spread_above"] else "not_triggered"
        template = rule["detail"] if state == "triggered" else rule.get("detail_not_triggered", "{summary}")
        results.append(result(rule, state, scope, fmt(template, {"summary": summary, "spread_above": det["spread_above"]}),
                              evidence, {"mean_output": means, "spread": spread, "mixer_axes": axes, "dominant_axis": dominant},
                              t_us=windows[0][0]))
    return results


def d_coverage(rule, ctx):
    det = rule["detector"]
    results = []
    for source in ctx.vehicle_sources:
        episodes = [(ep, ph) for _, ep, ph in ctx.episodes if ep["source"] == source]
        scope = {"type": "source", "source": source}
        if not episodes:
            results.append(result(rule, "not_applicable", scope, "No armed episode observed."))
            continue
        found = []
        for pattern in det["signals"]:
            for key, series in ctx.series.match(pattern, source):
                for ep, _ in episodes:
                    if ep["epoch"] == key[1] and len(series.window(ep["start_us"], ep["end_us"] or ep["start_us"] + 10**12)):
                        found.append(key[2])
        state = "not_triggered" if found else "triggered"
        evidence = [ep["start_record"] for ep, _ in episodes]
        results.append(result(rule, state, scope, rule["detail"] if state == "triggered" else rule.get("detail_not_triggered", ""),
                              evidence, {"signals_found": sorted(set(found))}))
    return results


def d_parameter(rule, ctx):
    det = rule["detector"]
    results = []
    for snapshot in ctx.parameter_sets:
        if snapshot["source"] not in ctx.vehicle_sources:
            continue
        scope = {"type": "snapshot", "source": snapshot["source"], "epoch": snapshot["epoch"], "param": det["param"]}
        item = snapshot["values"].get(det["param"])
        if item is None:
            results.append(result(rule, "unassessable", scope, f"{det['param']} not observed in this snapshot."))
            continue
        if item["value"] is None:
            results.append(result(rule, "unassessable", scope, f"{det['param']} observed but its encoding is unverified.", [item["record_id"]]))
            continue
        if det.get("requires_semantics") and ctx.semantics(snapshot["source"]) != det["requires_semantics"]:
            results.append(result(rule, "unassessable", scope,
                                  f"Bit meaning requires {det['requires_semantics']} semantics; the reported firmware does not match.",
                                  [item["record_id"]]))
            continue
        value = item["value"]
        if "equals" in det:
            hit = value == det["equals"]
        else:
            hit = value == int(value) and bool(int(value) & (1 << det["bit"]))
        results.append(result(rule, "triggered" if hit else "not_triggered", scope,
                              rule["detail"] if hit else f"{det['param']}={value:g}; condition not met.",
                              [item["record_id"]], {"value": value}, t_us=item["t_us"]))
    return results


def d_profile_parameters(rule, ctx):
    det = rule["detector"]
    expected = ctx.profile.get("expected_parameters") or {}
    results = []
    for snapshot in ctx.parameter_sets:
        if snapshot["source"] not in ctx.vehicle_sources:
            continue
        scope = {"type": "snapshot", "source": snapshot["source"], "epoch": snapshot["epoch"]}
        if not expected:
            results.append(result(rule, "not_applicable", scope, "The attached profile declares no expected parameters."))
            continue
        diffs, checked, unknown, evidence = [], 0, [], []
        for name, want in sorted(expected.items()):
            item = snapshot["values"].get(name)
            if item is None or item["value"] is None:
                unknown.append(name)
                continue
            checked += 1
            if abs(item["value"] - float(want)) > det.get("tolerance", 0):
                diffs.append({"name": name, "expected": want, "observed": item["value"]})
                evidence.append(item["record_id"])
        summary = "; ".join(f"{d['name']} expected {d['expected']:g}, observed {d['observed']:g}" for d in diffs)
        if unknown:
            summary += f" ({len(unknown)} expected parameter(s) not observed or unverified: {', '.join(unknown[:10])})"
        state = "triggered" if diffs else ("not_triggered" if checked else "unassessable")
        template = rule["detail"] if diffs else rule.get("detail_not_triggered", "")
        results.append(result(rule, state, scope, fmt(template, {"count": len(diffs), "summary": summary, "checked": checked}),
                              evidence, {"differences": diffs, "unverified": unknown}))
    return results


DETECTORS = {
    "event_present": d_event_present, "event_sequence": d_event_sequence, "persistence": d_persistence,
    "counter_increment": d_counter_increment, "peak": d_peak, "motor_balance": d_motor_balance,
    "coverage": d_coverage, "parameter": d_parameter, "profile_parameters": d_profile_parameters,
}


def evaluate(pack, analysis, parameter_sets):
    ctx = Context(analysis, parameter_sets)
    results = []
    for rule in pack["rules"]:
        for entry in DETECTORS[rule["detector"]["type"]](rule, ctx):
            entry["id"] = len(results) + 1
            results.append(entry)
    return results, rank_hypotheses(pack, ctx, results)


def _in_episode(entry, episode, index):
    scope = entry["scope"]
    if scope.get("type") == "episode":
        return scope["episode"] == index
    if scope.get("type") == "event":
        end = (episode["end_us"] or scope["t_us"]) + 2_000_000
        return scope["source"] == episode["source"] and episode["start_us"] <= scope["t_us"] <= end
    return False


def rank_hypotheses(pack, ctx, results):
    incidents = []
    cap = pack.get("family_cap", 1.5)
    for index, episode, phase in ctx.episodes:
        if phase["critical_event_us"] is None:
            continue
        scoped = [r for r in results if _in_episode(r, episode, index)]
        ranked = []
        for hypothesis in pack.get("hypotheses", []):
            families, support, against, assessed = {}, [], [], False
            for item in hypothesis.get("support", []):
                for r in (r for r in scoped if r["rule_id"] == item["rule"]):
                    if r["state"] in ("triggered", "not_triggered"):
                        assessed = True
                    if r["state"] == "triggered":
                        families[item["family"]] = families.get(item["family"], 0) + item["weight"]
                        support.append({"rule_id": r["rule_id"], "result_id": r["id"], "title": r["title"], "family": item["family"]})
            penalty = 0.0
            for item in hypothesis.get("contradict", []):
                for r in (r for r in scoped if r["rule_id"] == item["rule"]):
                    flagged = (item.get("flag") and r.get(item["flag"])) or (item.get("state") and r["state"] == item["state"])
                    if flagged:
                        penalty += item["weight"]
                        against.append({"rule_id": r["rule_id"], "result_id": r["id"], "title": r["title"],
                                        "reason": "; ".join(r.get(item["flag"], [])) if item.get("flag") else f"assessed as {r['state']}"})
            blocked = [r for r in results if r["rule_id"] in hypothesis.get("blocked_by", []) and r["state"] == "triggered"
                       and r["scope"].get("source") == episode["source"]]
            score = sum(min(cap, value) for value in families.values()) - penalty
            if blocked or (not assessed and not against):
                label = "unassessable"
            elif support and against:
                label = "mixed support"
            elif score >= 2:
                label = "strong support"
            elif score >= 1:
                label = "some support"
            elif score > 0:
                label = "weak support"
            else:
                label = "limited or contradicting evidence"
            ranked.append({
                "id": hypothesis["id"], "title": hypothesis["title"], "label": label, "score": round(score, 2),
                "support": support, "contradictions": against, "families": sorted(families),
                "blocked_by": [r["title"] for r in blocked], "severity_if_real": hypothesis.get("severity_if_real"),
                "discriminating_evidence": hypothesis.get("discriminate", ""), "probability": None,
            })
        ranked.sort(key=lambda h: (h["label"] == "unassessable", -h["score"]))
        incidents.append({"episode": index, "source": episode["source"], "epoch": episode["epoch"],
                          "critical_event_us": phase["critical_event_us"], "hypotheses": ranked,
                          "method": "Expert-weighted screening order: capped support per evidence family minus explicit "
                                    "contradictions. Not a probability; repeated messages are not counted as independent support."})
    return incidents
