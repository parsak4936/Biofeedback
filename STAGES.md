# Paper stages and open items

Working tracker. Not part of the paper.

## Blocking right now

### 1. Raw OpenSignals capture for the backend comparison

**Status:** blocked, needs one file from you.

`compare_backends.py` takes a raw OpenSignals `.txt` export and runs
both extraction paths over it. It cannot run on anything currently in
`data/`.

Why the 35 existing sessions do not work:

| File | Contains | Usable? |
|---|---|---|
| `samples.csv` | `eda, hr, hrv` already derived, 1 Hz | No. One backend's output is already baked in; the raw ECG is not present |
| `diagnostic.csv` | `raw_eda, raw_hr, raw_hrv` at 10 Hz | No. `raw_hr` and `raw_hrv` are also already derived, and 10 Hz is far below what R-peak detection needs |
| `metadata.json` | calibration constants | No signal data |
| `unity_udp.csv` | command log | No signal data |

Backend comparison must start from the raw 200 Hz ECG and EDA
waveforms. Once those are reduced to HR and RMSSD, the second backend
cannot be re-derived, because the information it would need is gone.

**What to capture:** in OpenSignals, enable saving to file before a
session. It writes a `.txt` with a JSON header followed by columns of
raw values. One file from one session is enough for the methodology
figure. Three or four would let the results section report agreement
across sessions rather than over a single recording.

`Config.MOCK_DATA_FILE` still points at
`data/LiveTest/opensignals_0007800F319C_2026-06-11_14-59-16.txt`,
which no longer exists, so a capture like this did exist at some point.

**Once you have it:**

```bash
python compare_backends.py path/to/opensignals_export.txt
```

That prints the agreement metrics and writes comparison PNGs next to
the input. Table 3 and Figure 6 in the methodology are already written
around those outputs, with `tbd` in the cells the script fills.

## Data protection, unresolved

Names are still on disk in three places. This was identified earlier
and is still outstanding:

- session directory names, e.g. `Firstname_Lastname_Session3_2026-09-09_M`
- `samples.csv` columns `patient_first_name`, `patient_last_name`
- `metadata.json` keys `first_name`, `last_name`

The methodology already states that neither given nor family name is
written to any file the system produces. That is currently a claim
about the intended state, not the observed one. It needs to be true
before submission, and existing recordings need renaming.

## UI changes

Distinguish two categories:

**Affects the measurement, belongs in the methodology:**
- calibration start interlock (already written, §Calibration interlock)
- anything altering when calibration may begin, how long it runs, or
  what the operator can see while deciding to proceed

**Cosmetic, does not belong in the methodology:**
- panel layout, colour, chart axis behaviour, readout formatting
- removing artifact counters or threshold displays from panels

If a change would alter what a replicating laboratory does, it goes in.
If it only alters what the operator looks at, it stays out.

## Venue stages

No venue committed. Both remain open and the decision can be deferred,
but the deadlines cannot.

### HCI International 2027, Berlin

| Stage | Date | Form |
|---|---|---|
| Proposal | 9 Oct 2026 | 800 words, DOCX or PDF, no formatting requirement |
| Decision | 20 Nov 2026 | |
| Full paper | 29 Jan 2027 | Springer LNCS, single column, 10 to 20 pages, 12 typical |
| Registration | 12 Feb 2027 | |
| Conference | 25 to 30 Jul 2027 | Estrel Berlin |

Single-blind, so authors are visible. The October deadline needs 800
words in Word, not LaTeX, which means co-authors can contribute to it
without touching Overleaf.

### Journal, undecided

Candidates and what each implies:

| Journal | Format for first submission |
|---|---|
| Journal of Anxiety Disorders | Elsevier, any reasonable format |
| Behaviour Research and Therapy | Elsevier, any reasonable format |
| Journal of Behavioral and Cognitive Therapy | Elsevier, any reasonable format |
| Computers in Human Behavior | Elsevier, any reasonable format |
| Internet Interventions | Elsevier, any reasonable format |
| Virtual Reality | Springer, own template |
| JMIR Mental Health | own submission system |
| Frontiers in Psychology | own web editor |

The Elsevier titles accept single column, double spaced, any reference
style for initial submission and only require house formatting after
acceptance. Reformatting between LNCS and IEEE afterwards is roughly
two hours.

## Awaiting from admin

- author list and order
- final title
- ethical approval reference, placeholder sits in §Ethics
- Daghooghi citation, bib entry currently carries a provisional year
  and title

## Next

1. Raw OpenSignals capture, so the backend comparison can run
2. Analysis over the 35 recorded sessions, for the prof report and for
   the results section
3. Scenario numbers: altitude range, escalation increment per command,
   equivalents for the other two scenarios
4. Name removal from directories, CSV columns, and metadata

## Overleaf sync

The repeated overwriting is avoidable. Overleaf projects expose a git
remote on paid plans:

```
Overleaf project -> Menu -> Git -> copy the clone URL
git clone https://git.overleaf.com/<project-id> overleaf-paper
```

Working in that clone means `git pull` before edits and `git push`
after, and neither side silently discards the other's work. GitHub
sync from the same menu achieves the same thing if the account has it.

Without either, the working rule is: pull the Overleaf copy down
before any local edit, never the reverse.
