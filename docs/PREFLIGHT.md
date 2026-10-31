# AnaTele Preflight: one-button calibration and pre-flight checklist

AnaTele Preflight runs the bench checks and calibrations you would normally do in a ground control station
before a flight. One button, **Run full preflight**, does them in order. It stops when it needs you to move the
vehicle, and it writes a timestamped report when it finishes.

!!! danger "Preflight talks to a live vehicle"
    The rest of AnaTele is **read-only**. It only reads log files. Preflight is the **only** part that opens a
    MAVLink connection to a vehicle and sends commands. It runs as a separate tool (`anatele preflight`, or
    **Tools → Preflight checklist…** in the desktop app, which opens its own window and process).

    - Calibrations **overwrite the vehicle's stored calibration** (gyro, barometer, level trim, compass offsets,
      accelerometer offsets and scales). Reboot the vehicle after compass or accelerometer calibration.
    - Preflight **never arms** the vehicle. It refuses to start if the vehicle is armed. If the vehicle becomes armed
      during the run, the run stops.
    - The **motor test spins motors**. It only runs when you tick **Props removed**, and the GUI asks you to confirm
      that again. In the terminal you must type `props off`. **Remove every propeller first.**
    - Every wait has a timeout, and **Abort** stops the run at the next check.

## What it checks

| # | Step | What happens | Needs you? |
|---|---|---|---|
| 1 | Connect and heartbeat | Waits for `HEARTBEAT`, then reads the vehicle type, autopilot and firmware (`AUTOPILOT_VERSION`). **Refuses to continue if armed.** | No |
| 2 | Gyro and barometer calibration | `MAV_CMD_PREFLIGHT_CALIBRATION` with param1=1 (gyro), then param3=1 (baro ground pressure), each waiting for `COMMAND_ACK`. | Keep it still |
| 3 | Level (board) calibration | `MAV_CMD_PREFLIGHT_CALIBRATION` param5=2. Sets the AHRS trim from the current attitude. | Keep it level and still |
| 4 | Compass calibration (guided) | `MAV_CMD_DO_START_MAG_CAL` (all compasses, retry, autosave). Shows `MAG_CAL_PROGRESS` per compass and the `MAG_CAL_REPORT` result (status, fitness). **Cancel** sends `MAV_CMD_DO_CANCEL_MAG_CAL`. | **Rotate the vehicle** |
| 5 | Accelerometer 6-position (guided) | `MAV_CMD_PREFLIGHT_CALIBRATION` param5=1. Follows the vehicle's `MAV_CMD_ACCELCAL_VEHICLE_POS` requests (LEVEL, LEFT, RIGHT, NOSE DOWN, NOSE UP, BACK). **Next** confirms each position. | **Place the vehicle 6 ways** |
| 6 | Pre-arm health | Sends `MAV_CMD_RUN_PREARM_CHECKS` and collects `SYS_STATUS` sensor health, `EKF_STATUS_REPORT` flags and variances, `GPS_RAW_INT` fix, satellites and HDOP, battery (`SYS_STATUS`/`BATTERY_STATUS`), `RC_CHANNELS` and every `PreArm:` message. | No |
| 7 | Motor test (props off) | `MAV_CMD_DO_MOTOR_TEST` for each motor in turn, at 6 % throttle (1 to 15 % allowed) for 2 s. Multicopters only. **Skipped unless "Props removed" is ticked.** | Confirm order and direction |
| 8 | Parameters vs profile | Reads the parameters in your profile (`PARAM_REQUEST_READ`, or `PARAM_REQUEST_LIST` for large profiles) and reports mismatches. | No |

Each step ends as **PASS**, **WARN**, **FAIL**, **SKIPPED** or **NEEDS_USER**, with a one-line message. NEEDS_USER
means a guided step was cancelled or had nobody to answer it. The overall result is the worst step result.
A failed step does not stop the run (except for a failed connection or an armed vehicle). You see everything in
one pass.

