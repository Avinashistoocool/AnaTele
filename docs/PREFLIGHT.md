# AnaTele Preflight: one-button calibration and pre-flight checklist

AnaTele Preflight does the bench checks and calibrations that you usually do in a ground control station
before a flight. One button, **Run full preflight**, does them in sequence. The tool stops when you must move the
vehicle. At the end, it writes a report with a timestamp.

!!! danger "Preflight communicates with a live vehicle"
    The other parts of AnaTele only **read** log files. Preflight is the **only** part that opens a
    MAVLink connection to a vehicle and sends commands to it. Preflight is a separate tool (`anatele preflight`, or
    **Tools → Preflight checklist…** in the desktop app). The tool opens in its own window and its own process.

    - **WARNING:** Remove all of the propellers before the motor test. The **motor test turns the motors**, and a turning propeller can cause injury.
    - The motor test operates only when you select **Props removed**. The GUI then asks you to confirm again.
      In the terminal, you must type `props off`.
    - **CAUTION:** Do not start a calibration unless you want to replace the stored calibration. The calibrations **overwrite the stored calibration of the vehicle**
      (gyro, barometer, level trim, compass offsets, accelerometer offsets and scales).
    - Reboot the vehicle after the compass calibration or the accelerometer calibration.
    - Preflight **never arms** the vehicle. If the vehicle is armed, Preflight does not start. If the vehicle becomes armed
      during the run, the run stops.
    - Each wait has a timeout. **Abort** stops the run at the next check.

## What it checks

| # | Step | Function | Do you have a task? |
|---|---|---|---|
| 1 | Connect and heartbeat | Waits for `HEARTBEAT`. Then reads the vehicle type, autopilot and firmware (`AUTOPILOT_VERSION`). **Stops if the vehicle is armed.** | No |
| 2 | Gyro and barometer calibration | `MAV_CMD_PREFLIGHT_CALIBRATION` with param1=1 (gyro), then param3=1 (baro ground pressure). Each command waits for `COMMAND_ACK`. | Keep the vehicle still |
| 3 | Level (board) calibration | `MAV_CMD_PREFLIGHT_CALIBRATION` param5=2. Sets the AHRS trim from the current attitude. | Keep the vehicle level and still |
| 4 | Compass calibration (guided) | `MAV_CMD_DO_START_MAG_CAL` (all compasses, retry, autosave). Shows `MAG_CAL_PROGRESS` for each compass and the `MAG_CAL_REPORT` result (status, fitness). **Cancel** sends `MAV_CMD_DO_CANCEL_MAG_CAL`. | **Turn the vehicle** |
| 5 | Accelerometer 6-position (guided) | `MAV_CMD_PREFLIGHT_CALIBRATION` param5=1. Follows the `MAV_CMD_ACCELCAL_VEHICLE_POS` requests from the vehicle (LEVEL, LEFT, RIGHT, NOSE DOWN, NOSE UP, BACK). **Next** confirms each position. | **Put the vehicle in 6 positions** |
| 6 | Pre-arm health | Sends `MAV_CMD_RUN_PREARM_CHECKS`. Collects `SYS_STATUS` sensor health, `EKF_STATUS_REPORT` flags and variances, `GPS_RAW_INT` fix, satellites and HDOP, battery (`SYS_STATUS`/`BATTERY_STATUS`), `RC_CHANNELS` and all `PreArm:` messages. | No |
| 7 | Motor test (props off) | `MAV_CMD_DO_MOTOR_TEST` for each motor in sequence, at 6 % throttle (1 to 15 % permitted) for 2 s. Multicopters only. **The tool skips this step if "Props removed" is not selected.** | Confirm the sequence and the direction |
| 8 | Parameters vs profile | Reads the parameters in your profile (`PARAM_REQUEST_READ`, or `PARAM_REQUEST_LIST` for large profiles). Reports the differences. | No |

