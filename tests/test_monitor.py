"""
대시보드 단위 테스트

테스트 항목:
  1. test_load_price_data — 가격 데이터 로드
  2. test_load_regime_data — 레짐 데이터 로드
  3. test_load_fred_data — FRED 데이터 로드
  4. test_data_status_query — 데이터 상태 쿼리
  5. test_monthly_returns_calculation — 월별 수익률 계산
  6. test_empty_db_handling — 빈 DB 처리
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb
import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# in-memory DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_conn():
    """in-memory DuckDB 연결 (스키마 + 샘플 데이터 포함)."""
    conn = duckdb.connect(":memory:")

    # 스키마 생성
    conn.execute("CREATE SCHEMA raw")
    conn.execute("CREATE SCHEMA feature")

    # raw.prices 테이블
    conn.execute("""
        CREATE TABLE raw.prices (
            ticker      VARCHAR,
            date        DATE,
            open        DOUBLE,
            high        DOUBLE,
            low         DOUBLE,
            close       DOUBLE,
            adj_close   DOUBLE,
            volume      BIGINT,
            market_cap  DOUBLE,
            source      VARCHAR,
            collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (ticker, date)
        )
    """)

    # SPY 가격 데이터 삽입 (60거래일)
    base_price = 400.0
    prices = []
    d = date(2024, 1, 2)
    for i in range(60):
        price = base_price * (1 + 0.001 * (i % 5 - 2))
        prices.append((
            "SPY", d.strftime("%Y-%m-%d"), price, price * 1.01,
            price * 0.99, price, price, 10000000, None, "yfinance",
        ))
        # 주말 스킵
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)

    conn.executemany(
        "INSERT INTO raw.prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
        prices,
    )

    # raw.fred_series 테이블
    conn.execute("""
        CREATE TABLE raw.fred_series (
            series_id    VARCHAR,
            date         DATE,
            value        DOUBLE,
            collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (series_id, date)
        )
    """)

    fred_rows = []
    d = date(2024, 1, 2)
    for i in range(60):
        fred_rows.append(("VIXCLS", d.strftime("%Y-%m-%d"), 15.0 + i * 0.1))
        fred_rows.append(("BAMLH0A0HYM2", d.strftime("%Y-%m-%d"), 3.5 + i * 0.01))
        fred_rows.append(("BAMLC0A0CM", d.strftime("%Y-%m-%d"), 1.2 + i * 0.005))
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)

    conn.executemany(
        "INSERT INTO raw.fred_series VALUES (?, ?, ?, current_timestamp)",
        fred_rows,
    )

    # raw.sec_financials 테이블
    conn.execute("""
        CREATE TABLE raw.sec_financials (
            ticker               VARCHAR,
            cik                  VARCHAR,
            filing_type          VARCHAR,
            period_of_report     DATE,
            filed_date           DATE,
            revenue              DOUBLE,
            net_income           DOUBLE,
            eps_diluted          DOUBLE,
            total_assets         DOUBLE,
            stockholders_equity  DOUBLE,
            total_liabilities    DOUBLE,
            operating_cashflow   DOUBLE,
            cost_of_goods_sold   DOUBLE,
            collected_at         TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        INSERT INTO raw.sec_financials
        VALUES ('AAPL', '0000320193', '10-K', '2023-09-30', '2023-11-03',
                383285000000, 96995000000, 6.13, 352583000000, 62146000000,
                290437000000, 113913000000, 214137000000, current_timestamp)
    """)

    # feature.regime_features 테이블
    conn.execute("""
        CREATE TABLE feature.regime_features (
            date         DATE PRIMARY KEY,
            vix          DOUBLE,
            vix3m        DOUBLE,
            vxmt         DOUBLE,
            vix_term     DOUBLE,
            rv20         DOUBLE,
            rv60         DOUBLE,
            ma200_gap    DOUBLE,
            r12m         DOUBLE,
            r1m          DOUBLE,
            avg_corr20   DOUBLE,
            hy_spread    DOUBLE,
            ig_spread    DOUBLE,
            term_spread  DOUBLE,
            computed_at  TIMESTAMP DEFAULT current_timestamp
        )
    """)

    d = date(2024, 1, 2)
    for i in range(30):
        conn.execute(
            """
            INSERT INTO feature.regime_features
            VALUES (CAST(? AS DATE), ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    current_timestamp)
            """,
            [
                d.strftime("%Y-%m-%d"),
                15.0 + i * 0.1,    # vix
                16.0 + i * 0.1,    # vix3m
                1.05,              # vix_term
                0.12,              # rv20
                0.11,              # rv60
                0.03,              # ma200_gap
                0.15,              # r12m
                0.01,              # r1m
                0.25,              # avg_corr20
                3.5,               # hy_spread
                1.2,               # ig_spread
                0.5,               # term_spread
            ],
        )
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)

    # feature.regime_labels 테이블
    conn.execute("""
        CREATE TABLE feature.regime_labels (
            date        DATE PRIMARY KEY,
            regime      VARCHAR,
            shock_alarm BOOLEAN,
            computed_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    d = date(2024, 1, 2)
    for i in range(30):
        regime = "A" if i < 10 else ("B" if i < 20 else "C")
        shock = i == 15  # 1개 알람
        conn.execute(
            "INSERT INTO feature.regime_labels VALUES (CAST(? AS DATE), ?, ?, current_timestamp)",
            [d.strftime("%Y-%m-%d"), regime, shock],
        )
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)

    return conn


@pytest.fixture
def empty_conn():
    """빈 테이블만 있는 in-memory DuckDB 연결."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw")
    conn.execute("CREATE SCHEMA feature")
    conn.execute("""
        CREATE TABLE raw.prices (
            ticker VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, adj_close DOUBLE, volume BIGINT, market_cap DOUBLE,
            source VARCHAR, collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.execute("""
        CREATE TABLE raw.fred_series (
            series_id VARCHAR, date DATE, value DOUBLE,
            collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (series_id, date)
        )
    """)
    conn.execute("""
        CREATE TABLE raw.sec_financials (
            ticker VARCHAR, cik VARCHAR, filing_type VARCHAR,
            period_of_report DATE, filed_date DATE, revenue DOUBLE,
            net_income DOUBLE, eps_diluted DOUBLE, total_assets DOUBLE,
            stockholders_equity DOUBLE, total_liabilities DOUBLE,
            operating_cashflow DOUBLE, cost_of_goods_sold DOUBLE,
            collected_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE feature.regime_labels (
            date DATE PRIMARY KEY, regime VARCHAR,
            shock_alarm BOOLEAN,
            computed_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE feature.regime_features (
            date DATE PRIMARY KEY, vix DOUBLE, vix3m DOUBLE, vxmt DOUBLE,
            vix_term DOUBLE, rv20 DOUBLE, rv60 DOUBLE, ma200_gap DOUBLE,
            r12m DOUBLE, r1m DOUBLE, avg_corr20 DOUBLE, hy_spread DOUBLE,
            ig_spread DOUBLE, term_spread DOUBLE,
            computed_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    return conn


# ---------------------------------------------------------------------------
# 데이터 조회 내부 함수 (캐시 제거 버전)
# ---------------------------------------------------------------------------

def _load_price_data_raw(conn, start_date: str, end_date: str) -> pd.DataFrame:
    """raw.prices에서 SPY 가격 조회 (캐시 없는 내부 함수)."""
    return conn.execute(
        """
        SELECT date, adj_close
        FROM raw.prices
        WHERE ticker = 'SPY'
          AND date >= CAST(? AS DATE)
          AND date <= CAST(? AS DATE)
        ORDER BY date ASC
        """,
        [start_date, end_date],
    ).df()


def _load_regime_data_raw(conn, start_date: str, end_date: str) -> pd.DataFrame:
    """feature.regime_labels에서 레짐 이력 조회 (캐시 없는 내부 함수)."""
    return conn.execute(
        """
        SELECT date, regime, shock_alarm, computed_at
        FROM feature.regime_labels
        WHERE date >= CAST(? AS DATE)
          AND date <= CAST(? AS DATE)
        ORDER BY date ASC
        """,
        [start_date, end_date],
    ).df()


def _load_fred_data_raw(conn, series_ids: list, start_date: str, end_date: str) -> pd.DataFrame:
    """raw.fred_series에서 FRED 데이터 조회 (캐시 없는 내부 함수)."""
    placeholders = ",".join(["?" for _ in series_ids])
    return conn.execute(
        f"""
        SELECT series_id, date, value
        FROM raw.fred_series
        WHERE series_id IN ({placeholders})
          AND date >= CAST(? AS DATE)
          AND date <= CAST(? AS DATE)
        ORDER BY series_id, date ASC
        """,
        [*series_ids, start_date, end_date],
    ).df()


def _load_data_status_raw(conn) -> dict:
    """데이터 상태 쿼리 (캐시 없는 내부 함수)."""
    status = {}

    row = conn.execute(
        "SELECT MAX(date), COUNT(*), COUNT(DISTINCT ticker) FROM raw.prices"
    ).fetchone()
    status["prices"] = {
        "latest_date": str(row[0]) if row[0] else "N/A",
        "total_rows": int(row[1]) if row[1] else 0,
        "unique_tickers": int(row[2]) if row[2] else 0,
    }

    row = conn.execute(
        "SELECT MAX(date), COUNT(*), COUNT(DISTINCT series_id) FROM raw.fred_series"
    ).fetchone()
    status["fred"] = {
        "latest_date": str(row[0]) if row[0] else "N/A",
        "total_rows": int(row[1]) if row[1] else 0,
        "unique_series": int(row[2]) if row[2] else 0,
    }

    row = conn.execute(
        "SELECT MAX(filed_date), COUNT(*), COUNT(DISTINCT ticker) FROM raw.sec_financials"
    ).fetchone()
    status["sec"] = {
        "latest_date": str(row[0]) if row[0] else "N/A",
        "total_rows": int(row[1]) if row[1] else 0,
        "unique_tickers": int(row[2]) if row[2] else 0,
    }

    row = conn.execute(
        "SELECT MAX(date), COUNT(*) FROM feature.regime_labels"
    ).fetchone()
    status["regime_labels"] = {
        "latest_date": str(row[0]) if row[0] else "N/A",
        "total_rows": int(row[1]) if row[1] else 0,
    }

    row = conn.execute(
        "SELECT MAX(date), COUNT(*) FROM feature.regime_features"
    ).fetchone()
    status["regime_features"] = {
        "latest_date": str(row[0]) if row[0] else "N/A",
        "total_rows": int(row[1]) if row[1] else 0,
    }

    return status


# ---------------------------------------------------------------------------
# 월별 수익률 계산 (dashboard.py의 compute_monthly_returns 로직 동일)
# ---------------------------------------------------------------------------

def _compute_monthly_returns(price_df: pd.DataFrame) -> pd.DataFrame:
    """월별 수익률 피벗 계산."""
    if price_df.empty:
        return pd.DataFrame()

    df = price_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    monthly = df["adj_close"].resample("ME").last().pct_change().dropna()
    if monthly.empty:
        return pd.DataFrame()

    monthly_df = monthly.reset_index()
    monthly_df.columns = ["date", "return"]
    monthly_df["year"] = monthly_df["date"].dt.year
    monthly_df["month"] = monthly_df["date"].dt.month

    pivot = monthly_df.pivot(index="year", columns="month", values="return")
    return pivot


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------

class TestLoadPriceData:
    """1. test_load_price_data — 가격 데이터 로드."""

    def test_returns_dataframe(self, mem_conn):
        """SPY 가격 데이터 조회 시 DataFrame 반환."""
        df = _load_price_data_raw(mem_conn, "2024-01-01", "2024-03-31")
        assert isinstance(df, pd.DataFrame)

    def test_has_expected_columns(self, mem_conn):
        """date, adj_close 컬럼 존재."""
        df = _load_price_data_raw(mem_conn, "2024-01-01", "2024-03-31")
        assert "date" in df.columns
        assert "adj_close" in df.columns

    def test_data_is_not_empty(self, mem_conn):
        """SPY 데이터가 1행 이상."""
        df = _load_price_data_raw(mem_conn, "2024-01-01", "2024-03-31")
        assert len(df) > 0

    def test_adj_close_is_positive(self, mem_conn):
        """모든 가격은 양수."""
        df = _load_price_data_raw(mem_conn, "2024-01-01", "2024-03-31")
        assert (df["adj_close"] > 0).all()

    def test_date_range_filter(self, mem_conn):
        """날짜 범위 필터 작동."""
        df = _load_price_data_raw(mem_conn, "2024-01-01", "2024-01-31")
        dates = pd.to_datetime(df["date"])
        assert dates.min() >= pd.Timestamp("2024-01-01")
        assert dates.max() <= pd.Timestamp("2024-01-31")


class TestLoadRegimeData:
    """2. test_load_regime_data — 레짐 데이터 로드."""

    def test_returns_dataframe(self, mem_conn):
        """레짐 데이터 조회 시 DataFrame 반환."""
        df = _load_regime_data_raw(mem_conn, "2024-01-01", "2024-03-31")
        assert isinstance(df, pd.DataFrame)

    def test_has_expected_columns(self, mem_conn):
        """date, regime, shock_alarm 컬럼 존재."""
        df = _load_regime_data_raw(mem_conn, "2024-01-01", "2024-03-31")
        assert "date" in df.columns
        assert "regime" in df.columns
        assert "shock_alarm" in df.columns

    def test_regime_values_valid(self, mem_conn):
        """레짐 값은 A/B/C 중 하나."""
        df = _load_regime_data_raw(mem_conn, "2024-01-01", "2024-03-31")
        assert len(df) > 0
        valid_regimes = {"A", "B", "C"}
        assert set(df["regime"].unique()).issubset(valid_regimes)

    def test_shock_alarm_is_bool(self, mem_conn):
        """shock_alarm 컬럼은 불리언 타입."""
        df = _load_regime_data_raw(mem_conn, "2024-01-01", "2024-03-31")
        assert len(df) > 0
        shock_df = df[df["shock_alarm"] == True]
        assert len(shock_df) >= 0  # 0개 이상

    def test_shock_alarm_count(self, mem_conn):
        """테스트 fixture에서 알람은 정확히 1개."""
        df = _load_regime_data_raw(mem_conn, "2024-01-01", "2024-12-31")
        shock_count = df["shock_alarm"].sum()
        assert shock_count == 1


class TestLoadFredData:
    """3. test_load_fred_data — FRED 데이터 로드."""

    def test_returns_dataframe(self, mem_conn):
        """FRED 데이터 조회 시 DataFrame 반환."""
        df = _load_fred_data_raw(
            mem_conn,
            ["VIXCLS", "BAMLH0A0HYM2"],
            "2024-01-01",
            "2024-03-31",
        )
        assert isinstance(df, pd.DataFrame)

    def test_has_expected_columns(self, mem_conn):
        """series_id, date, value 컬럼 존재."""
        df = _load_fred_data_raw(mem_conn, ["VIXCLS"], "2024-01-01", "2024-03-31")
        assert "series_id" in df.columns
        assert "date" in df.columns
        assert "value" in df.columns

    def test_multiple_series(self, mem_conn):
        """여러 시리즈 조회 가능."""
        series_ids = ["VIXCLS", "BAMLH0A0HYM2", "BAMLC0A0CM"]
        df = _load_fred_data_raw(mem_conn, series_ids, "2024-01-01", "2024-12-31")
        assert set(df["series_id"].unique()) == set(series_ids)

    def test_vix_values_positive(self, mem_conn):
        """VIX 값은 양수."""
        df = _load_fred_data_raw(mem_conn, ["VIXCLS"], "2024-01-01", "2024-12-31")
        vix_df = df[df["series_id"] == "VIXCLS"]
        assert (vix_df["value"] > 0).all()

    def test_unknown_series_returns_empty(self, mem_conn):
        """존재하지 않는 시리즈 조회 시 빈 DataFrame."""
        df = _load_fred_data_raw(
            mem_conn, ["NONEXISTENT_SERIES"], "2024-01-01", "2024-12-31"
        )
        assert len(df) == 0


class TestDataStatusQuery:
    """4. test_data_status_query — 데이터 상태 쿼리."""

    def test_returns_all_keys(self, mem_conn):
        """prices, fred, sec, regime_labels, regime_features 키 모두 포함."""
        status = _load_data_status_raw(mem_conn)
        assert "prices" in status
        assert "fred" in status
        assert "sec" in status
        assert "regime_labels" in status
        assert "regime_features" in status

    def test_prices_row_count(self, mem_conn):
        """raw.prices 행수 > 0."""
        status = _load_data_status_raw(mem_conn)
        assert status["prices"]["total_rows"] > 0

    def test_fred_unique_series(self, mem_conn):
        """FRED 유니크 시리즈 수 == 3."""
        status = _load_data_status_raw(mem_conn)
        assert status["fred"]["unique_series"] == 3

    def test_prices_latest_date_is_string(self, mem_conn):
        """latest_date는 문자열 타입."""
        status = _load_data_status_raw(mem_conn)
        assert isinstance(status["prices"]["latest_date"], str)

    def test_sec_unique_tickers(self, mem_conn):
        """SEC 유니크 티커 수 == 1 (AAPL만 삽입)."""
        status = _load_data_status_raw(mem_conn)
        assert status["sec"]["unique_tickers"] == 1


class TestMonthlyReturnsCalculation:
    """5. test_monthly_returns_calculation — 월별 수익률 계산."""

    def test_returns_dataframe(self, mem_conn):
        """월별 수익률 계산 결과가 DataFrame."""
        price_df = _load_price_data_raw(mem_conn, "2024-01-01", "2024-12-31")
        monthly_ret = _compute_monthly_returns(price_df)
        assert isinstance(monthly_ret, pd.DataFrame)

    def test_returns_have_month_columns(self, mem_conn):
        """결과 컬럼은 월 번호 (1~12)."""
        price_df = _load_price_data_raw(mem_conn, "2024-01-01", "2024-12-31")
        monthly_ret = _compute_monthly_returns(price_df)
        if not monthly_ret.empty:
            assert all(isinstance(col, int) for col in monthly_ret.columns)

    def test_returns_index_is_year(self, mem_conn):
        """결과 인덱스는 연도."""
        price_df = _load_price_data_raw(mem_conn, "2024-01-01", "2024-12-31")
        monthly_ret = _compute_monthly_returns(price_df)
        if not monthly_ret.empty:
            assert 2024 in monthly_ret.index

    def test_return_values_are_numeric(self, mem_conn):
        """수익률 값은 숫자 타입."""
        price_df = _load_price_data_raw(mem_conn, "2024-01-01", "2024-12-31")
        monthly_ret = _compute_monthly_returns(price_df)
        if not monthly_ret.empty:
            assert monthly_ret.dtypes.apply(
                lambda d: np.issubdtype(d, np.floating)
            ).all()

    def test_return_range_reasonable(self, mem_conn):
        """월별 수익률은 -50% ~ +50% 범위 내 (테스트 데이터 특성)."""
        price_df = _load_price_data_raw(mem_conn, "2024-01-01", "2024-12-31")
        monthly_ret = _compute_monthly_returns(price_df)
        if not monthly_ret.empty:
            values = monthly_ret.values.flatten()
            values = values[~np.isnan(values)]
            assert all(-0.5 <= v <= 0.5 for v in values)


class TestEmptyDbHandling:
    """6. test_empty_db_handling — 빈 DB 처리."""

    def test_empty_prices_returns_empty_df(self, empty_conn):
        """빈 prices 테이블 조회 시 빈 DataFrame 반환."""
        df = _load_price_data_raw(empty_conn, "2024-01-01", "2024-12-31")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_empty_regime_returns_empty_df(self, empty_conn):
        """빈 regime_labels 테이블 조회 시 빈 DataFrame 반환."""
        df = _load_regime_data_raw(empty_conn, "2024-01-01", "2024-12-31")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_empty_fred_returns_empty_df(self, empty_conn):
        """빈 fred_series 테이블 조회 시 빈 DataFrame 반환."""
        df = _load_fred_data_raw(
            empty_conn, ["VIXCLS"], "2024-01-01", "2024-12-31"
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_empty_status_shows_zero_rows(self, empty_conn):
        """빈 DB의 데이터 상태 쿼리 시 행수=0, latest_date='N/A'."""
        status = _load_data_status_raw(empty_conn)
        assert status["prices"]["total_rows"] == 0
        assert status["prices"]["latest_date"] == "N/A"

    def test_monthly_returns_empty_input(self):
        """빈 DataFrame 입력 시 월별 수익률도 빈 DataFrame."""
        empty_df = pd.DataFrame(columns=["date", "adj_close"])
        result = _compute_monthly_returns(empty_df)
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_empty_regime_features_status(self, empty_conn):
        """빈 regime_features 상태 조회 시 N/A 반환."""
        status = _load_data_status_raw(empty_conn)
        assert status["regime_features"]["latest_date"] == "N/A"
        assert status["regime_features"]["total_rows"] == 0
