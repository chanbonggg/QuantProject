# PostgreSQL 단일화 마이그레이션 계획

작성일: 2026-05-17

이 문서는 DuckDB 혼용 구조를 PostgreSQL only 구조로 전환하기 위한 실행 계획이다. 코드 수정 시 이 문서를 우선 참고한다.

---

## 목표

현재 구조:

- PostgreSQL: 저장/쓰기
- DuckDB in-memory: 분석 읽기
- `postgres_scanner`: PostgreSQL을 DuckDB에서 read-only attach

목표 구조:

- PostgreSQL: 저장/쓰기/읽기/분석
- 신규 코드는 PostgreSQL only 기준
- DuckDB 전용 API와 테스트 fixture는 점진 제거

전환 대상:

- `duckdb.connect(":memory:")`
- `postgres_scanner`
- DuckDB 전용 `.df()`
- DuckDB placeholder `?`
- `get_connection()` / `get_pg_connection()` 역할 혼동

삭제 금지:

- 기존 PostgreSQL 데이터
- Docker 컨테이너 `quant-pg`
- 백업 파일 `data/quant_us.dump`
- 전환 완료 전 DuckDB 관련 코드를 성급히 삭제하지 않는다.

---

## Phase 0. 기준 확정

### 작업

1. 운영 DB 기준을 로컬 PostgreSQL `127.0.0.1:5432/quant_us`로 고정한다.
2. 신규 코드에서 DuckDB 의존을 추가하지 않는다.
3. 모든 신규 SQL은 PostgreSQL placeholder `%s` 기준으로 작성한다.
4. 테스트는 운영 DB와 분리한다.

### 완료 기준

- `AGENTS.md`, `README.md`, `DATA_COLLECTION_GUIDE.md`가 PostgreSQL only 전환 방침을 설명한다.
- `docs/project_open_issues.md`에 P0 PostgreSQL 단일화 항목이 있다.

### 복잡도

낮음

---

## Phase 1. DB 연결 레이어 정리

### 대상 파일

- `quant_us/db/init.py`

### 작업

1. `get_pg_connection()`을 표준 PostgreSQL 연결 함수로 유지한다.
2. `get_connection()`은 전환 기간 동안 다음 중 하나로 정리한다.
   - PostgreSQL 연결을 반환하도록 변경
   - 또는 deprecated wrapper로 두고 내부에서 `get_pg_connection()`을 호출
3. `get_duckdb_connection()`은 바로 삭제하지 않고 deprecated 처리한다.
4. 기본 `PG_DSN`은 현재 로컬 PostgreSQL 기준으로 바꾸거나, 환경변수 필수로 전환한다.
5. `postgres_scanner` attach 코드는 전환 완료 후 제거한다.

### 리스크

- 기존 코드가 `conn.execute(...).df()`를 기대하면 깨진다.
- `get_connection()` 반환 타입 변경은 영향 범위가 크다.

### 완료 기준

- 신규 코드에서 `get_pg_connection()` 또는 PostgreSQL wrapper만 사용한다.
- `get_connection()` 사용처가 전환 대상 목록으로 추적된다.

### 복잡도

중간

---

## Phase 2. PostgreSQL 쿼리 헬퍼 추가

### 목적

DuckDB `.df()` 제거를 파일마다 반복 구현하지 않기 위해 PostgreSQL query helper를 만든다.

### 후보 위치

- `quant_us/db/init.py`
- 또는 신규 파일 `quant_us/db/query.py`

### 권장 함수

```python
def read_sql_df(query: str, params=None, conn=None) -> pd.DataFrame:
    ...

def execute_one(query: str, params=None, conn=None):
    ...

def execute_all(query: str, params=None, conn=None):
    ...

def execute_write(query: str, params=None, conn=None) -> None:
    ...
```

### 규칙

- placeholder는 `%s` 기준
- connection을 외부에서 받으면 닫지 않는다.
- helper가 직접 connection을 만들었을 때만 닫는다.
- 운영 DB에 테스트 데이터를 직접 쓰지 않는다.

### 우선 적용 대상

