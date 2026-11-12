# Glossary

| Term | Meaning |
|---|---|
| **ArduPilot** | Open-source autopilot firmware for drones, aeroplanes, rovers and boats. |
| **Armed** | The motors have permission to turn. An *episode* is one armed period. |
| **Attitude** | The orientation of the drone: roll, pitch and yaw. |
| **Baseline altitude** | The median altitude over the first 2 s after arming. AnaTele uses it as "ground". |
| **Ceiling / floor** | The highest / lowest permitted motor command (`MOT_SPIN_MAX` / `MOT_SPIN_MIN`). |
| **Clipping** | An accelerometer reading got to the maximum of the sensor. Thus, the true value is unknown. |
| **DataFlash log** | On-board log (`.bin`/`.log`) that the flight controller writes to its SD card. |
| **EKF** | Extended Kalman Filter. The sensor-fusion estimator of ArduPilot. |
| **Epoch** | A part of a log in which the clock only moves forward. AnaTele divides the log at reboots. |
| **Event guard** | The pre-event window ends 3 s before a crash report from the autopilot. |
| **Family (evidence)** | A group of related rules. Its contribution to a hypothesis has a limit of 1.5. |
| **Haversine** | A formula for the distance between two latitude/longitude points on a sphere. |
| **Hypothesis** | A possible explanation, with a score from the evidence for and against it. Not a probability. |
| **Innovation / test ratio** | The difference between a sensor reading and the EKF prediction, scaled so that 1.0 is the limit. |
| **MAVLink** | The message protocol between drones and ground stations. |
| **Mixer** | Changes throttle/roll/pitch/yaw demands into a command for each motor. |
| **Persistence** | A condition must stay true continuously for a minimum time to count. |
| **Pre-event window** | From takeoff to a short time before the upset or the crash report. The rules look for causes in this window. |
| **PWM** | Pulse width (µs) that goes to a motor controller. Here, 1000 = off and 2000 = full. |
| **Rule pack** | The YAML file, with a version, that contains all rules and hypotheses. |
| **Rule state** | triggered / not triggered / unassessable / not applicable. |
| **Saturation** | A motor that stays at its ceiling or floor. Thus, the controller has no margin. |
| **Source** | One MAVLink sender (system id : component id), for example `1:1`. |
| **STATUSTEXT** | Text messages from the autopilot that persons can read (for example "Crash: Disarming"). |
| **Tilt error** | The angle between the desired thrust direction and the actual thrust direction. |
| **.tlog** | The telemetry log that the ground station saves from the radio link. |
| **.ant** | The AnaTele analysis file that you can share. AnaTele verifies its hashes. |
| **Unassessable** | The necessary data for a check is missing or too sparse. Never "healthy". |
| **WP_RADIUS_M** | The maximum distance from a waypoint at which the waypoint counts as reached. |