## Connecting

You usually don't need a connection string. **Auto-detect is the default.** It lists the serial ports, puts
anything that looks like a flight controller first (USB vendor ids of ArduPilot/ChibiOS boards, or "ArduPilot",
"Pixhawk", "Cube" in the name), then USB-serial radios, and finally listens on UDP 14550. It picks the first one
that sends an autopilot HEARTBEAT. While scanning it only *listens*; nothing is sent until a vehicle answers.
Bluetooth serial ports are skipped because opening them can hang; choose one by hand if your radio uses Bluetooth.

```bash
anatele preflight --list-ports   # what auto-detect would see, flight controller first
```

| Link | Connection string | Notes |
|---|---|---|
| Auto-detect | `auto` (default) | Scans USB serial, then `udpin:0.0.0.0:14550`. |
| USB (Windows) | `COM3` | Baud is ignored for native USB, but 115200 is the usual value. Close Mission Planner/QGC first; only one program can open the port. |
| USB (Linux/macOS) | `/dev/ttyACM0`, `/dev/tty.usbmodem*` | You may need to be in the `dialout` group. |
| Telemetry radio | `COM5` with baud `57600` | SiK radios default to 57600. |
| UDP (listen) | `udpin:0.0.0.0:14550` | For SITL, MAVProxy `--out`, or a companion computer/Wi-Fi bridge sending to this PC. |
| UDP (send) | `udpout:192.168.4.1:14550` | When the vehicle side listens. |
| TCP | `tcp:127.0.0.1:5760` | SITL's default TCP port. |
| Simulated | `sim:` (or `--simulate`) | A built-in fake ArduCopter. Try the whole flow with no hardware. |

Preflight asks the vehicle for telemetry streams (`REQUEST_DATA_STREAM`), because some links stream nothing until a
ground station asks.

## Using the window

```bash
anatele preflight                      # opens the Preflight window (auto-detect selected)
anatele preflight --connect COM3       # pre-selects a port
anatele preflight --simulate           # simulated vehicle
```

You can also open it from the desktop app: **Tools → Preflight checklist…**.

1. The **Connection** list shows *Auto-detect*, every detected serial port (flight controllers first), UDP 14550
   and the simulated vehicle. Leave it on Auto-detect, pick a port, or type any connection string. **Refresh**
   re-scans after you plug the board in. Press **Connect**, or press **Run full preflight** straight away; it
   connects first.
2. Optional: **Load profile…** to compare parameters, and tick **Props removed** if you want the motor test.
3. Press **Run full preflight**. The checklist table fills in with coloured statuses, the progress bar shows the
   current step, and the log pane shows every vehicle message.
4. When a guided step needs you, the **Action needed** bar appears with the instruction and **Next** / **Cancel**
   (or **Yes** / **No**) buttons.
5. When the run finishes, the report is saved automatically to `~/AnaTele Preflight/`. **Export report…** saves another
   copy to any folder.

## Using the terminal

```bash
anatele preflight --no-gui                      # auto-detect
anatele preflight --no-gui --connect COM3
anatele preflight --no-gui --connect udpin:0.0.0.0:14550 --profile quad.json --report-dir reports
anatele preflight --no-gui --connect COM3 --props-removed          # asks you to type "props off"
anatele preflight --no-gui --simulate --skip compass accel
anatele preflight --no-gui --connect COM3 --save-profile quad.json  # save current parameters as a profile
```

