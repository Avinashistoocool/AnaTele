# Glossary

| Term | Meaning |
|---|---|
| **ArduPilot** | Open-source autopilot firmware for drones, planes, rovers and boats. |
| **Armed** | Motors are allowed to spin. An *episode* is one armed period. |
| **Attitude** | Orientation of the drone: roll, pitch and yaw. |
| **Baseline altitude** | Median altitude over the first 2 s after arming, treated as "ground". |
| **Ceiling / floor** | The highest / lowest motor command allowed (`MOT_SPIN_MAX` / `MOT_SPIN_MIN`). |
| **Clipping** | An accelerometer reading hit the sensor's maximum, so the true value is unknown. |
| **DataFlash log** | On-board log (`.bin`/`.log`) written to the flight controller's SD card. |
| **EKF** | Extended Kalman Filter, ArduPilot's sensor-fusion estimator. |
| **Epoch** | A stretch of a log in which the clock only moves forward (split at reboots). |
| **Event guard** | The pre-event window ends 3 s before an autopilot crash report. |
| **Family (evidence)** | A group of related rules; its contribution to a hypothesis is capped at 1.5. |
| **Haversine** | Formula for distance between two latitude/longitude points on a sphere. |
| **Hypothesis** | A possible explanation, scored by evidence for and against. Not a probability. |
| **Innovation / test ratio** | How far a sensor reading is from the EKF's prediction, scaled so 1.0 is the limit. |
| **MAVLink** | The message protocol between drones and ground stations. |
| **Mixer** | Turns throttle/roll/pitch/yaw demands into individual motor commands. |
| **Persistence** | A condition must hold continuously for a minimum time to count. |
| **Pre-event window** | From takeoff to just before the upset or crash report; where rules look for causes. |
| **PWM** | Pulse width (µs) sent to a motor controller; here 1000 = off, 2000 = full. |
| **Rule pack** | The versioned YAML file of all rules and hypotheses. |
| **Rule state** | triggered / not triggered / unassessable / not applicable. |
| **Saturation** | A motor stuck at its ceiling or floor, so the controller has no headroom left. |
| **Source** | One MAVLink sender (system id : component id), e.g. `1:1`. |
| **STATUSTEXT** | Human-readable text messages from the autopilot (e.g. "Crash: Disarming"). |
| **Tilt error** | Angle between desired and actual thrust direction. |
| **.tlog** | Telemetry log saved by the ground station from the radio link. |
| **.ant** | AnaTele's shareable, hash-verified analysis file. |
| **Unassessable** | The data needed to check something is missing or too sparse. Never "healthy". |
| **WP_RADIUS_M** | How close the drone must get for a waypoint to count as reached. |
