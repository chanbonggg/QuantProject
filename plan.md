# DB 마이그레이션 계획: DuckDB → PostgreSQL(OLTP) + DuckDB(OLAP)

## 목표
- 저장/쓰기: PostgreSQL (잠금 문제 해결)
- 분석/읽기: DuckDB in-memory + postgres_scanner
- 파일 구조 유지, DB 레이어만 교체

## 확정 결정사항
- Q1: Docker PostgreSQL 15
- Q2: 각 워커 독립 연결 (단순 방식)
- Q3: SET search_path 방식 (기존 쿼리 무수정)

---

## PHASE 0: 사전 준비

### Docker PostgreSQL 기동
```bash
docker run --name quant-pg \
  -e POSTGRES_PASSWORD=quant \
  -e POSTGRES_DB=quant_us \
  -p 5432:5432 \
  -d postgres:15
```

### requirements.txt 추가
```
psycopg2-binary==2.9.9
```

### .env 추가
```
PG_DSN=postgresql://postgres:quant@localhost:5432/quant_us
```

---

## PHASE 1: db/init.py 교체

### 신규 함수
- `get_pg_connection()` — psycopg2 연결 (쓰기용)
- `get_duckdb_connection()` — DuckDB in-memory + postgres_scanner attach (읽기용)
- `get_connection()` — 하위 호환 래퍼 → get_duckdb_connection() 반환
- `init_db()` — PostgreSQL 스키마 초기화

### SQL 호환성 변경
| DuckDB | PostgreSQL |
|--------|-----------|
| `VARCHAR` | `TEXT` |
| `TIMESTAMP` | `TIMESTAMPTZ` |
| `DOUBLE` | `DOUBLE PRECISION` |
| `?` 플레이스홀더 | `%s` |
| `INTERVAL 30 DAY` | `INTERVAL '30 days'` |

---

## PHASE 1 스키마 (postgresql-table-design 스킬 적용)

```sql
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS feature;
CREATE SCHEMA IF NOT EXISTS normalized;

-- raw.prices (777K행)
CREATE TABLE IF NOT EXISTS raw.prices (
    ticker       TEXT        NOT NULL,
    date         DATE        NOT NULL,
    open         DOUBLE PRECISION,
    high         DOUBLE PRECISION,
    low          DOUBLE PRECISION,
    close        DOUBLE PRECISION,
    adj_close    DOUBLE PRECISION,
    volume       BIGINT,
    market_cap   DOUBLE PRECISION,
    source       TEXT,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON raw.prices (date DESC);

-- raw.fred_series (16K행)
CREATE TABLE IF NOT EXISTS raw.fred_series (
    series_id    TEXT        NOT NULL,
    date         DATE        NOT NULL,
    value        DOUBLE PRECISION,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (series_id, date)
);

-- raw.sec_financials (39K행)
CREATE TABLE IF NOT EXISTS raw.sec_financials (
    ticker               TEXT,
    cik                  TEXT,
    filing_type          TEXT,
    period_of_report     DATE,
    filed_date           DATE,
    revenue              DOUBLE PRECISION,
    net_income           DOUBLE PRECISION,
    eps_diluted          DOUBLE PRECISION,
    total_assets         DOUBLE PRECISION,
    stockholders_equity  DOUBLE PRECISION,
    total_liabilities    DOUBLE PRECISION,
    operating_cashflow   DOUBLE PRECISION,
    cost_of_goods_sold   DOUBLE PRECISION,
    collected_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sec_ticker_filed
    ON raw.sec_financials (ticker, filed_date DESC);

-- raw.sp500_changes
CREATE TABLE IF NOT EXISTS raw.sp500_changes (
    date        DATE NOT NULL,
    ticker      TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('add', 'remove')),
    reason      TEXT,
    replacement TEXT
);
CREATE INDEX IF NOT EXISTS idx_sp500_ticker_date ON raw.sp500_changes (ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_sp500_action_date ON raw.sp500_changes (action, date DESC);

-- raw.ticker_events
CREATE TABLE IF NOT EXISTS raw.ticker_events (
    ticker      TEXT NOT NULL,
    event_date  DATE NOT NULL,
    event_type  TEXT NOT NULL CHECK (event_type IN ('delisted', 'merger', 'ticker_change')),
    details     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticker_events_ticker ON raw.ticker_events (ticker, event_date DESC);

-- feature.regime_features
CREATE TABLE IF NOT EXISTS feature.regime_features (
    date         DATE PRIMARY KEY,
    vix          DOUBLE PRECISION,
    vix3m        DOUBLE PRECISION,
    vxmt         DOUBLE PRECISION,
    vix_term     DOUBLE PRECISION,
    rv20         DOUBLE PRECISION,
    rv60         DOUBLE PRECISION,
    ma200_gap    DOUBLE PRECISION,
    r12m         DOUBLE PRECISION,
    r1m          DOUBLE PRECISION,
    avg_corr20   DOUBLE PRECISION,
    hy_spread    DOUBLE PRECISION,
    ig_spread    DOUBLE PRECISION,
    term_spread  DOUBLE PRECISION,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- feature.regime_labels (raw_regime 컬럼 포함 → ALTER TABLE 불필요)
CREATE TABLE IF NOT EXISTS feature.regime_labels (
    date        DATE PRIMARY KEY,
    regime      TEXT NOT NULL CHECK (regime IN ('A', 'B', 'C')),
    shock_alarm BOOLEAN NOT NULL DEFAULT FALSE,
    raw_regime  TEXT,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_regime_labels_date_desc ON feature.regime_labels (date DESC);

-- feature.pipeline_log
CREATE TABLE IF NOT EXISTS feature.pipeline_log (
    run_date    DATE    NOT NULL,
    step_name   TEXT    NOT NULL,
    status      TEXT    NOT NULL CHECK (status IN ('success', 'error', 'skipped')),
    detail      TEXT,
    logged_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_date, step_name)
);
```

