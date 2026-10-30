"""AnaTele desktop application (PySide6 + matplotlib), sharing the analysis engine and data directory.

Read-only: no MAVLink connection, parameter writes or flight commands. The separate AnaTele Preflight tool
(Tools > Preflight checklist, telemetry_platform/preflight) is the only part that talks to a live vehicle and
runs in its own process.
"""
import json
import math
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")

import duckdb
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QProcess, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSplitter,
    QSlider, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import antfile, compare as comparison, replay, store
from .desktop_import import import_file
from .worker import TIMEOUT_SECONDS

ACCENT = "#e05a5a"
PLOT_BG = "#1e1e1e"
FG = "#dcdcdc"
COLORS = ["#e05a5a", "#5ab4e0", "#e0c35a", "#7ad17a", "#c07ae0", "#e0915a", "#5ae0c8", "#b0b0b0"]
GAP_US = 5_000_000
DEFAULT_SIGNALS = ("attitude.roll_deg", "attitude.pitch_deg", "control.tilt_tracking_error_deg", "imu.0.vibration.z")
EVIDENCE_ROLE = Qt.UserRole + 1


def sql_path(path):
    return "'" + str(path).replace("'", "''") + "'"


def apply_theme(app):
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in ((QPalette.Window, "#202020"), (QPalette.WindowText, FG), (QPalette.Base, "#181818"),
                        (QPalette.AlternateBase, "#262626"), (QPalette.Text, FG), (QPalette.Button, "#2d2d2d"),
                        (QPalette.ButtonText, FG), (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#ffffff"),
                        (QPalette.ToolTipBase, "#2d2d2d"), (QPalette.ToolTipText, FG), (QPalette.PlaceholderText, "#808080")):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#707070"))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#707070"))
    app.setPalette(palette)
    app.setStyleSheet("QGroupBox{border:1px solid #555;margin-top:10px;padding-top:6px}"
                      "QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 3px}"
                      "QTabBar::tab{padding:6px 14px}QHeaderView::section{background:#2a2a2a;padding:4px;border:0}")


def table(headers, stretch_last=True):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().setVisible(False)
    widget.setEditTriggers(QAbstractItemView.NoEditTriggers)
    widget.setSelectionBehavior(QAbstractItemView.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SingleSelection)
    widget.setWordWrap(True)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    if stretch_last:
        widget.horizontalHeader().setStretchLastSection(True)
    return widget


def fill(widget, rows, evidence=None):
    widget.setRowCount(0)
    widget.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            text = "—" if value is None else (f"{value:.3f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value))
            item = QTableWidgetItem(text)
            if evidence and evidence[r]:
                item.setData(EVIDENCE_ROLE, list(evidence[r]))
            widget.setItem(r, c, item)
    widget.resizeRowsToContents()


class PlotPanel(QWidget):
    def __init__(self, on_pick):
        super().__init__()
        self.on_pick = on_pick
        self.figure = Figure(facecolor=PLOT_BG, tight_layout=True)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.mpl_connect("button_press_event", self.clicked)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)
        self.lines = []
        self.empty("Open a log to begin")

    def style(self, ax):
        ax.set_facecolor(PLOT_BG)
        for spine in ax.spines.values():
            spine.set_color(FG)
        ax.tick_params(colors=FG, labelsize=9)
        ax.grid(color="#3a3a3a", linewidth=.6)

    def empty(self, text):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        self.style(ax)
        ax.text(.5, .5, text, color="#909090", ha="center", va="center", transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        self.canvas.draw_idle()

    def draw(self, groups, x_label, x_range, markers, spans, cursor=None):
        """groups: {unit: [(label, xs, ys, ids)]}; markers: [(x, label, color)]; spans: [(x0, x1, label)]."""
        self.figure.clear()
        self.lines = []
        if not groups:
            self.empty("Select one or more signals on the right")
            return
        axes = self.figure.subplots(len(groups), 1, sharex=True, squeeze=False)[:, 0]
        color = 0
        for ax, (unit, series) in zip(axes, groups.items()):
            self.style(ax)
            for label, xs, ys, ids in series:
                line, = ax.plot(xs, ys, color=COLORS[color % len(COLORS)], linewidth=1.2, label=label)
                color += 1
                self.lines.append((ax, xs, ys, ids))
            ax.set_ylabel(unit, color=FG, fontsize=9)
            for x0, x1, label in spans:
                ax.axvspan(x0, x1, color="#5ab4e0", alpha=.08)
            for x, label, mark in markers:
                ax.axvline(x, color=mark, linewidth=1, linestyle="--")
            if cursor is not None:
                ax.axvline(cursor, color="#ffffff", linewidth=1)
            legend = ax.legend(loc="upper left", fontsize=8, facecolor="#262626", edgecolor="#555", labelcolor=FG)
            legend.set_draggable(True)
        top = axes[0]
        for x, label, mark in markers:
            top.annotate(label, (x, 1), xycoords=("data", "axes fraction"), color=mark, fontsize=8,
                         rotation=90, va="top", ha="right")
        axes[-1].set_xlabel(x_label, color=FG, fontsize=9)
        if x_range:
            axes[-1].set_xlim(*x_range)
        self.canvas.draw_idle()

    def clicked(self, event):
        if event.inaxes is None or event.xdata is None or event.button != 1:
            return
        best, distance = None, math.inf
        for ax, xs, ys, ids in self.lines:
            if ax is not event.inaxes:
                continue
            for x, y, rid in zip(xs, ys, ids):
                if rid is None:
                    continue
                d = abs(x - event.xdata)
                if d < distance:
                    best, distance = rid, d
        if best is not None:
            self.on_pick(best)


class FlightPathPanel(QWidget):
    """3D replay of the recorded estimated path with waypoints, mode colouring and a time slider."""

    def __init__(self, on_pick):
        super().__init__()
        self.on_pick = on_pick
        self.track = replay.Track()
        self.figure = Figure(facecolor=PLOT_BG)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(111, projection="3d")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.valueChanged.connect(self.moved)
        self.play = QPushButton("Play")
        self.play.clicked.connect(self.toggle)
        self.label = QLabel("")
        self.export = QPushButton("Export GIF?")
        self.export.clicked.connect(self.export_gif)
        self.timer = QTimer(self, interval=60, timeout=self.step)
        controls = QHBoxLayout()
        controls.addWidget(self.play)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.label)
        controls.addWidget(self.export)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(controls)
        legend = QLabel("Path colour = flight mode · waypoint colour: green reported complete, yellow-green near (within "
                        "radius + GPS accuracy), amber completion not observed, red not visited · X = takeoff / excursion / "
                        "crash report. Estimated positions relative to the first armed fix; not ground truth.")
        legend.setWordWrap(True)
        legend.setStyleSheet("color:#909090")
        layout.addWidget(legend)
        replay.draw(self.ax, self.track)

    def load(self, job, episode):
        self.timer.stop()
        self.play.setText("Play")
        try:
            self.track = replay.build_track(job, episode)
        except Exception as error:
            self.track = replay.Track(notes=[f"Path unavailable: {error}"])
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, len(self.track) - 1))
        self.slider.setValue(max(0, len(self.track) - 1))
        self.slider.blockSignals(False)
        self.moved(self.slider.value(), full=True)

    def moved(self, value, full=False):
        if not len(self.track):
            replay.draw(self.ax, self.track)
            self.label.setText("")
        else:
            elev, azim = self.ax.elev, self.ax.azim
            replay.draw(self.ax, self.track, None if full else value)
            self.ax.view_init(elev=elev, azim=azim)
            up = self.track.up[value]
            self.label.setText(f"t+{(self.track.t[value] - self.track.t[0]) / 1e6:6.1f} s · {self.track.mode[value]} · "
                               f"alt {up:5.1f} m · roll {self.track.roll[value]:5.1f}° pitch {self.track.pitch[value]:5.1f}°")
        self.canvas.draw_idle()

    def toggle(self):
        if self.timer.isActive():
            self.timer.stop()
            self.play.setText("Play")
        elif len(self.track):
            if self.slider.value() >= self.slider.maximum():
                self.slider.setValue(0)
            self.timer.start()
            self.play.setText("Pause")

    def step(self):
        stride = max(1, len(self.track) // 300)
        if self.slider.value() >= self.slider.maximum():
            self.toggle()
            return
        self.slider.setValue(min(self.slider.maximum(), self.slider.value() + stride))

    def export_gif(self):
        if not len(self.track):
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export replay", str(Path.home() / "flight_replay.gif"), "GIF (*.gif);;PNG (*.png)")
        if path:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                replay.render(self.track, path, animate=path.lower().endswith(".gif"))
            finally:
                QApplication.restoreOverrideCursor()


class ProfileDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Profile Editor")
        form = QFormLayout(self)
        self.key, self.name, self.frame, self.firmware = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        self.key.setPlaceholderText("quad-01 (reuse to create a new version)")
        self.frame.setPlaceholderText("Quad-X, operator-declared")
        self.firmware.setPlaceholderText("ArduCopter 4.7.2, operator-declared")
        self.vibration = QDoubleSpinBox()
        self.vibration.setRange(.1, 500)
        self.vibration.setValue(30)
        self.vibration.setSuffix(" m/s²")
        self.expected = QPlainTextEdit()
        self.expected.setPlaceholderText("FS_THR_VALUE=975\nFRAME_CLASS=1")
        self.notes = QPlainTextEdit()
        for label, widget in (("Aircraft key", self.key), ("Name", self.name), ("Frame", self.frame),
                              ("Firmware", self.firmware), ("Vibration screening", self.vibration),
                              ("Expected parameters", self.expected), ("Notes", self.notes)):
            form.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.profile = None

    def save(self):
        from .api import Profile
        expected = {}
        try:
            for line in self.expected.toPlainText().splitlines():
                if line.strip():
                    name, value = line.split("=", 1)
                    expected[name.strip()] = float(value)
            body = Profile(drone_key=self.key.text().strip(), name=self.name.text().strip(), frame=self.frame.text(),
                           firmware=self.firmware.text(), notes=self.notes.toPlainText(),
                           vibration_warning=self.vibration.value(), expected_parameters=expected)
        except Exception as error:
            QMessageBox.warning(self, "Invalid profile", str(error))
            return
        self.profile = store.save_profile(body.model_dump())
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        store.initialize()
        self.job = None
        self.cache = {}
        self.process = None
        self.running_job = None
        self.cursor_record = None
        self.resize(1500, 950)
        self.build_menu()
        self.build_ui()
        self.refresh_logs()
        self.timer = QTimer(self, interval=1000, timeout=self.poll)
        self.timer.start()
        self.update_title()

    # ------------------------------------------------------------------ layout
    def build_menu(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("File")
        self.add_action(file_menu, "Open Log or Analysis…", self.open_log, "Ctrl+O")
        self.add_action(file_menu, "Save Analysis As (.ant)…", self.save_ant, "Ctrl+S")
        self.add_action(file_menu, "Export Report…", self.export_report, "Ctrl+E")
        self.add_action(file_menu, "Show Original File", self.show_original)
        file_menu.addSeparator()
        self.add_action(file_menu, "Exit", self.close, "Ctrl+Q")
        analysis = bar.addMenu("Analysis")
        self.add_action(analysis, "Re-analyse With Selected Profile", self.reanalyse)
        self.add_action(analysis, "Refresh Log List", self.refresh_logs, "F5")
        tools = bar.addMenu("Tools")
        self.add_action(tools, "Profile Editor…", self.edit_profile)
        self.add_action(tools, "Compare With Log…", self.compare_dialog)
        self.add_action(tools, "Aircraft Trends…", self.trends_dialog)
        tools.addSeparator()
        self.add_action(tools, "Preflight checklist…", self.launch_preflight)
        help_menu = bar.addMenu("Help")
        self.add_action(help_menu, "Limitations", self.show_limitations)
        self.add_action(help_menu, "About", lambda: QMessageBox.about(
            self, "About AnaTele", "AnaTele — local, read-only flight-log investigation.\n\n"
            "Findings are evidence-linked observations and screening hypotheses, never a crash verdict, "
            "probability or airworthiness approval."))

    def add_action(self, menu, text, slot, shortcut=None):
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def build_ui(self):
        # Left: logs, profile, episodes.
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("Recordings"))
        self.logs = QListWidget()
        self.logs.currentItemChanged.connect(self.log_selected)
        lv.addWidget(self.logs, 3)
        row = QHBoxLayout()
        self.profile = QComboBox()
        edit_profile = QPushButton("Profile Editor")
        edit_profile.clicked.connect(self.edit_profile)
        row.addWidget(self.profile, 1)
        row.addWidget(edit_profile)
        lv.addLayout(row)
        self.episodes = table(["#", "Phase", "Takeoff", "Crash report"])
        self.episodes.itemSelectionChanged.connect(self.episode_changed)
        lv.addWidget(self.episodes, 2)
        row = QHBoxLayout()
        open_button, report_button = QPushButton("Open Log"), QPushButton("Report")
        open_button.clicked.connect(self.open_log)
        report_button.clicked.connect(self.export_report)
        row.addWidget(open_button)
        row.addWidget(report_button)
        lv.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lv.addWidget(self.status)

        # Centre: plot. Right: axes selection.
        self.plot = PlotPanel(self.show_evidence)
        right = QWidget()
        rv = QVBoxLayout(right)
        source_box = QGroupBox("Source")
        sv = QFormLayout(source_box)
        self.source, self.epoch = QComboBox(), QComboBox()
        self.source.currentIndexChanged.connect(self.source_changed)
        self.epoch.currentIndexChanged.connect(self.redraw)
        sv.addRow("Stream", self.source)
        sv.addRow("Clock epoch", self.epoch)
        rv.addWidget(source_box)
        x_box = QGroupBox("X Axis")
        xv = QVBoxLayout(x_box)
        self.x_group = QButtonGroup(self)
        for index, text in enumerate(("Time from log start", "Time from takeoff")):
            button = QRadioButton(text)
            button.setChecked(index == 0)
            self.x_group.addButton(button, index)
            xv.addWidget(button)
        self.x_group.idToggled.connect(lambda *_: self.redraw())
        rv.addWidget(x_box)
        y_box = QGroupBox("Y Axis")
        yv = QVBoxLayout(y_box)
        self.signal_filter = QLineEdit()
        self.signal_filter.setPlaceholderText("Filter signals…")
        self.signal_filter.textChanged.connect(self.filter_signals)
        yv.addWidget(self.signal_filter)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.signal_host = QWidget()
        self.signal_layout = QVBoxLayout(self.signal_host)
        self.signal_layout.addStretch()
        scroll.setWidget(self.signal_host)
        yv.addWidget(scroll)
        rv.addWidget(y_box, 1)
        self.checks = {}

        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.plot)
        top.addWidget(right)
        top.setStretchFactor(0, 4)
        top.setStretchFactor(1, 1)

        # Tabs below the plot.
        self.tabs = QTabWidget()
        self.findings = table(["Severity", "Finding", "Detail"])
        self.hypotheses = table(["Episode", "Rank", "Hypothesis", "Assessment", "Support / contradictions", "Would discriminate"])
        self.rules = table(["Rule", "Scope", "State", "Detail"])
        events_page = QWidget()
        ev = QVBoxLayout(events_page)
        ev.setContentsMargins(0, 0, 0, 0)
        self.event_filter = QLineEdit()
        self.event_filter.setPlaceholderText("Filter events (text or type)…")
        self.event_filter.textChanged.connect(self.fill_events)
        self.events = table(["Time (s)", "Source", "Type", "Text"])
        ev.addWidget(self.event_filter)
        ev.addWidget(self.events)
        params_page = QWidget()
        pv = QVBoxLayout(params_page)
        pv.setContentsMargins(0, 0, 0, 0)
        prow = QHBoxLayout()
        self.param_filter = QLineEdit()
        self.param_filter.setPlaceholderText("Filter parameters…")
        self.param_filter.textChanged.connect(self.fill_parameters)
        self.diff_target = QComboBox()
        diff_button = QPushButton("Diff")
        diff_button.clicked.connect(self.run_diff)
        prow.addWidget(self.param_filter, 2)
        prow.addWidget(QLabel("Compare against"))
        prow.addWidget(self.diff_target, 2)
        prow.addWidget(diff_button)
        pv.addLayout(prow)
        self.params = table(["Name", "Value", "Encoding / other", "Meaning"])
        pv.addWidget(self.params)
        self.missions = table(["Episode", "Item", "Command", "Status", "Note"])
        self.flight_path = FlightPathPanel(self.show_evidence)
        self.evidence = QPlainTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setFont(QFont("Consolas", 9))
        for widget, name in ((self.findings, "Findings"), (self.hypotheses, "Hypotheses"), (self.rules, "Rules"),
                             (self.flight_path, "Flight Path"), (events_page, "Events"), (params_page, "Parameters"),
                             (self.missions, "Missions"), (self.evidence, "Evidence")):
            self.tabs.addTab(widget, name)
        for widget in (self.findings, self.hypotheses, self.rules, self.events, self.params, self.missions):
            widget.itemDoubleClicked.connect(self.item_evidence)

        # Statistics.
        stats_box = QGroupBox("Flight Statistics")
        grid = QGridLayout(stats_box)
        self.stats = {}
        names = ["Firmware", "Semantics", "Frame", "Records", "Armed episodes", "Status",
                 "Takeoff", "Airborne time", "Crash report", "Vibration Z (pre-event)", "EKF compass ratio",
                 "Tilt error mean", "Motor spread", "Battery monitoring", "Top hypothesis"]
        for index, name in enumerate(names):
            r, c = index % 5, (index // 5) * 2
            grid.addWidget(QLabel(name + ":"), r, c)
            value = QLabel("-")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.stats[name] = value
            grid.addWidget(value, r, c + 1)
        for column in (1, 3, 5):
            grid.setColumnStretch(column, 1)

        centre = QSplitter(Qt.Vertical)
        centre.addWidget(top)
        centre.addWidget(self.tabs)
        centre.addWidget(stats_box)
        centre.setStretchFactor(0, 5)
        centre.setStretchFactor(1, 3)
        centre.setStretchFactor(2, 0)

        main = QSplitter(Qt.Horizontal)
        main.addWidget(left)
        main.addWidget(centre)
        main.setStretchFactor(1, 1)
        main.setSizes([360, 1140])
        self.setCentralWidget(main)

    # ------------------------------------------------------------------ data
    def update_title(self):
        self.setWindowTitle("AnaTele" + (f" - {self.job['filename']}" if self.job else ""))

    def refresh_logs(self):
        current = self.job["id"] if self.job else None
        self.logs.blockSignals(True)
        self.logs.clear()
        for job in store.list_jobs():
            item = QListWidgetItem(f"{job['filename']}\n{job['status']} · {job['progress']}% · {job['created_at'][:16]}")
            item.setData(Qt.UserRole, job["id"])
            if job["status"] == "failed":
                item.setForeground(QColor(ACCENT))
            self.logs.addItem(item)
            if job["id"] == current:
                self.logs.setCurrentItem(item)
        self.logs.blockSignals(False)
        chosen = self.profile.currentData()
        self.profile.clear()
        self.profile.addItem("No profile — generic screening", None)
        for profile in store.list_profiles():
            self.profile.addItem(f"{profile['body']['name']} ({profile['drone_key']}) v{profile['version']}", profile["id"])
        index = self.profile.findData(chosen)
        self.profile.setCurrentIndex(max(index, 0))

    def poll(self):
        if self.process is None:
            job_id = store.claim()
            if job_id:
                self.start_worker(job_id)
        elif self.process.state() != QProcess.NotRunning and self.started.elapsed() > TIMEOUT_SECONDS * 1000:
            self.process.kill()
        if self.running_job:
            job = store.get_job(self.running_job)
            self.status.setText(f"Analysing {job['filename']} … {job['progress']}%")

    def start_worker(self, job_id):
        from PySide6.QtCore import QElapsedTimer
        self.running_job = job_id
        self.started = QElapsedTimer()
        self.started.start()
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(store.ROOT))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.setStandardOutputFile(str(store.DATA / "worker_logs" / f"{job_id}.txt"))
        from PySide6.QtCore import QProcessEnvironment
        from .worker import worker_command
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("TELEMETRY_DATA_DIR", str(store.DATA))
        self.process.setProcessEnvironment(environment)
        self.process.finished.connect(lambda *_: self.worker_done(job_id))
        command = worker_command(job_id)
        self.process.start(command[0], command[1:])

    def worker_done(self, job_id):
        job = store.get_job(job_id)
        if job["status"] == "processing":
            store.fail(job_id, "Parser process stopped (crash or 180-second deadline); see the worker log")
            job = store.get_job(job_id)
        self.process, self.running_job = None, None
        self.status.setText(f"{job['filename']}: {job['status']}" + (f" — {job['error']}" if job["error"] else ""))
        self.refresh_logs()
        self.select_job(job_id)

    def select_job(self, job_id):
        for row in range(self.logs.count()):
            if self.logs.item(row).data(Qt.UserRole) == job_id:
                self.logs.blockSignals(True)
                self.logs.setCurrentRow(row)
                self.logs.blockSignals(False)
                self.log_selected(self.logs.item(row))
                return

    def open_log(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open flight log or analysis", str(Path.home() / "Downloads"),
            "Logs and analyses (*.tlog *.bin *.log *.csv *.ant);;Flight logs (*.tlog *.bin *.log *.csv);;"
            "AnaTele analysis (*.ant)")
        if path:
            self.open_path(path)

    def open_path(self, path):
        try:
            if Path(path).suffix.lower() == ".ant":
                job_id = antfile.import_ant(path)
                self.status.setText(f"Opened shared analysis {Path(path).name}")
            else:
                job_id = import_file(path, self.profile.currentData())
                self.status.setText(f"Queued {Path(path).name}")
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Cannot open file", str(error))
            return
        self.refresh_logs()
        self.select_job(job_id)

    def save_ant(self):
        if not self.job or not self.job.get("manifest"):
            QMessageBox.information(self, "Save analysis", "Select a finished analysis first.")
            return
        include = False
        if store.has_original(self.job):
            box = QMessageBox(self)
            box.setWindowTitle("Save analysis (.ant)")
            box.setText("Include the original flight log in the .ant file?")
            box.setInformativeText(
                "Without it, the recipient sees all findings, hypotheses, plots and the flight path, "
                "but not raw record bytes, and cannot re-analyse.\n\n"
                "Both versions contain GPS positions and timestamps of the flight. Share only with people you trust.")
            without = box.addButton("Analysis only", QMessageBox.AcceptRole)
            with_original = box.addButton("Include original log", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(without)
            box.exec()
            if box.clickedButton() not in (without, with_original):
                return
            include = box.clickedButton() is with_original
        path, _ = QFileDialog.getSaveFileName(self, "Save analysis", str(Path.home() / f"{Path(self.job['filename']).stem}.ant"),
                                              "AnaTele analysis (*.ant)")
        if not path:
            return
        if not path.lower().endswith(".ant"):
            path += ".ant"
        try:
            antfile.export_ant(self.job["id"], path, include)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Cannot save analysis", str(error))
            return
        self.status.setText(f"Saved {Path(path).name} ({Path(path).stat().st_size / 1048576:.1f} MiB)")

    def reanalyse(self):
        if not self.job:
            return
        if not store.has_original(self.job):
            QMessageBox.information(self, "Re-analyse", "This shared analysis does not include the original log.")
            return
        job_id = import_file(self.job["path"], self.profile.currentData())
        # Keep the original filename for display.
        with store.connection() as db, db:
            db.execute("UPDATE jobs SET filename=? WHERE id=?", (self.job["filename"], job_id))
        self.refresh_logs()
        self.select_job(job_id)

    def log_selected(self, item, _previous=None):
        if item is None:
            return
        job = store.get_job(item.data(Qt.UserRole))
        self.job, self.cache, self.cursor_record = job, {}, None
        self.update_title()
        m = job.get("manifest")
        if not m:
            for widget in (self.findings, self.hypotheses, self.rules, self.events, self.params, self.missions, self.episodes):
                widget.setRowCount(0)
            self.plot.empty(f"{job['status']}" + (f": {job['error']}" if job["error"] else " — waiting for analysis"))
            self.source.blockSignals(True)
            self.source.clear()
            self.source.blockSignals(False)
            return
        if not m.get("rule_results") and m.get("version", "0") < "0.2":
            self.status.setText("Analysed by an older engine; use Analysis → Re-analyse for phases and rules.")
        self.source.blockSignals(True)
        self.source.clear()
        for source in m["sources"]:
            self.source.addItem(f"{source['key']} — {source['role']}", source["key"])
        vehicle = next((i for i, s in enumerate(m["sources"]) if s["role"] in ("vehicle", "onboard_log")), 0)
        self.source.setCurrentIndex(vehicle)
        self.source.blockSignals(False)
        self.diff_target.clear()
        self.diff_target.addItem("Attached profile", None)
        for other in store.list_jobs():
            if other["id"] != job["id"] and other["status"] in ("complete", "partial"):
                self.diff_target.addItem(f"{other['filename']} · {other['created_at'][:16]}", other["id"])
        self.fill_episodes()
        self.fill_tabs()
        self.fill_stats()
        self.source_changed()
        self.episode_changed()

    def rel(self, t_us):
        return None if t_us is None else round((t_us - self.job["manifest"]["first_time_us"]) / 1e6, 2)

    def fill_episodes(self):
        phases = self.job["manifest"].get("phases") or []
        episodes = self.job["manifest"]["armed_episodes"]
        rows, evidence = [("All", "whole recording", None, None)], [None]
        for index, episode in enumerate(episodes):
            phase = phases[index] if index < len(phases) else {}
            rows.append((index, (phase.get("status") or episode["airborne"]).replace("_", " "),
                         self.rel(phase.get("takeoff_us")), self.rel(phase.get("critical_event_us"))))
            evidence.append(phase.get("evidence") or [episode["start_record"]])
        self.episodes.blockSignals(True)
        fill(self.episodes, rows, evidence)
        crash = next((i + 1 for i, p in enumerate(phases) if p.get("critical_event_us")), 0)
        self.episodes.selectRow(crash)
        self.episodes.blockSignals(False)

    def episode_changed(self):
        self.redraw()
        if self.job and self.job.get("manifest"):
            episode, _ = self.current_phase()
            index = self.job["manifest"]["armed_episodes"].index(episode) if episode else None
            self.flight_path.load(self.job, index)

    def current_phase(self):
        rows = self.episodes.selectionModel().selectedRows() if self.episodes.selectionModel() else []
        index = rows[0].row() - 1 if rows else -1
        phases = self.job["manifest"].get("phases") or []
        episodes = self.job["manifest"]["armed_episodes"]
        if 0 <= index < len(episodes):
            return episodes[index], phases[index] if index < len(phases) else None
        return None, None

    def fill_tabs(self):
        m = self.job["manifest"]
        fill(self.findings, [(f["severity"], f["title"], f["detail"]) for f in m["findings"]],
             [f["evidence"] for f in m["findings"]])
        rows, evidence = [], []
        for incident in m.get("incidents", []):
            for rank, h in enumerate(incident["hypotheses"], 1):
                notes = [f"+ {s['title']} ({s['family']})" for s in h["support"]]
                notes += [f"− {c['title']}: {c['reason']}" for c in h["contradictions"]]
                notes += [f"blocked: {b}" for b in h["blocked_by"]]
                rows.append((incident["episode"], rank, h["title"], f"{h['label']} (score {h['score']})",
                             "\n".join(notes) or "no assessed evidence", h["discriminating_evidence"]))
                evidence.append(None)
        fill(self.hypotheses, rows, evidence)
        results = m.get("rule_results", [])

        def scope(r):
            s = r["scope"]
            return {"episode": f"episode {s.get('episode')}", "series": s.get("signal"),
                    "event": f"event @ {self.rel(s.get('t_us'))} s"}.get(s.get("type"), f"{s.get('type')} {s.get('source', '')}")
        order = {"triggered": 0, "unassessable": 1, "not_triggered": 2, "not_applicable": 3}
        results = sorted(results, key=lambda r: (order[r["state"]], r["id"]))
        fill(self.rules, [(f"{r['title']}\n{r['rule_id']} v{r['rule_version']}", scope(r), r["state"].replace("_", " "), r["detail"])
                          for r in results], [r["evidence"] for r in results])
        for row, r in enumerate(results):
            if r["state"] == "triggered":
                self.rules.item(row, 2).setForeground(QColor(ACCENT))
        rows, evidence = [], []
        for entry in m["missions"].get("execution", []):
            for item in entry["items"]:
                rows.append((entry["episode"], item["seq"], item["command"], item["status"].replace("_", " "), item.get("note")))
                evidence.append(item["evidence"])
        for tx in m["missions"]["transactions"]:
            rows.append(("transfer", tx["id"], tx.get("kind", tx["direction"]), tx["status"].replace("_", " "),
                         f"{tx['count']} items; " + " ".join(tx.get("notes", []))))
            evidence.append(tx["evidence"])
        fill(self.missions, rows, evidence)
        self.fill_events()
        self.fill_parameters()

    def fill_events(self):
        if not self.job or not self.job.get("manifest"):
            return
        text = self.event_filter.text().lower()
        rows, evidence = [], []
        for e in self.job["manifest"]["events"]:
            if e["kind"] in ("COMMAND_LONG", "COMMAND_ACK") and not text:
                continue
            if text and text not in (e["kind"] + " " + e["text"]).lower():
                continue
            rows.append((self.rel(e["t_us"]), e["source"], e["kind"], e["text"]))
            evidence.append(e["evidence"])
            if len(rows) >= 5000:
                break
        fill(self.events, rows, evidence)
        for row, (_, _, kind, _) in enumerate(rows):
            if kind in ("CRASH_CHECK_DISARM", "PREARM_BLOCK"):
                self.events.item(row, 2).setForeground(QColor(ACCENT))

    def snapshot(self):
        return comparison.vehicle_snapshot(self.job["manifest"]) if self.job and self.job.get("manifest") else None

    def fill_parameters(self):
        snapshot = self.snapshot()
        if snapshot is None:
            self.params.setRowCount(0)
            return
        text = self.param_filter.text().upper()
        names = [n for n in sorted(snapshot["values"]) if text in n][:2000]
        items = [snapshot["values"][n] for n in names]
        fill(self.params, [(n, i["value"] if i["value"] is not None else f"raw {i['raw_value']} (unverified)",
                            i["encoding"], (i.get("meta") or {}).get("description", "")) for n, i in zip(names, items)],
             [[i["record_id"]] for i in items])

    def run_diff(self):
        if not self.job or not self.job.get("manifest"):
            return
        other = self.diff_target.currentData()
        if other:
            result = comparison.parameter_diff(self.job["manifest"], store.get_job(other)["manifest"])
        else:
            profile = store.get_profile(self.job["profile_id"]) if self.job["profile_id"] else None
            result = comparison.parameter_diff(self.job["manifest"], profile=profile)
        if "error" in result:
            QMessageBox.information(self, "Parameter diff", result["error"])
            return
        rows = [(c["name"], c["left"], f"other: {c['right']}", (c.get("meta") or {}).get("description", ""))
                for c in result["changed"]]
        rows += [(u["name"], f"raw {u['left_raw']}", f"other raw: {u['right_raw']} (unverified)", "") for u in result["unverified_changes"]]
        fill(self.params, rows, [[c["left_record"]] if c.get("left_record") else None for c in result["changed"]]
             + [None] * len(result["unverified_changes"]))
        self.status.setText(f"{len(result['changed'])} typed differences. " + " ".join(result["notes"]))

    def fill_stats(self):
        m = self.job["manifest"]
        source = next((s for s in m["sources"] if s["role"] in ("vehicle", "onboard_log")), {})
        phases = m.get("phases") or []
        metrics = m.get("flight_metrics") or []
        crash = next((i for i, p in enumerate(phases) if p.get("critical_event_us")), None)
        index = crash if crash is not None else next((i for i, p in enumerate(phases) if p.get("takeoff_us")), None)
        phase = phases[index] if index is not None else {}
        flight = next((f for f in metrics if f["episode"] == index), {}) if index is not None else {}
        motor = (m.get("motor_maps") or {}).get(source.get("key"), {})
        power = next((r for r in m.get("rule_results", []) if r["rule_id"] == "power-monitoring-gap"), None)
        incident = next((i for i in m.get("incidents", []) if i["episode"] == index), None)
        top = incident["hypotheses"][0] if incident else None

        def fmt(value, unit=""):
            return "-" if value is None else f"{value:g}{unit}" if isinstance(value, (int, float)) else str(value)
        values = {
            "Firmware": f"{source.get('firmware_reported') or '-'} {('(' + source['firmware_git'] + ')') if source.get('firmware_git') else ''}",
            "Semantics": source.get("semantic_pack") or "-",
            "Frame": motor.get("frame") or "-",
            "Records": f"{m['record_count']:,}",
            "Armed episodes": str(len(m["armed_episodes"])),
            "Status": self.job["status"],
            "Takeoff": f"{fmt(self.rel(phase.get('takeoff_us')), ' s')} ({(phase.get('status') or '-').replace('airborne_', '')})",
            "Airborne time": fmt(flight.get("airborne_s"), " s"),
            "Crash report": fmt(self.rel(phase.get("critical_event_us")), " s"),
            "Vibration Z (pre-event)": f"mean {fmt(flight.get('vibration_z_mean'))} / max {fmt(flight.get('vibration_z_max'))}",
            "EKF compass ratio": f"max {fmt(flight.get('ekf_compass_ratio_max'))}",
            "Tilt error mean": fmt(flight.get("tilt_error_deg_mean"), "°"),
            "Motor spread": fmt(flight.get("motor_spread")),
            "Battery monitoring": "none (unassessable)" if power and power["state"] == "triggered" else "present" if power else "-",
            "Top hypothesis": f"{top['title']} — {top['label']}" if top else "-",
        }
        for name, label in self.stats.items():
            label.setText(values.get(name, "-"))

    # ------------------------------------------------------------------ plotting
    def source_changed(self):
        if not self.job or not self.job.get("manifest"):
            return
        key = self.source.currentData()
        source = next((s for s in self.job["manifest"]["sources"] if s["key"] == key), None)
        if source is None:
            return
        self.epoch.blockSignals(True)
        self.epoch.clear()
        for epoch in source["epoch_ids"]:
            self.epoch.addItem(str(epoch), epoch)
        self.epoch.blockSignals(False)
        previous = {name for name, check in self.checks.items() if check.isChecked()}
        for check in self.checks.values():
            check.deleteLater()
        self.checks = {}
        signals = sorted((s for s in self.job["manifest"]["signals"] if s["source"] == key), key=lambda s: s["name"])
        available = {s["name"] for s in signals}
        wanted = (previous & available) or {n for n in DEFAULT_SIGNALS if n in available} or {s["name"] for s in signals[:1]}
        for s in signals:
            check = QCheckBox(f"{s['name']} [{s['unit']}]")
            check.setChecked(s["name"] in wanted)
            check.toggled.connect(self.redraw)
            self.signal_layout.insertWidget(self.signal_layout.count() - 1, check)
            self.checks[s["name"]] = check
        self.filter_signals()
        self.redraw()

    def filter_signals(self):
        text = self.signal_filter.text().lower()
        for name, check in self.checks.items():
            check.setVisible(text in name.lower() or check.isChecked())

    def series(self, source, epoch, name):
        key = (source, epoch, name)
        if key not in self.cache:
            path = store.DATA / "datasets" / self.job["id"] / "signals.parquet"
            with duckdb.connect() as db:
                self.cache[key] = db.execute(
                    f"SELECT t_us, value, id, unit FROM read_parquet({sql_path(path)}) "
                    "WHERE source=? AND epoch=? AND name=? ORDER BY t_us, id", [source, epoch, name]).fetchall()
        return self.cache[key]

    def redraw(self):
        if not self.job or not self.job.get("manifest") or self.source.currentData() is None:
            return
        source, epoch = self.source.currentData(), self.epoch.currentData()
        episode, phase = self.current_phase()
        from_takeoff = self.x_group.checkedId() == 1 and phase and phase.get("takeoff_us")
        zero = phase["takeoff_us"] if from_takeoff else self.job["manifest"]["first_time_us"]
        x = lambda t: (t - zero) / 1e6
        groups = {}
        for name, check in self.checks.items():
            if not check.isChecked():
                continue
            rows = self.series(source, epoch, name)
            if not rows:
                continue
            xs, ys, ids = [], [], []
            last = None
            for t, value, rid, _ in rows:
                if last is not None and t - last > GAP_US:
                    xs.append(math.nan), ys.append(math.nan), ids.append(None)
                xs.append(x(t)), ys.append(value), ids.append(rid)
                last = t
            groups.setdefault(rows[0][3] or "", []).append((name, xs, ys, ids))
        markers, spans, x_range = [], [], None
        if episode and episode["source"] == source and episode["epoch"] == epoch:
            end = episode["end_us"] or self.job["manifest"]["last_time_us"]
            x_range = (x(episode["start_us"]) - 3, x(end) + 3)
            markers.append((x(episode["start_us"]), "armed", "#909090"))
            if episode["end_us"]:
                markers.append((x(episode["end_us"]), "disarmed", "#909090"))
            if phase:
                if phase.get("takeoff_us"):
                    markers.append((x(phase["takeoff_us"]), "takeoff", "#7ad17a"))
                if phase.get("critical_event_us"):
                    markers.append((x(phase["critical_event_us"]), "crash report", ACCENT))
                if phase.get("takeoff_us") and phase.get("pre_event_end_us") and phase["pre_event_end_us"] > phase["takeoff_us"]:
                    spans.append((x(phase["takeoff_us"]), x(phase["pre_event_end_us"]), "pre-event"))
        cursor = None
        if self.cursor_record:
            cursor = x(self.cursor_record)
        label = "Time from takeoff (s)" if from_takeoff else "Time from log start (s)"
        self.plot.draw(groups, label, x_range, markers, spans, cursor)

    # ------------------------------------------------------------------ evidence
    def item_evidence(self, item):
        ids = item.data(EVIDENCE_ROLE) or (item.tableWidget().item(item.row(), 0).data(EVIDENCE_ROLE)
                                           if item.tableWidget().item(item.row(), 0) else None)
        if ids:
            self.show_evidence(ids[0], ids)

    def show_evidence(self, record_id, all_ids=None):
        from .api import evidence
        try:
            record = evidence(self.job["id"], int(record_id))
        except Exception as error:
            self.evidence.setPlainText(f"Record {record_id} unavailable: {error}")
            self.tabs.setCurrentWidget(self.evidence)
            return
        header = (f"Record #{record['id']} · {record['message']} from {record['source']} · {record['locator_kind']} "
                  f"{record['offset']} · {record['quality']} · clock {record['clock']} · epoch {record['epoch']}\n"
                  f"Time: {self.rel(record['t_us'])} s from log start · file SHA-256 {record['artifact_sha256']}\n")
        if all_ids and len(all_ids) > 1:
            header += f"Evidence set ({len(all_ids)}): " + ", ".join(f"#{i}" for i in all_ids[:40]) + "\n"
        self.evidence.setPlainText(header + "\n" + json.dumps(record["payload"], indent=2) +
                                   ("\n\nRaw bytes:\n" + record["raw_hex_preview"] if record.get("raw_hex_preview") else "") +
                                   ("\n\n" + record["raw_note"] if record.get("raw_note") else ""))
        self.tabs.setCurrentWidget(self.evidence)
        if record["source"] == self.source.currentData():
            self.cursor_record = record["t_us"]
            self.redraw()

    # ------------------------------------------------------------------ tools
    def export_report(self):
        if not self.job or not self.job.get("manifest"):
            return
        from .api import report
        path, _ = QFileDialog.getSaveFileName(self, "Save report", str(Path.home() / f"{Path(self.job['filename']).stem}-report.html"),
                                              "HTML (*.html)")
        if path:
            Path(path).write_text(report(self.job["id"]), encoding="utf-8")
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def show_original(self):
        if self.job and not store.has_original(self.job):
            QMessageBox.information(self, "Original file", "This shared analysis does not include the original log.")
        elif self.job:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.job["path"]).parent)))

    def edit_profile(self):
        dialog = ProfileDialog(self)
        if dialog.exec():
            self.refresh_logs()
            self.profile.setCurrentIndex(self.profile.findData(dialog.profile["id"]))

    def show_limitations(self):
        items = self.job["manifest"]["limitations"] if self.job and self.job.get("manifest") else []
        QMessageBox.information(self, "Limitations", "\n\n".join(items) or "Open an analysed log to see its limitations.")

    def result_dialog(self, title, widgets):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(1000, 650)
        layout = QVBoxLayout(dialog)
        for widget in widgets:
            layout.addWidget(widget)
        dialog.exec()

    def compare_dialog(self):
        if not self.job or not self.job.get("manifest"):
            return
        others = [j for j in store.list_jobs() if j["id"] != self.job["id"] and j["status"] in ("complete", "partial")]
        if not others:
            QMessageBox.information(self, "Compare", "Analyse another log first.")
            return
        from PySide6.QtWidgets import QInputDialog
        labels = [f"{j['filename']} · {j['created_at'][:16]}" for j in others]
        choice, ok = QInputDialog.getItem(self, "Compare", "Compare with:", labels, 0, False)
        if not ok:
            return
        result = comparison.compare(self.job, store.get_job(others[labels.index(choice)]["id"]))
        metrics = table(["Metric (pre-event airborne)", "This log", "Other", "Δ"])
        fill(metrics, [(r["metric"], r["left"], r["right"], r["delta"]) for r in result["metrics"]])
        states = table(["Rule", "This log", "Other"])
        fill(states, [(r["rule_id"], r["left"], r["right"]) for r in result["rules"]])
        params = result["parameters"]
        note = QLabel(" ".join(result["notes"]) + (f"  Parameters: {len(params['changed'])} typed differences."
                                                  if "changed" in params else ""))
        note.setWordWrap(True)
        self.result_dialog(f"{result['left']['filename']} vs {result['right']['filename']}", [metrics, states, note])

    def trends_dialog(self):
        from PySide6.QtWidgets import QInputDialog
        default = store.get_profile(self.job["profile_id"])["drone_key"] if self.job and self.job.get("profile_id") else ""
        key, ok = QInputDialog.getText(self, "Aircraft trends", "Aircraft key:", text=default)
        if not ok or not key.strip():
            return
        data = comparison.trends(key.strip())
        keys = ["vibration_z_mean", "vibration_z_max", "ekf_compass_ratio_max", "tilt_error_deg_mean", "throttle_pct_mean", "motor_spread"]
        grid = table(["Log", "Ep.", "Takeoff", *keys, "Crash"])
        fill(grid, [(f["filename"], f["episode"], f["phase_status"].replace("airborne_", ""), *[f.get(k) for k in keys],
                     "yes" if f["crash_check"] else "no") for f in data["flights"]])
        figure = Figure(facecolor=PLOT_BG, tight_layout=True)
        canvas = FigureCanvasQTAgg(figure)
        ax = figure.add_subplot(111)
        self.plot.style(ax)
        for color, k in zip(COLORS, ("vibration_z_mean", "ekf_compass_ratio_max", "motor_spread")):
            points = data["series"][k]
            if points:
                ax.plot(range(1, len(points) + 1), [p["value"] for p in points], "o-", color=color, label=k)
        ax.set_xlabel("Flight (chronological)", color=FG)
        if ax.lines:
            ax.legend(facecolor="#262626", edgecolor="#555", labelcolor=FG, fontsize=8)
        note = QLabel(" ".join(data["notes"]))
        note.setWordWrap(True)
        self.result_dialog(f"Trends — {key} ({len(data['flights'])} flights)", [canvas, grid, note])

    def launch_preflight(self):
        """AnaTele Preflight talks to a live vehicle, so it runs as its own process, apart from this read-only app."""
        answer = QMessageBox.question(
            self, "Preflight checklist",
            "AnaTele Preflight connects to a LIVE vehicle (USB or telemetry) and can run calibrations, which change "
            "the vehicle's stored calibration. It opens in a separate window.\n\nContinue?")
        if answer != QMessageBox.Yes:
            return
        command = preflight_command()
        if not QProcess.startDetached(command[0], command[1:]):
            QMessageBox.warning(self, "Preflight checklist", "Could not start: " + " ".join(command))

    def closeEvent(self, event):
        if self.process and self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(5000)
        super().closeEvent(event)


def preflight_command():
    """Command line that opens AnaTele Preflight; a frozen build re-invokes its own executable."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "preflight"]
    return [sys.executable, "-m", "telemetry_platform.cli", "preflight"]


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AnaTele")
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "static" / "icon.png")))
    apply_theme(app)
    window = MainWindow()
    window.show()
    for argument in sys.argv[1:]:
        if Path(argument).is_file():
            window.open_path(argument)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
