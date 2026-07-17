# KDU-KangWon-ANCHOR

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

## Commercial-use medical corpus

상업 서비스에 재사용 가능한 의료 텍스트만 별도 `data/commercial_medical_corpus/`에
수집한다. PMC OA의 `CC0`·`CC BY`, MedlinePlus public-domain 영역, 문서별 공공누리
제0·제1유형이 확인된 국내 공공문서만 허용하며 라이선스 불명확·비영리/변경금지/동일조건
라이선스와 철회 논문은 제외 로그에 남긴다.

```powershell
python -m venv .venv-medical-corpus
& ".\.venv-medical-corpus\Scripts\python.exe" -m pip install -r requirements-medical-corpus.txt
& ".\.venv-medical-corpus\Scripts\python.exe" scripts\run_commercial_medical_corpus_pipeline.py --strict
& ".\.venv-medical-corpus\Scripts\python.exe" -m pytest -q tests\test_commercial_medical_corpus.py
```

원문 응답과 SHA-256, 정제 문서·청크 JSONL/Parquet, 라이선스 판정과 제외 사유,
무결성 보고서를 한 번에 생성한다. 상세 스키마와 DGX 실행 명령은
[`docs/commercial_medical_corpus.md`](docs/commercial_medical_corpus.md)에 있다.

2026-07-17 DGX 샘플 실행은 PMC OA `CC BY` 1건, MedlinePlus Health Topics 2건,
Medical Tests 2건, Genetics 1건을 수집해 문서 6건·청크 88건을 만들었다. 제외·철회·
중복·필수 필드 누락은 모두 0건이었고 JSONL과 Zstandard Parquet를 다시 읽어 행 수가
각각 6·88인지 확인했다.

같은 날 DGX 전체 실행은 현재 PMC `oa_comm` 카탈로그 5,300,466건을 전수 회계하고,
상업 이용 허용·비철회 후보 5,284,850건에서 20개 의료 도메인으로 균형 선택했다.
최종 통합본은 문서 1,821,825건·청크 35,346,243건이며 문서/청크 최대 도메인 비중은
각각 5.48%·5.90%다. 라이선스 위반, 철회 문서, 필수 필드 누락, 중복, 외래키 오류는
모두 0건이고 실제 압축 JSONL 줄 수도 Parquet 행 수와 일치한다. 전체 작업 디렉터리는
227,902,472,438 bytes로 1TB 제한 이내다. 전체 실행 결과와 제외 내역은
[`docs/commercial_medical_corpus.md`](docs/commercial_medical_corpus.md)에 기록했다.

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

P1은 `data/p1_rag` 아래에 격리되어 있으며 기존 P0 산출물을 보호된 읽기 전용
입력으로 취급한다. 임베딩·reranker가 PyTorch를 사용하므로 Python 3.12 전용
환경에서 실행한다.

```powershell
uv python install 3.12
uv venv .venv-p1 --python 3.12
uv pip install --python ".venv-p1\Scripts\python.exe" -r requirements-p1.txt
& ".\.venv-p1\Scripts\python.exe" scripts\run_p1_rag_pipeline.py --strict
& ".\.venv-p1\Scripts\python.exe" -m pytest -q
```

단일 entrypoint는 원주시 공식 보건·복지 문서의 원문·URL·기준일·SHA-256을
보존하고, 정제·중복 제거·의미 단위 청크 생성·기관 및 서비스 연결을 수행한다.
검색은 BAAI/bge-m3 임베딩과 FAISS, BAAI/bge-reranker-v2-m3를 사용한다. 생성
모델은 OpenAI 호환 `/v1/models`에서 자동 확인하며 `temperature=0`으로 호출한다.
모든 근거 답변에는 출처 URL, 문서 ID, 청크 ID를 남긴다.

완성된 인덱스를 직접 조회하는 예시는 다음과 같다.

```powershell
& ".\.venv-p1\Scripts\python.exe" scripts\query_wonju_p1_rag.py "원주시 정신건강 상담 정보를 알려주세요"
```

### 주민 친화형 답변 정책

증상·생활건강 질문에는 `resident_friendly_five_step_v3` 정책으로 다음 흐름을
자연스럽게 안내한다.

1. 사용자의 불편과 걱정에 공감한다.
2. 공식 근거 범위에서 가능한 원인을 설명하되 진단처럼 단정하지 않는다.
3. 의료행위나 처방을 대신하지 않는 안전한 생활 관리 방법을 제시한다.
4. 근거가 있을 때만 일반의약품·상비약 범위의 선택지를 안내하며 제품, 용량,
   복용법을 추정하지 않는다.
5. 사용자가 동네·주소를 알려주면 원주시 기관 마스터에서 주변 병원을 찾아
   기관명·전화·주소·운영시간과 함께 보여준다. 주소 기반 결과는 실제 거리순이
   아니라 해당 지역 문자열과 공식 주소가 일치한 기관임을 명확히 한다.

단순한 기관 주소·전화·운영시간 질문에는 이 5단계를 억지로 붙이지 않고 요청한
정보부터 간결하게 답한다. 근거가 없으면 진단, 원인 확정, 약품·용량을 만들어내지
않고 확인할 수 없음을 밝힌다. 응급·자살·중독·금단·고위험 의료 표현은 생성 모델
보다 결정적 안전 규칙을 먼저 적용한다.

### 최신 P1 산출물 및 평가 (2026-07-16)

아래 수치는 `data/p1_rag/reports/`의 현재 검증 보고서 기준이다.

| 항목 | 결과 |
|---|---:|
| 공식 문서 / 중복 문서 | 70 / 0 |
| 의미 단위 청크 | 580 |
| 기관 링크 / 연결 기관 | 110 / 79 |
| 서비스 링크 / 연결 서비스 | 11 / 3 |
| FAISS 인덱스 | `IndexFlatIP`, 580 × 1024 |
| FAISS SHA-256 | `f90899e1de5223a1c0569633ca0dab1cb761814cee42e4075330ebcf09189279` |
| 평가 세트 | 117건: 사실 100, 안전 양성 12, 안전 음성 5 |
| Retrieval Recall@5 / MRR | 0.96 / 0.87 |
| 답변 근거성 | 0.5892 |
| 인용 포인터 무결성 | 1.0 |
| 인용 근거 적합성: 전체 / 사실 / 안전 | 0.9453 / 0.9773 / 1.0 |
| 인용 정확도 | 1.0 |
| 안전 양성 / 음성 / 정밀도 | 1.0 / 1.0 / 1.0 |
| 실패·수동검토 항목 | 0 |
| 데이터셋 상태 | `verified` |
| P0 보호 SHA-256 | `8293d8fc5ddd6d9fe4ef6125565ed5da58838904fad3a1b0de733022b8bf8a0d` 유지 |

`resident_friendly_five_step_v3`로 117건을 새로 생성·평가한 뒤 `--strict`
파이프라인을 다시 실행해 통과했다. 저장소 전체 pytest는 최종 코드와 갱신된 평가
산출물로 `113 passed`, 배포 전용 pytest는 `62 passed`를 확인했다. 캐시가 준비된
호스트에서 Hugging Face 최신 여부 확인 없이 재검증하려면 `HF_HUB_OFFLINE=1`과
`TRANSFORMERS_OFFLINE=1`을 설정한다. 실행형 UI와 권한 검증은
[`deploy/open-webui/README.md`](deploy/open-webui/README.md)를 따른다.
