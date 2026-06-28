"""STRATA evaluation harness: the pre-registered rubric and the metrics.

`rubric.py` is the reporting spine's contract — the taxonomy, the gold-extraction
cue lexicon, and the scoring rule, content-hashed and committed BEFORE any
synthesizer run (invariant #6). `metrics.py` scores predictions against the gold
taxonomy and computes inter-annotator agreement.
"""
