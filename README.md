# Mini DBMS

크래프톤 정글 13기 과제: **SQL 파서 → 파일 저장 엔진 → B+ 트리 → Thread Pool HTTP API**를 직접 구현한 미니 DBMS입니다. SQLite 같은 기존 DB 엔진이나 SQL 파싱 라이브러리를 사용하지 않습니다.

Python **3.11 이상**, **macOS / Linux**에서 실행합니다. 런타임·테스트 외부 의존성은 없습니다. Windows에서는 WSL을 사용합니다(`fcntl.flock` 사용).

## 바로 실행하기

프로젝트 루트에서 실행합니다. 별도 설치 없이 `python3 -m minidb`를 사용할 수 있습니다.

```bash
# Phase 1: 텍스트 SQL 파일 처리, 기본 data/에 저장
python3 -m minidb exec --file examples/demo.sql

# 다른 프로세스에서 다시 실행해도 이전 데이터가 유지됨
python3 -m minidb exec --sql "SELECT * FROM users WHERE id = 2"

# stdin / 기계가 읽을 수 있는 JSON Lines 출력
printf 'SELECT COUNT(*) FROM users;' | python3 -m minidb exec --stdin --format json

# Phase 2: 실제 100만 건 삽입 + 인덱스/선형 탐색 비교 + 재시작 검증
python3 scripts/benchmark.py --rows 1000000 --output benchmarks/latest.json

# Phase 3: 같은 data/를 엔진으로 사용하는 API 서버
python3 -m minidb serve --host 127.0.0.1 --port 8080 --workers 8 --queue-size 32
```

`demo.sql`은 실행할 때마다 3행을 추가합니다. 같은 DB 디렉터리를 여는 프로세스는 한 개만 허용합니다. 서버가 실행 중이면 브라우저 GUI나 HTTP API로 SQL을 보내세요. 종료는 Ctrl+C이며, 이미 접수한 요청을 처리한 후 DB 잠금을 해제합니다.

