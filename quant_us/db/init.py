"""
DB 초기화 스크립트
저장(OLTP): PostgreSQL — 수집/쓰기 모듈용
분석(OLAP): DuckDB in-memory + postgres_scanner — 읽기/분석 모듈용
"""

import os
import sys
import duckdb
import psycopg2
import psycopg2.extensions
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.logger import logger

try:
    load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env", encoding="utf-8")
except Exception as e:
    logger.warning(f"[.env 로드] 인코딩 오류, 환경변수 기본값 사용: {e}")

DB_PATH = os.getenv("DB_PATH", "./data/quant_us.duckdb")
PG_DSN = os.getenv("PG_DSN") or "postgresql://postgres:quant@localhost:5433/quant_us"


def get_pg_connection() -> psycopg2.extensions.connection:
    """PostgreSQL 연결 반환 (쓰기 모듈용)."""
    return psycopg2.connect(PG_DSN)


def get_duckdb_connection() -> duckdb.DuckDBPyConnection:
    """DuckDB in-memory + postgres_scanner 연결 반환 (읽기/분석 모듈용).
    기존 쿼리(raw.prices, feature.regime_labels 등)를 그대로 사용 가능.
    """
    con = duckdb.connect(":memory:")
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{PG_DSN}' AS pg (TYPE POSTGRES, READ_ONLY);")
    # search_path 설정으로 기존 쿼리 무수정 유지
    con.execute("SET search_path = 'pg.raw,pg.feature,pg.normalized';")
    logger.debug("[DuckDB] in-memory + postgres_scanner 연결 완료")
    return con


def get_connection(db_path: str = None) -> duckdb.DuckDBPyConnection:
    """하위 호환 래퍼. 기존 읽기 모듈은 이 함수를 그대로 사용."""
    return get_duckdb_connection()


def init_db() -> None:
    """PostgreSQL 스키마 및 테이블 초기화."""
    logger.info("[DB 초기화] 시작: PostgreSQL")
    conn = get_pg_connection()
    cur = conn.cursor()

    # 스키마 생성
    cur.execute("CREATE SCHEMA IF NOT EXISTS raw")
    cur.execute("CREATE SCHEMA IF NOT EXISTS normalized")
    cur.execute("CREATE SCHEMA IF NOT EXISTS feature")
    logger.debug("[DB] 스키마 생성 완료 (raw, normalized, feature)")

    # ── raw.prices ─────────────────────────────────────────────────────
    cur.execute("""
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
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_prices_date ON raw.prices (date DESC)"
    )

    # ── raw.fred_series ────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS raw.fred_series (
            series_id    TEXT        NOT NULL,
            date         DATE        NOT NULL,
            value        DOUBLE PRECISION,
            collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (series_id, date)
        )
    """)

    # ── raw.sec_financials ─────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS raw.sec_financials (
            ticker               TEXT        NOT NULL,
            cik                  TEXT,
            filing_type          TEXT        NOT NULL,
            period_of_report     DATE,
            filed_date           DATE        NOT NULL,
            revenue              DOUBLE PRECISION,
            net_income           DOUBLE PRECISION,
            eps_diluted          DOUBLE PRECISION,
            total_assets         DOUBLE PRECISION,
            stockholders_equity  DOUBLE PRECISION,
            total_liabilities    DOUBLE PRECISION,
            operating_cashflow   DOUBLE PRECISION,
            cost_of_goods_sold   DOUBLE PRECISION,
            collected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (ticker, filed_date, filing_type)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_sec_ticker_filed
            ON raw.sec_financials (ticker, filed_date DESC)
    """)

    # ── raw.sp500_changes ──────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS raw.sp500_changes (
            date        DATE NOT NULL,
            ticker      TEXT NOT NULL,
            action      TEXT NOT NULL CHECK (action IN ('add', 'remove')),
            reason      TEXT,
            replacement TEXT
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_sp500_ticker_date
            ON raw.sp500_changes (ticker, date DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_sp500_action_date
            ON raw.sp500_changes (action, date DESC)
    """)

    # ── raw.ticker_events ──────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS raw.ticker_events (
            ticker      TEXT NOT NULL,
            event_date  DATE NOT NULL,
            event_type  TEXT NOT NULL CHECK (event_type IN ('delisted', 'merger', 'ticker_change')),
            details     TEXT
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_ticker_events_ticker
            ON raw.ticker_events (ticker, event_date DESC)
    """)

    # ── feature.regime_features ────────────────────────────────────────
    cur.execute("""
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
        )
    """)

    # ── feature.regime_labels ──────────────────────────────────────────
    # raw_regime 컬럼 포함 → model.py의 ALTER TABLE ADD COLUMN 불필요
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feature.regime_labels (
            date        DATE PRIMARY KEY,
            regime      TEXT NOT NULL CHECK (regime IN ('A', 'B', 'C')),
            shock_alarm BOOLEAN NOT NULL DEFAULT FALSE,
            raw_regime  TEXT,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_regime_labels_date_desc
            ON feature.regime_labels (date DESC)
    """)

    # ── feature.pipeline_log ───────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feature.pipeline_log (
            run_date    DATE    NOT NULL,
            step_name   TEXT    NOT NULL,
            status      TEXT    NOT NULL CHECK (status IN ('success', 'error', 'skipped')),
            detail      TEXT,
            logged_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (run_date, step_name)
        )
    """)

    # ── normalized.portfolio_state ─────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS normalized.portfolio_state (
            date                 DATE PRIMARY KEY,
            total_value          DOUBLE PRECISION NOT NULL,
            cash_amount          DOUBLE PRECISION,
            equity_value         DOUBLE PRECISION,
            target_portfolio     JSONB,
            current_drift        DOUBLE PRECISION,
            rebalance_triggered  BOOLEAN NOT NULL DEFAULT FALSE,
            rebalance_reason     TEXT CHECK (rebalance_reason IN ('drift', 'regime_shift', 'monthly', 'manual', 'skipped')),
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_portfolio_state_date_desc
            ON normalized.portfolio_state (date DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_portfolio_state_rebalance
            ON normalized.portfolio_state (rebalance_triggered, date DESC)
    """)

    conn.commit()
    cur.close()
    conn.close()
    logger.info("[DB 초기화] 완료: PostgreSQL 스키마 8개 테이블 생성")


if __name__ == "__main__":
    init_db()
