"""AnaTele Preflight: one-button calibration and pre-flight checklist for ArduPilot vehicles.

This is the ONLY part of AnaTele that talks to a live vehicle. The log analyser stays read-only.
Calibrations change vehicle state (they write calibration parameters); the vehicle is never armed,
and the motor test only runs after an explicit props-removed confirmation. See docs/PREFLIGHT.md.
"""
