# PostgreSQL 단일화와 테스트 격리 명세

## 목표

기본 테스트가 운영 DB 없이 통과하고, 운영 코드는 PostgreSQL only 구조로 점진 전환된다.

## 현재 미구현 근거

- `pytest.ini` 없음
- `tests/conftest.py` 없음
- `quant_us/db/query.py` 없음
- `quant_us/db/init.py`에 `duckdb`, `postgres_scanner`, `get_duckdb_connection()` 남음
- `tests/test_portfolio_state.py`가 DuckDB fixture 사용 중 `get_pg_connection()`을 직접 호출
- 여러 운영 모듈에 DuckDB import와 `.df()` 호출 남음

## 대상 파일

- `pytest.ini`
- `tests/conftest.py`
- `tests/test_portfolio_state.py`
- `quant_us/db/init.py`
- 신규 `quant_us/db/query.py`
- `quant_us/portfolio/state.py`
- 후속 전환 대상:
  - `quant_us/scripts/daily_run.py`
  - `quant_us/regime/features.py`
  - `quant_us/regime/model.py`
  - `quant_us/regime/shock_alarm.py`
  - `quant_us/portfolio/optimizer.py`
  - `quant_us/portfolio/weight_engine.py`
  - `quant_us/strategies/*.py`
  - `quant_us/backtest/*.py`
  - `quant_us/monitor/dashboard.py`

## 구현 범위

### 1. 테스트 마커와 기본 정책

- `pytest.ini`에 `integration` 마커를 등록한다.
- 기본 테스트 명령은 운영 DB 접근 없이 통과해야 한다.
- 실제 PostgreSQL 접근 테스트는 `@pytest.mark.integration`으로 분리한다.
- `TEST_PG_DSN`이 없으면 integration 테스트는 skip한다.

```ini
[pytest]
markers =
    integration: requires a real PostgreSQL database connection
```

### 2. `test_portfolio_state.py` 정리

- `get_pg_connection()` 직접 호출 검증은 integration 테스트로 이동한다.
- DuckDB fixture와 실제 PostgreSQL 검증을 한 테스트에서 섞지 않는다.
- 기본 테스트는 mock, fake repository, 또는 주입 fixture만 검증한다.
- 운영 테이블 `normalized.portfolio_state`에 기본 테스트 데이터를 쓰지 않는다.

### 3. PostgreSQL query helper 추가

신규 파일 후보: `quant_us/db/query.py`

필수 함수:

```python
def read_sql_df(query: str, params=None, conn=None):
    ...

def execute_one(query: str, params=None, conn=None):
    ...

def execute_all(query: str, params=None, conn=None):
    ...

def execute_write(query: str, params=None, conn=None) -> None:
    ...
```

규칙:

- SQL placeholder는 `%s`만 사용한다.
- helper가 직접 connection을 열었을 때만 닫는다.
- helper가 직접 connection을 열었을 때만 write commit을 수행한다.
- 외부 connection이 전달되면 commit/rollback은 호출자가 책임진다.
- 동적 `IN` 조건은 `= ANY(%s)`와 list 파라미터를 우선 사용한다.

### 4. `portfolio/state.py` 우선 전환

- DuckDB import를 제거한다.
- 저장/조회 SQL을 PostgreSQL helper 기준으로 바꾼다.
- JSON/JSONB 저장과 조회 타입 처리를 명확히 유지한다.
- PostgreSQL 저장 실패 후 DuckDB에 best-effort 저장하는 호환 로직은 제거한다.
- 실제 PostgreSQL 저장/조회 검증은 integration 테스트로만 수행한다.

### 5. 후속 모듈 전환

- `.df()` 호출을 `read_sql_df()` 또는 cursor fetch 변환으로 바꾼다.
- `?` placeholder를 `%s`로 바꾼다.
- `get_connection()` 반환 타입은 모든 주요 호출부 전환 전까지 성급히 바꾸지 않는다.
- `quant_us/scripts/migrate_duckdb_to_pg.py`는 데이터 이전 보관 스크립트로 예외 판단한다.

## 완료 기준

- `python -m pytest tests/ -q -m "not integration"`가 운영 DB 없이 통과한다.
- `pytest` 마커 경고가 없다.
- `portfolio/state.py`에 DuckDB import가 없다.
- 신규 query helper 테스트가 있다.
- 운영 코드 PostgreSQL 전환 진행 상황이 `docs/project_open_issues.md`에 반영된다.

## 검증 명령

```powershell
python -m pytest tests/ -q -m "not integration"
python -m pytest tests/test_portfolio_state.py -q -m "not integration"
python -m pytest tests/test_portfolio_state.py -q -m integration
rg "duckdb|DuckDB|postgres_scanner|\\.df\\(\\)|get_duckdb_connection" quant_us tests -S
```

