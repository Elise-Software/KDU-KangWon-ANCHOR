# 원주시 생활건강 안내 AI

원주시 공식 보건·복지 자료와 2,487개 기관 마스터를 근거로 생활건강, 의료·약국,
보건·복지기관 이용 정보를 안내하는 RAG 서비스다. 진단·처방을 대신하지 않으며, 답변에는
공식 출처와 기관 정보를 함께 표시한다.

현재 서비스는 DGX Spark에서 Docker Compose로 운영하며, 일반 사용자는
`wonju-health-rag`만 이용한다. raw 모델 `gemma-4-31b-nvfp4`는 허용된 개발자만
접근할 수 있고, 원본 vLLM은 LAN에 직접 공개하지 않는다.

## 빠른 사용법

현재 내부 서비스 주소는 `http://192.168.100.58`이다.

1. 브라우저에서 서비스에 로그인한다.
2. 증상, 원주시 읍면동, 보건소·병원·약국 이름을 자연어로 입력한다.
3. 답변의 출처 카드에서 공식 문서와 URL을 확인한다.
4. 기관 카드의 전화·주소·운영시간·지도 버튼을 이용한다.
5. 응급·자살·중독 위험 상황에서는 화면 상단 안전 카드의 119·109 등 즉시 연락
   버튼을 우선 사용한다.

의료 위험 질의에는 공감 → 가능한 원인 → 안전한 대처 → 근거가 있을 때의 일반의약품
범위 안내 → 지역 기관 찾기 순서로 안내한다. 기관 주소·전화처럼 단순한 질의는 필요한
정보부터 간결하게 답한다. 공식 근거가 없으면 값을 추정하지 않는다.

## 구성

```text
브라우저
  │ HTTP 80
  ▼
Caddy ──► Open WebUI ──► P1 API ──► bge-m3 + FAISS + bge-reranker-v2-m3
                  │                         │
                  │                         └─ P0 기관·약국·공공보건 마스터 (읽기 전용)
                  └─ 개발자 전용 raw proxy ─► vLLM (Docker 내부 + 127.0.0.1:8000)
```

- `data/integrated/wonju/`: P0 기관 마스터와 약국·공공보건 보강 결과
- `data/p1_rag/`: 문서, 청크, 벡터 인덱스, 평가·감사 보고서
- `scripts/run_p1_rag_pipeline.py`: P1 RAG 단일 실행 진입점
- `deploy/open-webui/`: Open WebUI overlay, Caddy, 권한, API, Spark systemd 배포 계층
- `docs/rise/`: 최종 결과보고서 작성용 문서·증빙 인덱스

## 현재 검증 결과

| 항목 | 결과 |
|---|---:|
| 기관 마스터 | 2,487개 |
| 공식 문서 / 청크 | 70 / 580 |
| 연결 기관 | 79개 |
| Retrieval Recall@5 / MRR | 0.96 / 0.865 |
| 인용 정확도 | 1.0 |
| 안전 판정 패턴 | 31개 |
| 실제 사용자 평가 | 5명, 과업 5종 전수, 완료율 100% |
| 전체 테스트 | 140 passed, 1 skipped |

실제 사용자 의견에서 의료 안내 명확성, 병원 운영정보, 위치 감지, 모바일 전화 연결,
한국어 설명 보강이 개선 우선순위로 확인됐다. 상세 결과는
[`rise_actual_user_evaluation.md`](data/p1_rag/reports/rise_actual_user_evaluation.md)에
있다.

## 로컬 재현

### 1. P0 데이터 확인

P0 산출물은 기존 기관 핵심 필드를 비파괴 방식으로 보존한다. 이미 수집된 스냅샷을
네트워크 없이 재검증하려면 Python 환경에서 다음을 실행한다.

```powershell
& ".\.venv\Scripts\python.exe" scripts\validate_wonju_p0_public_health_sources.py --strict
& ".\.venv\Scripts\python.exe" scripts\extract_wonju_p0_public_health_entities.py --strict
& ".\.venv\Scripts\python.exe" scripts\recover_wonju_p0_public_health_schedules.py --strict
& ".\.venv\Scripts\python.exe" scripts\normalize_wonju_p0_public_health.py --strict
& ".\.venv\Scripts\python.exe" scripts\integrate_wonju_p0_public_health.py --strict
& ".\.venv\Scripts\python.exe" scripts\audit_wonju_p0_public_health_profiles.py --strict
```

약국 운영정보 전처리·통합 결과는 `data/processed/pharmacy_operations/`와
`data/integrated/wonju/`에 있다. 공식 출처 간 운영시간·전화번호 충돌은 삭제하지 않고
수동검토·출처 레코드로 유지한다.

### 2. P1 RAG 재현

P1은 Python 3.12를 사용한다. 임베딩과 reranker의 첫 실행에는 모델 다운로드와 메모리가
필요하다. 생성 모델 서버는 `config/p1_rag_config.json`의 OpenAI 호환 endpoint로
자동 확인한다.

```powershell
uv python install 3.12
uv venv .venv-p1 --python 3.12
uv pip install --python ".venv-p1\Scripts\python.exe" -r requirements-p1.txt
& ".\.venv-p1\Scripts\python.exe" scripts\run_p1_rag_pipeline.py --strict
& ".\.venv-p1\Scripts\python.exe" scripts\query_wonju_p1_rag.py "원주시 정신건강 상담 정보를 알려주세요"
```

