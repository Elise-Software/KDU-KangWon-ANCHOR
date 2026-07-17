# Commercial medical RAG corpus pipeline

상업 서비스에 재사용할 수 있는 의료 텍스트만 수집한다. 설정에 URL이 있다는 사실만으로
허용하지 않으며, 원천별 라이선스 검증을 통과한 문서만 처리·통합 데이터셋에 들어간다.

## 허용 범위

| Source | Accepted content | Required evidence |
|---|---|---|
| PMC Open Access | `oa_comm` article XML | 현재 OA 파일 목록의 라이선스가 정확히 `CC0` 또는 `CC BY`이고 철회 표시가 없음 |
| MedlinePlus Health Topics | NLM 작성 `full-summary` | MedlinePlus public-domain 정책과 공식 Health Topics XML |
| MedlinePlus Medical Tests | `/lab-tests/` 본문 | public-domain 정책과 canonical URL |
| MedlinePlus Genetics | 공식 bulk XML의 summary | public-domain 정책과 Genetics bulk URL |
| 국내 공공문서 | 개별 문서 본문 | 해당 문서 페이지 자체에 공공누리 제0유형 또는 제1유형이 명시됨 |

`CC BY-NC`, `CC BY-ND`, `CC BY-SA`, 일반 저작권, 라이선스 불명, 철회 논문은 제외한다.
국내 문서도 문서별 공공누리 표시가 없으면 포함하지 않는다.

공식 정책:

- PMC OA subset: <https://pmc.ncbi.nlm.nih.gov/tools/openftlist/>
- PMC AWS/file-list schema: <https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/>
- MedlinePlus reuse policy: <https://medlineplus.gov/about/using/usingcontent/>
- MedlinePlus Health Topic XML: <https://medlineplus.gov/xml.html>
- MedlinePlus Genetics data: <https://medlineplus.gov/about/developers/geneticsdatafilesapi/>
- 공공누리 이용 안내: <https://www.kogl.or.kr/info/userGuide.do>

## 편향 제어

원천 아카이브는 허용 범위 전체를 보존하지만 RAG 통합본은 다음 20개 도메인으로 분류해
균형 선별한다.

`cardiovascular`, `respiratory`, `neurology`, `mental_health`,
`endocrine_metabolic`, `infectious_disease`, `oncology`, `musculoskeletal`,
`gastro_hepatology`, `renal_urology`, `reproductive_womens`, `pediatrics`,
`geriatrics`, `dermatology`, `ophthalmology_ent`, `dental_oral`,
`genetics_rare`, `public_health_prevention`, `diagnostics_pharmacology`,
`general_other`

- PMC는 도메인별 최대 100,000문서(전체 이론상 최대 2,000,000문서)를 stable SHA-256
  순으로 결정론적으로 뽑는다.
- 최종 통합본에서 단일 도메인 비중은 12%를 넘을 수 없다.
- 긴 논문 한 편이 검색 인덱스를 독점하지 않도록 문서당 최대 24청크를 유지한다. 24개를
  넘으면 본문 전체에서 등간격으로 뽑으며, 최종 청크 기준 도메인 점유율도 12% 이하인지
  별도로 검사한다.
- 짧은 키워드는 완전한 단어로만 매칭한다. 예를 들어 `ear`가 `research`에 포함됐다는
  이유로 이비인후과 문서가 되지 않는다.
- 원천이 적은 도메인의 문서를 복제하거나 근거 없이 증강하지 않는다. 부족량은 보고서에
  그대로 남긴다.
- `reports/pmc_balance_report.json`과 `reports/bulk_integration_report.json`에 선별 전후
  도메인 수, 최대 점유율, 통과 여부를 기록한다.

## 1TB 저장 한도

`config/commercial_medical_corpus_bulk.json`은 hard limit 1,000,000,000,000 bytes,
작업 중단선 900,000,000,000 bytes를 사용한다. PMC 원천은 tar.gz 그대로 보존하고 최종
Parquet은 Zstandard 압축, JSONL은 `.jsonl.zst` shard로 저장한다. 중단된 대형 다운로드는
`.part`에서 이어받고 패키지별로 최대 5회 재시도한다.

## DGX 전체 실행

```bash
cd /home/elise/Desktop/KDU-KangWon-ANCHOR
python3 -m venv .venv-medical-corpus
.venv-medical-corpus/bin/python -m pip install -r requirements-medical-corpus.txt

.venv-medical-corpus/bin/python scripts/run_bulk_commercial_medical_corpus.py --stage plan --strict
.venv-medical-corpus/bin/python scripts/run_bulk_commercial_medical_corpus.py --stage download --strict
.venv-medical-corpus/bin/python scripts/run_bulk_commercial_medical_corpus.py --stage scan --strict
.venv-medical-corpus/bin/python scripts/run_bulk_commercial_medical_corpus.py --stage select --strict
.venv-medical-corpus/bin/python scripts/run_bulk_commercial_medical_corpus.py --stage medlineplus --strict
.venv-medical-corpus/bin/python scripts/run_bulk_commercial_medical_corpus.py --stage korean --strict
.venv-medical-corpus/bin/python scripts/run_bulk_commercial_medical_corpus.py --stage materialize --strict
.venv-medical-corpus/bin/python scripts/run_bulk_commercial_medical_corpus.py --stage integrate --strict
.venv-medical-corpus/bin/python -m pytest -q tests/test_commercial_medical_corpus.py
```