Each step ends as **PASS**, **WARN**, **FAIL**, **SKIPPED** or **NEEDS_USER**, with a message of one line. NEEDS_USER
means that a person cancelled a guided step, or that no person answered it. The overall result is the worst step result.
A failed step does not stop the run, except for a failed connection or an armed vehicle. Thus, you see all of the results in
one run.

## Connection

Usually, you do not need a connection string. **Automatic detection is the default.** The procedure is:

1. The tool makes a list of the serial ports.
2. It puts the ports that are possibly flight controllers first. These ports have the USB vendor ids of ArduPilot/ChibiOS boards, or "ArduPilot",
   "Pixhawk" or "Cube" in the name.
3. It puts USB-serial radios next.
4. At the end, it listens on UDP 14550.
5. It selects the first port that sends an autopilot HEARTBEAT.

During the scan, the tool only *listens*. It sends nothing until a vehicle answers.
The tool skips Bluetooth serial ports, because the port can stop the tool when it opens. If your radio uses Bluetooth, select the port manually.

```bash
anatele preflight --list-ports   # what auto-detect would see, flight controller first
```

| Link | Connection string | Notes |
|---|---|---|
| Auto-detect | `auto` (default) | Scans USB serial, then `udpin:0.0.0.0:14550`. |
| USB (Windows) | `COM3` | The tool ignores the baud for native USB, but 115200 is the usual value. Close Mission Planner/QGC first, because only one program can open the port. |
| USB (Linux/macOS) | `/dev/ttyACM0`, `/dev/tty.usbmodem*` | Possibly, your user must be in the `dialout` group. |
| Telemetry radio | `COM5` with baud `57600` | The default of SiK radios is 57600. |
| UDP (listen) | `udpin:0.0.0.0:14550` | For SITL, MAVProxy `--out`, or a companion computer/Wi-Fi bridge that sends to this PC. |
| UDP (send) | `udpout:192.168.4.1:14550` | Use when the vehicle side listens. |
| TCP | `tcp:127.0.0.1:5760` | The default TCP port of SITL. |
| Simulated | `sim:` (or `--simulate`) | A built-in simulated ArduCopter. Use it to try the full procedure without hardware. |

Preflight asks the vehicle for telemetry streams (`REQUEST_DATA_STREAM`). Some links send no data until a
ground station asks for it.

## Use the window

```bash
anatele preflight                      # opens the Preflight window (auto-detect selected)
anatele preflight --connect COM3       # pre-selects a port
anatele preflight --simulate           # simulated vehicle
```

You can also open the window from the desktop app: **Tools → Preflight checklist…**.

1. Select the connection in the **Connection** list. The list shows *Auto-detect*, all detected serial ports (flight controllers first), UDP 14550
   and the simulated vehicle. You can also type a connection string.
2. If you connect the board after the tool starts, push **Refresh** to scan again.
3. Push **Connect**. Alternatively, push **Run full preflight** immediately, because it connects first.
4. Optional: To compare parameters, push **Load profile…**.
5. Optional: To do the motor test, remove all of the propellers. Then select **Props removed**.
6. Push **Run full preflight**. The checklist table shows a colour for each status. The progress bar shows the
   current step, and the log pane shows all of the vehicle messages.
7. When a guided step needs you, the **Action needed** bar shows the instruction. Do the instruction, then push **Next** or **Cancel**
   (or **Yes** or **No**).
8. When the run is complete, the tool saves the report automatically to `~/AnaTele Preflight/`. To save one more copy to a different folder, push **Export report…**.

## Use the terminal

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
| `--connect STR` | Connection string (refer to the table above). The default is `auto`. |
| `--list-ports` | Show a list of the serial ports, with the possible flight controller first, and stop |
| `--baud N` | Serial baud rate (default 115200) |
| `--simulate` | Use the built-in simulated vehicle |
| `--no-gui` | Run in the terminal. Guided steps ask for input in the terminal: **Enter** = Next/Yes, `n` = No, `c` = Cancel |
| `--profile FILE` | The parameter profile for the comparison |
| `--save-profile FILE` | Download the current parameters, save them as a profile, then stop |
| `--report-dir DIR` | The folder for the report (default `~/AnaTele Preflight`) |
| `--skip STEP …` | Skip steps: `gyro_baro`, `level`, `compass`, `accel`, `health`, `motors`, `params` |
| `--props-removed` | Permit the motor test. You must confirm again when you type `props off`. |
| `--motor-throttle P` | Throttle of the motor test in percent, 1 to 15 (default 6) |

