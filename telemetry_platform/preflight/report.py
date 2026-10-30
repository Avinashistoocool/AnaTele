"""Preflight report: JSON (machine-readable) and Markdown (human-readable), timestamped."""
import json
import time
from pathlib import Path

from .. import __version__
from .steps import overall


def build(results, vehicle, options, started, finished=None):
    finished = finished or time.time()
    return {
        "tool": "AnaTele Preflight",
        "anatele_version": __version__,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(finished)),
        "connection": vehicle.get("connection") or options.connect,
        "vehicle": {k: v for k, v in vehicle.items() if k != "connection"},
        "overall": overall(results),
        "props_removed_confirmed": bool(options.props_removed),
        "profile": (options.profile or {}).get("name"),
        "steps": [r.as_dict() for r in results],
    }


def markdown(report):
    v = report["vehicle"]
    lines = [f"# AnaTele Preflight report: {report['overall']}", "",
             f"- Started: {report['started']}  ",
             f"- Finished: {report['finished']}  ",
             f"- Connection: `{report['connection']}`  ",
             f"- Vehicle: {v.get('vehicle_type', '?')} / {v.get('autopilot', '?')}, firmware {v.get('firmware', 'unknown')}, "
             f"system {v.get('system_id', '?')}  ",
             f"- AnaTele {report['anatele_version']}", "",
             "| # | Step | Status | Result |", "|---|---|---|---|"]
    for index, step in enumerate(report["steps"], 1):
        message = step["message"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {index} | {step['title']} | **{step['status']}** | {message} |")
    lines.append("")
    for step in report["steps"]:
        details = {k: val for k, val in step["details"].items() if val not in ([], {}, None, "")}
        if details:
            lines += [f"## {step['title']}", "", "```json", json.dumps(details, indent=1, default=str), "```", ""]
    lines += ["Calibrations change the vehicle's stored calibration. Reboot the vehicle after compass or "
              "accelerometer calibration. This report is a checklist record, not an airworthiness approval.", ""]
    return "\n".join(lines)


def write(report, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base, counter = folder / f"preflight-{stamp}", 1
    while base.with_suffix(".json").exists():
        counter += 1
        base = folder / f"preflight-{stamp}-{counter}"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    md_path.write_text(markdown(report), encoding="utf-8")
    return json_path, md_path
