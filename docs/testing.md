# 검증 기록

2026-11-16 KST, macOS arm64 / Python 3.13.2에서 실제 실행했습니다.

```text
python3 -m unittest discover -s tests -v

Ran 39 tests in 1.739s
OK
```

테스트 메서드 기준 39개이며, 일부 메서드는 여러 입력 순서·차수·바이트 절단 위치를 반복 검증합니다. 테스트마다 임시 디렉터리를 사용하므로 사용자의 data/를 변경하지 않습니다.

## 단위·기능 테스트 범위

| 파일 | 검증 |
|---|---|
| `test_btree.py` | order 4/5/8/64, 정순·역순·무작위 1,500개 삽입, 다단계 분할, 키/값 일치, separator/리프 깊이/리프 연결 불변식, 중복 키 거부, 없는 키, 열린/닫힌 범위 경계 |
| `test_sql.py` | 대소문자, 한글, 이스케이프 작은따옴표, 문자열 내 세미콜론·주석 기호, 주석, 여러 문장, COUNT/EXPLAIN, bool/NULL/숫자, 미지원 문법, 누락 토큰, 입력·배치 제한, 잘못된 Unicode |
| `test_engine.py` | INSERT/SELECT, projection, 자동 ID, 재시작, 범위+잔여 조건, NULL 의미, 타입/컬럼/스키마 오류, 문장 원자성, 결과 행/바이트 제한, count/limit, 파일 잠금, 파일·스키마 유실 감지 |
| `test_engine.py` 동시성 | 16개 스레드의 400회 INSERT/조회, ID 중복·유실 검증, barrier로 4개 SELECT가 동시에 read lock을 보유하는지 검증 |
| `test_engine.py` 저장 복구 | 마지막 INSERT 프레임을 **모든 바이트 위치에서 잘라** 완전한 앞선 INSERT만 복구되는지 확인, header의 모든 바이트 위치 및 payload/footer 손상 시 오류 확인, fsync/인덱스 갱신 예외 주입 후 롤백 |
| `test_cli.py` | 별도 Python 프로세스에서 SQL 파일·stdin·SQL 문자열 입력, JSON/table 출력, 영속성, 종료 코드, 파일 문법 오류 시 쓰기 방지 |
| `test_server.py` | 실제 loopback HTTP, health/metrics/query, 한글 응답, 선택적 인증, SQL/JSON/HTTP 오류, body 제한·timeout, 12개 클라이언트 동시 INSERT/SELECT, 4 workers+32 queue 포화 후 503 |

## 인덱스 사용을 확인하는 방법

실행 계획 문자열만 검사하지 않습니다.

1. ID 조회는 `rows_examined == 1`, 존재하지 않는 ID는 0인지 확인합니다.
2. ID 조건 조회 중 `Table.records()`가 호출되면 실패하도록 주입해 전체 스캔을 우회하는지 확인합니다.
3. 무작위 범위+age 조건의 결과를 Python의 독립적인 리스트 필터 결과와 비교합니다.
4. 100만 건 실제 벤치마크에서 ID와 email 조건의 전체 결과가 동일하고, 검사 행 수가 각각 1과 1,000,000인지 확인합니다.

## 대규모·API 통합 검증

```bash
python3 scripts/benchmark.py --rows 1000000 --output benchmarks/million-rows.json
python3 scripts/api_loadtest.py --pairs 500 --clients 16 --workers 8 --output benchmarks/api-load.json
```

두 명령 모두 성공했습니다. [측정 결과](benchmark.md)에 실제 수치, 환경, 방법과 해석 한계가 있습니다. API 스크립트는 별도 CLI 서버 프로세스의 시작, 실제 HTTP 1,000요청, SIGTERM 정상 종료, DB 재오픈 후 500행 영속성까지 확인합니다.

## CI

`.github/workflows/tests.yml`은 Ubuntu/macOS × Python 3.11/3.13에서 전체 테스트와 작은 SQL/API 부하 검증을 실행하도록 구성했습니다. 이 작업 중 실제 실행한 환경은 위의 로컬 macOS/Python 3.13.2이며, 원격 GitHub Actions 결과를 확인했다고 주장하지 않습니다.

## 검증의 경계

파일 절단·손상·fsync 실패 테스트는 결정적인 실패 시나리오를 재현합니다. 실제 전원 차단, 모든 저장 장치의 내구성, 장기간 운영이나 악의적인 인터넷 트래픽에 대한 검증을 대체하지는 않습니다. 요구한 동작과 구현한 복구 규칙을 확인한 테스트이며, 임의의 장애에 대한 무결함 보장은 아닙니다.