**브라우저 GUI**는 서버를 실행한 뒤 [127.0.0.1:8080](http://127.0.0.1:8080/)을 열면 됩니다. 테이블을 선택해 조회문을 불러오고 SQL을 한 문장씩 실행할 수 있습니다. `CREATE TABLE`로 만든 테이블은 목록에 바로 나타납니다. 실행 단축키는 Ctrl+Enter 또는 ⌘+Enter입니다. `MINIDB_API_TOKEN`을 설정했다면 화면에서 토큰을 입력합니다.

다른 터미널에서:

```bash
curl -s http://127.0.0.1:8080/health

curl -s http://127.0.0.1:8080/query \
  -H 'Content-Type: application/json' \
  -d '{"sql":"INSERT INTO users (name, age, email) VALUES ('\''API 사용자'\'', 25, NULL)"}'

curl -s http://127.0.0.1:8080/query \
  -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT * FROM users WHERE id = 1"}'

curl -s http://127.0.0.1:8080/metrics
```

외부 머신에서 연결하려면 `--host 0.0.0.0`을 사용하고 서버 IP로 접속합니다. 선택적으로 `MINIDB_API_TOKEN` 환경 변수를 설정하면 API 엔드포인트가 `Authorization: Bearer <token>`을 요구합니다. HTTP API 규격은 [API 문서](docs/api.md)와 [OpenAPI](docs/openapi.yaml)를 참고하세요.

## 과제 요구사항과 구현 위치

| 요구사항 | 구현 | 검증 |
|---|---|---|
| SQL 파일 CLI, INSERT / SELECT 파싱 | `sql.py`, `__main__.py` | 파서 단위 테스트, 실제 CLI subprocess 테스트 |
| 테이블별 파일 저장과 재실행 후 조회 | `storage.py`, `engine.py` | 파일 재오픈, 스키마·타입 검증 |
| 자동 ID, 직접 구현한 메모리 B+ 트리 | `btree.py` | 정순·역순·무작위 삽입, 분할 불변식, 범위 검색 |
| WHERE id = 조건의 인덱스 사용 | `engine.py` | 조회 경로 검증, `rows_examined`, `EXPLAIN` |
| 100만 건 삽입 및 성능 비교 | `scripts/benchmark.py` | [실측 JSON](benchmarks/million-rows.json), [결과 해석](docs/benchmark.md) |
| Phase 1·2 엔진을 재사용하는 API | `server.py` | 실제 HTTP 요청, API 부하 테스트 |
| 스레드 풀과 동시성 제어 | `server.py`, `locking.py` | 동시 읽기·쓰기, ID 무결성, 과부하 503 |
| 단위·기능·엣지 케이스 테스트 | `tests/` | `python3 -m unittest discover -s tests -v` |

## 지원 SQL

기본 테이블 `users`는 사전에 정의된 스키마로 자동 초기화됩니다.

| 컬럼 | 타입 | 조건 |
|---|---|---|
| id | INTEGER | 1부터 자동 증가, 직접 지정 불가 |
| name | TEXT | NOT NULL |
| age | INTEGER | NOT NULL |
| email | TEXT | NULL 허용 |

```sql
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL);

INSERT INTO users (name, age) VALUES ('정글', 25);
INSERT INTO users VALUES ('Alice', 23, 'alice@example.com'), ('O''Brien', 31, NULL);

SELECT * FROM users;
SELECT name, age FROM users WHERE id = 2;
SELECT * FROM users WHERE age >= 20 AND email IS NOT NULL LIMIT 100;
SELECT * FROM users WHERE id >= 2 AND id < 100;
SELECT COUNT(*) FROM users WHERE age >= 20;
EXPLAIN SELECT * FROM users WHERE id = 2;
```

- 키워드와 식별자는 대소문자를 구분하지 않습니다. 문자열은 작은따옴표로 감싸며 작은따옴표는 `''`로 이스케이프합니다. 문자열 비교는 대소문자를 구분합니다.
- `-- 한 줄 주석`, `/* 여러 줄 주석 */`, 여러 줄 SQL, 세미콜론으로 구분한 SQL 파일을 지원합니다.
- `WHERE`는 `=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`, `IS NULL`, `IS NOT NULL`, `AND`를 지원합니다. `column = NULL`은 일치하지 않습니다.
- `SELECT COUNT(*)`의 `LIMIT`은 집계 결과 행에 적용합니다. `LIMIT 1`은 전체 집계를 반환하고 `LIMIT 0`은 결과를 반환하지 않습니다.
- 컬럼 목록을 생략한 INSERT는 **id를 제외한 스키마 순서**를 사용합니다. NULL 허용 컬럼을 명시적 목록에서 생략하면 NULL로 저장됩니다.
- INTEGER는 signed 64-bit, REAL은 유한한 숫자, BOOLEAN은 TRUE/FALSE, TEXT는 UTF-8 최대 64 KiB입니다. 암묵적인 문자열→숫자 변환은 없습니다.
- `CREATE TABLE`은 네 가지 컬럼 타입과 `NULL`(기본값), `NOT NULL`을 지원합니다. 일반 컬럼이 하나 이상 필요합니다. 모든 테이블의 `id`는 자동 증가하며, 생략하거나 `id INTEGER PRIMARY KEY`로 명시할 수 있습니다. 같은 이름의 테이블이 이미 있으면 오류입니다.
- SQL 입력은 최대 2 MiB, 한 INSERT는 최대 10,000행·저장 프레임 64 MiB입니다. 큰 적재는 벤치마크처럼 여러 SQL 배치로 나눕니다.
- 결과는 기본 최대 10,000행·행 JSON 합계 8 MiB이며, 잘리면 `truncated: true`를 반환합니다. `WHERE id > 마지막ID LIMIT 1000`으로 다음 페이지를 조회할 수 있습니다. 한 행의 projection만으로 8 MiB를 넘으면 오류입니다.
- `UPDATE`, `DELETE`, `JOIN`, `OR`, 괄호 조건식, `ORDER BY`, 별칭, 서브쿼리, 명시적 트랜잭션은 구현 범위에 포함하지 않습니다. 현재 반환 순서는 자동 증가 ID 순서입니다.

## 미리 정의한 스키마 사용

```bash
python3 -m minidb --data-dir data/catalog --schema examples/schema.json exec \
  --sql "INSERT INTO products VALUES ('키보드', 99000.0, TRUE)"
python3 -m minidb --data-dir data/catalog exec --sql "SELECT * FROM products"
```

`--schema`는 초기 테이블 정의를 JSON으로 제공합니다. 테이블·컬럼 이름은 소문자 영문/숫자/밑줄이고 숫자로 시작할 수 없습니다. `id`는 모든 테이블에 암묵적으로 추가합니다. 스키마는 DB 디렉터리의 `schema.json`에 저장되며, 재실행 시 다시 지정할 필요가 없습니다. 재실행 시 `--schema`를 넘기면 그 파일에 있는 각 테이블 정의가 저장된 정의와 일치해야 합니다.

## 테스트와 재현

```bash
make test

# 16개 클라이언트, 8개 서버 worker, 실제 HTTP 1,000요청
python3 scripts/api_loadtest.py --pairs 500 --clients 16 --workers 8

# 100만 건 데이터 파일을 보관하려면 새 디렉터리를 지정
python3 scripts/benchmark.py --rows 1000000 --data-dir data/bench-run
```

벤치마크는 기본적으로 임시 DB를 생성하고 검증 후 삭제하며, 측정 결과 JSON은 남깁니다. 지정한 디렉터리에 기존 DB가 있으면 벤치마크를 거부합니다. 기본값은 INSERT 문장마다 `flush + fsync`를 수행합니다. `--no-fsync`는 별도 실험용 옵션이며, 제출용 실측은 fsync를 활성화했습니다.

테스트 범위와 실행 기록은 [검증 문서](docs/testing.md)에 있습니다. GitHub Actions에서도 macOS/Linux의 Python 3.11/3.13으로 테스트합니다.

## 구조와 설명 자료

```text
minidb/
  sql.py          토큰화, 재귀 하강 파싱, AST
  engine.py       검증, 실행 계획 선택, SQL 실행
  btree.py        B+ 트리 삽입·분할·점 조회·리프 범위 순회
  storage.py      파일 포맷, 체크섬, append, 재시작 복구
  locking.py      writer 우선 읽기/쓰기 잠금
  server.py       HTTP, bounded ThreadPoolExecutor, 계측
  __main__.py     CLI, 서버 시작/종료
tests/            단위·통합 테스트
scripts/          100만 건 benchmark, 실제 서버 부하 검증
docs/             설계, 학습 설명, API, 측정 결과, 검증
```

[설계와 핵심 로직 설명](docs/architecture.md)은 B+ 트리 분할, 디스크 오프셋 조회, 문장 단위 원자성, 동시성 경계와 구현 한계를 다룹니다. 포트폴리오에서 설명할 수 있도록 복잡도와 선택 이유도 정리했습니다.

차별화 요소로 ID 범위 인덱스, `EXPLAIN`, `rows_examined`, 체크섬 손상 탐지, 중단된 쓰기 복구, 요청 대기열 제한, 선택적 인증과 서버 지표를 구현했습니다. 메모리 기반 인덱스는 재시작할 때 파일에서 재구성합니다. 다중 문장/다중 테이블 트랜잭션과 MVCC는 없으며, CPython의 GIL 때문에 CPU 연산 자체가 여러 코어에서 병렬 실행되는 것은 아닙니다. 여러 요청의 I/O는 스레드 풀에서 동시 처리합니다.
