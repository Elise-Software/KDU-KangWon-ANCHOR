# 원주시 관내의료기관 P0 데이터 파이프라인 v2

수집과 전처리를 분리했다.

```text
사이트 HTML
   │
   ▼
collect_wonju_medical.py
   ├─ 페이지별 원본 HTML
   ├─ 원문 JSONL/CSV
   └─ crawl_report.json
   │
   ▼
preprocess_wonju_medical.py
   ├─ 문자열/전화번호 정규화
   ├─ 중복·누락·번호·페이지·원천 ID 검증
   ├─ 잘못된 좌표값 제거
   └─ 처리 CSV/JSONL/검증 보고서
```

## 반영된 수정

- 목록의 `onclick` 주소 안 숫자를 위도·경도로 오인하지 않는다.
- 수집기는 좌표를 생성하지 않고 `map_action_raw`만 보존한다.
- 전처리기는 이전 결과에 잘못 들어간 `latitude`, `longitude`도 전부 `null`로 만든다.
- 좌표는 후속 주소 지오코딩 파이프라인에서 별도로 생성한다.
- 사이트 표시 2,462건, 247페이지, 번호 연속성, 번호·원천 ID 중복, 상세 URL의 `resrceNo`, 번호-페이지 관계를 검사한다.
- 수집과 정규화/중복 제거를 서로 다른 파일로 분리했다.

## Windows + uv 설치

프로젝트 폴더에서:

```powershell
uv venv .venv --python 3.14
uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt
```

가상환경 활성화 없이 직접 실행해도 된다.

## 1. 2페이지 시험 수집

```powershell
& ".\.venv\Scripts\python.exe" `
  ".\scripts\collect_wonju_medical.py" `
  --output-dir ".\data" `
  --max-pages 2
```

## 2. 전체 원천 수집

```powershell
& ".\.venv\Scripts\python.exe" `
  ".\scripts\collect_wonju_medical.py" `
  --output-dir ".\data" `
  --delay 0.7 `
  --strict
```

결과 위치:

```text
data/raw/wonju_medical/YYYY-MM-DD/
├── pages/page_001.html ...
├── page_rows/page_001.json ...
├── raw_rows.jsonl
├── raw_rows.csv
└── crawl_report.json
```

GET 페이지 이동을 먼저 시도하고, 실패하면 사용자가 캡처한 POST 폼 방식으로 자동 재시도한다. 브라우저 쿠키는 저장하지 않는다.

## 3. 전처리

실행일이 `2026-07-15`라면:

```powershell
& ".\.venv\Scripts\python.exe" `
  ".\scripts\preprocess_wonju_medical.py" `
  --input ".\data\raw\wonju_medical\2026-07-15\raw_rows.jsonl" `
  --crawl-report ".\data\raw\wonju_medical\2026-07-15\crawl_report.json" `
  --output-dir ".\data\processed\wonju_medical" `
  --strict
```

출력:

```text
data/processed/wonju_medical/
├── wonju_medical_institutions.csv
├── wonju_medical_institutions.jsonl
├── wonju_medical_duplicates.json
├── wonju_medical_rejected.json
└── validation_report.json
```

## 기존 수집 결과를 즉시 재전처리

이전 `wonju_medical_institutions.jsonl`도 입력 가능하다.

```powershell
& ".\.venv\Scripts\python.exe" `
  ".\scripts\preprocess_wonju_medical.py" `
  --input ".\wonju_medical_institutions.jsonl" `
  --crawl-report ".\validation_report.json" `
  --output-dir ".\data\processed\wonju_medical_fixed" `
  --strict
```

이 경우 기존에 주소 숫자에서 잘못 추출된 좌표값은 전부 제거된다.

## 선택: Parquet 생성

```powershell
uv pip install --python ".venv\Scripts\python.exe" pandas pyarrow

& ".\.venv\Scripts\python.exe" `
  ".\scripts\preprocess_wonju_medical.py" `
  --input ".\data\raw\wonju_medical\2026-07-15\raw_rows.jsonl" `
  --output-dir ".\data\processed\wonju_medical" `
  --parquet `
  --strict
```

## 합격 기준

`validation_report.json`에서 다음이 모두 `true`여야 한다.

```json
{
  "checks": {
    "input_rows_accounted_for": true,
    "normalized_count_matches_expected": true,
    "all_pages_present": true,
    "number_sequence_complete": true,
    "number_unique": true,
    "source_id_unique": true,
    "page_number_relation_valid": true,
    "source_id_matches_detail_url": true,
    "core_fields_complete": true,
    "coordinates_cleared": true
  },
  "all_checks_passed": true
}
```

전화번호는 원천 공란이 존재하므로 100% 커버리지를 합격 조건으로 두지 않는다. 카테고리별 전화번호 커버리지는 보고서에 별도로 기록된다.
