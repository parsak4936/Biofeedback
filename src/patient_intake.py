# src/patient_intake.py
"""
Patient intake dialog.

A modal PyQt5 form that runs before the main pipeline starts. It collects the
patient's name, last name, ID, gender, the session date, and the session
number, validates each field with clear inline error messages, and returns a
dict to the caller once everything is valid. The dashboard and the session
CSV/baseline JSON all use these fields downstream.

The validation deliberately uses a gender dropdown (M / F) rather than a free
text input — the requirement was "no 'T' for gender" type mistakes, and the
cleanest way to enforce that is to not allow free typing in the first place.
"""

import re
import sys
from datetime import date

from PyQt5.QtCore import Qt, QDate
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QComboBox, QSpinBox, QPushButton,
    QWidget, QFrame,
)

from config import Config


# A patient ID should be alphanumeric (plus underscore/dash) and reasonably short.
# Adjust here if your clinic uses a different convention.
PATIENT_ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,32}$')
NAME_RE = re.compile(r"^[A-Za-z\s\-']{1,40}$")


class PatientIntakeDialog(QDialog):
    """Modal form for capturing patient info before a session starts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Session — Patient Information")
        self.setModal(True)
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
        for w in (self.name_edit, self.lastname_edit, self.id_edit):
            w.textChanged.connect(self._clear_field_error)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(12)
        outer.setContentsMargins(20, 18, 20, 18)

        title = QLabel("Patient information")
        title.setFont(QFont("Arial", 14, QFont.Bold))
        title.setStyleSheet("color: #0099ff;")
        outer.addWidget(title)

        subtitle = QLabel(
            "Fill in the patient's details before the session begins.\n"
            "Fields marked with * are required."
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

        # First name
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Alice")
        self.name_err = self._error_label()
        form.addRow(self._label("First name *"), self._field_with_error(self.name_edit, self.name_err))

        # Last name
        self.lastname_edit = QLineEdit()
        self.lastname_edit.setPlaceholderText("e.g. Rossi")
        self.lastname_err = self._error_label()
        form.addRow(self._label("Last name *"), self._field_with_error(self.lastname_edit, self.lastname_err))

        # Patient ID
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("alphanumeric, e.g. P0042")
        self.id_err = self._error_label()
        form.addRow(self._label("Patient ID *"), self._field_with_error(self.id_edit, self.id_err))

        # Gender — dropdown, not free text. Prevents the 'T for gender' problem
        # at the source rather than catching it in validation.
        self.gender_combo = QComboBox()
        self.gender_combo.addItems(["— select —", "F", "M"])
        self.gender_err = self._error_label()
        form.addRow(self._label("Gender *"), self._field_with_error(self.gender_combo, self.gender_err))

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

        # Session number — capped at Config.MAX_SESSION_NUMBER. Raise that
        # constant for multi-session protocols.
        self.session_spin = QSpinBox()
        self.session_spin.setMinimum(1)
        self.session_spin.setMaximum(max(1, int(Config.MAX_SESSION_NUMBER)))
        self.session_spin.setValue(1)
        form.addRow(
            self._label(f"Session number * (1-{Config.MAX_SESSION_NUMBER})"),
            self.session_spin,
        )

        outer.addLayout(form)

        # Global error banner, shown when the user tries to submit incomplete info.
        self.global_err = QLabel("")
        self.global_err.setStyleSheet("color: #ff6666; font-weight: bold;")
        self.global_err.setWordWrap(True)
        outer.addWidget(self.global_err)

        # Buttons
        button_row = QHBoxLayout()
        button_row.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("cancel")
        self.cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(self.cancel_btn)

        self.start_btn = QPushButton("Start session")
        self.start_btn.clicked.connect(self._on_start)
        button_row.addWidget(self.start_btn)
        outer.addLayout(button_row)

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
        sender = self.sender()
        if isinstance(sender, QLineEdit):
            sender.setStyleSheet("")
        for err in (self.name_err, self.lastname_err, self.id_err, self.gender_err):
            # Best-effort: clear whatever the user is interacting with.
            pass

    def _clear_all_errors(self):
        for field in (self.name_edit, self.lastname_edit, self.id_edit):
            field.setStyleSheet("")
        for err in (self.name_err, self.lastname_err, self.id_err, self.gender_err):
            err.setText("")
        self.global_err.setText("")

    # ---- validation + submit ----
    def _validate(self):
        """Return True if every field passes, otherwise mark errors and return False."""
        self._clear_all_errors()
        ok = True

        name = self.name_edit.text().strip()
        if not name:
            self._mark_invalid(self.name_edit, self.name_err, "First name is required.")
            ok = False
        elif not NAME_RE.match(name):
            self._mark_invalid(self.name_edit, self.name_err,
                               "Letters, spaces, hyphens and apostrophes only (max 40).")
            ok = False

        lastname = self.lastname_edit.text().strip()
        if not lastname:
            self._mark_invalid(self.lastname_edit, self.lastname_err, "Last name is required.")
            ok = False
        elif not NAME_RE.match(lastname):
            self._mark_invalid(self.lastname_edit, self.lastname_err,
                               "Letters, spaces, hyphens and apostrophes only (max 40).")
            ok = False

        pid = self.id_edit.text().strip()
        if not pid:
            self._mark_invalid(self.id_edit, self.id_err, "Patient ID is required.")
            ok = False
        elif not PATIENT_ID_RE.match(pid):
            self._mark_invalid(self.id_edit, self.id_err,
                               "Letters, digits, underscore or dash only (max 32 characters).")
            ok = False

        gender = self.gender_combo.currentText()
        if gender not in ("F", "M"):
            self.gender_err.setText("Please select F or M.")
            ok = False

        # Session date is locked to today, no validation needed.

        if not ok:
            self.global_err.setText("Please fix the highlighted fields and try again.")
        return ok

    def _on_start(self):
        if not self._validate():
            return
        self.patient = {
            "first_name":     self.name_edit.text().strip(),
            "last_name":      self.lastname_edit.text().strip(),
            "patient_id":     self.id_edit.text().strip(),
            "gender":         self.gender_combo.currentText(),
            "session_date":   QDate.currentDate().toString("yyyy-MM-dd"),
            "session_number": self.session_spin.value(),
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
