# 설계와 핵심 로직

## 1. 공통 엔진으로 세 단계를 연결

```mermaid
flowchart LR
    File[SQL 파일 / stdin] --> CLI[CLI]
    Client[외부 클라이언트] --> HTTP[HTTP API]
    HTTP --> Pool[제한된 Thread Pool]
    CLI --> Parser[Tokenizer + Parser]
    Pool --> Parser
    Parser --> AST[CreateTable / Insert / Select AST]
    AST --> Engine[검증 + 실행 계획]
    Engine --> Lock[테이블 RWLock]
    Lock --> Tree[B+ Tree: ID → 파일 오프셋]
    Lock --> Scan[파일 선형 탐색]
    Tree --> FileDB[테이블별 .mdb 파일]
    Scan --> FileDB
```

CLI와 HTTP가 공유하는 진입점은 `Database`입니다. API는 `Database.execute(sql)`로 단일 문장만 실행합니다. CLI는 파일 전체를 먼저 파싱하고, AST를 `execute_statement`에 전달합니다. 따라서 API용 별도 SQL 처리기나 인덱스 구현이 없습니다.

## 2. SQL: 문자열을 실행 가능한 구조로 바꾸기

`sql.py`는 정규식 tokenizer와 재귀 하강 parser를 사용합니다. 정규식은 토큰을 인식하는 데만 쓰고, 문장의 구조는 parser가 검증합니다.

1. 문자열 리터럴·숫자·식별자·구분 기호를 토큰으로 나눕니다. 문자열 안의 세미콜론은 문장 경계가 아닙니다.
2. `INSERT INTO ... VALUES ...`를 `Insert(table, columns, rows)`로 만듭니다.
3. `SELECT ... WHERE ...`를 `Select(table, columns, conditions, limit, count, explain)`로 만듭니다.
4. `CREATE TABLE ... (...)`을 `CreateTable(table, columns)`로 만듭니다. executor가 스키마와 값 타입을 검증하고 명령을 실행합니다.

문법 검증과 의미 검증을 분리했습니다. `SELECT missing FROM users`는 문법상 가능하지만 스키마 검증에서 거부됩니다. `eval`이나 실행 가능한 코드 변환을 사용하지 않습니다.

SQL 파일은 전체 문법 검증을 통과해야 첫 쓰기를 시작합니다. 이후 문장은 각각 독립적으로 커밋합니다. 뒤쪽 문장의 타입 오류나 I/O 오류가 앞선 문장의 커밋을 취소하지는 않습니다.

## 3. 직접 구현한 B+ 트리

기본 order는 64입니다. 내부 노드는 최대 64개 자식·63개 separator를 가지며, 실제 `(id, offset)` 데이터는 리프에만 존재합니다.

- **검색:** 내부 노드에서는 `bisect_right`로 자식을 선택합니다. separator는 오른쪽 서브트리의 최솟값이므로 동일한 키는 오른쪽으로 갑니다. 리프에서는 `bisect_left`로 키를 찾습니다.
- **리프 분할:** 64개 키가 되면 절반을 오른쪽 리프로 옮깁니다. 오른쪽 첫 키를 부모 separator로 복사하고 리프 연결을 갱신합니다. 실제 키는 리프에 남습니다.
- **내부 노드 분할:** 가운데 separator를 부모로 올립니다. 이 키는 분할한 내부 노드들에서는 제거됩니다.
- **루트 분할:** 부모가 없으면 새 루트를 만들어 높이를 1 늘립니다.
- **범위 검색:** 시작 키의 리프를 찾고 `next` 연결을 따라가다가 상한에 도달하면 종료합니다.

```text
               [ 30 | 60 ]
                /    |    \
       [10,20] -> [30,40] -> [60,70,80]
       ID:offset  ID:offset  ID:offset
```

노드 차수가 고정일 때 점 조회는 O(log N), 범위 조회는 O(log N + K), 인덱스 공간은 O(N)입니다. 파이썬 list의 노드 내 삽입은 O(order)이므로 큰 차수를 무조건 선택할 수는 없습니다. `validate()`는 키 정렬·점유율·separator 경계·동일 리프 깊이·리프 링크·전체 개수를 검증합니다.

ID를 dictionary로 조회하거나 모든 레코드를 메모리에 보관하지 않습니다. B+ 트리에는 ID와 파일 오프셋을 저장하고, 점 조회 때 새 파일 핸들로 seek하여 레코드 하나를 읽습니다. 프레임 단위 체크섬은 재시작/선형 읽기에서, 레코드 체크섬은 점 조회에서도 검증합니다.

## 4. 실행 계획