The exit code is 0 when the overall result is PASS or WARN. In all other conditions, the exit code is 1. Thus, you can use the tool in scripts.

## Step-by-step procedures

### Gyro, barometer and level

1. Put the vehicle on a level, stable surface.
2. Do not touch the vehicle.
3. When Preflight asks you, confirm that the vehicle is still. Preflight asks one time only.

The gyro calibration takes some seconds. The barometer calibration sets the current pressure as the ground level. The level
calibration keeps the current attitude as "level" (AHRS trim). ArduPilot does not accept trims of more than 10°.

On a new board, do the accelerometer calibration first. The level calibration only makes small adjustments to it.

### Compass calibration

1. Move the vehicle away from metal, cars, reinforced concrete, laptops and phone chargers.
2. If the battery is near the compass in flight, install the battery.
3. When the prompt shows, lift the vehicle. Turn it slowly until each side (top, bottom, nose, tail,
   left, right) points at the ground one time. A smooth "figure of eight" movement, while you turn around, gives good results.
4. Monitor the progress of each compass in percent. When all compasses have a report, the step shows the status and
   the **fitness** of each compass. A lower fitness is better.
5. If a compass fails, the vehicle tries it again automatically. If it fails again, the step is **FAIL**. Then, examine the
   compass orientation (`COMPASS_ORIENT`) and move farther away from the interference.
6. When the calibration is successful, the vehicle saves the results automatically. **Reboot the vehicle** to use the new offsets.

**Cancel** stops the calibration on the vehicle. The vehicle keeps the previous calibration.

### Accelerometer 6-position calibration

The vehicle controls this calibration. It asks for each position in sequence, and the prompt follows the vehicle.

1. Put the vehicle in the position that the prompt shows. The positions are:
   **level**, **on its LEFT side**, **on its RIGHT side**, **nose DOWN**, **nose UP**, **on its BACK**.
2. Hold the vehicle **fully still**.
3. Push **Next**. The vehicle takes samples for about one second. Then it asks for the next position.
4. Do steps 1 to 3 again for each of the six positions.
5. After the sixth position, the vehicle reports success or failure. If the calibration is successful, **reboot the vehicle**.

If you cancel before the end, the vehicle continues to wait for positions until it reboots. To stop the
calibration fully, reboot the vehicle.

### Pre-arm health

Preflight listens to the telemetry for some seconds and asks the vehicle to do its pre-arm checks. Then it gives these results:

- **FAIL**: one of these conditions:
    - an enabled sensor reports that it is unhealthy;
    - the pre-arm checks fail (any `PreArm:` message);
    - the EKF has no attitude;
    - an EKF variance is 0.8 or more.
- **WARN**: one of these conditions:
    - no 3D GPS fix;
    - fewer than 6 satellites;
    - HDOP more than 2;
    - EKF variance from 0.5 to 0.8;
    - battery less than 30 %;
    - no battery data or RC data.

### Motor test

> **WARNING:** Remove all of the propellers before the motor test. The motors turn during the test and a propeller can cause injury.

The motor test is for multicopters only. It operates only when **Props removed** is selected. The tool gets the motor count from `FRAME_CLASS`.
If `FRAME_CLASS` is unknown, the tool uses 4 motors.

1. Each motor turns in sequence at low throttle for 2 s. The test sequence is A, B, C… (clockwise
   from the front right motor on most frames).
2. Look at each motor while it turns.
3. When the tool asks, tell it whether the sequence and the direction were correct. Compare them with the ArduPilot motor-order diagram for your frame.