| Option | Meaning |
|---|---|
| `--connect STR` | Connection string (see the table above); default `auto` |
| `--list-ports` | List serial ports, likely flight controller first, and exit |
| `--baud N` | Serial baud rate (default 115200) |
| `--simulate` | Use the built-in simulated vehicle |
| `--no-gui` | Run in the terminal. Guided steps prompt there: **Enter** = Next/Yes, `n` = No, `c` = Cancel |
| `--profile FILE` | Parameter profile to compare against |
| `--save-profile FILE` | Download the current parameters, save them as a profile, then exit |
| `--report-dir DIR` | Where to write the report (default `~/AnaTele Preflight`) |
| `--skip STEP …` | Skip steps: `gyro_baro`, `level`, `compass`, `accel`, `health`, `motors`, `params` |
| `--props-removed` | Allow the motor test (confirmed again by typing `props off`) |
| `--motor-throttle P` | Motor-test throttle in percent, 1 to 15 (default 6) |

The exit code is 0 when the overall result is PASS or WARN and 1 otherwise. That makes it usable in scripts.

## Step by step

### Gyro, barometer and level

Put the vehicle on a level, stable surface and don't touch it. Preflight asks you once to confirm that it is still.
Gyro calibration takes a few seconds. The barometer calibration sets the current pressure as ground level. Level
calibration stores the current attitude as "level" (AHRS trim). ArduPilot refuses trims over 10°. Do the
accelerometer calibration first on a new board; level calibration only fine-tunes it.

### Compass calibration

1. Take the vehicle away from metal, cars, reinforced concrete, laptops and phone chargers. Fit the battery if it
   is near the compass in flight.
2. When the prompt appears, pick the vehicle up and slowly rotate it so that each side (top, bottom, nose, tail,
   left, right) points at the ground in turn. A smooth "figure of eight" while turning around yourself works well.
3. Progress for each compass is shown in percent. When every compass has reported, the step shows the status and
   **fitness** (lower is better) for each one.
4. A failed compass is retried automatically by the vehicle. If it still fails, the step is **FAIL**. Check the
   compass orientation (`COMPASS_ORIENT`) and move further from interference.
5. Results are saved automatically on success. **Reboot the vehicle** to use the new offsets.

**Cancel** stops the calibration on the vehicle and keeps the previous calibration.

### Accelerometer 6-position calibration

The vehicle drives this calibration: it asks for each position in turn, and the prompt follows it.

1. Place the vehicle in the position shown and hold it **completely still**:
   **level**, **on its LEFT side**, **on its RIGHT side**, **nose DOWN**, **nose UP**, **on its BACK**.
2. Press **Next**. The vehicle samples for about a second and then asks for the next position.
3. After the sixth position the vehicle reports success or failure. On success, **reboot the vehicle**.

If you cancel half-way, the vehicle keeps waiting for positions until it reboots. Reboot it to abandon the
calibration.

### Pre-arm health

Preflight listens to telemetry for a few seconds, asks the vehicle to run its pre-arm checks, and then reports:

- **FAIL**: an enabled sensor reports unhealthy, pre-arm checks are failing (any `PreArm:` message), the EKF has no
  attitude, or an EKF variance is 0.8 or higher.
- **WARN**: no 3D GPS fix, fewer than 6 satellites, HDOP above 2, EKF variance from 0.5 to 0.8, battery below
  30 %, battery or RC data missing.

### Motor test

Only for multicopters, and only with **Props removed** ticked. The motor count comes from `FRAME_CLASS`
(4 is assumed if unknown). Each motor spins in turn at low throttle for 2 s, in test order A, B, C… (clockwise
from front right on most frames). Afterwards you are asked whether the order and direction were right. Compare
them with the ArduPilot motor-order diagram for your frame.

The vehicle refuses the test if it is not landed, the board is still initialising, or RC is not calibrated. The
vehicle's reason is shown.

### Parameter profiles

A profile is a JSON file:

```json
{
  "name": "my-quad",
  "tolerance": 0.0001,
  "params": {"FRAME_CLASS": 1, "FRAME_TYPE": 1, "BATT_LOW_VOLT": 14.0, "FS_THR_ENABLE": 1}
}
```

