# 원주시 생활건강 안내 AI 배포

이 디렉터리는 기존 P0 데이터와 P1 RAG 파이프라인을 읽기 전용으로 사용하는
Open WebUI 배포 계층이다. 참조 저장소에서는 Caddy 단일 진입점, 빌드 시 overlay
주입, 그룹 기반 모델 권한 구조만 선별해 적용했다. CDXVI, 문서 자동화,
재고·구매 기능과 브랜딩은 포함하지 않는다.

## 배포 구조

```text
사용자 브라우저
    │ http://<DGX-IP>:80
    ▼
  Caddy ─────────────► Open WebUI
                         ├─ wonju-health-rag ─► P1 API ─┐
                         │   검색·reranker·안전·인용     │
                         └─ raw model ─► 개발자 프록시 ──┤
                                                        ▼
                       Docker DNS: wonju-vllm:8000 ─► vLLM
                       host bind: 127.0.0.1:8000 only
```

- `caddy`: 외부에 공개되는 유일한 Compose 서비스다. 호스트 80번 포트에서
  Open WebUI만 전달하고 `/v1`, `/p1-api`, `/raw-api` 직접 접근은 404로 막는다.
- `open-webui`: `v0.9.6` 기반이며 큰 글자·높은 대비·단순한 버튼 구조와 답변 카드
  overlay를 빌드에 주입한다. 자체 Knowledge/RAG, 파일·웹 업로드, 웹 검색, 도구,
  직접 연결은 비활성화한다.
- `p1-api`: 기존 `scripts/p1_rag`, `data/p1_rag`, 기관 통합 산출물을 읽기 전용으로
  불러 OpenAI 호환 API를 제공한다. 일반 사용자 모델은 `wonju-health-rag` 하나다.
- `raw-dev-proxy`: raw 모델 검색은 Open WebUI 내부 연결 키로 유지하되, 실제
  `/v1/chat/completions`는 내부 키, Open WebUI 서명 JWT, `DEVELOPER_EMAILS`를
  모두 통과해야 vLLM에 전달한다.
- `permissions-bootstrap`: 일반 모델은 일반 사용자에게, raw 모델은 `개발자`
  그룹에만 읽기 권한을 부여하고 환경변수의 개발자 목록과 주기적으로 동기화한다.

DGX Spark 운영 배포에서는 vLLM을 `wonju-health-internal` Docker 네트워크에
연결하고 P1 API와 개발자 프록시가 `http://wonju-vllm:8000/v1`로 접근한다.
vLLM의 호스트 바인딩은 `127.0.0.1:8000`뿐이므로 LAN 사용자는 원본 모델을 직접
호출할 수 없다. 현재 공개 UI 주소는 `http://192.168.100.58`이다.

## 주민 친화 답변과 화면 표시

증상·생활건강 질문은 다음 순서로 안내한다.

1. 먼저 불편과 걱정에 공감한다.
2. 공식 근거 범위에서 가능한 원인을 설명하되 진단처럼 단정하지 않는다.
3. 의료행위나 처방을 대신하지 않는 안전한 생활 관리 방법을 제시한다.
4. 근거가 있을 때만 일반의약품·상비약 범위의 선택지를 안내하며 제품·용량·
   복용법을 추정하지 않는다.
5. 동네나 주소가 포함되면 원주시 기관 마스터에서 해당 지역 병원과 약국을 찾아준다.