캐시가 완성된 오프라인 환경에서는 `HF_HUB_OFFLINE=1`과
`TRANSFORMERS_OFFLINE=1`을 설정할 수 있다. 캐시가 없을 때는 설정하지 않는다.

### 3. 전체 테스트

```powershell
& ".\.venv-p1\Scripts\python.exe" -m pytest -q
```

배포 의존성이 아직 없는 경우에는 다음을 먼저 설치한다.

```powershell
uv pip install --python ".venv-p1\Scripts\python.exe" `
  -r deploy\open-webui\p1-api\requirements.txt PyYAML pytest httpx playwright
& ".\.venv-p1\Scripts\python.exe" -m playwright install chromium
```

## DGX Spark 배포 재현

### 사전 조건

- Docker Engine 및 Docker Compose v2
- Spark에서 실행 가능한 vLLM recipe와 `gemma-4-31b-nvfp4` 모델
- P1 임베딩·reranker 모델 캐시 또는 외부 다운로드 권한
- 운영용 비밀값을 담은 `deploy/open-webui/.env`

비밀값은 저장소에 커밋하지 않는다. `.env.example`을 복사해 관리자 계정, JWT/내부 API
키, 개발자 이메일 목록을 채운다.

```bash
cd /home/elise/Desktop/KDU-KangWon-ANCHOR/deploy/open-webui
cp .env.example .env
# .env의 replace-with-* 값을 실제 난수·운영 계정으로 변경
docker compose config
docker compose up --build -d --wait
docker compose ps
```

서비스 상태는 다음으로 확인한다.

```bash
curl --fail http://127.0.0.1/gateway/health
docker compose exec p1-api python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8010/ready').read().decode())"
docker compose exec open-webui curl --fail http://127.0.0.1:8080/health
```

### 재부팅 자동복구

Spark에서는 두 개의 사용자 systemd 서비스가 필요하다.

- `wonju-vllm.service`: vLLM을 `127.0.0.1:8000`과 내부 Docker 네트워크로 복구
- `wonju-health-ai.service`: vLLM 이후 Compose 스택을 healthcheck 완료까지 기동

`.env`를 준비한 뒤 한 번만 설치한다.

```bash
cd /home/elise/Desktop/KDU-KangWon-ANCHOR/deploy/open-webui
chmod 0755 systemd/*.sh
./systemd/install-dgx-spark-services.sh
systemctl --user status wonju-vllm.service wonju-health-ai.service
```

로그인 없이도 시작되도록 `loginctl enable-linger`가 필요하다. 셸·systemd 파일은
`.gitattributes`로 LF를 강제한다. Windows에서 직접 복사한 뒤에는
`file systemd/run-wonju-health-ai.sh`로 `CRLF`가 아닌지 확인한다.

### 운영 검증

실서비스 검증은 비밀번호를 명령행에 넣지 않는다. 필요 시 대화형 입력 또는 보안 비밀
저장소 환경변수를 사용한다.

```powershell
& ".\.venv-p1\Scripts\python.exe" deploy\open-webui\verify_live_stack.py `
  --base-url http://192.168.100.58 `
  --create-temporary-user `
  --admin-email admin@example.org `
  --developer-email developer@example.org

& ".\.venv-p1\Scripts\python.exe" deploy\open-webui\verify_live_browser.py `
  --base-url http://192.168.100.58 `
  --create-temporary-user `
  --admin-email admin@example.org

& ".\.venv-p1\Scripts\python.exe" deploy\open-webui\verify_audit_runtime.py `
  --base-url http://192.168.100.58 `
  --admin-email admin@example.org
```

일반 사용자가 `wonju-health-rag` 이외 모델을 보거나, 공개 `/v1` endpoint가 열리거나,
안전 답변에 119·109 안내가 빠지면 배포를 승인하지 않는다.

## 데이터·평가·결과보고 문서

- [P0 기관·공공보건 통합 결과](data/integrated/wonju/)
- [P1 평가 보고서](data/p1_rag/reports/evaluation_report.json)
- [RISE 최종 결과보고서 초안](data/p1_rag/reports/rise_final_report_draft.md)
- [RISE 문서 작성 인덱스](docs/rise/README.md)
- [Spark/Open WebUI 상세 배포 문서](deploy/open-webui/README.md)
- [상업 이용 가능 의료 코퍼스 문서](docs/commercial_medical_corpus.md)

상업용 의료 코퍼스는 PMC OA의 CC0·CC BY, MedlinePlus public-domain 콘텐츠,
문서별 공공누리 제0·제1유형이 확인된 국내 공공문서만 사용한다. 상세 수집·라이선스
검증·DGX 전체 실행 방법은 위 문서를 따른다.

## 운영 원칙

- 공식 근거가 없는 건강·운영정보는 생성하지 않는다.
- 진단, 처방, 약물 용량 확정은 제공하지 않는다.
- 응급·자살·중독·금단 위험은 생성 전에 결정적 안전 규칙으로 분기한다.
- 일반 사용자는 raw vLLM endpoint에 직접 접근할 수 없다.
- P0 데이터와 P1 입력은 Compose에서 읽기 전용으로 마운트한다.
- 감사 로그는 직접 식별정보를 마스킹하고 기본 30일만 보존한다.