`--stage all --strict`도 같은 순서를 한 번에 실행한다. 각 단계는 재실행 가능하며 완료된
아카이브와 파티션을 다시 만들지 않는다.

## 산출물

```text
data/commercial_medical_corpus_bulk/
  catalogs/                         # PMC package plan/current file list
  raw/pmc_oa_comm/packages/         # 원본 OA tar.gz
  raw/medlineplus/                  # 원본 XML/HTML과 manifest
  raw/korean_public/                # 문서별 KOGL 표시를 확인한 원본 HTML과 manifest
  staging/                          # scan, balanced selection, materialized partitions
  integrated/parquet/
    documents.parquet
    chunks.parquet
  integrated/jsonl/
    documents/*.jsonl.zst
    chunks/*.jsonl.zst
  reports/
    pmc_balance_report.json
    pmc_materialization_report.json
    pmc_materialization_exclusions.jsonl
    medlineplus_bulk_report.json
    korean_public_report.json
    korean_public_exclusions.json
    bulk_integration_report.json
```

각 청크에는 원문, 원문 URL, 제목, 기관, 저자, PMID/PMCID, 라이선스와 근거,
발행·수정·수집일, 철회 여부, 언어, 섹션, 문서/청크 ID, SHA-256, 의료 도메인을 저장한다.
최종 strict 검사는 라이선스, 철회, 필수 필드, 중복, 문서-청크 외래키, 두 출력 형식의
행 수, 도메인 최대 점유율, 저장 한도를 함께 확인한다.

## 2026-07-17 DGX 전체 실행 결과

공식 PMC 현재 카탈로그와 내려받은 186개 `oa_comm` 패키지, 누락분 공식 S3 보충 묶음,
MedlinePlus 세 영역, 문서별 공공누리 제1유형이 확인된 국내 공공문서로 전체 파이프라인을
실행했다.

| 항목 | 결과 |
|---|---:|
| PMC 현재 카탈로그 | 5,300,466 |
| PMC 허용 라이선스·비철회 후보 | 5,284,850 |
| PMC 20개 도메인 균형 선택 | 1,867,126 |
| PMC 정제 문서 | 1,820,438 |
| PMC 정제 청크 | 35,339,123 |
| MedlinePlus 문서 / 청크 | 5,161 / 12,599 |
| 국내 공공문서 문서 / 청크 | 5 / 10 |
| 최종 중복 제거 문서 | 1,821,825 |
| 최종 청크 | 35,346,243 |
| 제거한 입력 문서 중복 | 3,779 |
| 문서 최대 도메인 비중 | 5.4824% |
| 청크 최대 도메인 비중 | 5.9036% |
| 최종 작업 디렉터리 | 227,902,472,438 bytes |

PMC 선택 1,867,126건은 정제 문서 1,820,438건과 명시적 제외 46,688건으로 전수
회계된다. 제외 사유는 XML 자체 철회 근거 4,261건, 검색 가능한 제목·초록·본문 없음
42,426건, 잘못된 XML namespace 때문에 파싱할 수 없었던 `PMC7688217` 1건이다.
전체 레코드는 `reports/pmc_materialization_exclusions.jsonl`에 PMCID, 패키지, 멤버명,
분류, 원 오류와 함께 저장했다. 이 한 건을 임의 복구하거나 코퍼스에 포함하지 않았다.

최종 출처별 문서 수는 PMC 1,816,659건, MedlinePlus Health Topics 2,029건,
Medical Tests 302건, Genetics 2,830건, 국내 공공문서 5건이다. 다음 검사는 모두 0건
또는 통과다.

- 허용되지 않은 라이선스 0건, 철회 문서 0건
- 문서·청크 필수 필드 누락 0건
- 최종 문서·청크 중복 0건
- 고아 청크와 청크 없는 문서 0건
- 문서당 최대 청크 24건
- 현재 PMC 카탈로그 5,300,466건 회계 일치
- `integrity_checks_passed=true`, `dataset_status=verified`

Parquet와 압축 JSONL을 별도로 읽어 실제 건수도 검증했다. `documents.parquet`과
문서 JSONL은 각각 1,821,825행, `chunks.parquet`과 청크 JSONL은 각각
35,346,243행이다. 최종 파일 크기는 문서 Parquet 18,693,946,901 bytes, 청크
Parquet 16,237,568,121 bytes, 문서 JSONL.zst 18,092,660,999 bytes, 청크
JSONL.zst 17,889,228,905 bytes다.

전체 실행 명령은 다음과 같다.

```bash
.venv-medical-corpus/bin/python scripts/run_bulk_commercial_medical_corpus.py --stage all --strict
.venv-medical-corpus/bin/python -m pytest -q tests/test_commercial_medical_corpus.py
```

수집·XML 파싱은 네트워크/CPU 작업이므로 GPU를 억지로 사용하지 않는다. 완성 코퍼스의
임베딩과 인덱스 구축 단계에서 DGX CUDA를 사용한다.
