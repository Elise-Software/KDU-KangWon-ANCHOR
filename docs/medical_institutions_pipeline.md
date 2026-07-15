# Medical-institution pipeline

Run all commands from the repository root.

1. `collect_wonju_medical_institutions.py` downloads source pages and writes a
   dated immutable run to `data/raw/medical_institutions/runs/<YYYY-MM-DD>/`.
2. `preprocess_wonju_medical_institutions.py` reads `raw_rows.csv` or JSONL
   and writes canonical master files to `data/processed/medical_institutions/`.
3. `integrate_wonju_medical_institutions.py` enriches that master with the
   CSV files in `data/raw/public_datasets/` and writes final tables to
   `data/integrated/wonju/`.

```powershell
& .\.venv\Scripts\python.exe .\scripts\collect_wonju_medical_institutions.py `
  --output-dir .\data --run-date 2026-07-15

& .\.venv\Scripts\python.exe .\scripts\preprocess_wonju_medical_institutions.py `
  --input .\data\raw\medical_institutions\runs\2026-07-15\raw_rows.csv `
  --crawl-report .\data\raw\medical_institutions\runs\2026-07-15\crawl_report.json `
  --output-dir .\data\processed\medical_institutions --strict
```

The legacy HTML page snapshots are preserved under
`data/raw/medical_institutions/legacy_pages/` for traceability.
