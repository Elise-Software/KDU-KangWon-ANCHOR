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

## P1 RAG pipeline

The P1 pipeline is isolated under `data/p1_rag` and treats all P0 artifacts as
protected read-only inputs. Use the dedicated Python 3.12 environment because
the embedding and reranker runtime depends on PyTorch.

```powershell
uv python install 3.12
uv venv .venv-p1 --python 3.12
uv pip install --python ".venv-p1\Scripts\python.exe" -r requirements-p1.txt
& ".\.venv-p1\Scripts\python.exe" scripts\run_p1_rag_pipeline.py --strict
& ".\.venv-p1\Scripts\python.exe" -m pytest -q
```

The entrypoint collects at least 50 official Wonju health/welfare pages,
records raw SHA-256 provenance, cleans and chunks the content, links chunks to
the 2,487-row institution master and normalized services, builds a normalized
BAAI/bge-m3 FAISS index, reranks with BAAI/bge-reranker-v2-m3, discovers the
generation model through the configured OpenAI-compatible `/v1/models`
endpoint, and evaluates retrieval, grounding, citations, and safety rules.

To query the completed index:

```powershell
& ".\.venv-p1\Scripts\python.exe" scripts\query_wonju_p1_rag.py "원주시 정신건강 상담 정보를 알려줘"
```

### P1 push-readiness validation (2026-07-16)

The complete strict pipeline was executed twice in independent processes before
push preparation. Both runs produced the same 70 canonical documents, 435
chunks, 115 institution links, 11 service links, and FAISS index SHA-256
`b46aa15e636adf7f4ca00fc2810b2648e370be0c02bbbd37adb78cd7cac07871`.
The protected P0 digest was unchanged on both runs.

| Check | Repetitions | Result |
|---|---:|---:|
| Full P1 pipeline `--strict` | 2 | 2 passed |
| Full pytest suite | 3 | 23 passed each run |
| Retrieval Recall@5 | 2 | 0.96 |
| Mean reciprocal rank | 2 | 0.87 |
| Answer groundedness | 2 | 0.6167 |
| Citation accuracy | 2 | 0.9911 |
| Safety-rule pass rate | 2 | 1.0 |
| Failures or manual-review items | 2 | 0 |

Two live smoke queries were also checked in one model process. A normal smoking
cessation question returned one supporting document/chunk citation. A
suicide-risk question bypassed generation, applied the deterministic safety
rule, included both `109` and `119`, and returned two supporting chunk
citations. The detailed snapshot is stored in
`data/p1_rag/reports/push_readiness_report.json`.
