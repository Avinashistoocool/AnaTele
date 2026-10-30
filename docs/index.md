# AnaTele

![AnaTele: evidence-first drone flight-log investigation](images/anatele_github_readme_cover.svg)

**Evidence-first investigation of ArduPilot flight logs.** AnaTele reads Mission Planner `.tlog` and
DataFlash `.bin`/`.log` files and turns them into a reproducible investigation:

- **What happened?** A timeline, armed episodes and flight phases (takeoff, pre-event window, upset, disarm).
- **Where did it go?** A 3D replay of the recorded path with mission waypoints and their status.
- **What looks wrong?** A versioned rule pack. Each rule is *triggered*, *not triggered*, *unassessable* or *not applicable*.
- **Why might it have happened?** Hypotheses ranked by the evidence for and against each one. No invented probabilities.
- **Can I check it?** Every claim links back to the exact log record and its raw bytes.
- **Can I share it?** Save the whole analysis as one [`.ant` file](USAGE.md#sharing-analyses-ant).

![Desktop app](images/desktop_overview.png)

## Get it

[Download for Windows, macOS or Linux](https://github.com/Avinashistoocool/AnaTele/releases/latest){ .md-button .md-button--primary }
[Source on GitHub](https://github.com/Avinashistoocool/AnaTele){ .md-button }

```bash
pip install "git+https://github.com/Avinashistoocool/AnaTele"
anatele simulate --out simulations
anatele simulations/sim_motor_failure.tlog
```

## Documentation

| | |
|---|---|
| [**Learn AnaTele from scratch**](learn/index.md) | Illustrated course, no background needed: 12 chapters, 32 figures, interactive widgets ([PDF](AnaTele_Illustrated_Guide.pdf)) |
| [Usage guide](USAGE.md) | Desktop walkthrough, CLI, web UI, API, profiles, simulator, `.ant` sharing |
| [Preflight checklist](PREFLIGHT.md) | Separate tool for a **live** vehicle: one-button gyro/baro/level/compass/accelerometer calibration, pre-arm health, props-off motor test, parameter profile check |
| [Diagnostics reference](DIAGNOSTICS.md) | Every rule, hypothesis ranking, flight phases, mission statuses |
| [Deployment](DEPLOYMENT.md) | Standalone builds, pip, Docker, data directories, security |
| [Technical report (PDF)](AnaTele_Technical_Report.pdf) | Architecture and all of the mathematics, from framing to the simulator |
| [Design](DESIGN.md) | The original architecture proposal |

!!! note
    AnaTele gives you observations and hypotheses. It is **not** a crash verdict, a probability of cause or an
    airworthiness approval. Missing data is treated as *unknown*, never as *healthy*.