1. `quant_us/scripts/daily_run.py`
2. `quant_us/regime/features.py`
3. `quant_us/regime/model.py`
4. `quant_us/regime/shock_alarm.py`
5. `quant_us/portfolio/optimizer.py`
6. `quant_us/portfolio/state.py`

### 복잡도

중간

---

## Phase 3. 핵심 파이프라인 전환

### 순서

1. `quant_us/scripts/daily_run.py`
2. `quant_us/regime/features.py`
3. `quant_us/regime/model.py`
4. `quant_us/regime/shock_alarm.py`
5. `quant_us/portfolio/state.py`
6. `quant_us/portfolio/optimizer.py`
7. `quant_us/portfolio/weight_engine.py`

### 작업

1. `.df()` 호출을 helper 또는 `pandas.read_sql_query()`로 교체한다.
2. `?` placeholder를 `%s`로 바꾼다.
3. 동적 `IN (?, ?, ?)` 쿼리는 PostgreSQL 방식으로 안전하게 바꾼다.
4. `conn` 주입은 유지하되 PostgreSQL connection 기준으로 통일한다.
5. PostgreSQL 쓰기 실패 시 DuckDB best-effort 저장 같은 호환 로직은 제거 방향으로 정리한다.

### 리스크

- 동적 IN 쿼리는 단순 문자열 치환으로 처리하면 SQL 오류 또는 보안 문제가 생길 수 있다.
- pandas/psycopg2 경고가 생길 수 있으므로 SQLAlchemy 사용 여부를 검토한다.

### 완료 기준

- daily pipeline dry-run이 PostgreSQL 연결만으로 동작한다.
- 레짐 피처/라벨 저장과 조회가 PostgreSQL 기준으로 동작한다.
- 포트폴리오 state 저장/조회가 PostgreSQL 기준으로 동작한다.

### 복잡도

높음

---

## Phase 4. 전략/백테스트/대시보드 전환

### 순서

1. `quant_us/strategies/universe.py`
2. `quant_us/strategies/momentum.py`
3. `quant_us/strategies/value.py`
4. `quant_us/strategies/quality.py`
5. `quant_us/strategies/low_vol.py`
6. `quant_us/backtest/engine.py`
7. `quant_us/backtest/walk_forward.py`
8. `quant_us/monitor/dashboard.py`

### 작업

1. DuckDB query/result API를 PostgreSQL helper로 교체한다.
2. 테스트 fixture를 PostgreSQL 기준으로 수정한다.
3. 대량 분석 성능이 느려지는 쿼리는 PostgreSQL 인덱스 또는 SQL 최적화로 해결한다.
4. 필요 시 pandas 계산 전 필요한 컬럼만 읽도록 쿼리를 줄인다.

### 완료 기준

- 전략 신호 산출 테스트가 PostgreSQL 기준 fixture로 통과한다.
- 백테스트 테스트가 PostgreSQL 기준 fixture로 통과한다.
- Streamlit 대시보드가 PostgreSQL에서 데이터를 읽는다.

### 복잡도

높음

---

## Phase 5. 테스트 구조 변경

### 현재 문제

`tests/test_portfolio_state.py` 일부 테스트는 DuckDB fixture를 쓰다가 마지막 검증에서 실제 PostgreSQL `get_pg_connection()`을 직접 호출한다. 이 때문에 로컬 운영 DB 연결 상태에 따라 기본 테스트가 실패한다.

### 작업

1. 기본 단위 테스트는 운영 DB 직접 접근을 금지한다.
2. 실제 PostgreSQL이 필요한 테스트는 `pytest.mark.integration`으로 분리한다.
3. 가능하면 별도 테스트 DB `quant_us_test`를 사용한다.
4. 테스트 데이터는 트랜잭션 롤백 또는 스키마 재생성 fixture로 격리한다.
5. 기존 DuckDB fixture는 PostgreSQL fixture 또는 mock으로 전환한다.

### 권장 명령

기본 테스트:

```powershell
python -m pytest tests/ -q
```

통합 테스트:

```powershell
python -m pytest tests/ -q -m integration
```

### 완료 기준