The vehicle does not accept the test if it is not landed, if the board continues its initialisation, or if the RC is not calibrated. The tool shows the
reason from the vehicle.

### Parameter profiles

A profile is a JSON file:

```json
{
  "name": "my-quad",
  "tolerance": 0.0001,
  "params": {"FRAME_CLASS": 1, "FRAME_TYPE": 1, "BATT_LOW_VOLT": 14.0, "FS_THR_ENABLE": 1}
}
```

**Save current as profile…** (or `--save-profile`) downloads all of the parameters. It keeps the parameters that are important for
safety: `FRAME_*`, `BATT_*`, `FS_*`, `FENCE_*`, `RTL_*`, `ARMING_*`, `MOT_SPIN_*`, `MOT_PWM_*`, `MOT_BAT_*`,
`THR_FS*`, `RC_FS*` and `BRD_SAFETY*`. To add or remove parameters, edit the file.

The tool compares the values with a
relative tolerance (default 1e-4). A missing parameter gives a WARN. A different value gives a FAIL.

Preflight **never writes parameters**. The only exception is the calibration values that the vehicle saves itself.

## Simulate mode

`--simulate` (or the connection `sim:`) uses a built-in simulated ArduCopter. It answers in the same way as the real firmware: ACKs, compass
progress and reports, accelerometer position requests, telemetry, parameters and motor-test ACKs. Use it to learn
the procedure or to show the tool to other persons. The test suite (`test_preflight.py`) uses the same simulator.

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

The Markdown file has the same content as a table. After the table, it gives the details of each step (compass fitness and
offsets, accelerometer positions, EKF variances, GPS, battery, PreArm messages, parameter differences).
A report is a record of a checklist. **It is not an airworthiness approval.**

## Troubleshooting

| Symptom | Possible solution |
|---|---|
| *No HEARTBEAT within 10 s* | Possible causes: incorrect port or baud; a different program (Mission Planner, QGC, MAVProxy) uses the port; the radio has no link. For UDP, examine the firewall, and make sure that the vehicle sends to this PC. |
| *Vehicle is ARMED* | Disarm the vehicle with your transmitter or GCS. Preflight never disarms or arms the vehicle. |
| Gyro calibration FAILED | The vehicle moved. Put the vehicle on a stable surface, do not touch it, and run the calibration again. |
| Level calibration FAILED / *trim over maximum* | The vehicle is more than 10° from level, or the accelerometers are not calibrated. Do the accelerometer calibration first. |
| Compass: no `MAG_CAL_PROGRESS` | The `EXTRA3` stream is disabled on that link (`SRn_EXTRA3` = 0). Increase the value, or use USB. |
| Compass fitness high / FAILED | Move away from metal and electronics. Examine `COMPASS_ORIENT`. Change `COMPASS_CAL_FIT` only if you understand the effect of the change. |
| Accelerometer stops asking for positions | The calibration had a timeout, or the vehicle moved during the samples. Reboot the vehicle and do the calibration again. |
| Motor test refused: *not landed* / *RC not calibrated* | Calibrate the radio. Make sure that the vehicle is on the ground and disarmed. |
| Health WARN *no RC_CHANNELS* | The receiver is not connected or not bound, or `RC_PROTOCOLS` does not include it. |
| All steps are slow over the radio | Long-range radios are slow. The parameter download reads one parameter at a time, unless the profile has more than 40 entries. |

## Verified and not verified

The command parameters and the procedure of the guided calibration follow the ArduPilot sources (`GCS_Common.cpp`,
`AP_AccelCal.cpp`, `AP_Compass_Calibration.cpp`, `CompassCalibrator.cpp`, Copter `motor_test.cpp`). The automated
tests use the simulated vehicle. The behaviour on your firmware version, board and link can be different.

> **CAUTION:** Monitor the first runs on real hardware carefully. Keep your ground control station available as the reference tool. The behaviour on your hardware is not verified.
