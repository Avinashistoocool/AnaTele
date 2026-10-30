"""AnaTele Preflight window (PySide6). Talks to a LIVE vehicle; separate from the read-only analyser.

The heavy lifting (MAVLink I/O, waits) runs on a worker thread; events are marshalled to the GUI through
Qt signals. Guided steps show their instruction in the prompt area and wait for Next / Cancel.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import __version__
from . import report as reports
from .link import LinkError
from .steps import FAIL, NEEDS_USER, PASS, SKIPPED, STEPS, WARN, Options, Prompter, Runner, load_profile, save_profile

STATUS_COLORS = {PASS: "#7ad17a", WARN: "#e0c35a", FAIL: "#e05a5a", SKIPPED: "#909090", NEEDS_USER: "#5ab4e0",
                 "RUNNING": "#dcdcdc", "": "#707070"}
SAFETY = ("This tool talks to a LIVE vehicle. Calibrations overwrite stored calibration values; compass and "
          "accelerometer calibration need you to move the vehicle when prompted. The vehicle is never armed. "
          "The motor test only runs when 'Props removed' is ticked.")


class Bridge(QObject):
    event = Signal(str, object)
    prompt = Signal(str, str, object)
    prompt_hidden = Signal()


class GuiPrompter(Prompter):
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge

    def show(self, key, text, options):
        self.bridge.prompt.emit(key, text, list(options))

    def hide(self):
        self.bridge.prompt_hidden.emit()


class PreflightWindow(QMainWindow):
    def __init__(self, connect="", baud=115200, profile=None, report_dir=None, skip=()):
        super().__init__()
        self.setWindowTitle(f"AnaTele Preflight {__version__}: calibration and pre-flight checklist")
        self.resize(1100, 760)
        self.bridge = Bridge()
        self.bridge.event.connect(self.on_event)
        self.bridge.prompt.connect(self.show_prompt)
        self.bridge.prompt_hidden.connect(self.hide_prompt)
        self.prompter = GuiPrompter(self.bridge)
        self.link = None
        self.runner = None
        self.worker = None
        self.profile = None
        self.skip = set(skip)
        self.last_report = None
        self.report_dir = Path(report_dir) if report_dir else Path.home() / "AnaTele Preflight"
        self.build_ui(connect, baud)
        if profile:
            self.load_profile(profile)

    # -- layout

    def build_ui(self, connect, baud):
        root = QWidget()
        layout = QVBoxLayout(root)
        warning = QLabel(SAFETY)
        warning.setWordWrap(True)
        warning.setStyleSheet("color:#e0c35a;padding:4px")
        layout.addWidget(warning)

        conn_box = QGroupBox("Connection")
        row = QHBoxLayout(conn_box)
        self.connect_edit = QLineEdit(connect)
        self.connect_edit.setPlaceholderText("COM3, /dev/ttyACM0, udpin:0.0.0.0:14550, tcp:127.0.0.1:5760 or sim:")
        self.baud = QComboBox()
        self.baud.setEditable(True)
        self.baud.addItems(["115200", "57600", "921600", "500000", "230400"])
        self.baud.setCurrentText(str(baud))
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.connect_clicked)
        self.vehicle_label = QLabel("Not connected")
        self.vehicle_label.setStyleSheet("color:#909090")
        row.addWidget(QLabel("Connection"))
        row.addWidget(self.connect_edit, 3)
        row.addWidget(QLabel("Baud"))
        row.addWidget(self.baud)
        row.addWidget(self.connect_button)
        row.addWidget(self.vehicle_label, 2)
        layout.addWidget(conn_box)

        actions = QHBoxLayout()
        self.run_button = QPushButton("Run full preflight")
        self.run_button.setMinimumHeight(40)
        font = self.run_button.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.run_button.setFont(font)
        self.run_button.clicked.connect(self.run_clicked)
        self.abort_button = QPushButton("Abort")
        self.abort_button.setEnabled(False)
        self.abort_button.clicked.connect(self.abort_clicked)
        self.props = QCheckBox("Props removed (allow motor test)")
        self.props.setToolTip("Tick ONLY when every propeller is physically removed. Motors will spin.")
        self.profile_label = QLabel("No parameter profile")
        load = QPushButton("Load profile…")
        load.clicked.connect(lambda: self.load_profile())
        save = QPushButton("Save current as profile…")
        save.clicked.connect(self.save_profile_clicked)
        self.export_button = QPushButton("Export report…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_clicked)
        self.busy_widgets = [self.run_button, self.connect_button, load, save]
        actions.addWidget(self.run_button, 2)
        actions.addWidget(self.abort_button)
        actions.addWidget(self.props)
        actions.addStretch()
        actions.addWidget(self.profile_label)
        actions.addWidget(load)
        actions.addWidget(save)
        actions.addWidget(self.export_button)
        layout.addLayout(actions)

        self.prompt_box = QGroupBox("Action needed")
        prompt_row = QHBoxLayout(self.prompt_box)
        self.prompt_text = QLabel()
        self.prompt_text.setWordWrap(True)
        self.prompt_text.setStyleSheet("font-size:11pt;padding:4px")
        prompt_row.addWidget(self.prompt_text, 1)
        self.prompt_buttons = {}
        for option in ("Next", "Yes", "No", "Cancel"):
            button = QPushButton(option)
            button.setMinimumWidth(90)
            button.clicked.connect(lambda _=False, o=option: self.prompter.respond(o))
            prompt_row.addWidget(button)
            self.prompt_buttons[option] = button
        self.prompt_box.setVisible(False)
        layout.addWidget(self.prompt_box)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFormat("")
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        splitter = QSplitter(Qt.Vertical)
        self.table = QTableWidget(len(STEPS), 3)
        self.table.setHorizontalHeaderLabels(["Step", "Status", "Result"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setWordWrap(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        for index, (_id, title, _func) in enumerate(STEPS):
            self.table.setItem(index, 0, QTableWidgetItem(f"{index + 1}. {title}"))
            self.set_status(index, "", "")
        splitter.addWidget(self.table)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        splitter.addWidget(self.log)
        splitter.setSizes([380, 220])
        layout.addWidget(splitter, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Connect a vehicle (or type sim: for the simulated vehicle), then Run full preflight.")

    def set_status(self, index, status, message):
        item = QTableWidgetItem(status or "—")
        item.setForeground(QColor(STATUS_COLORS.get(status, "#dcdcdc")))
        bold = item.font()
        bold.setBold(True)
        item.setFont(bold)
        self.table.setItem(index, 1, item)
        self.table.setItem(index, 2, QTableWidgetItem(message))
        self.table.resizeRowToContents(index)

    def append_log(self, text):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    # -- prompts

    def show_prompt(self, key, text, options):
        self.prompt_text.setText(text)
        for option, button in self.prompt_buttons.items():
            button.setVisible(option in options)
        self.prompt_box.setVisible(True)
        self.append_log("PROMPT: " + text)
        self.raise_()
        self.activateWindow()

    def hide_prompt(self):
        self.prompt_box.setVisible(False)

    # -- worker

    def options(self):
        try:
            baud = int(self.baud.currentText())
        except ValueError:
            baud = 115200
        return Options(connect=self.connect_edit.text().strip(), baud=baud, skip=set(self.skip),
                       props_removed=self.props.isChecked(), profile=self.profile)

    def busy(self, value):
        for widget in self.busy_widgets:
            widget.setEnabled(not value)
        self.abort_button.setEnabled(value)
        self.connect_edit.setEnabled(not value and self.link is None)

    def start(self, only=None):
        if self.worker and self.worker.is_alive():
            return
        options = self.options()
        if self.link is None and not options.connect:
            QMessageBox.warning(self, "Preflight", "Enter a connection string first (for example COM3 or sim:).")
            return
        if only is None:
            for index in range(len(STEPS)):
                self.set_status(index, "", "")
        self.runner = Runner(self.link, options, self.prompter,
                             on_event=lambda kind, **data: self.bridge.event.emit(kind, data))
        self.started = time.time()
        self.busy(True)
        self.worker = threading.Thread(target=self.work, args=(only,), daemon=True)
        self.worker.start()

    def work(self, only):
        try:
            self.runner.run(only=only)
        except Exception as error:  # surfaced in the log; the runner already catches step errors
            self.bridge.event.emit("log", {"text": f"Error: {error}"})
        self.bridge.event.emit("done", {"only": only})

    def on_event(self, kind, data):
        if kind == "step_started":
            self.set_status(data["index"], "RUNNING", "")
            self.progress.setValue(0)
            self.progress.setFormat(data["title"])
            self.append_log(f"== {data['title']}")
        elif kind == "step_finished":
            res = data["result"]
            self.set_status(data["index"], res.status, res.message)
            self.append_log(f"{res.status}: {res.title}: {res.message}")
        elif kind == "progress":
            self.progress.setValue(int(data["fraction"] * 1000))
            if data.get("text"):
                self.progress.setFormat(data["text"])
        elif kind == "statustext":
            self.append_log(f"vehicle: {data['text']}")
        elif kind == "log":
            self.append_log(data["text"])
        elif kind == "done":
            self.finished(data["only"])

    def finished(self, only):
        self.busy(False)
        self.hide_prompt()
        runner = self.runner
        if runner.link is not None:
            self.link = runner.link
        self.connect_edit.setEnabled(self.link is None)
        vehicle = runner.vehicle
        connect = next((r for r in runner.results if r.id == "connect"), None)
        if connect and connect.status == FAIL:
            if self.link is not None:
                self.link.close()
            self.link = None
            self.connect_edit.setEnabled(True)
            self.vehicle_label.setText("Not connected")
            self.connect_button.setText("Connect")
        elif vehicle:
            self.vehicle_label.setText(f"{vehicle.get('vehicle_type')} · {vehicle.get('firmware', 'firmware ?')} · "
                                       f"sys {vehicle.get('system_id')}")
            self.vehicle_label.setStyleSheet("color:#7ad17a")
            self.connect_button.setText("Disconnect")
        if only is None:
            self.last_report = reports.build(runner.results, vehicle, runner.options, self.started)
            self.export_button.setEnabled(True)
            self.progress.setValue(1000)
            self.progress.setFormat(f"Finished: {self.last_report['overall']}")
            try:
                paths = reports.write(self.last_report, self.report_dir)
                self.append_log(f"Report saved: {paths[0]} and {paths[1]}")
                self.statusBar().showMessage(f"Overall {self.last_report['overall']}. Report saved to {self.report_dir}")
            except OSError as error:
                self.append_log(f"Could not save report to {self.report_dir}: {error}")
        else:
            self.progress.setFormat("Connected" if self.link else "Not connected")

    # -- buttons

    def connect_clicked(self):
        if self.link is not None:
            self.link.close()
            self.link = None
            self.connect_button.setText("Connect")
            self.vehicle_label.setText("Not connected")
            self.vehicle_label.setStyleSheet("color:#909090")
            self.connect_edit.setEnabled(True)
            self.append_log("Disconnected")
            return
        self.start(only={"connect"})

    def run_clicked(self):
        if self.props.isChecked() and "motors" not in self.skip:
            answer = QMessageBox.warning(
                self, "Motor test", "The motor test will spin each motor in turn.\n\nAre ALL propellers removed?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self.props.setChecked(False)
        self.start()

    def abort_clicked(self):
        if self.runner:
            self.runner.abort()
            self.append_log("Abort requested: the current step stops at its next check.")

    def load_profile(self, path=None):
        path = path or QFileDialog.getOpenFileName(self, "Load parameter profile", "", "Profile (*.json)")[0]
        if not path:
            return
        try:
            self.profile = load_profile(path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Profile", f"Cannot load {path}: {error}")
            return
        self.profile.setdefault("name", Path(path).stem)
        self.profile_label.setText(f"Profile: {self.profile['name']} ({len(self.profile['params'])} params)")

    def save_profile_clicked(self):
        if self.link is None:
            QMessageBox.information(self, "Profile", "Connect to the vehicle first.")
            return
        path = QFileDialog.getSaveFileName(self, "Save current parameters as profile", "profile.json",
                                           "Profile (*.json)")[0]
        if not path:
            return
        self.busy(True)
        self.progress.setFormat("Downloading parameters")

        def job():
            try:
                profile = save_profile(self.link, path, Path(path).stem,
                                       progress=lambda f: self.bridge.event.emit("progress", {"fraction": f}))
                self.bridge.event.emit("log", {"text": f"Saved profile {path} ({len(profile['params'])} parameters)"})
            except (LinkError, OSError) as error:
                self.bridge.event.emit("log", {"text": f"Profile not saved: {error}"})
            self.bridge.event.emit("profile_done", {})

        self.bridge.event.connect(self.profile_done)
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def profile_done(self, kind, _data):
        if kind == "profile_done":
            self.bridge.event.disconnect(self.profile_done)
            self.busy(False)

    def export_clicked(self):
        folder = QFileDialog.getExistingDirectory(self, "Export report to folder", str(self.report_dir))
        if folder and self.last_report:
            paths = reports.write(self.last_report, folder)
            self.append_log(f"Report exported: {paths[0]} and {paths[1]}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def closeEvent(self, event):
        if self.worker and self.worker.is_alive():
            answer = QMessageBox.question(self, "Preflight", "A step is still running. Abort and close?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            if self.runner:
                self.runner.abort()
            self.worker.join(5)
        if self.link is not None:
            self.link.close()
        super().closeEvent(event)


def main(connect="", baud=115200, profile=None, report_dir=None, skip=()):
    from ..desktop import apply_theme
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AnaTele Preflight")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent.parent / "static" / "icon.png")))
    apply_theme(app)
    window = PreflightWindow(connect, baud, profile, report_dir, skip)
    window.show()
    return app.exec()