- 기본 테스트가 운영 DB 없이 통과한다.
- 통합 테스트는 명시적으로 실행할 때만 실제 PostgreSQL에 접근한다.
- 현재 실패 중인 2개 테스트가 기본 테스트 실패를 만들지 않는다.

### 복잡도

중간~높음

---

## Phase 6. DuckDB 의존 제거

### 검색 대상

```powershell
rg "duckdb|DuckDB|postgres_scanner|\\.df\\(\\)|get_duckdb_connection|\\?" quant_us tests -S
```

### 작업

1. `duckdb` import 제거
2. DuckDB 전용 fixture 제거
3. `requirements.txt`에서 DuckDB 제거 여부 검토
4. 문서 상태를 “PostgreSQL 단일화 완료”로 갱신

### 주의

- `?` 문자는 SQL placeholder 외에도 다른 문자열에 등장할 수 있으므로 검색 결과를 수동 확인한다.
- DuckDB 제거는 모든 테스트가 PostgreSQL 기준으로 통과한 뒤 진행한다.

### 완료 기준

- 운영 코드에서 DuckDB import가 없다.
- 기본 테스트가 통과한다.
- `README.md`, `AGENTS.md`, `docs/project_open_issues.md`가 전환 완료 상태를 반영한다.

### 복잡도

중간

---

## Phase 7. 검증

### 1차: 정적 확인

```powershell
python -m py_compile quant_us/db/init.py
```

주요 수정 파일도 함께 py_compile한다.

### 2차: 테스트

```powershell
python -m pytest tests/ -q
```

### 3차: DB 읽기 검증

```powershell
python -c "import sys; sys.path.insert(0, 'quant_us'); from db.init import get_pg_connection; c=get_pg_connection(); cur=c.cursor(); cur.execute('SELECT COUNT(*) FROM raw.prices'); print(cur.fetchone()); cur.execute('SELECT COUNT(*) FROM raw.fred_series'); print(cur.fetchone()); cur.execute('SELECT COUNT(*) FROM raw.sec_financials'); print(cur.fetchone()); c.close()"
```

### 4차: daily dry-run

```powershell
python quant_us/scripts/daily_run.py --date 2026-04-02 --dry-run
```

### 5차: 대시보드 확인

```powershell
streamlit run quant_us/monitor/dashboard.py
```

---

## 권장 첫 작업 단위

처음에는 너무 넓게 건드리지 않는다. 다음 범위를 첫 PR/첫 작업 단위로 잡는다.

1. `quant_us/db/init.py` 연결 함수 정리
2. PostgreSQL query helper 추가
3. `tests/test_portfolio_state.py` 실패 2개를 기본 테스트에서 분리
4. `portfolio/state.py`를 PostgreSQL 기준으로 정리
5. 관련 테스트만 실행

이 작업이 끝나면 같은 패턴으로 `daily_run.py`와 레짐 모듈을 옮긴다.

---

## 주요 리스크

| 리스크 | 영향 | 대응 |
| ------ | ---- | ---- |
| `.df()` 제거 누락 | 런타임 오류 | `rg "\\.df\\(" quant_us tests -S`로 추적 |
| `?` placeholder 잔존 | SQL 오류 | `%s` 전환 후 테스트 |
| 동적 IN 쿼리 변환 실수 | SQL 오류 또는 보안 리스크 | helper 또는 psycopg2 안전 파라미터 사용 |
| 운영 DB에 테스트 쓰기 | 데이터 오염 | 테스트 DB 또는 트랜잭션 롤백 |
| 대량 분석 속도 저하 | 백테스트 지연 | 인덱스/쿼리 최적화, 필요한 컬럼만 로드 |
| 한 번에 너무 많이 수정 | 디버깅 어려움 | 모듈 단위 전환 |

---

## 진행 상태 체크리스트

- [ ] Phase 0 기준 확정
- [ ] Phase 1 DB 연결 레이어 정리
- [ ] Phase 2 PostgreSQL query helper 추가
- [ ] Phase 3 핵심 파이프라인 전환
- [ ] Phase 4 전략/백테스트/대시보드 전환
- [ ] Phase 5 테스트 구조 변경
- [ ] Phase 6 DuckDB 의존 제거
- [ ] Phase 7 검증 완료