주소·전화·운영시간 같은 단순 기관 질의에는 5단계를 강제하지 않고 요청한
정보부터 간결하게 답한다. 근거가 없으면 진단, 원인 확정, 약품·용량을 생성하지
않는다. 주소 기반 결과는 거리 계산 결과가 아니라 지역명과 공식 주소가 일치한
기관임을 화면에 알린다. 심야·연중무휴 약국 자료에 없는 동네도 관내의료기관
마스터의 일반 약국으로 보완하되, 운영시간은 추정하지 않는다. 각 기관 카드는
[Kakao 지도 공식 검색 URL](https://apis.map.kakao.com/web/guide/)로 여는
`지도에서 보기` 버튼을 제공하며 별도 지도 API 키를 사용하지 않는다.

P1 API 답변의 `wonju-health-meta`는 사용자에게 그대로 노출하지 않는다. overlay가
메타데이터를 파싱해 다음 요소로 나누어 표시한다.

- 출처 카드: 문서 제목, 공식 URL, 문서 ID, 청크 ID
- 기관 카드: 기관명, 전화번호, 주소, 운영시간, 지도에서 보기
- 안전 카드: 위험 유형, 행동 안내, `tel:` 즉시 연락 버튼

구조화 카드가 만들어진 항목은 마크다운 fallback에서 중복 표시하지 않는다.
응급·자살·중독·금단 표현은 생성 모델보다 결정적 안전 규칙을 우선 적용한다.
자살 위기에는 109·119 연락 버튼을 상단에 표시한다. 개인 용량 질문처럼
`medical_high_risk`이지만 즉시 응급 징후가 없는 경우에는 무조건 119를 붙이지
않고 안전한 확인 절차를 안내한다.

화면은 Open WebUI의 모델 선택 바·사이드바·기본 시작 제안·assistant avatar를
일반 사용자에게 보이지 않고, 원주시 서비스 헤더 하나만 사용한다. 홈에서는
검색형 질문창과 4개 빠른 질문을 제공하고, 대화가 시작되면 질문창을 94px의
간결한 별도 입력 행으로 줄여 답변 영역을 넓힌다. 기관·출처·안전 정보는 중첩 카드나
그라데이션 없이 평평한 공공서비스 표면으로 구분한다. 답변 도구는 복사·소리로
듣기·평가만 주민 친화 라벨로 제공하며, 겹침을 만드는 Open WebUI의 부유형 최신
답변 버튼은 표시하지 않는다. 헤더·전화·응급·답변 도구는 모바일에서도 44px
이상의 터치 영역을 유지한다.

900px 이하에서는 새 질문·지난 질문·내 정보/로그아웃을 하나의 서비스 메뉴로
묶고 응급 119는 항상 헤더에 유지한다. 개발자 계정은 권한 API에서
`gemma-4-31b-nvfp4` 접근이 확인될 때만 내부 모델 선택 도구가 나타나며, 일반
사용자에게는 계속 `wonju-health-rag`만 노출된다. 정적 HTML 제목·언어·파비콘,
PWA manifest와 테마 색상도 원주시 서비스 자산으로 교체해 최초 로딩과 앱 설치
화면에도 Open WebUI 명칭이나 기본 아이콘이 남지 않는다.

## 환경 준비

Docker Engine과 Compose v2가 필요하다. P1 API 이미지는 x86_64에서는 PyTorch
CPU 인덱스를, DGX Spark 같은 ARM64에서는 PyPI의 ARM64 wheel을 선택한다.

```powershell
Set-Location C:\Users\user\Desktop\KDU-KangWon-ANCHOR\deploy\open-webui
Copy-Item .env.example .env
```

`.env`에서 다음 값을 반드시 운영용 값으로 바꾼다.

- `WEBUI_ADMIN_EMAIL`, `WEBUI_ADMIN_PASSWORD`
- `WEBUI_SECRET_KEY`, `OPEN_WEBUI_JWT_SECRET`: 서로 다른 긴 난수
- `P1_INTERNAL_API_KEY`, `RAW_PROXY_INTERNAL_API_KEY`: 서로 다른 긴 난수
- `DEVELOPER_EMAILS`: raw 모델을 허용할 실제 Open WebUI 계정 이메일
- 필요 시 `VLLM_BASE_URL`, `VLLM_API_KEY`, `RAW_MODEL_ID`

처음 모델 캐시를 만드는 호스트에서는 `P1_MODEL_OFFLINE=0`을 사용한다. bge-m3와
bge-reranker-v2-m3 캐시가 완전히 준비된 운영 호스트에서는
`P1_MODEL_OFFLINE=1`로 바꾸면 시작 시 외부 Hugging Face 확인 요청 없이 로컬
캐시만 사용한다. 캐시가 없는 상태에서 `1`로 설정하면 P1 API가 준비되지 않는다.

회원가입은 닫혀 있다. 일반 사용자와 개발자 계정은 관리자가 생성한다.
`DEVELOPER_EMAILS`에 적힌 계정은 동기화 서비스가 개발자 그룹에 추가하며,
목록에서 제거된 계정은 그룹에서도 제거된다.

## 실행과 상태 확인

```powershell
docker compose config
docker compose up --build -d
docker compose ps
docker compose logs -f p1-api open-webui permissions-bootstrap caddy
```

운영 중에는 다음 경계를 확인한다.

```powershell
Invoke-WebRequest http://localhost/gateway/health
docker compose exec p1-api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8010/ready').read().decode())"
docker compose exec open-webui curl --fail http://127.0.0.1:8080/health
```

`p1-api`와 `raw-dev-proxy`에는 호스트 포트가 없다. vLLM 컨테이너도 다음 조건을
유지해야 한다.

```text
Docker network: wonju-health-internal
container DNS:  wonju-vllm:8000
host listener:  127.0.0.1:8000
LAN listener:   없음
```

### DGX vLLM 재부팅 자동 복구

레시피로 시작한 vLLM 컨테이너는 `--rm`이므로 Docker restart policy를 사용할 수
없다. DGX에서는 사용자 systemd 서비스가 현재 컨테이너를 감시하고, 재부팅 또는
예상하지 못한 종료 후 같은 loopback 포트와 내부 네트워크로 다시 실행한다.

```bash
chmod 0755 deploy/open-webui/systemd/run-wonju-vllm.sh
mkdir -p /home/elise/.config/systemd/user
cp deploy/open-webui/systemd/wonju-vllm.service /home/elise/.config/systemd/user/
sudo loginctl enable-linger elise
systemctl --user daemon-reload
systemctl --user enable --now wonju-vllm.service
systemctl --user status wonju-vllm.service
```

`Linger=yes`여야 로그인 세션이 없어도 부팅 시 사용자 서비스가 시작된다. 서비스는
Docker 준비를 기다린 뒤 `wonju-health-internal` 네트워크를 보장하고,
`127.0.0.1:8000:8000` 외의 호스트 포트를 열지 않는다.

### DGX Spark Compose 스택 자동 복구

vLLM이 준비된 뒤 Caddy·Open WebUI·P1 API·개발자 프록시·권한 동기화 컨테이너도
자동으로 시작해야 한다. `wonju-health-ai.service`는 Compose의 healthcheck가 모두
통과할 때까지 최대 15분 기다리며, 부팅마다 이미지를 불필요하게 다시 빌드하지 않는다.

Spark 서버에서 `.env`를 실제 운영 비밀값으로 준비한 뒤 한 번만 실행한다.

```bash
cd /home/elise/Desktop/KDU-KangWon-ANCHOR/deploy/open-webui
chmod 0755 systemd/run-wonju-vllm.sh systemd/run-wonju-health-ai.sh \
  systemd/install-dgx-spark-services.sh
./systemd/install-dgx-spark-services.sh
systemctl --user status wonju-vllm.service wonju-health-ai.service
docker compose ps
```

운영 코드를 갱신할 때는 먼저 `docker compose up --build -d --wait`로 새 이미지를
검증하고, 이후 재부팅은 `wonju-health-ai.service`가 기존 검증된 이미지를 기동한다.
`.env`와 Docker volume은 저장소에 포함하지 않는다.

Windows 워크스테이션에서 Spark로 파일을 복사하는 경우에도 `.gitattributes`가
`*.sh`와 `*.service`를 LF로 유지한다. Linux systemd 스크립트에 CRLF가 들어가면
`/usr/bin/env: bash\r` 오류로 기동하지 않으므로, 직접 복사한 파일은 서비스 시작 전
`file systemd/run-wonju-health-ai.sh`로 줄바꿈을 확인한다.

## 모델 권한

일반 사용자에게 보이는 모델은 정확히 하나여야 한다.

```text
wonju-health-rag
```

`DEVELOPER_EMAILS`에 포함된 계정에는 일반 모델과 raw 모델이 보인다.

```text
gemma-4-31b-nvfp4
```

raw 모델의 `/v1/models` 검색은 Open WebUI 공급자 탐색이 끊기지 않도록 내부
연결 키로 허용한다. 사용자별 노출은 Open WebUI 모델 ACL이 통제하고, 실제 raw
추론은 개발자 프록시가 서명 JWT와 이메일 허용 목록을 다시 검사한다.
`BYPASS_MODEL_ACCESS_CONTROL=false`를 유지한다. 시스템 관리자도 개발자 허용
목록에 없다면 raw 추론은 403으로 거부된다.

## 자동 테스트

저장소의 Python 3.12 P1 환경에서 배포 의존성을 설치하고 테스트한다.

```powershell
uv pip install --python ".venv-p1\Scripts\python.exe" `
  -r deploy\open-webui\p1-api\requirements.txt PyYAML pytest httpx playwright
& ".\.venv-p1\Scripts\python.exe" -m playwright install chromium
& ".\.venv-p1\Scripts\python.exe" -m pytest -q deploy\open-webui\tests
& ".\.venv-p1\Scripts\python.exe" -m pytest -q
```

최종 배포 전용 테스트는 `68 passed`, 저장소 전체 테스트는
`127 passed, 1 skipped`를 확인했다.

실행 중인 전체 스택은 기존 사용자의 비밀번호를 명령행에 남기지 않고 임시 일반
사용자를 만들어 검증할 수 있다. 아래처럼 비밀번호 인자를 생략하면 검증기가
관리자·개발자 비밀번호를 대화형으로 입력받는다.

```powershell
& ".\.venv-p1\Scripts\python.exe" deploy\open-webui\verify_live_stack.py `
  --base-url http://192.168.100.58 `
  --create-temporary-user `
  --admin-email admin@example.org `
  --developer-email developer@example.org
```

Edge 또는 Chrome과 Playwright가 있는 워크스테이션에서는 실제 렌더링까지 같은
방식으로 확인한다. 이 검증은 증상 5단계, 기관·출처 카드 중복, 원시 메타데이터,
자살 안전 카드와 전화 버튼을 검사한다.

```powershell
& ".\.venv-p1\Scripts\python.exe" deploy\open-webui\verify_live_browser.py `
  --base-url http://192.168.100.58 `
  --create-temporary-user `
  --admin-email admin@example.org `
  --screenshot-dir data\p1_rag\reports\live_browser
```

노인, 유아 보호자, 일반 주민 관점의 다중 턴 흐름은 별도 검증기로 반복한다.
증상 흐름은 세 번의 확인 질문 뒤 최종 답변을 검사하고, 일반 주민 흐름은
읍면동 확인 뒤 병원·약국, 전화, 지도 연결을 검사한다.

```powershell
& ".\.venv-p1\Scripts\python.exe" deploy\open-webui\verify_persona_usability.py `
  --base-url http://192.168.100.58 `
  --create-temporary-user `
  --admin-email admin@example.org `
  --screenshot-dir data\p1_rag\reports\persona_usability `
  --report data\p1_rag\reports\persona_usability_report.json
```

관리자 화면, 개발자 모델 설정 바, 첫 스트리밍 답변은 관리자 전용 검증기로
확인한다. 관리자 비밀번호는 환경변수 또는 대화형 입력으로만 전달한다.

```powershell
& ".\.venv-p1\Scripts\python.exe" deploy\open-webui\verify_admin_browser.py `
  --base-url http://192.168.100.58 `
  --admin-email admin@example.org `
  --screenshot-dir data\p1_rag\reports\admin_browser `
  --report data\p1_rag\reports\admin_browser_report.json
```

이 검증은 사용자·환경 설정·모델 관리 경로의 전용 관리자 헤더와 반응형 배치,
첫 질문 직후 새로고침 없는 답변 표시, 기관·출처 카드 렌더링, 개발자 설정 바의
`sticky` 배치 및 본문 비가림을 검사한다.

관리자 헤더의 **운영 현황**에서는 최근 질의, 응답 실패, 위험 분류, 사용자
피드백을 필터링하고 CSV로 내보낼 수 있다. 질문의 전화번호·전자우편·주민등록번호
형식은 저장 전에 마스킹하고 사용자 ID는 단방향 해시로만 저장한다. 기본 보존기간은
30일이며 `P1_AUDIT_RETENTION_DAYS`로 조정한다. 감사 데이터는
`p1_audit_data` 볼륨에 저장되고 일반 사용자에게는 목록·요약·내보내기 API를
허용하지 않는다. 답변의 도움됨/개선 필요 버튼은 해당 응답의 감사 이벤트에만
연결된다.

RISE 정량 증빙은 기존 평가 결과를 다시 세어 생성한다.

```powershell
& ".\.venv-p1\Scripts\python.exe" scripts\generate_rise_evidence.py --strict
```

이 명령은 고령자 시나리오 10건과 안전 판정 패턴 31개를 고유 ID로 출력하며,
근거 평가 케이스가 사라지거나 중복되면 실패한다.

실서비스의 관리자 권한 경계, 질의 기록, 답변별 피드백, 필터 조회와 CSV 내보내기는
다음 검증기로 확인한다. 검증용 일반 사용자는 종료 시 삭제된다.

```powershell
$env:WONJU_HEALTH_ADMIN_PASSWORD = Read-Host -AsSecureString | ConvertFrom-SecureString -AsPlainText
& ".\.venv-p1\Scripts\python.exe" deploy\open-webui\verify_audit_runtime.py `
  --base-url http://192.168.100.58 `
  --admin-email admin@example.org `
  --report data\p1_rag\reports\audit_runtime_report.json
Remove-Item Env:WONJU_HEALTH_ADMIN_PASSWORD
```

자동화에서는 비밀 저장소가 `WONJU_HEALTH_ADMIN_PASSWORD`와
`WONJU_HEALTH_DEVELOPER_PASSWORD`를 프로세스 환경변수로 주입하게 한다.
비밀번호를 스크립트나 README에 기록하지 않는다.

검증기는 다음을 실패로 처리한다.

- 공개 `/v1/models`가 404가 아님
- 일반 사용자에게 `wonju-health-rag` 이외 모델이 보임
- 일반 답변에 출처 또는 연결 기관이 없음
- 자살 위험 답변에 안전 규칙과 109·119 연락 근거가 없음
- 허용된 개발자에게 raw 모델이 보이지 않음
- 생성한 임시 사용자를 검증 종료 시 삭제하지 못함

## DGX Spark 실배포 검증 결과 (2026-07-17)

`http://192.168.100.58`의 실제 Open WebUI와 DGX vLLM을 대상으로 확인했다.

| 검사 | 결과 |
|---|---:|
| Compose 공개 포트 | Caddy `80`만 공개 |
| 원본 vLLM LAN 직접 접근 | 차단, 호스트 `127.0.0.1:8000`만 수신 |
| vLLM 사용자 systemd 서비스 | linger 활성화·자동 시작·강제 중지 후 자동 재생성 통과 |
| 일반 사용자 모델 목록 | `wonju-health-rag`만 노출 |
| 개발자 모델 목록 | 일반 모델 + `gemma-4-31b-nvfp4` |
| live verifier 일반 답변 | 출처·기관 연결 통과 |
| live verifier 자살 안전 답변 | 안전 규칙과 109·119 통과 |
| live verifier 임시 사용자 | 검증 후 삭제 통과 |
| 증상 답변 실브라우저 | 공감→원인→대처→상비약→병원 찾기 5단계 통과 |
| 증상+지역 병원 찾기 | 5단계 답변과 기관 카드 동시 표시 통과 |
| 노인 사용자 페르소나 | 3회 확인 질문, 5단계 답변, 기관 3곳, 출처 4건 통과 |
| 유아 보호자 페르소나 | 3회 확인 질문, 5단계 답변, 기관 3곳, 출처 2건 통과 |
| 일반 주민 페르소나 | 행구동 확인, 병원·약국 3곳, 전화 3개, 지도 3개 통과 |
| 답변 본문 가독성 | 16px 글자, 28px 줄높이, 정확한 약 용량 임의 제시 0 |
| 기관 카드 / 출처 카드 | 질의에 따라 1~3개 / 1~4개 표시 |
| 마크다운 fallback 중복 | 0 |
| raw `wonju-health-meta` 노출 | 0 |
| 대용량 카드 메타데이터 | 화면 필드 투영 및 손실 없는 분할 회귀 테스트 통과 |
| 자살 안전 카드 | 답변 상단, 109·119 버튼 통과 |
| 응급·금단 표현 변형 | 안전 라우팅 통과 |
| 서비스 전용 시작·로그인 화면 | 원주시 공식자료 연계 헤더, 4개 빠른 질문, 응급 연락, 모바일 overflow 0 |
| 관리자 전용 화면 | 사용자·환경 설정·모델 관리·운영 현황 4개 경로, 데스크톱·모바일 통과 |
| 운영 감사 기능 | 일반 사용자 목록 403, 피드백 200, 관리자 조회·CSV 200, 지표 6개·필터 4개 |
| 첫 질문 응답 표시 | 새로고침 없이 2.407초에 표시, 기관·출처 카드 각 1개 |
| 개발자 모델 설정 바 | `sticky`, 서비스 헤더 아래 배치, 시작 화면 비가림 0 |
| 반응형 실브라우저 폭 | `320·360·390·768·1024·1440px`, overflow·겹침 0 |
| 대화 입력창 | 데스크톱·모바일 94px, 메시지 영역과 겹침 0, 문서 흐름 유지 |
| 모바일 핵심 터치 영역 | 헤더·전화·안전·답변 도구 44px 이상 |
| stock Open WebUI 셸 노출 | 모델 바·사이드바·기본 시작화면·assistant avatar 0, 기술 출처 식별자는 접힘 상태 |
| 실제 화면 캡처 | 로그인·홈·기관·5단계 증상·자살 안전 답변의 데스크톱/모바일 시작·끝 화면 확인 |
| P0 보호 SHA-256 | `8293d8fc5ddd6d9fe4ef6125565ed5da58838904fad3a1b0de733022b8bf8a0d` 유지 |

## 파일 역할

- `docker-compose.yml`, `Caddyfile`, `.env.example`: 격리 배포와 환경 설정
- `open-webui/`, `overlay/`: 재현 가능한 Open WebUI 이미지와 원주시 생활건강 전용 UI 셸
- `p1-api/app.py`: 기존 P1 RAG의 OpenAI 호환 서빙 어댑터
- `p1-api/raw_proxy.py`: 개발자 전용 raw 모델 프록시
- `bootstrap/bootstrap_permissions.py`: 모델 ACL·개발자 그룹 동기화
- `systemd/`: DGX 재부팅 후 loopback-only vLLM 자동 복구
- `verify_live_stack.py`, `verify_live_browser.py`, `verify_persona_usability.py`,
  `verify_admin_browser.py`, `tests/`: 실스택, 실제 렌더링, 사용자군별 다중 턴,
  관리자 화면·첫 응답·설정 바, 단위·보안 경계 검증

기존 P0·P1 파일은 수정하지 않으며 Compose에서도 모두 읽기 전용으로 마운트한다.

## 구현 기준 자료

- [참조 저장소](https://github.com/Elise-Software/Document-Automation-LLM): Caddy, overlay 주입, 그룹 권한 구조만 참고
- [Open WebUI 모델 접근 제어](https://docs.openwebui.com/features/workspace/models/): 일반·raw 모델을 독립 연결로 분리하는 공식 패턴
- [Open WebUI RBAC](https://docs.openwebui.com/features/authentication-access/rbac/): 역할·권한·그룹과 additive 권한 모델
- [Open WebUI 환경변수](https://docs.openwebui.com/reference/env-configuration/): OpenAI 호환 연결과 기능 비활성화 설정
