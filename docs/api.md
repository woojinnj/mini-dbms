# HTTP API

시작: `python3 -m minidb serve --port 8080 --workers 8 --queue-size 32`.
기본 주소: `http://127.0.0.1:8080`. 다른 호스트에서 접근하려면 `--host 0.0.0.0`.

## POST /query

`Content-Type: application/json`과 `Content-Length`가 필요합니다. 일반 HTTP 클라이언트는 Content-Length를 자동 설정합니다. Chunked 전송은 지원하지 않습니다.

```json
{"sql": "SELECT name, age FROM users WHERE id = 1"}
```

성공 시 HTTP 200:

```json
{
  "result": {
    "command": "SELECT",
    "columns": ["name", "age"],
    "rows": [{"name": "정글", "age": 25}],
    "row_count": 1,
    "plan": "BPLUS_POINT_LOOKUP",
    "rows_examined": 1,
    "truncated": false,
    "elapsed_ms": 0.1
  }
}
```

`elapsed_ms`는 엔진 실행과 테이블 잠금 대기를 포함하고 SQL 파싱·HTTP 대기/전송은 제외합니다. 벤치마크 스크립트는 외부에서 시간을 측정하여 SQL 파싱 또는 HTTP 왕복까지 포함합니다.

INSERT 결과:

```json
{"result":{"command":"INSERT","affected_rows":2,"first_id":1,"last_id":2,"elapsed_ms":0.5}}
```

요청당 SQL 문장 하나만 허용합니다. 다중 문장은 쓰기를 시작하기 전에 거부합니다. 여러 행을 넣으려면 단일 `INSERT ... VALUES (...), (...)`를 사용하세요.

결과 행이 제한을 넘으면 `truncated: true`입니다. ID를 포함해 조회하고 마지막 ID보다 큰 범위를 다음 요청에 사용하세요. 이 페이지 방식은 현재 INSERT-only 모델에 적합하며 여러 요청을 하나의 snapshot으로 고정하지는 않습니다.

## GET /health

```json
{"status":"ok","tables":["users"]}
```

HTTP 서버의 응답 여부를 확인하는 liveness 엔드포인트입니다. 저장 장치에 새로 읽기/쓰기를 수행하는 무결성 검사나 durability 검사는 아닙니다.

## GET /metrics

| 필드 | 의미 |
|---|---|
| accepted | worker/대기열에 접수한 연결 수 |
| rejected | 수용량 초과로 503 처리한 연결 수 |
| active / peak_active | 현재/최대 실행 중인 worker 작업 수 |
| completed | 완료된 worker 작업 수 |
| queries | 엔진 실행을 시도한 SQL 요청 수 |
| errors | handler가 반환한 오류 응답 수; overload는 rejected로 집계 |
| workers / queue_capacity | 설정한 worker 수와 애플리케이션 대기열 크기 |
| uptime_seconds | 서버 시작 후 경과 시간 |

지표는 연결/HTTP 처리 기준입니다. `active`는 실행 중인 SQL 수와 같지 않을 수 있으며, `/metrics` 요청 자체도 active에 포함됩니다. 대기 수는 `accepted - completed - active`로 계산할 수 있습니다. 오류 지표는 모든 네트워크 단절을 포함하는 총 장애 횟수가 아닙니다.

## 오류 응답

```json
{"error":{"code":"VALIDATIONERROR","message":"Unknown table: missing"}}
```

| HTTP | 상황 |
|---|---|
| 400 | SQL 문법/타입/스키마 오류, 잘못된 JSON, 다중 SQL, chunked 요청 |
| 401 | 토큰이 설정돼 있고 인증 실패 |
| 404 | 없는 경로 |
| 408 | 요청 본문 읽기 timeout |
| 411 | 없거나 잘못된/중복된 Content-Length |
| 413 | 본문 3 MiB 또는 SQL 2 MiB 초과 |
| 415 | Content-Type이 application/json이 아님 |
| 500 | 저장소 또는 내부 오류. 서버 로그에 상세 원인 기록 |
| 501 | 지원하지 않는 HTTP 메서드 |
| 503 | worker + queue 수용량 초과. `Retry-After: 1` |

오류 발생 시 SQL 본문이나 인증 토큰을 HTTP 로그에 기록하지 않습니다. 응답 전 연결이 끊기면 INSERT의 커밋 여부가 불명확할 수 있으므로, 무조건 재시도하는 대신 데이터를 확인해야 합니다.

## 선택적 Bearer 인증

서버 프로세스에 `MINIDB_API_TOKEN`이 설정된 경우 `/query`, `/health`, `/metrics` 모두 다음 헤더가 필요합니다.

```text
Authorization: Bearer <configured-token>
```

토큰이 없으면 로컬 개발용으로 인증 없이 동작합니다. 브라우저 GUI를 여는 데 필요한 정적 파일은 토큰 입력 전에 제공되며, 테이블 목록 조회와 SQL 실행에는 API 인증이 적용됩니다. 역할별 권한, 테이블별 접근 제어, TLS는 구현 범위 밖입니다.
