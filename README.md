# KNU-KangWon-ANCHOR

Wonju public-health data pipelines. Each executable under `scripts/` describes
the dataset and stage it handles; there are no nested script directories.

```text
scripts/
  collect_wonju_medical_institutions.py     # download medical-institution pages
  preprocess_wonju_medical_institutions.py  # validate and canonicalize collection output
  integrate_wonju_medical_institutions.py   # merge public medical datasets into the master
  collect_wonju_pharmacy_operations.py      # download and compare pharmacy operation data
  validate_wonju_p0_public_health_sources.py # validate the approved P0-DATA-03 seed snapshot
  extract_wonju_p0_public_health_entities.py # extract bounded entity/contact evidence
  recover_wonju_p0_public_health_schedules.py # recover institution-owned operation schedules
  normalize_wonju_p0_public_health.py       # normalize evidence and field coverage
  integrate_wonju_p0_public_health.py       # non-destructive public-health master integration
  audit_wonju_p0_public_health_profiles.py  # verify target representation and lineage
data/
  raw/medical_institutions/                 # immutable HTML and raw collection runs
  raw/public_datasets/                      # downloaded public CSV inputs
  processed/medical_institutions/           # canonical medical-institution master
  collected/pharmacy_operations/            # pharmacy raw HTML and parsed source data
  integrated/wonju/                         # final institution master and integration outputs
config/                                     # aliases and review decisions
docs/                                       # pipeline details
tests/                                      # automated tests
```

## Medical-institution integration

```powershell
& .\.venv\Scripts\python.exe .\scripts\integrate_wonju_medical_institutions.py `
  --master .\data\processed\medical_institutions\wonju_medical_institutions.csv `
  --clinic '.\data\raw\public_datasets\강원특별자치도 원주시_의원 정보_20251125.csv' `
  --mental '.\data\raw\public_datasets\강원도 원주시_정신건강증진시설 및 요양병원 현황_20220813.csv' `
  --dental-lab '.\data\raw\public_datasets\강원도 원주시_치과기공소 현황_20230612.csv' `
  --aliases .\config\wonju_institution_aliases.csv `
  --review-decisions .\config\wonju_manual_review_decisions.csv `
  --output-dir .\data\integrated\wonju `
  --strict
```

## Pharmacy-operation collection

```powershell
& .\.venv\Scripts\python.exe .\scripts\collect_wonju_pharmacy_operations.py `
  --output-dir .\data\collected\pharmacy_operations `
  --master .\data\integrated\wonju\institutions.csv
```

The collector writes immutable HTML snapshots to `raw/<date>/` and parsed
tables, validation, and master-comparison outputs to `processed/`.

## P0-DATA-03 public-health integration

The checked-in snapshot can be replayed without network access. Each stage has
a meaningful `--strict` mode; a structural error exits non-zero.

```powershell
& .\.venv\Scripts\python.exe .\scripts\validate_wonju_p0_public_health_sources.py --strict
& .\.venv\Scripts\python.exe .\scripts\extract_wonju_p0_public_health_entities.py --strict
& .\.venv\Scripts\python.exe .\scripts\recover_wonju_p0_public_health_schedules.py --strict
& .\.venv\Scripts\python.exe .\scripts\normalize_wonju_p0_public_health.py --strict
& .\.venv\Scripts\python.exe .\scripts\integrate_wonju_p0_public_health.py --strict
& .\.venv\Scripts\python.exe .\scripts\audit_wonju_p0_public_health_profiles.py --strict
```

The final outputs are under `data/integrated/wonju/`. The integration preserves
every column of all 2,481 input master rows, links profiles only with the
documented multi-field match policy, and creates a new master row only from an
official name, a Wonju address, and field-level evidence. Missing fields remain
blank and are recorded in `public_health_coverage_gaps.csv`; actual source or
identity conflicts are kept separately in `public_health_manual_review.csv`.