**Save current as profile…** (or `--save-profile`) downloads every parameter and keeps the ones that matter for
safety: `FRAME_*`, `BATT_*`, `FS_*`, `FENCE_*`, `RTL_*`, `ARMING_*`, `MOT_SPIN_*`, `MOT_PWM_*`, `MOT_BAT_*`,
`THR_FS*`, `RC_FS*` and `BRD_SAFETY*`. Edit the file to add or remove parameters. Values are compared with a
relative tolerance (default 1e-4). Missing parameters are a WARN, and differing values are a FAIL.

Preflight **never writes parameters** except for the calibration values the vehicle saves itself.

## Simulate mode

`--simulate` (or connection `sim:`) uses a built-in fake ArduCopter. It answers like the real firmware: ACKs, compass
progress and reports, accelerometer position requests, telemetry, parameters and motor-test ACKs. Use it to learn
the flow or to demo the tool. The test suite (`test_preflight.py`) uses the same simulator.

## Report format

Each run writes `preflight-YYYYMMDD-HHMMSS.json` and `.md` to the report folder.

```json
{
  "tool": "AnaTele Preflight",
  "anatele_version": "0.5.0",
  "started": "2026-10-30T18:40:02+0530",
  "finished": "2026-10-30T18:47:31+0530",
  "connection": "COM3 @ 115200",
  "vehicle": {"vehicle_type": "QUADROTOR", "autopilot": "ARDUPILOTMEGA", "firmware": "4.6.3 (official)", "system_id": 1},
  "overall": "WARN",
  "props_removed_confirmed": true,
  "profile": "my-quad",
  "steps": [
    {"id": "connect", "title": "Connect and heartbeat", "status": "PASS", "message": "…", "details": {}, "duration_s": 1.2}
  ]
}
```

The Markdown file has the same content as a table, followed by the details for each step (compass fitness and
offsets, accelerometer positions, EKF variances, GPS, battery, PreArm messages, parameter mismatches).
A report is a checklist record. **It is not an airworthiness approval.**

## Troubleshooting

| Symptom | What to try |
|---|---|
| *No HEARTBEAT within 10 s* | Wrong port or baud; another program (Mission Planner, QGC, MAVProxy) holds the port; radio not linked; for UDP, check the firewall and that the vehicle sends to this PC. |
| *Vehicle is ARMED* | Disarm with your transmitter or GCS. Preflight never disarms or arms. |
| Gyro calibration FAILED | The vehicle moved. Leave it still on a stable surface and run again. |
| Level calibration FAILED / *trim over maximum* | The vehicle is more than 10° off level, or the accelerometers are not calibrated yet. Do the accelerometer calibration first. |
| Compass: no `MAG_CAL_PROGRESS` | The `EXTRA3` stream is disabled on that link (`SRn_EXTRA3` = 0). Raise it, or use USB. |
| Compass fitness high / FAILED | Move away from metal and electronics. Check `COMPASS_ORIENT`, and try `COMPASS_CAL_FIT` only if you understand the trade-off. |
| Accelerometer stops asking for positions | The calibration timed out or the vehicle moved while sampling. Reboot the vehicle and repeat. |
| Motor test refused: *not landed* / *RC not calibrated* | Calibrate the radio. Make sure the vehicle is on the ground and disarmed. |
| Health WARN *no RC_CHANNELS* | The receiver is not connected or bound, or `RC_PROTOCOLS` excludes it. |
| Everything is slow over the radio | Long-range radios are slow. Parameter download uses single reads unless the profile has more than 40 entries. |

## Verified and not verified

The command parameters and the guided-calibration flow follow the ArduPilot sources (`GCS_Common.cpp`,
`AP_AccelCal.cpp`, `AP_Compass_Calibration.cpp`, `CompassCalibrator.cpp`, Copter `motor_test.cpp`). The automated
tests run against the simulated vehicle. Behaviour on your firmware version, board and link may differ. Watch the
first runs on real hardware closely, and keep your ground control station available as the reference tool.
