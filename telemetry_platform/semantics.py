"""Firmware-versioned semantic packs (parameter encoding conventions and metadata)."""
import hashlib
from functools import lru_cache
from pathlib import Path

import yaml

SEMANTIC_DIR = Path(__file__).parent / "semantics"
INT_RANGES = {1: (0, 255), 2: (-128, 127), 3: (0, 65535), 4: (-32768, 32767), 5: (0, 2**32 - 1), 6: (-2**31, 2**31 - 1)}


@lru_cache(maxsize=1)
def packs():
    result = {}
    for path in sorted(SEMANTIC_DIR.glob("*.yaml")):
        raw = path.read_bytes()
        pack = yaml.safe_load(raw)
        pack["sha256"] = hashlib.sha256(raw).hexdigest()
        result[pack["id"]] = pack
    for pack in result.values():
        parent = result.get(pack.get("extends"))
        if parent:
            for key, value in parent.items():
                if key in ("parameters",):
                    pack[key] = {**value, **pack.get(key, {})}
                elif key not in pack and key not in ("id", "match", "sha256"):
                    pack[key] = value
    return result


def select(source):
    """Pick the most specific pack for a source dict. Returns (pack_id, match_note) or (None, reason)."""
    autopilot, vehicle_type = source.get("autopilot"), source.get("vehicle_type")
    version, git_hash = source.get("firmware_reported"), source.get("firmware_git")
    onboard = source.get("role") == "onboard_log"
    for pack in packs().values():
        match = pack.get("match", {})
        if "firmware_version" not in match or version != match["firmware_version"]:
            continue
        if not onboard and vehicle_type is not None and vehicle_type not in match.get("vehicle_types", []):
            continue
        prefix = match.get("git_hash_prefix")
        if git_hash and prefix and not git_hash.lower().startswith(prefix):
            return pack["id"], f"version {version} reported but git hash {git_hash} differs from official {prefix}; custom build likely"
        note = (f"version {version} and git hash {git_hash} match the official tag" if git_hash and prefix
                else f"version string {version} only; build hash not observed")
        return pack["id"], note + " (self-reported, not binary verification)"
    if autopilot == 3 or onboard:
        return "ardupilot-common", "ArduPilot conventions only; no version-specific semantics"
    return None, "No semantic pack matches this source"


def get(pack_id):
    return packs().get(pack_id) if pack_id else None


def c_cast_value(raw, param_type):
    """Decode a C-cast parameter value only if it is plausible for the declared integer type."""
    if param_type not in INT_RANGES or raw is None or raw != int(raw):
        return None
    low, high = INT_RANGES[param_type]
    return int(raw) if low <= raw <= high else None


def metadata(pack_id, name):
    pack = get(pack_id)
    return (pack or {}).get("parameters", {}).get(name)
