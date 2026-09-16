# src/patient_intake.py
"""
Patient intake dialog.

A modal PyQt5 form that runs before the main pipeline starts. It collects the
participant ID, gender, session date and session number, validates each field
with clear inline error messages, and returns a dict to the caller once
everything is valid. The dashboard and the session CSV/metadata JSON use these
fields downstream.

No name is collected. Given name and family name were captured by earlier
versions of this form; they were removed so that nothing the pipeline writes
to disk can identify a participant directly. The participant ID is the only
identifier recorded, and resolving it to a person happens outside this system
against a register the pipeline never reads.

Gender and session number are radio groups rather than free text. The
requirement was to prevent "T for gender" style entries, and refusing free
typing removes that class of error at the point of entry instead of catching
it later. Neither gender option starts selected, so an unanswered field stays
distinguishable from a deliberate one.
"""

import re
import sys
from datetime import date

from PyQt5.QtCore import Qt, QDate
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
    QWidget, QFrame,
)

from config import Config


# A participant ID should be alphanumeric (plus underscore/dash) and reasonably short.
# Adjust here if the laboratory uses a different convention.
PATIENT_ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,32}$')


class PatientIntakeDialog(QDialog):
    """Modal form for capturing session info before a session starts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Session — Participant Information")
        self.setModal(True)
        # Drop the "?" context-help button Qt puts in the title bar of a
        # QDialog on Windows. Nothing here defines whatsThis text, so the
        # button does nothing when clicked and only invites confusion.
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setMinimumWidth(460)
        self.setStyleSheet("""
            QDialog { background-color: #1a1a1a; color: #ffffff; }
            QLabel  { color: #dddddd; }
            QLineEdit, QComboBox, QDateEdit, QSpinBox {
                background-color: #2a2a2a; color: #ffffff;
                border: 1px solid #555; border-radius: 3px; padding: 5px;
            }
            QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus {
                border: 1px solid #0099ff;
            }
            QPushButton {
                background-color: #0099ff; color: #ffffff; font-weight: bold;
                border: none; border-radius: 4px; padding: 8px 18px;
            }
            QPushButton:disabled { background-color: #555555; color: #aaaaaa; }
            QPushButton#cancel {
                background-color: #444444;
            }
        """)

        # The result dict, populated when the user clicks Start and validation passes.
        self.patient = None

        self._build_ui()
        # Listen for changes so we can re-validate live and clear errors as
        # the user types.
        self.id_edit.textChanged.connect(self._clear_field_error)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(12)
        outer.setContentsMargins(20, 18, 20, 18)

        title = QLabel("Participant information")
        title.setFont(QFont("Arial", 14, QFont.Bold))
        title.setStyleSheet("color: #0099ff;")
        outer.addWidget(title)

        subtitle = QLabel(
            "Enter the session details before starting.\n"
            "Fields marked with * are required. No name is recorded."
        )
        subtitle.setStyleSheet("color: #aaaaaa;")
        outer.addWidget(subtitle)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #333333;")
        outer.addWidget(line)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignRight)

        # Participant ID. This is the ONLY identifier the system records.
        # Given name and family name were collected by earlier versions of
        # this form and have been removed: nothing the pipeline writes to
        # disk may carry a name. Resolving an ID back to a person is done
        # outside this system, against a register the pipeline never reads.
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("alphanumeric, e.g. P0042")
        self.id_err = self._error_label()
        form.addRow(self._label("Participant ID *"), self._field_with_error(self.id_edit, self.id_err))

        # Gender — two radio buttons rather than a dropdown. With only two
        # options a list costs two clicks and hides both until opened;
        # radios show the full choice and take one click. Neither is
        # pre-selected, so the operator has to make the choice actively:
        # a default would be recorded silently whenever someone tabbed past
        # this row, and a wrong value here is undetectable afterwards.
        self.gender_group = QButtonGroup(self)
        gender_row = QHBoxLayout()
        gender_row.setSpacing(18)
        gender_row.setContentsMargins(0, 0, 0, 0)
        for value in ("F", "M"):
            rb = QRadioButton(value)
            rb.setStyleSheet("QRadioButton { color: #dddddd; }")
            self.gender_group.addButton(rb)
            rb.setProperty("value", value)
            gender_row.addWidget(rb)
        gender_row.addStretch()
        gender_widget = QWidget()
        gender_widget.setLayout(gender_row)
        self.gender_err = self._error_label()
        form.addRow(self._label("Gender *"),
                    self._field_with_error(gender_widget, self.gender_err))

        # Date — fixed to today and not adjustable. We still re-read
        # QDate.currentDate() at submit time (in case the dialog is left
        # open across midnight), so the label is purely informational.
        today_str = QDate.currentDate().toString("yyyy-MM-dd")
        self.date_label = QLabel(f"{today_str}  (today)")
        self.date_label.setStyleSheet(
            "color: #cccccc; padding: 5px; "
            "background-color: #232323; border: 1px solid #333; border-radius: 3px;"
        )
        form.addRow(self._label("Session date *"), self.date_label)

        # Session number — one radio per session, mutually exclusive. The
        # buttons are generated from Config.MAX_SESSION_NUMBER rather than
        # hardcoded, so a protocol with more visits still works by raising
        # that one constant. Session 1 is pre-selected, matching what the
        # spin box this replaced did.
        self.session_group = QButtonGroup(self)
        session_row = QHBoxLayout()
        session_row.setSpacing(18)
        session_row.setContentsMargins(0, 0, 0, 0)
        for n in range(1, max(1, int(Config.MAX_SESSION_NUMBER)) + 1):
            rb = QRadioButton(str(n))
            rb.setStyleSheet("QRadioButton { color: #dddddd; }")
            rb.setProperty("value", n)
            if n == 1:
                rb.setChecked(True)
            self.session_group.addButton(rb)
            session_row.addWidget(rb)
        session_row.addStretch()
        session_widget = QWidget()
        session_widget.setLayout(session_row)
        form.addRow(
            self._label("Session number *"),
            session_widget,
        )

        outer.addLayout(form)

        # Global error banner, shown when the user tries to submit incomplete info.
        self.global_err = QLabel("")
        self.global_err.setStyleSheet("color: #ff6666; font-weight: bold;")
        self.global_err.setWordWrap(True)
        outer.addWidget(self.global_err)

        # Buttons. ORDER MATTERS for the Enter-key default:
        # Qt makes the first QPushButton added with autoDefault=True
        # (the default for buttons inside a QDialog) the implicit
        # default. The earlier version added Cancel first, so pressing
        # Enter while the form was incomplete silently triggered
        # Cancel -- the operator saw the dialog disappear and assumed
        # the form had accepted. We now:
        #   - put Start first in the layout AND mark it as the default,
        #   - explicitly turn autoDefault OFF on Cancel,
        # so Enter always triggers Start (which runs _validate first
        # and surfaces missing fields).
        button_row = QHBoxLayout()
        button_row.addStretch()

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("cancel")
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.setDefault(False)
        self.cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(self.cancel_btn)

        self.start_btn = QPushButton("Start session")
        self.start_btn.setAutoDefault(True)
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        button_row.addWidget(self.start_btn)
        outer.addLayout(button_row)

        # Clear the gender error the moment either option is picked, so it
        # doesn't keep glaring after the field is already valid.
        self.gender_group.buttonClicked.connect(
            lambda _b: self.gender_err.setText(""))

    # ---- small UI helpers ----
    def _label(self, text):
        lab = QLabel(text)
        lab.setMinimumWidth(120)
        return lab

    def _error_label(self):
        err = QLabel("")
        err.setStyleSheet("color: #ff6666; font-size: 10px;")
        err.setWordWrap(True)
        return err

    def _field_with_error(self, field, err_label):
        wrapper = QWidget()
        v = QVBoxLayout(wrapper)
        v.setSpacing(2)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(field)
        v.addWidget(err_label)
        return wrapper

    def _mark_invalid(self, field, err_label, message):
        # Red border on the offending field plus a small explanation under it.
        field.setStyleSheet(field.styleSheet() + "border: 1px solid #ff6666;")
        err_label.setText(message)

    def _clear_field_error(self, *_):
        """Clear the red border + inline error label for whichever field
        the operator is currently editing. Mapping sender -> err label
        used to be missing (the loop body was a `pass`), so the inline
        error stayed visible even after the field had been corrected."""
        sender = self.sender()
        if isinstance(sender, QLineEdit):
            sender.setStyleSheet("")
        err_map = {
            self.id_edit: self.id_err,
        }
        err_label = err_map.get(sender)
        if err_label is not None:
            err_label.setText("")
        # Also clear the global banner whenever any single field changes,
        # so it doesn't keep accusing the operator after they've fixed it.
        self.global_err.setText("")

    def _clear_all_errors(self):
        self.id_edit.setStyleSheet("")
        for err in (self.id_err, self.gender_err):
            err.setText("")
        self.global_err.setText("")

    # ---- selection accessors ----
    def selected_gender(self) -> str:
        """"F", "M", or "" when neither radio has been chosen."""
        btn = self.gender_group.checkedButton()
        return btn.property("value") if btn is not None else ""

    def selected_session(self) -> int:
        """Chosen session number. Falls back to 1, which is pre-selected."""
        btn = self.session_group.checkedButton()
        return int(btn.property("value")) if btn is not None else 1

    # ---- validation + submit ----
    def _validate(self):
        """Return True if every field passes, otherwise mark errors and return False."""
        self._clear_all_errors()
        ok = True

        pid = self.id_edit.text().strip()
        if not pid:
            self._mark_invalid(self.id_edit, self.id_err, "Participant ID is required.")
            ok = False
        elif not PATIENT_ID_RE.match(pid):
            self._mark_invalid(self.id_edit, self.id_err,
                               "Letters, digits, underscore or dash only (max 32 characters).")
            ok = False

        # Gender. Neither radio starts checked, so an unanswered field is
        # distinguishable from a deliberate answer and cannot slip through
        # as a default.
        if self.selected_gender() not in ("F", "M"):
            self.gender_err.setText("Gender is required -- select F or M.")
            ok = False

        # Session date is locked to today, no validation needed.

        if not ok:
            self.global_err.setText("Please fix the highlighted fields and try again.")
        return ok

    def _on_start(self):
        if not self._validate():
            return
        self.patient = {
            "patient_id":     self.id_edit.text().strip(),
            "gender":         self.selected_gender(),
            "session_date":   QDate.currentDate().toString("yyyy-MM-dd"),
            "session_number": self.selected_session(),
        }
        self.accept()


def collect_patient_info():
    """
    Show the intake dialog and return the validated patient dict.
    Returns None if the user cancels.
    Safe to call from any context; creates a QApplication if there isn't one.
    """
    app = QApplication.instance()
    owns_app = False
    if app is None:
        app = QApplication(sys.argv)
        owns_app = True

    dlg = PatientIntakeDialog()
    result = dlg.exec_()

    info = dlg.patient if result == QDialog.Accepted else None

    if owns_app:
        # We created the QApplication here; we don't quit it because the rest
        # of the pipeline may want to use it. The caller is responsible.
        pass
    return info


if __name__ == "__main__":
    # Manual test: print the captured dict to stdout.
    import json
    info = collect_patient_info()
    if info is None:
        print("Cancelled.")
    else:
        print(json.dumps(info, indent=2))
