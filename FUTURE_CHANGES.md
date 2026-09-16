# Future Changes and Decisions Log

Running log of the changes we discussed and the decisions the
supervisor has already made. Split into two parts: what has been
decided (Section 1) and what is still open (Section 2). Third
section carries the technical to-do list that follows from those
decisions.

Updated after the supervisor meeting.

---

## 1. Decisions the supervisor has made

Concrete answers we already have. These become the ground truth for
the ethics paragraph in the paper and drive the code refactor.

### 1.1 Data protection and privacy

Name and last name. To be removed from every file the biofeedback
engine writes. The intake form keeps them briefly to let the
operator confirm the participant on the day, but nothing that lands
on disk stores them. Session folder names stop using the name too.

Patient identifier. Stays. Random or lab-assigned ID like `P031`.
Everything downstream references only the ID.

Data retention. Two horizons: five years for the technical files
(samples.csv, diagnostic.csv, metadata.json, unity_udp.csv), ten
years for the research-facing artifacts (analysis outputs, published
figures, derived tables). After the respective horizon expires the
files are destroyed.

Data controller. Lab admin: Elaheh Bakhtiari (verify spelling
before it goes into the consent form).

Encryption at rest. Not currently in place. Data sits on the lab
workstation. Not a blocker for the current stage; may need to
change once we start sharing recordings between machines.

Data sharing. Files are shared on request only. There is no cloud
sync, no automatic upload, no backup service touching the data.

Right to erasure. A participant can request removal at any time.
Because there are no cross-references outside the session folder,
deleting the folder is sufficient. With name/lastname stripped, the
operator uses the participant ID from the intake form (kept off
disk) to find the folder to delete.

Reference to prior work at this lab. The admin has already worked
on an earlier version of this experiment without the biofeedback
loop; see the `Daghooghi_et_al_International_Conference_on_
Research_in_Psychology (1).docx` in the references folder. Any
paper we write should be aligned with the framing that document
uses, so the two versions of the study read as continuous rather
than parallel.

### 1.2 Codebase scope

Do not delete backends. Both NeuroKit2 and biosignalsnotebooks stay
in the code. Comparison between them is part of the story.

Do not delete mock data. The MockDataSource stays. Being able to
replay recorded sessions is part of the reproducibility argument.

Only visual / non-behavioural fixes for now. Anything that changes
what the pipeline computes needs supervisor sign-off. Anything that
just improves the operator's view of the same data is fine to
adjust.

### 1.3 Behavioural fix explicitly approved

Reset the HRV 60-second window when baseline starts rather than
when the application launches. Reason: operator delay between
launching the app and clicking Start Baseline is variable, and
the current behaviour means some sessions have HRV ready at the
start of live and others do not. The engine should protect against
this operator-side timing issue so that HRV becomes available at a
predictable time relative to the clinical event, not to the
application launch.

### 1.4 Documentation format

Primary format for the paper is LaTeX. Some co-authors do not use
LaTeX, so a Word version needs to be generated alongside. Solution:
the source stays as Markdown (`METHODOLOGY.md`), and we produce
both `.tex` and `.docx` from it as artifacts.

---

## 2. Still open

These need further clarification either from the supervisor, the
university Data Protection Officer, or the psychology co-authors.

Physiological signals — biometric or health data under GDPR
Article 9? Affects the consent language. Ask the university DPO or
copy whatever framing the earlier Daghooghi paper used.

DPIA required? Health-adjacent data usually triggers Article 35 in
the Italian framework. Confirm.

Consent form template. Does the university provide one, or do we
write our own? The admin (Elaheh) probably knows what the earlier
study used.

Ethics committee approval number. If a blanket protocol exists for
this lab, note the number for the paper. If not, apply now.

Left-panel readouts on the dashboard. Currently show phEDA, dHR,
dHRV, threshold MILD, threshold HIGH as numeric values. Are they
used by the operator, or clutter? If the answer from the operator
side is "clutter", remove them.

Server audit — which type. Runtime health check, data integrity
check, or config drift check. Or all three. Decision needed before
we scope this work.

---

## 3. Technical to-do (follows from Section 1)

Ordered by the sequence we will apply the changes.

### 3.1 Documentation

Update `METHODOLOGY.md` §12 ethics paragraph with concrete answers
from Section 1.1. Fill in retention periods, data controller name,
and reference to Daghooghi et al.

Add the Daghooghi reference to `METHODOLOGY.md` §13.

Consolidate the outdated MD files at the root. Not deletion —
merger. Everything that documents the actual system (data flow,
mock mode, outputs, tuning, concepts) becomes one file
`DOCUMENTATION.md`; the standalone MDs that only made sense at the
time of writing (`PAPER_DRAFT.md`, `METHODS.md` since it is now
duplicated in `METHODOLOGY.md`) get removed. Backend and mock-mode
material stays; the files just get organised into one place.

Generate `.tex` and `.docx` versions of `METHODOLOGY.md` for the
co-authors.

Update `DOWNLOAD_MANIFEST.md` in `references/` to reflect what has
actually been downloaded, what is still missing, and what the
alternative citation strategy is for each missing paper.

Rewrite `README.md` as a proper Github landing page: what the
project is, who it is for, how to install, how to run, where the
docs live.

### 3.2 Code — visual / non-behavioural

Dashboard cleanup on the baseline panel: remove the artifact
counters (EDA, HR, HRV), remove the threshold MILD and HIGH
displays, remove the sigma baseline display. Keep only the four
physiology charts and the state indicator.

Dashboard cleanup on the bottom Data Quality strip: keep the
Samples counter and the Unity link pill; artifact counters can go
if the operator does not use them (still open — see Section 2).

Chart Y-axis auto-scale bug. Currently a chart initialised with a
range of 0 to 50 does not expand when the signal goes to 89. Fix:
at each tick, if the visible data max exceeds the current Y-max,
expand upward; never shrink below the initial default half-range.

### 3.3 Code — defensive

Stream-loss handler. If the LSL stream to OpenSignals stops
delivering samples for longer than the acquisition deadman
timeout, exit the app cleanly with a clear message to the operator
instructing them to restart. Currently the pipeline holds the last
valid value forever, which can look like nothing is wrong when
actually the sensor died.

### 3.4 Code — HRV window fix (behavioural, prof-approved)

Reset the RMSSD 60-second buffer when the operator clicks Start
Baseline, not when the app launches. Reason in Section 1.3.

### 3.5 Code — GDPR (name/lastname removal)

Update `src/patient_intake.py` to keep name/lastname in memory
only for the operator's confirmation, and to write only patient
ID + gender + session number + session date into the intake dict
that flows to `metadata.json` and `samples.csv`.

Update `launcher.py` to not put name in the session folder name.
New format: `<patientId>_Session<n>_<date>_<gender>/`.

Update `src/session_manager.py` — the CSV writer already treats
these as columns, so removing them from the header is a one-line
change.

### 3.6 Later / not now

Server audit script (waiting on decision from Section 2).

Consent form template (drafted after we hear from the admin).

Encryption at rest (waiting until data starts being shared between
machines).

Ethics committee approval reference (waiting on admin to check
whether a lab-wide protocol already covers us).

---

## 4. Change log

To be filled in as changes actually land. Format: date, what
changed, why.

- 2026-XX-XX  Prof met with student. Decisions from Section 1
  recorded. Awaiting green light to apply Section 3.