| 조건 | 계획 | 파일 읽기 |
|---|---|---|
| `WHERE id = 42` | `BPLUS_POINT_LOOKUP` | 존재하면 1행 |
| `WHERE id >= 42 AND id < 100` | `BPLUS_RANGE_SCAN` | 범위에 해당하는 행 |
| `WHERE email = 'a'` | `FULL_TABLE_SCAN` | 전체 파일 |
| `WHERE id = 42 AND age > 20` | `BPLUS_POINT_LOOKUP` | ID 조회 후 나머지 조건 평가 |

`EXPLAIN SELECT ...`는 실행 없이 계획과 테이블 크기를 반환합니다. `rows_examined`는 실제 조건 검사를 위해 읽은 행 수입니다. LIMIT이나 결과 제한에 도달하면 조기 종료할 수 있습니다. `truncated` 판별을 위해 행 제한보다 한 행 더 검사할 수 있습니다.

## 5. 파일 포맷과 내구성

한 테이블은 하나의 `.mdb` 파일입니다. 모든 정수 헤더는 big-endian입니다.

```text
파일 magic: 8 bytes = MINIDB01

반복되는 INSERT 프레임:
  transaction header: 20 bytes
    TXN1                 4 bytes
    payload length       8 bytes
    row count            4 bytes
    CRC32(header 앞 16B) 4 bytes
  payload:
    반복되는 row:
      JSON byte length   4 bytes
      CRC32(JSON bytes)  4 bytes
      UTF-8 JSON         variable
  commit trailer: 8 bytes
    CMIT                 4 bytes
    CRC32(header+payload) 4 bytes
```

JSON은 레코드의 값을 읽기 쉽게 직렬화하기 위해 사용합니다. 길이를 별도로 기록하므로 문자열 안의 줄바꿈이나 쉼표는 레코드 경계를 깨뜨리지 않습니다. 각 레코드 오프셋은 `row header`의 시작 위치입니다.

INSERT는 다음 순서로 처리합니다.

1. 테이블 write lock을 얻습니다.
2. 문장의 모든 행을 검증하고 연속 ID를 부여합니다.
3. 프레임을 파일 끝에 append하고 `flush`, 기본적으로 `fsync`합니다.
4. 각 ID와 파일 오프셋을 B+ 트리에 넣고 다음 ID를 갱신합니다.
5. lock을 해제한 후 성공 응답을 반환합니다.

일반적인 쓰기 예외가 발생하면 시작 오프셋으로 truncate하고 인덱스를 재구성합니다. 복구 작업도 실패하면 해당 테이블을 사용 불가 상태로 만들어 추가 읽기/쓰기를 막습니다. 이때는 로그와 저장 장치를 확인하고 재시작해야 합니다.

재시작 시에는 프레임의 헤더·전체 체크섬·레코드 체크섬·스키마·연속 ID를 검증하면서 인덱스를 재구성합니다. 파일 끝의 불완전한 프레임은 전체를 제거합니다. 완성된 프레임의 체크섬 오류와 손상된 완전한 헤더는 자동 삭제하지 않고 오류로 보고합니다. 프레임 길이 자체도 별도 체크섬으로 보호하여 길이 필드 손상을 불완전한 쓰기로 오인하지 않도록 했습니다.

`CREATE TABLE`은 스키마 잠금 안에서 빈 테이블 파일을 먼저 저장한 뒤 `schema.json`을 원자적으로 교체합니다. 스키마가 교체되기 전에 중단되어 빈 테이블 파일이 남았다면 같은 테이블 만들기를 재시도할 수 있습니다.

**보장 경계:** 단일 INSERT의 행들은 함께 복구되거나 함께 제거됩니다. 완전한 프레임을 쓴 뒤 응답 전에 종료하면 클라이언트가 성공을 못 받았어도 저장은 완료됐을 수 있습니다. 요청 재시도의 exactly-once 보장이나 idempotency key는 없습니다. CRC32는 우발적 손상 검출용이며 악의적 변조를 막는 인증 수단은 아닙니다. fsync의 실제 전원 장애 내구성은 OS/파일시스템/장치의 보장에도 의존합니다.

테이블 파일이나 스키마가 사라진 기존 DB는 빈 DB로 조용히 재생성하지 않습니다. 백업은 서버를 종료한 뒤 DB 디렉터리 전체를 복사하는 방식으로 수행합니다. 새로운 DB 초기화 도중 실패한 경우에는 해당 초기화 파일을 확인하고 새 디렉터리에서 다시 초기화해야 합니다. 데이터가 있는 디렉터리의 파일을 임의로 지우면 안 됩니다.

## 6. 동시성과 API 서버

