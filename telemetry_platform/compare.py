"""Parameter diffs, side-by-side flight comparison and per-aircraft trends (read-only)."""
from . import semantics, store

TREND_KEYS = ("vibration_x_mean", "vibration_y_mean", "vibration_z_mean", "vibration_z_max", "ekf_compass_ratio_mean",
              "ekf_compass_ratio_max", "tilt_error_deg_mean", "throttle_pct_mean", "motor_spread")


def vehicle_snapshot(manifest, source=None):
    """Pick the vehicle parameter snapshot with the most observed names."""
    roles = {s["key"]: s["role"] for s in manifest["sources"]}
    candidates = [p for p in manifest["parameters"] if roles.get(p["source"]) in ("vehicle", "onboard_log")
                  and (source is None or p["source"] == source)]
    return max(candidates, key=lambda p: len(p["values"]), default=None)


def semantic_pack(manifest, source):
    return next((s.get("semantic_pack") for s in manifest["sources"] if s["key"] == source), None)


def values_of(snapshot):
    return {name: item for name, item in snapshot["values"].items()}


def diff_values(left, right, volatile=(), tolerance=1e-6):
    changed, only_left, only_right, unverified = [], [], [], []
    for name in sorted(set(left) | set(right)):
        if name in volatile:
            continue
        a, b = left.get(name), right.get(name)
        if a is None:
            only_right.append(name)
        elif b is None:
            only_left.append(name)
        else:
            va = a["value"] if isinstance(a, dict) else a
            vb = b["value"] if isinstance(b, dict) else b
            if va is None or vb is None:
                raw_a = a.get("raw_value") if isinstance(a, dict) else a
                raw_b = b.get("raw_value") if isinstance(b, dict) else b
                if raw_a != raw_b:
                    unverified.append({"name": name, "left_raw": raw_a, "right_raw": raw_b})
                continue
            if abs(float(va) - float(vb)) > tolerance * max(1.0, abs(float(va))):
                changed.append({"name": name, "left": va, "right": vb,
                                "left_record": a.get("record_id") if isinstance(a, dict) else None,
                                "right_record": b.get("record_id") if isinstance(b, dict) else None})
    return {"changed": changed, "only_left": only_left, "only_right": only_right, "unverified_changes": unverified}


def parameter_diff(manifest, other_manifest=None, profile=None):
    snapshot = vehicle_snapshot(manifest)
    if snapshot is None:
        return {"error": "No vehicle parameter snapshot observed in this log."}
    pack = semantic_pack(manifest, snapshot["source"])
    volatile = set((semantics.get(pack) or {}).get("volatile_parameters", []))
    result = {"left": {"source": snapshot["source"], "epoch": snapshot["epoch"], "completeness": snapshot["completeness"],
                       "observed": len(snapshot["values"]), "semantic_pack": pack},
              "notes": ["Parameter sets are not atomic snapshots; values were observed at different times.",
                        "Names missing on one side may be unobserved rather than absent."]}
    if other_manifest is not None:
        other = vehicle_snapshot(other_manifest)
        if other is None:
            return {"error": "No vehicle parameter snapshot in the comparison log."}
        right_pack = semantic_pack(other_manifest, other["source"])
        result["right"] = {"source": other["source"], "epoch": other["epoch"], "completeness": other["completeness"],
                           "observed": len(other["values"]), "semantic_pack": right_pack}
        if pack != right_pack:
            result["notes"].append(f"Different semantic packs ({pack} vs {right_pack}); same names may differ in meaning.")
        result.update(diff_values(values_of(snapshot), values_of(other), volatile))
    else:
        expected = (profile or {}).get("body", {}).get("expected_parameters") or {}
        result["right"] = {"profile_id": profile["id"] if profile else None, "observed": len(expected),
                           "label": "profile expected values"}
        if not expected:
            result["notes"].append("The profile declares no expected parameters.")
        diff = diff_values(values_of(snapshot), expected, volatile)
        diff["only_left"] = []  # a profile lists a subset; unlisted names are not differences
        diff["not_observed"] = diff.pop("only_right")
        result.update(diff)
    for item in result.get("changed", []):
        meta = semantics.metadata(pack, item["name"])
        if meta:
            item["meta"] = meta
    return result


def comparable_jobs(drone_key):
    rows = []
    for job in store.list_jobs_full():
        manifest = job.get("manifest") or {}
        key = manifest.get("drone_key")
        if key is None and job.get("profile_id"):
            profile = store.get_profile(job["profile_id"])
            key = profile["drone_key"] if profile else None
        if key == drone_key and job["status"] in ("complete", "partial"):
            rows.append(job)
    return rows


def trends(drone_key):
    flights = []
    for job in comparable_jobs(drone_key):
        manifest = job["manifest"]
        for metrics in manifest.get("flight_metrics", []):
            if metrics["phase_status"] not in ("airborne_reported", "airborne_inferred"):
                continue
            first = manifest.get("first_time_us")
            flights.append({"log_id": job["id"], "filename": job["filename"], "created_at": job["created_at"],
                            "first_time_us": first, "rule_version": (manifest.get("rule_pack") or {}).get("version"),
                            "engine_version": manifest.get("version"), "profile_id": job["profile_id"], **metrics})
    flights.sort(key=lambda f: (f["first_time_us"] or 0, f["episode"]))
    series = {key: [{"log_id": f["log_id"], "episode": f["episode"], "value": f[key]} for f in flights if f.get(key) is not None]
              for key in TREND_KEYS}
    return {"drone_key": drone_key, "flights": flights, "series": series,
            "notes": ["Values are pre-event airborne means/maxima per flight; telemetry rates and streamed messages differ between "
                      "captures, so compare like with like.",
                      "A trend is a condition indicator, not remaining-useful-life or a component diagnosis.",
                      "Flights analysed with different engine/rule versions are labelled; re-upload to re-analyse."]}


def compare(left_job, right_job, left_episode=None, right_episode=None):
    def pick(job, episode):
        metrics = job["manifest"].get("flight_metrics", [])
        if episode is not None:
            return next((m for m in metrics if m["episode"] == episode), None)
        airborne = [m for m in metrics if m["phase_status"] in ("airborne_reported", "airborne_inferred")]
        return airborne[-1] if airborne else (metrics[-1] if metrics else None)

    left, right = pick(left_job, left_episode), pick(right_job, right_episode)
    rows = []
    keys = sorted({k for m in (left or {}, right or {}) for k, v in m.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
                  - {"episode", "epoch"})
    for key in keys:
        a, b = (left or {}).get(key), (right or {}).get(key)
        rows.append({"metric": key, "left": a, "right": b,
                     "delta": round(b - a, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None})
    states = sorted(set((left or {}).get("rule_states", {})) | set((right or {}).get("rule_states", {})))
    return {
        "left": {"log_id": left_job["id"], "filename": left_job["filename"], "flight": left},
        "right": {"log_id": right_job["id"], "filename": right_job["filename"], "flight": right},
        "metrics": rows,
        "rules": [{"rule_id": r, "left": (left or {}).get("rule_states", {}).get(r),
                   "right": (right or {}).get("rule_states", {}).get(r)} for r in states],
        "parameters": parameter_diff(left_job["manifest"], right_job["manifest"]),
        "notes": ["Flights are aligned by phase (pre-event airborne window), not wall-clock time.",
                  "Mission item numbers are not equated across different plan versions."],
    }
