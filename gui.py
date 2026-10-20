#!/usr/bin/env python3
"""AutonomiNotary - PySide6 desktop front-end for the autonomi_timestamp engine.

The engine (autonomi_timestamp.Engine) is imported and runs inside a QThread,
so the UI never blocks and the app can be frozen into one executable with
PyInstaller (see build.spec). OpenTimestamps is used as a library; the only
external tool is `ant`, auto-detected next to the program, in the working
directory, on PATH, or chosen in the UI.

SECRET_KEY is handed only to the `ant` child process environment; it is never
written to disk or logged.

    python gui.py                 # GUI
    python gui.py --cli FILE ...  # headless engine CLI (same as autonomi_timestamp.py)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import autonomi_timestamp as engine_mod
from autonomi_timestamp import STAGE_TO_STEP, Cancelled, Engine, StepError, find_ant

STEPS = ("Upload File", "OTS Stamp", "Waiting for Bitcoin", "Final Proof Upload")


def run_cli():
    sys.exit(engine_mod.main(sys.argv[2:]))


if len(sys.argv) > 1 and sys.argv[1] == "--cli":
    run_cli()

from PySide6.QtCore import QDir, QObject, QSettings, Qt, QThread, QTimer, Signal, Slot  # noqa: E402
from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication, QPalette  # noqa: E402
from PySide6.QtWidgets import (QApplication, QFileDialog, QGridLayout,  # noqa: E402
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

class EngineWorker(QObject):
    """Runs Engine.start/resume in a QThread and relays its callbacks as signals."""

    log = Signal(str)
    event = Signal(str, dict)
    finished = Signal(int, str)   # code (0 done, 2 pending, 1 error, -1 cancelled), message

    def __init__(self, mode, target, secret_key, ant_path, interval_min):
        super().__init__()
        self.mode = mode
        self.target = target
        self.engine = Engine(
            ant=ant_path or None, secret_key=secret_key or None,
            interval_min=interval_min, max_wait_hours=0, wait=True,
            log=lambda m: self.log.emit(f"[{datetime.now():%H:%M:%S}] {m}"),
            on_event=lambda name, **kw: self.event.emit(name, kw))

    @Slot()
    def run(self):
        try:
            if self.mode == "resume":
                code = self.engine.resume(self.target)
            else:
                code = self.engine.start(self.target)
            self.finished.emit(code, "")
        except Cancelled:
            self.finished.emit(-1, "")
        except StepError as e:
            self.finished.emit(1, f"error: {e}")
        except Exception as e:  # never let the thread die silently
            self.finished.emit(1, f"unexpected error: {type(e).__name__}: {e}")

    def stop(self):
        """Thread-safe."""
        self.engine.cancel()


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------

class Stepper(QWidget):
    COLORS = {
        "todo":   ("#2b2f36", "#8b93a1", "#3a3f48"),
        "active": ("#1f3b63", "#ffffff", "#3d8bfd"),
        "done":   ("#1d3d2c", "#c9f3d9", "#2ea866"),
        "error":  ("#4a1f24", "#ffd5d9", "#d9534f"),
    }

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.labels = []
        for i, name in enumerate(STEPS):
            if i:
                arrow = QLabel("→")
                arrow.setStyleSheet("color:#5c6370; font-size:16px;")
                lay.addWidget(arrow)
            lbl = QLabel(f"[{i + 1}] {name}")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setMinimumHeight(38)
            self.labels.append(lbl)
            lay.addWidget(lbl, 1)
        self.set_progress(0, active=False)

    def set_progress(self, done_count, active=True, error=False):
        """Steps [0, done_count) are done; step done_count is active/error."""
        for i, lbl in enumerate(self.labels):
            if i < done_count:
                state = "done"
            elif i == done_count and error:
                state = "error"
            elif i == done_count and active:
                state = "active"
            else:
                state = "todo"
            bg, fg, border = self.COLORS[state]
            lbl.setStyleSheet(f"background:{bg}; color:{fg}; border:1px solid {border};"
                              "border-radius:6px; padding:6px 10px; font-weight:600;")


def copy_row(grid, row, title):
    grid.addWidget(QLabel(title), row, 0)
    field = QLineEdit()
    field.setReadOnly(True)
    btn = QPushButton("Copy")
    btn.setFixedWidth(70)
    btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(field.text()))
    grid.addWidget(field, row, 1)
    grid.addWidget(btn, row, 2)
    return field


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AutonomiNotary - Autonomi + Bitcoin proof of existence")
        self.resize(1000, 820)
        self.settings = QSettings("AutonomiNotary", "AutonomiNotary")
        self.thread = None
        self.worker = None
        self.state_file = None
        self.done_steps = 0
        self.next_check = None

        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setSpacing(10)

        # Wallet key + ant binary
        key_box = QGroupBox("Wallet & Tools")
        kg = QGridLayout(key_box)
        kg.addWidget(QLabel("SECRET_KEY"), 0, 0)
        self.key_edit = QLineEdit(os.environ.get("SECRET_KEY", ""))
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("EVM private key (hex) that pays for uploads")
        if self.key_edit.text():
            self.key_edit.setToolTip("prefilled from the SECRET_KEY environment variable")
        self.key_toggle = QPushButton("Show")
        self.key_toggle.setCheckable(True)
        self.key_toggle.setFixedWidth(90)
        self.key_toggle.toggled.connect(self.toggle_key)
        kg.addWidget(self.key_edit, 0, 1)
        kg.addWidget(self.key_toggle, 0, 2)

        kg.addWidget(QLabel("ant binary"), 1, 0)
        self.ant_edit = QLineEdit()
        self.ant_edit.setReadOnly(True)
        ant_btn = QPushButton("Choose...")
        ant_btn.setFixedWidth(90)
        ant_btn.clicked.connect(self.choose_ant)
        kg.addWidget(self.ant_edit, 1, 1)
        kg.addWidget(ant_btn, 1, 2)
        self.refresh_ant(self.settings.value("ant_path", "", str))
        main.addWidget(key_box)

        # File selection
        file_box = QGroupBox("File")
        fl = QHBoxLayout(file_box)
        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        self.file_edit.setPlaceholderText("choose a file to notarize")
        browse = QPushButton("Browse File")
        browse.clicked.connect(self.browse_file)
        fl.addWidget(self.file_edit)
        fl.addWidget(browse)
        main.addWidget(file_box)

        # Actions
        al = QHBoxLayout()
        self.start_btn = QPushButton("Start Notarization")
        self.start_btn.setObjectName("primary")
        self.resume_btn = QPushButton("Resume Pending")
        self.stop_btn = QPushButton("Cancel / Stop")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_new)
        self.resume_btn.clicked.connect(self.resume_pending)
        self.stop_btn.clicked.connect(self.stop_task)
        al.addWidget(self.start_btn)
        al.addWidget(self.resume_btn)
        al.addWidget(self.stop_btn)
        al.addStretch(1)
        al.addWidget(QLabel("Check every"))
        self.interval = QSpinBox()
        self.interval.setRange(1, 180)
        self.interval.setValue(20)
        self.interval.setSuffix(" min")
        al.addWidget(self.interval)
        main.addLayout(al)

        # Progress
        prog_box = QGroupBox("Progress")
        pl = QVBoxLayout(prog_box)
        self.stepper = Stepper()
        pl.addWidget(self.stepper)
        info = QHBoxLayout()
        self.last_check_lbl = QLabel("Last checked: -")
        self.countdown_lbl = QLabel("Next check: -")
        info.addWidget(self.last_check_lbl)
        info.addSpacing(30)
        info.addWidget(self.countdown_lbl)
        info.addStretch(1)
        pl.addLayout(info)
        self.status_lbl = QLabel("Status: idle")
        self.status_lbl.setWordWrap(True)
        pl.addWidget(self.status_lbl)
        main.addWidget(prog_box)

        # Log
        log_box = QGroupBox("Engine Log")
        ll = QVBoxLayout(log_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        ll.addWidget(self.log)
        main.addWidget(log_box, 1)

        # Results
        res_box = QGroupBox("Results")
        grid = QGridLayout(res_box)
        self.res_data = copy_row(grid, 0, "Original File URL")
        self.res_block = copy_row(grid, 1, "Bitcoin Block")
        self.res_proof = copy_row(grid, 2, "Final OTS Proof URL")
        main.addWidget(res_box)

        self.tick = QTimer(self)
        self.tick.timeout.connect(self.update_countdown)
        self.tick.start(1000)

    # ---- UI helpers -----------------------------------------------------

    def toggle_key(self, shown):
        self.key_edit.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)
        self.key_toggle.setText("Hide" if shown else "Show")

    def refresh_ant(self, override=""):
        found = find_ant(override or None)
        if found:
            self.ant_edit.setText(found)
            self.ant_edit.setStyleSheet("")
        else:
            self.ant_edit.setText("not found - place `ant` next to this program, "
                                  "add it to PATH, or choose it")
            self.ant_edit.setStyleSheet("color:#ff9aa2;")
        return found

    def ant_path(self):
        p = self.ant_edit.text()
        return p if Path(p).is_file() else ""

    def choose_ant(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose the ant binary",
                                              str(engine_mod.app_dir()))
        if path:
            if self.refresh_ant(path):
                self.settings.setValue("ant_path", path)

    def browse_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose file to notarize",
                                              self.file_edit.text() or str(Path.home()))
        if path:
            self.file_edit.setText(str(Path(path)))

    def set_running(self, running):
        self.start_btn.setEnabled(not running)
        self.resume_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def set_status(self, text):
        self.status_lbl.setText(f"Status: {text}")

    def require_ant(self):
        if self.ant_path():
            return True
        QMessageBox.warning(self, "ant not found",
                            "The Autonomi `ant` CLI was not found.\n\nPlace it next to "
                            "this program or in the working directory, add it to PATH, "
                            "or choose it with \"Choose...\".")
        return False

    # ---- Actions --------------------------------------------------------

    def start_new(self):
        path = self.file_edit.text()
        if not path or not Path(path).is_file():
            QMessageBox.warning(self, "No file", "Choose a file to notarize first.")
            return
        src = Path(path)
        state = engine_mod.state_path_for(src, src.parent)
        if state.exists():
            ans = QMessageBox.question(
                self, "Existing notarization",
                f"A state file already exists for this file:\n{state}\n\n"
                "Continue that notarization instead of starting over?")
            if ans != QMessageBox.Yes:
                return
        if not self.key_edit.text().strip():
            QMessageBox.warning(self, "Wallet key required",
                                "Enter the SECRET_KEY; uploads are paid from this wallet.")
            return
        if not self.require_ant():
            return
        self.state_file = state
        self.launch("start", str(src))

    def resume_pending(self):
        dlg = QFileDialog(self, "Choose a .state.json file")
        dlg.setFileMode(QFileDialog.ExistingFile)
        dlg.setNameFilters(["State files (*.state.json)", "All files (*)"])
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)  # so dot-files are listed
        dlg.setFilter(dlg.filter() | QDir.Hidden)
        if self.file_edit.text():
            dlg.setDirectory(str(Path(self.file_edit.text()).parent))
        if not dlg.exec():
            return
        state = Path(dlg.selectedFiles()[0])
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, "Invalid state file", str(e))
            return
        stage = data.get("stage")
        if stage not in STAGE_TO_STEP:
            QMessageBox.critical(self, "Invalid state file", f"Unknown stage: {stage!r}")
            return
        if stage != "done":
            if not self.require_ant():
                return
            if not self.key_edit.text().strip():
                ans = QMessageBox.question(
                    self, "No wallet key",
                    "No SECRET_KEY entered. Polling works without it, but the final "
                    "proof upload will fail. Continue anyway?")
                if ans != QMessageBox.Yes:
                    return
        self.file_edit.setText(data.get("file", ""))
        self.state_file = state
        self.launch("resume", str(state))

    def launch(self, mode, target):
        for f in (self.res_data, self.res_block, self.res_proof):
            f.clear()
        self.log.clear()
        self.next_check = None
        self.last_check_lbl.setText("Last checked: -")
        self.done_steps = 0
        self.stepper.set_progress(0)
        self.set_status("starting...")

        self.worker = EngineWorker(mode, target, self.key_edit.text().strip(),
                                   self.ant_path(), self.interval.value())
        self.thread = QThread(self)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.event.connect(self.on_event)
        self.worker.finished.connect(self.on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.set_running(True)
        self.thread.start()

    def stop_task(self):
        if self.worker:
            self.set_status("stopping...")
            self.stop_btn.setEnabled(False)
            self.worker.stop()

    # ---- Engine callbacks (GUI thread) ----------------------------------

    @Slot(str)
    def append_log(self, text):
        self.log.appendPlainText(text)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    @Slot(str, dict)
    def on_event(self, name, kw):
        if name == "state":
            self.state_file = Path(kw["state_file"])
            self.done_steps = STAGE_TO_STEP[kw["stage"]]
        elif name == "step":
            self.done_steps = kw["step"] - 1
            self.set_status(STEPS[self.done_steps] + "...")
        elif name == "check":
            self.last_check_lbl.setText(f"Last checked: {datetime.now():%H:%M:%S}")
            self.next_check = None
            self.set_status("checking OpenTimestamps calendars...")
        elif name == "pending":
            self.next_check = (datetime.now() + timedelta(seconds=kw["next_in"])
                               if kw["next_in"] else None)
            self.set_status(f"waiting for Bitcoin - {kw['reason']}")
        elif name == "anchored":
            self.next_check = None
            self.set_status(f"anchored in Bitcoin block {kw['height']}")
        self.stepper.set_progress(self.done_steps)

    def update_countdown(self):
        if not self.next_check:
            self.countdown_lbl.setText("Next check: -")
            return
        left = max(0, int((self.next_check - datetime.now()).total_seconds()))
        self.countdown_lbl.setText(f"Next check in: {left // 60:02d}:{left % 60:02d}")

    @Slot(int, str)
    def on_finished(self, code, message):
        self.set_running(False)
        self.worker = None
        self.thread = None
        self.next_check = None
        self.update_countdown()
        where = f"\nProgress is saved in:\n{self.state_file}" if self.state_file else ""
        if code == 0:
            self.stepper.set_progress(4)
            self.set_status("completed - proof anchored in Bitcoin and stored on Autonomi")
            self.show_results()
        elif code == -1:
            self.stepper.set_progress(self.done_steps, active=False)
            self.set_status("stopped by user")
            self.append_log(f"[gui] stopped. Progress saved in {self.state_file}")
        elif code == engine_mod.EXIT_PENDING:
            self.stepper.set_progress(self.done_steps)
            self.set_status("still pending - resume later")
        else:
            self.stepper.set_progress(self.done_steps, error=True)
            self.append_log(message)
            self.set_status(message.splitlines()[0])
            QMessageBox.critical(self, "Notarization failed", message + where)

    def show_results(self):
        try:
            s = engine_mod.load_state(self.state_file)
        except StepError as e:
            self.append_log(f"[gui] cannot read results: {e}")
            return
        self.append_log("\n" + engine_mod.summary_text(s))
        when = datetime.fromtimestamp(s["block_time"], timezone.utc)
        self.res_data.setText(f"ant://{s['data_address']}")
        self.res_block.setText(f"{s['block_height']}  -  {when:%Y-%m-%d %H:%M:%S} UTC")
        self.res_proof.setText(f"ant://{s['ots_address']}")

    # ---- Close ----------------------------------------------------------

    def closeEvent(self, event):
        if self.worker:
            where = self.state_file or "the .<file>.state.json next to your file"
            ans = QMessageBox.warning(
                self, "Task in progress",
                "A notarization is still running. Closing will stop the Bitcoin "
                f"polling.\n\nProgress is saved in:\n{where}\n\n"
                "You can continue later with \"Resume Pending\". Close anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.finished.disconnect(self.on_finished)
            self.worker.stop()
            # finished->quit is queued to this (blocked) thread, so quit directly;
            # the thread's event loop exits as soon as the engine returns.
            if self.thread:
                self.thread.quit()
            # Network calls time out within ~1 min; state writes are atomic, so
            # if the thread is still stuck after that it is safe to hard-exit.
            if self.thread and not self.thread.wait(60000):
                os._exit(0)
        event.accept()


# --------------------------------------------------------------------------
# Styling
# --------------------------------------------------------------------------

def apply_dark_theme(app):
    app.setStyle("Fusion")
    pal = QPalette()
    colors = {
        QPalette.Window: "#1e2127", QPalette.WindowText: "#d7dae0",
        QPalette.Base: "#16181d", QPalette.AlternateBase: "#22252b",
        QPalette.Text: "#d7dae0", QPalette.Button: "#2b2f36",
        QPalette.ButtonText: "#d7dae0", QPalette.Highlight: "#3d8bfd",
        QPalette.HighlightedText: "#ffffff", QPalette.ToolTipBase: "#2b2f36",
        QPalette.ToolTipText: "#d7dae0", QPalette.PlaceholderText: "#6b7280",
    }
    for role, c in colors.items():
        pal.setColor(role, QColor(c))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5c6370"))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#5c6370"))
    app.setPalette(pal)
    app.setStyleSheet("""
        QGroupBox { border:1px solid #333842; border-radius:8px; margin-top:12px;
                    padding:10px 8px 8px 8px; font-weight:600; }
        QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px;
                           color:#9aa4b2; }
        QLineEdit, QPlainTextEdit, QSpinBox { border:1px solid #333842; border-radius:5px;
                    padding:5px; background:#16181d; selection-background-color:#3d8bfd; }
        QLineEdit:read-only { color:#b8c0cc; }
        QPushButton { border:1px solid #3a3f48; border-radius:5px; padding:7px 14px;
                      background:#2b2f36; }
        QPushButton:hover { background:#353a43; }
        QPushButton:disabled { color:#5c6370; background:#24272d; }
        QPushButton#primary { background:#2563eb; border-color:#2563eb; color:white;
                              font-weight:600; }
        QPushButton#primary:hover { background:#3b76f0; }
        QPushButton#primary:disabled { background:#2a3550; color:#7d8799; }
        QPushButton#danger:enabled { border-color:#b4434a; color:#ffb3b8; }
        QPushButton#danger:hover:enabled { background:#4a1f24; }
    """)
    font = app.font()
    font.setPointSize(max(font.pointSize(), 10))
    app.setFont(font)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AutonomiNotary")
    apply_dark_theme(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