- 서버 하나가 DB 디렉터리를 소유합니다. `flock`으로 서로 다른 프로세스가 같은 DB를 동시에 수정하는 것을 막습니다.
- 테이블별 RWLock을 사용합니다. SELECT끼리는 읽기 잠금을 공유하고, INSERT는 배타적으로 실행합니다. 다른 테이블은 독립 잠금을 사용합니다.
- 대기 중인 writer가 있으면 새로운 reader의 진입을 막아 writer 기아를 줄입니다.
- 읽기 잠금은 SELECT의 스캔이 끝날 때까지 유지됩니다. 한 SELECT가 읽는 동안 같은 테이블의 상태가 바뀌지 않습니다. 긴 선형 탐색은 쓰기를 지연시킬 수 있습니다.
- ID 배정·파일 append·인덱스 갱신은 동일한 write lock 안에 있습니다. 파일과 인덱스 사이의 중간 상태가 SELECT에 노출되지 않습니다.
- 조회마다 독립된 파일 핸들을 사용합니다. 공유 파일 핸들의 seek 위치가 다른 요청에 의해 바뀌는 문제를 방지합니다.

HTTP accept 루프는 `ThreadPoolExecutor`에 작업을 제출합니다. executor의 내부 큐는 제한이 없으므로 **worker 수 + queue size만큼의 semaphore**로 제출 전에 수용량을 제한했습니다. 초과 요청에는 `503`과 `Retry-After: 1`을 반환합니다. OS listen backlog는 이 애플리케이션 대기열과 별개입니다.

HTTP 연결 하나당 요청 하나를 처리하고 닫습니다. 유휴 keep-alive가 worker를 계속 점유하지 않게 했습니다. 요청 본문 크기, socket I/O timeout, SQL/배치/결과 크기 제한을 적용했습니다. timeout은 전체 SQL 실행 제한이 아닙니다. SIGINT/SIGTERM 시 accept 루프를 종료하고 접수된 작업을 마친 뒤 DB를 닫습니다.

CPython의 GIL이 있는 기본 실행 환경에서는 CPU를 쓰는 Python 코드가 여러 코어에서 동시에 실행되지 않습니다. 스레드 풀은 요청 I/O와 작업 스케줄링의 동시성을 제공하며, 여러 SELECT가 읽기 잠금을 동시에 획득할 수 있습니다. 따라서 worker 수 증가가 CPU 중심 선형 탐색의 처리량을 선형적으로 높인다고 주장하지 않습니다.

## 7. 설명할 수 있어야 하는 질문

1. **왜 B+ 트리인가?** 모든 값이 리프에 있고 리프끼리 연결되어 점 조회와 정렬된 범위 조회를 함께 지원합니다. 내부 노드에는 값 대신 separator만 둡니다.
2. **인덱스를 썼는데 왜 디스크를 읽는가?** 메모리에는 ID와 위치만 있고 실제 레코드는 파일에 있습니다. 인덱스는 전체 파일을 읽지 않고 필요한 위치를 찾는 수단입니다.
3. **파일만 저장하고 인덱스를 저장하지 않은 이유는?** 과제의 메모리 기반 인덱스 모델에 맞추고, 파일을 진실의 원천으로 두어 복구 규칙을 단순화했습니다. 대신 시작 비용 O(N)이 발생합니다.
4. **왜 SELECT에도 lock이 필요한가?** 분할 중인 트리나 아직 쓰는 중인 파일을 읽으면 일관성이 깨질 수 있습니다. RWLock으로 같은 테이블의 쓰기와 분리합니다.
5. **왜 100만 번의 단일 INSERT 대신 배치인가?** 파싱·fsync 오버헤드를 여러 행에 나눠 대규모 테스트를 쉽게 재현하기 위해서입니다. 모든 행은 동일 SQL 처리 경로를 통과합니다.
6. **더 확장하려면?** 디스크 페이지 기반 트리, buffer pool, WAL/checkpoint, 다중 문장 트랜잭션, MVCC, 보조 인덱스, 쿼리 취소·공정한 스케줄링을 단계적으로 추가할 수 있습니다.

## 8. 현재 범위

이 구현은 과제와 포트폴리오를 위한 단일 노드 DBMS입니다. SQL 표준 전체, 다중 문장 트랜잭션, DELETE/UPDATE 및 트리 병합, 디스크 기반 인덱스, 백그라운드 체크포인트, 복제, TLS는 구현하지 않습니다. HTTP 서버는 Python 표준 라이브러리 기반이므로 인터넷 공개 서비스로 운영하려면 별도 TLS reverse proxy와 운영 구성이 필요합니다.
