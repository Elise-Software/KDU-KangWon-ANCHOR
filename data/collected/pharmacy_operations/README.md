# Pharmacy operation data

- `raw/<YYYY-MM-DD>/`: immutable HTML snapshots downloaded from the two official Wonju Health Center pages.
- `processed/`: parsed source rows, merged pharmacy records, source conflicts, master comparison results, validation report, and collection manifest.

Do not edit files under `raw/`; rerun `scripts/collect_wonju_pharmacy_hours.py` with a new `--run-date` to create another snapshot.
