# OCR fixtures

`test_fixtures.py` runs the text-anchor resolver and the grounding check against real
1080p screencast frames — the Windows R/RStudio/Positron/Quarto setup recording whose
fifth take motivated the OCR work. The frames show a personal desktop, so they are not
committed: `frames/` is gitignored and the tests skip when it is empty.

To populate it on a machine that has the recording:

```bash
tests/fixtures/extract.sh /path/to/fall2026-setup_R_Rstudio_Positron_Quarto.mp4
```

`ground_truth.json` (committed) lists, per frame, the on-screen text a highlight should
be anchored to and where it is, plus the negative cases: boxes that must be reported as
a MISS, labels OCR must decline to ground rather than guess at, and text OCR is known
not to read (light-on-dark button captions, the dark terminal) so a regression there is
visible rather than silent.