---

## PHASE 2: 쓰기 모듈 6개 PostgreSQL 전환

### 공통 변경 패턴
- `import duckdb` → `import psycopg2`
- `conn.execute(sql, params)` → `cur = conn.cursor(); cur.execute(sql, params)`
- `?` → `%s` 플레이스홀더
- `CAST(? AS DATE)` → `%s::date`
- `INTERVAL 30 DAY` → `INTERVAL '30 days'`
- 쓰기 후 `conn.commit()` 명시
- 연결 타입힌트: `psycopg2.extensions.connection`

### 파일별 핵심 변경

#### price_collector.py
- `conn.register("_bulk_df", df)` → `psycopg2.extras.execute_values()`
- ThreadPoolExecutor: 각 워커에서 `get_pg_connection()` 독립 생성/해제

```python
# 변경 전
conn.register("_bulk_df", df)
conn.execute("INSERT ... FROM _bulk_df ON CONFLICT DO NOTHING")

# 변경 후
from psycopg2.extras import execute_values
rows = list(df[cols].itertuples(index=False, name=None))
with conn.cursor() as cur:
    execute_values(cur,
        "INSERT INTO raw.prices (ticker,date,...) VALUES %s ON CONFLICT DO NOTHING",
        rows, page_size=500)
conn.commit()
```

#### fred_collector.py
- `executemany()` → `psycopg2.extras.execute_values()`
- `ON CONFLICT ... DO UPDATE SET value = EXCLUDED.value` (PG 호환)

#### sec_collector.py, features.py, model.py, daily_run.py
- `?` → `%s`, cursor 패턴, `conn.commit()`
- model.py: `ALTER TABLE ADD COLUMN raw_regime` 블록 제거 (스키마에 포함됨)
- daily_run.py: `CREATE TABLE pipeline_log` 블록 제거 (init_db로 이동)

---

## PHASE 3: 읽기 모듈 DuckDB(postgres_scanner) 전환

### get_duckdb_connection() 패턴
```python
def get_duckdb_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("INSTALL postgres; LOAD postgres;")
    pg_dsn = os.getenv("PG_DSN")
    con.execute(f"ATTACH '{pg_dsn}' AS pg (TYPE POSTGRES, READ_ONLY);")
    # search_path로 기존 쿼리 무수정 유지
    con.execute("SET search_path = 'pg.raw,pg.feature,pg.normalized';")
    return con
```

### 영향 파일 (기존 쿼리 변경 없음)
- strategies/ 5개, backtest/ 2개, portfolio/ 2개
- monitor/dashboard.py, regime/shock_alarm.py, regime/features.py (읽기 부분)

---

## PHASE 4: 테스트 전략

### 쓰기 테스트
- `tests/conftest.py` 신규 작성
- `pg_conn` fixture: 실제 로컬 PG 연결
- 트랜잭션 롤백으로 테스트 격리

### 읽기 테스트
- 기존 `duckdb.connect(":memory:")` 유지 (변경 없음)

---

## PHASE 5: 데이터 마이그레이션

```bash
# 1. 백업
cp ./data/quant_us.duckdb ./data/quant_us_backup_20260327.duckdb

# 2. PG 스키마 초기화
python -c "from quant_us.db.init import init_db; init_db()"

# 3. 마이그레이션 스크립트 실행
python scripts/migrate_duckdb_to_pg.py
```

### 검증 기준
- [ ] `raw.prices` COUNT 일치
- [ ] `daily_run.py --dry-run` 10단계 통과
- [ ] pytest 221개 전원 통과
- [ ] Streamlit 대시보드 정상 렌더링

---

## 리스크

| 항목 | 수준 | 완화 |
|------|------|------|
| ThreadPoolExecutor + psycopg2 | 높음 | 워커 독립 연결 |
| postgres_scanner search_path | 중간 | PHASE 3 후 전체 쿼리 테스트 |
| commit 누락 | 중간 | 각 쓰기 함수 종료 시 강제 |
| 데이터 마이그레이션 손실 | 높음 | 사전 백업 필수 |
