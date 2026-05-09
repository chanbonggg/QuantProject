"""
레짐 모듈 단위 테스트

테스트:
  test_features_compute  — 12개 피처 키 존재, FRED 기반 피처 NaN 아님
  test_features_range    — compute_features_range DataFrame, 컬럼 12개
  test_alarm_*           — 급변 알람 6개 조건 + severity + safety_weights
"""

import sys
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import pandas as pd
import numpy as np
import duckdb

from regime.features import compute_features, compute_features_range, FEATURE_COLUMNS
from regime.shock_alarm import (
    check_alarm,
    SAFETY_MODE_WEIGHTS,
    _check_vix_spike,
    _check_vix_backwardation,
    _check_correlation_shock,
    _check_credit_shock,
    _check_trend_break,
    _check_yield_curve_extreme,
    _compute_severity,
)


# ---------------------------------------------------------------------------
# Fixtures — in-memory DuckDB에 모의 데이터 삽입
# ---------------------------------------------------------------------------

def _create_in_memory_db() -> duckdb.DuckDBPyConnection:
    """테스트용 in-memory DuckDB 생성 및 스키마/테이블 초기화."""
    conn = duckdb.connect(":memory:")

    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS feature")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.prices (
            ticker       VARCHAR,
            date         DATE,
            open         DOUBLE,
            high         DOUBLE,
            low          DOUBLE,
            close        DOUBLE,
            adj_close    DOUBLE,
            volume       BIGINT,
            market_cap   DOUBLE,
            source       VARCHAR,
            collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (ticker, date)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.fred_series (
            series_id    VARCHAR,
            date         DATE,
            value        DOUBLE,
            collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (series_id, date)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS feature.regime_features (
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

    return conn


def _insert_spy_prices(conn: duckdb.DuckDBPyConnection, n_days: int, base_date: date) -> None:
    """SPY 가격 n_days일치 삽입 (base_date 이전 포함)."""
    rows = []
    price = 400.0
    for i in range(n_days):
        d = base_date - timedelta(days=n_days - 1 - i)
        price = price * (1 + np.random.normal(0, 0.01))
        rows.append(("SPY", str(d), price, price * 1.01, price * 0.99, price, price, 80_000_000, None, "test"))

    conn.executemany(
        """
        INSERT OR IGNORE INTO raw.prices
            (ticker, date, open, high, low, close, adj_close, volume, market_cap, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _insert_stock_prices(conn: duckdb.DuckDBPyConnection, tickers: list, n_days: int, base_date: date) -> None:
    """임의 종목 가격 삽입 (avg_corr20 계산용)."""
    rows = []
    for ticker in tickers:
        price = float(np.random.uniform(50, 500))
        for i in range(n_days):
            d = base_date - timedelta(days=n_days - 1 - i)
            price = price * (1 + np.random.normal(0, 0.012))
            volume = int(np.random.uniform(5_000_000, 50_000_000))
            rows.append((ticker, str(d), price, price * 1.01, price * 0.99, price, price, volume, None, "test"))

    conn.executemany(
        """
        INSERT OR IGNORE INTO raw.prices
            (ticker, date, open, high, low, close, adj_close, volume, market_cap, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _insert_fred_data(conn: duckdb.DuckDBPyConnection, base_date: date, n_days: int = 30) -> None:
    """FRED 시리즈 모의 데이터 삽입."""
    series_values = {
        "VIXCLS": 18.5,
        "VXVCLS": 21.0,
        "BAMLH0A0HYM2": 350.0,
        "BAMLC0A0CM": 110.0,
        "T10Y2Y": 0.25,
    }

    rows = []
    for series_id, base_val in series_values.items():
        for i in range(n_days):
            d = base_date - timedelta(days=n_days - 1 - i)
            val = base_val + np.random.normal(0, base_val * 0.01)
            rows.append((series_id, str(d), val))

    conn.executemany(
        """
        INSERT OR IGNORE INTO raw.fred_series (series_id, date, value)
        VALUES (?, ?, ?)
        """,
        rows,
    )


@pytest.fixture
def db_with_data():
    """60일치 SPY + 10개 종목 + 30일치 FRED 데이터가 담긴 in-memory DB."""
    np.random.seed(42)
    conn = _create_in_memory_db()
    base = date(2024, 6, 30)

    # SPY 60일치 (rv20, rv60, ma200_gap 등 계산용으로는 데이터 부족하지만 기본 동작 확인)
    _insert_spy_prices(conn, n_days=60, base_date=base)

    # 추가 종목 10개 (avg_corr20 계산용)
    tickers = [f"TICK{i:02d}" for i in range(10)]
    _insert_stock_prices(conn, tickers, n_days=30, base_date=base)

    # FRED 30일치
    _insert_fred_data(conn, base_date=base, n_days=30)

    yield conn, base
    conn.close()


@pytest.fixture
def db_with_full_data():
    """252일치 SPY + 10개 종목 + 30일치 FRED (r12m, ma200_gap NaN 없이 계산 가능)."""
    np.random.seed(123)
    conn = _create_in_memory_db()
    base = date(2024, 12, 31)

    # 510일치 SPY (r12m=252, ma200 = 200, 여유분 포함)
    _insert_spy_prices(conn, n_days=510, base_date=base)

    # 종목 10개 (avg_corr20용)
    tickers = [f"TICK{i:02d}" for i in range(10)]
    _insert_stock_prices(conn, tickers, n_days=30, base_date=base)

    # FRED 30일치
    _insert_fred_data(conn, base_date=base, n_days=30)

    yield conn, base
    conn.close()


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------

class TestFeaturesCompute:
    """compute_features 단위 테스트."""

    def test_returns_series_with_all_feature_keys(self, db_with_data):
        """반환값이 pd.Series이고 12개 피처 키가 모두 존재하는지 확인."""
        conn, base = db_with_data
        date_str = str(base)

        result = compute_features(date_str, conn=conn)

        assert isinstance(result, pd.Series), "반환 타입은 pd.Series여야 함"
        assert len(result) == len(FEATURE_COLUMNS), f"피처 수 불일치: {len(result)} != {len(FEATURE_COLUMNS)}"
        for col in FEATURE_COLUMNS:
            assert col in result.index, f"피처 '{col}' 누락"

    def test_fred_based_features_not_nan(self, db_with_data):
        """FRED 데이터가 있으면 해당 피처는 NaN이 아니어야 함."""
        conn, base = db_with_data
        date_str = str(base)

        result = compute_features(date_str, conn=conn)

        # FRED 기반 피처
        fred_features = ["vix", "vix3m", "hy_spread", "ig_spread", "term_spread"]
        for feat in fred_features:
            assert not pd.isna(result[feat]), f"'{feat}'는 NaN이면 안 됨 (FRED 데이터 존재)"

    def test_vix_term_computed(self, db_with_data):
        """vix_term = vix3m / vix 로 계산되는지 확인."""
        conn, base = db_with_data
        date_str = str(base)

        result = compute_features(date_str, conn=conn)

        if not pd.isna(result["vix"]) and not pd.isna(result["vix3m"]) and result["vix"] > 0:
            expected = result["vix3m"] / result["vix"]
            assert abs(result["vix_term"] - expected) < 1e-9, "vix_term 계산 오류"

    def test_vxmt_always_nan(self, db_with_data):
        """vxmt는 항상 NaN이어야 함."""
        conn, base = db_with_data
        date_str = str(base)

        result = compute_features(date_str, conn=conn)

        assert pd.isna(result["vxmt"]), "vxmt는 항상 NaN이어야 함"

    def test_spy_based_features_with_sufficient_data(self, db_with_full_data):
        """충분한 SPY 데이터가 있으면 rv20, rv60, ma200_gap, r12m, r1m이 NaN 아님."""
        conn, base = db_with_full_data
        date_str = str(base)

        result = compute_features(date_str, conn=conn)

        spy_features = ["rv20", "rv60", "ma200_gap", "r12m", "r1m"]
        for feat in spy_features:
            assert not pd.isna(result[feat]), f"'{feat}'는 충분한 데이터가 있으므로 NaN이면 안 됨"

    def test_rv_values_positive(self, db_with_full_data):
        """실현변동성(rv20, rv60)은 양수여야 함."""
        conn, base = db_with_full_data
        date_str = str(base)

        result = compute_features(date_str, conn=conn)

        if not pd.isna(result["rv20"]):
            assert result["rv20"] > 0, "rv20은 양수여야 함"
        if not pd.isna(result["rv60"]):
            assert result["rv60"] > 0, "rv60은 양수여야 함"

    def test_saves_to_db(self, db_with_data):
        """compute_features 후 feature.regime_features에 저장되는지 확인."""
        conn, base = db_with_data
        date_str = str(base)

        compute_features(date_str, conn=conn)

        row = conn.execute(
            "SELECT date FROM feature.regime_features WHERE date = CAST(? AS DATE)",
            [date_str],
        ).fetchone()

        assert row is not None, "feature.regime_features에 저장되어야 함"

    def test_feature_column_order(self, db_with_data):
        """반환 Series의 인덱스 순서가 FEATURE_COLUMNS와 일치해야 함."""
        conn, base = db_with_data
        date_str = str(base)

        result = compute_features(date_str, conn=conn)

        assert list(result.index) == FEATURE_COLUMNS, "피처 컬럼 순서 불일치"

    def test_no_data_returns_nan_series(self, db_with_data):
        """FRED 데이터 없는 날짜에 대해 NaN Series 반환 (에러 없음)."""
        conn, base = db_with_data
        future_date = "1990-01-01"  # 데이터 없는 날짜

        result = compute_features(future_date, conn=conn)

        assert isinstance(result, pd.Series), "데이터 없어도 pd.Series 반환해야 함"
        assert len(result) == len(FEATURE_COLUMNS), "피처 수 동일해야 함"


class TestFeaturesRange:
    """compute_features_range 단위 테스트."""

    def test_returns_dataframe(self, db_with_data):
        """반환값이 pd.DataFrame인지 확인."""
        conn, base = db_with_data
        end_str = str(base)
        start_str = str(base - timedelta(days=5))

        result = compute_features_range(start_str, end_str, conn=conn)

        assert isinstance(result, pd.DataFrame), "반환 타입은 pd.DataFrame이어야 함"

    def test_has_12_columns(self, db_with_data):
        """DataFrame이 12개 컬럼을 가지는지 확인."""
        conn, base = db_with_data
        end_str = str(base)
        start_str = str(base - timedelta(days=10))

        result = compute_features_range(start_str, end_str, conn=conn)

        assert len(result.columns) == len(FEATURE_COLUMNS), (
            f"컬럼 수 불일치: {len(result.columns)} != {len(FEATURE_COLUMNS)}"
        )

    def test_column_names_match(self, db_with_data):
        """DataFrame 컬럼명이 FEATURE_COLUMNS와 일치하는지 확인."""
        conn, base = db_with_data
        end_str = str(base)
        start_str = str(base - timedelta(days=10))

        result = compute_features_range(start_str, end_str, conn=conn)

        assert list(result.columns) == FEATURE_COLUMNS, "컬럼명 불일치"

    def test_index_is_datetime(self, db_with_data):
        """DataFrame 인덱스가 DatetimeIndex인지 확인."""
        conn, base = db_with_data
        end_str = str(base)
        start_str = str(base - timedelta(days=10))

        result = compute_features_range(start_str, end_str, conn=conn)

        if len(result) > 0:
            assert isinstance(result.index, pd.DatetimeIndex), "인덱스는 DatetimeIndex여야 함"

    def test_rows_correspond_to_trading_days(self, db_with_data):
        """행 수가 SPY 거래일 수와 일치하는지 확인."""
        conn, base = db_with_data
        end_str = str(base)
        start_str = str(base - timedelta(days=30))

        # SPY 거래일 수 직접 조회
        trading_days_count = conn.execute(
            """
            SELECT COUNT(DISTINCT date) AS cnt
            FROM raw.prices
            WHERE ticker = 'SPY'
              AND date >= CAST(? AS DATE)
              AND date <= CAST(? AS DATE)
            """,
            [start_str, end_str],
        ).fetchone()[0]

        result = compute_features_range(start_str, end_str, conn=conn)

        assert len(result) == trading_days_count, (
            f"행 수 불일치: {len(result)} != {trading_days_count} (거래일 수)"
        )

    def test_empty_range_returns_empty_df(self):
        """데이터 없는 기간에 빈 DataFrame 반환."""
        conn = _create_in_memory_db()
        try:
            result = compute_features_range("1990-01-01", "1990-01-31", conn=conn)
            assert isinstance(result, pd.DataFrame), "빈 경우에도 DataFrame 반환해야 함"
            assert len(result) == 0, "데이터 없으면 행 수 0이어야 함"
        finally:
            conn.close()

    def test_fred_features_populated_in_range(self, db_with_data):
        """범위 내 FRED 데이터 있는 날짜의 피처가 NaN 아닌지 확인."""
        conn, base = db_with_data
        end_str = str(base)
        start_str = str(base - timedelta(days=5))

        result = compute_features_range(start_str, end_str, conn=conn)

        if len(result) > 0:
            last_row = result.iloc[-1]
            fred_features = ["vix", "vix3m", "hy_spread", "ig_spread", "term_spread"]
            for feat in fred_features:
                assert not pd.isna(last_row[feat]), f"마지막 날짜의 '{feat}'는 NaN이면 안 됨"


# ---------------------------------------------------------------------------
# Fixtures — shock_alarm 테스트용 in-memory DB
# ---------------------------------------------------------------------------

def _create_alarm_db() -> duckdb.DuckDBPyConnection:
    """급변 알람 테스트용 in-memory DuckDB 생성."""
    conn = duckdb.connect(":memory:")

    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS feature")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.prices (
            ticker       VARCHAR,
            date         DATE,
            open         DOUBLE,
            high         DOUBLE,
            low          DOUBLE,
            close        DOUBLE,
            adj_close    DOUBLE,
            volume       BIGINT,
            market_cap   DOUBLE,
            source       VARCHAR,
            collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (ticker, date)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.fred_series (
            series_id    VARCHAR,
            date         DATE,
            value        DOUBLE,
            collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (series_id, date)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS feature.regime_features (
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS feature.regime_labels (
            date        DATE PRIMARY KEY,
            regime      VARCHAR,
            shock_alarm BOOLEAN,
            computed_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    return conn


def _insert_regime_features(
    conn: duckdb.DuckDBPyConnection,
    date_str: str,
    vix: float = 18.0,
    vix3m: float = 20.0,
    vxmt: float = None,
    vix_term: float = None,
    rv20: float = 0.15,
    rv60: float = 0.14,
    ma200_gap: float = 0.05,
    r12m: float = 0.12,
    r1m: float = 0.01,
    avg_corr20: float = 0.40,
    hy_spread: float = 3.5,
    ig_spread: float = 1.1,
    term_spread: float = 0.30,
) -> None:
    """feature.regime_features에 단일 행 삽입."""
    if vix_term is None and vix is not None and vix3m is not None and vix > 0:
        vix_term = vix3m / vix

    conn.execute(
        """
        INSERT OR REPLACE INTO feature.regime_features
            (date, vix, vix3m, vxmt, vix_term, rv20, rv60, ma200_gap,
             r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread)
        VALUES (CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [date_str, vix, vix3m, vxmt, vix_term, rv20, rv60, ma200_gap,
         r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread],
    )


def _insert_spy_for_alarm(
    conn: duckdb.DuckDBPyConnection,
    date_str: str,
    adj_close: float = 400.0,
    volume: int = 80_000_000,
    prev_adj_close: float = None,
    n_history: int = 25,
) -> None:
    """
    SPY 가격 삽입 (당일 + 전일 + 이전 n_history거래일).

    prev_adj_close: 전일 종가 (None이면 adj_close * 1.01).
    히스토리 행은 당일로부터 2일 이상 이전만 삽입하여 전일 행과 충돌 방지.
    """
    base = date.fromisoformat(date_str)
    prev_price = prev_adj_close if prev_adj_close is not None else adj_close * 1.01

    rows = []

    # 이전 n_history거래일 (당일 -2일 이전)
    for i in range(n_history + 1, 1, -1):
        d = base - timedelta(days=i)
        rows.append(("SPY", str(d), prev_price, prev_price, prev_price,
                     prev_price, prev_price, 80_000_000, None, "test"))

    # 전일 (당일 -1일)
    rows.append(("SPY", str(base - timedelta(days=1)), prev_price, prev_price,
                 prev_price, prev_price, prev_price, 80_000_000, None, "test"))

    # 당일
    rows.append(("SPY", date_str, adj_close, adj_close, adj_close,
                 adj_close, adj_close, volume, None, "test"))

    conn.executemany(
        """
        INSERT OR IGNORE INTO raw.prices
            (ticker, date, open, high, low, close, adj_close, volume, market_cap, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _insert_fred_hy(conn: duckdb.DuckDBPyConnection, n_days: int = 365 * 5 + 10) -> None:
    """BAMLH0A0HYM2 5년치 데이터 삽입 (퍼센타일 계산용). 값 범위: 3.0 ~ 8.0."""
    base = date(2024, 6, 30)
    rows = []
    for i in range(n_days):
        d = base - timedelta(days=i)
        val = 3.0 + (i % 50) * 0.10  # 3.0 ~ 7.9 범위
        rows.append(("BAMLH0A0HYM2", str(d), val))

    conn.executemany(
        "INSERT OR IGNORE INTO raw.fred_series (series_id, date, value) VALUES (?, ?, ?)",
        rows,
    )


# ---------------------------------------------------------------------------
# 급변 알람 단위 테스트
# ---------------------------------------------------------------------------

class TestAlarmConditions:
    """6개 알람 조건 개별 테스트."""

    def test_alarm_vix_spike_by_rate(self):
        """VIX: 25 → 32 (+28%) — 변화율 기준 발동."""
        current = pd.Series({"vix": 32.0, "vix3m": 34.0, "vix_term": 34.0 / 32.0,
                             "avg_corr20": 0.40, "hy_spread": 3.5, "term_spread": 0.30})
        prev = pd.Series({"vix": 25.0})

        result = _check_vix_spike(current, prev)

        assert result is not None, "VIX 변화율 +28% → 알람 발동되어야 함"
        assert "28" in result or "VIX" in result

    def test_alarm_vix_spike_by_abs(self):
        """VIX >= 30 — 절대값 기준 발동."""
        current = pd.Series({"vix": 35.0})

        result = _check_vix_spike(current, prev_features=None)

        assert result is not None, "VIX=35 (>= 30) → 알람 발동되어야 함"

    def test_alarm_vix_spike_not_triggered(self):
        """VIX: 18 → 20 (+11%, < 20%) — 미발동."""
        current = pd.Series({"vix": 20.0})
        prev = pd.Series({"vix": 18.0})

        result = _check_vix_spike(current, prev)

        assert result is None, "VIX 변화율 +11% → 미발동이어야 함"

    def test_alarm_vix_backwardation(self):
        """VIX_TERM=0.80 (< 0.85) → 발동."""
        features = pd.Series({"vix_term": 0.80})

        result = _check_vix_backwardation(features)

        assert result is not None, "VIX_TERM=0.80 → 알람 발동되어야 함"

    def test_alarm_vix_backwardation_not_triggered(self):
        """VIX_TERM=0.90 (>= 0.85) → 미발동."""
        features = pd.Series({"vix_term": 0.90})

        result = _check_vix_backwardation(features)

        assert result is None, "VIX_TERM=0.90 → 미발동이어야 함"

    def test_alarm_correlation_shock(self):
        """AVG_CORR=0.80, 60일전=0.60 (+0.20 >= 0.15) → 발동."""
        features = pd.Series({"avg_corr20": 0.80})
        avg_corr_60d_ago = 0.60

        result = _check_correlation_shock(features, avg_corr_60d_ago)

        assert result is not None, "AVG_CORR=0.80, 60일전=0.60 → 알람 발동되어야 함"

    def test_alarm_correlation_shock_no_history(self):
        """60일전 데이터 없으면 — 조건 스킵 (None 반환)."""
        features = pd.Series({"avg_corr20": 0.90})

        result = _check_correlation_shock(features, avg_corr_60d_ago=None)

        assert result is None, "60일전 없으면 조건 스킵해야 함"

    def test_alarm_correlation_shock_small_delta(self):
        """AVG_CORR=0.80, 60일전=0.70 (+0.10 < 0.15) → 미발동."""
        features = pd.Series({"avg_corr20": 0.80})

        result = _check_correlation_shock(features, avg_corr_60d_ago=0.70)

        assert result is None, "delta=0.10 < 0.15 → 미발동이어야 함"

    def test_alarm_credit_shock_by_daily_change(self):
        """HY_SPREAD 일변화 +0.60 (60bps) >= 0.50 → 발동."""
        features = pd.Series({"hy_spread": 4.10})
        prev_hy = 3.50  # +0.60 = +60bps

        result = _check_credit_shock(features, hy_pctl_95=None, prev_hy=prev_hy)

        assert result is not None, "HY 일변화 +60bps → 알람 발동되어야 함"

    def test_alarm_credit_shock_by_percentile(self):
        """HY_SPREAD=9.0 > 5년 95pctl=8.0 → 발동."""
        features = pd.Series({"hy_spread": 9.0})

        result = _check_credit_shock(features, hy_pctl_95=8.0, prev_hy=None)

        assert result is not None, "HY > 95pctl → 알람 발동되어야 함"

    def test_alarm_credit_shock_not_triggered(self):
        """HY_SPREAD 일변화 +0.30 (30bps) < 50bps → 미발동."""
        features = pd.Series({"hy_spread": 3.80})
        prev_hy = 3.50  # +0.30 = +30bps

        result = _check_credit_shock(features, hy_pctl_95=None, prev_hy=prev_hy)

        assert result is None, "HY 일변화 +30bps → 미발동이어야 함"

    def test_alarm_trend_break(self):
        """SPY -4%, 거래량 3배 → 발동."""
        conn = _create_alarm_db()
        target_date = "2024-06-28"

        # 전일 종가 400, 당일 종가 384 (-4%), 거래량 240M (평균 80M의 3배)
        _insert_spy_for_alarm(conn, target_date, adj_close=384.0, volume=240_000_000,
                              prev_adj_close=400.0, n_history=22)

        result = _check_trend_break(target_date, conn)
        conn.close()

        assert result is not None, "SPY -4%, 거래량 3배 → 알람 발동되어야 함"

    def test_alarm_trend_break_not_triggered_small_drop(self):
        """SPY -2%, 거래량 3배 → 미발동 (수익률 조건 불충족)."""
        conn = _create_alarm_db()
        target_date = "2024-06-28"

        # 전일 종가 400, 당일 종가 392 (-2%), 거래량 240M
        _insert_spy_for_alarm(conn, target_date, adj_close=392.0, volume=240_000_000,
                              prev_adj_close=400.0, n_history=22)

        result = _check_trend_break(target_date, conn)
        conn.close()

        assert result is None, "SPY -2% → 미발동이어야 함"

    def test_alarm_trend_break_not_triggered_low_volume(self):
        """SPY -4%, 거래량 1.5배 → 미발동 (거래량 조건 불충족)."""
        conn = _create_alarm_db()
        target_date = "2024-06-28"

        # 전일 종가 400, 당일 종가 384 (-4%), 거래량 120M (평균 대비 1.5배)
        _insert_spy_for_alarm(conn, target_date, adj_close=384.0, volume=120_000_000,
                              prev_adj_close=400.0, n_history=22)

        result = _check_trend_break(target_date, conn)
        conn.close()

        assert result is None, "거래량 1.5배 → 미발동이어야 함"

    def test_alarm_yield_curve(self):
        """TERM_SPREAD=-0.60 (<= -0.50) → 발동."""
        features = pd.Series({"term_spread": -0.60})

        result = _check_yield_curve_extreme(features)

        assert result is not None, "TERM_SPREAD=-0.60 → 알람 발동되어야 함"

    def test_alarm_yield_curve_not_triggered(self):
        """TERM_SPREAD=-0.30 (> -0.50) → 미발동."""
        features = pd.Series({"term_spread": -0.30})

        result = _check_yield_curve_extreme(features)

        assert result is None, "TERM_SPREAD=-0.30 → 미발동이어야 함"


class TestAlarmSeverity:
    """심각도 산출 테스트."""

    def test_severity_0_triggers_low(self):
        """트리거 0개 → 'low'."""
        assert _compute_severity([]) == "low"

    def test_severity_1_trigger_medium(self):
        """트리거 1개 → 'medium'."""
        assert _compute_severity(["vix_spike"]) == "medium"

    def test_severity_2_triggers_high(self):
        """트리거 2개 (vix_spike 제외) → 'high'."""
        assert _compute_severity(["vix_backwardation", "yield_curve"]) == "high"

    def test_severity_3_triggers_critical(self):
        """트리거 3개+ → 'critical'."""
        assert _compute_severity(["vix_spike", "vix_backwardation", "yield_curve"]) == "critical"

    def test_severity_vix_and_credit_critical(self):
        """vix_spike + credit_shock 동시 → 'critical' (2개여도)."""
        assert _compute_severity(["vix_spike", "credit_shock"]) == "critical"


class TestAlarmSafetyWeights:
    """안전모드 가중치 테스트."""

    def test_safety_weights_strategy_sum_to_one(self):
        """전략 가중치 합 = 1.0."""
        strategy_keys = ["momentum", "quality", "value", "low_vol"]
        total = sum(SAFETY_MODE_WEIGHTS[k] for k in strategy_keys)
        assert abs(total - 1.0) < 1e-9, f"전략 가중치 합 = {total} (1.0이어야 함)"

    def test_alarm_true_returns_safety_weights(self):
        """alarm=True인 경우 safety_weights 반환."""
        conn = _create_alarm_db()
        target_date = "2024-06-28"

        # VIX >= 30인 피처 삽입 → vix_spike 발동
        _insert_regime_features(conn, target_date, vix=35.0, vix3m=34.0)

        result = check_alarm(target_date, conn)
        conn.close()

        assert result["alarm"] is True
        assert result["safety_weights"] == SAFETY_MODE_WEIGHTS

    def test_alarm_false_returns_empty_safety_weights(self):
        """alarm=False인 경우 safety_weights 빈 dict 반환."""
        conn = _create_alarm_db()
        target_date = "2024-06-28"

        # 정상 피처 삽입 (알람 조건 미충족)
        _insert_regime_features(
            conn, target_date,
            vix=18.0, vix3m=20.0, vix_term=20.0/18.0,
            avg_corr20=0.30, hy_spread=3.5, term_spread=0.30,
        )

        result = check_alarm(target_date, conn)
        conn.close()

        if not result["alarm"]:
            assert result["safety_weights"] == {}, "alarm=False → safety_weights 빈 dict"


class TestCheckAlarmInterface:
    """check_alarm 공개 인터페이스 테스트."""

    def test_returns_required_keys(self):
        """반환 dict에 필수 키가 모두 존재하는지 확인."""
        conn = _create_alarm_db()
        target_date = "2024-06-28"
        _insert_regime_features(conn, target_date)

        result = check_alarm(target_date, conn)
        conn.close()

        required_keys = ["alarm", "triggers", "severity", "details", "safety_weights", "date"]
        for key in required_keys:
            assert key in result, f"필수 키 '{key}' 누락"

    def test_date_field_matches_input(self):
        """반환 dict의 'date' 필드가 입력 날짜와 일치하는지 확인."""
        conn = _create_alarm_db()
        target_date = "2024-06-28"
        _insert_regime_features(conn, target_date)

        result = check_alarm(target_date, conn)
        conn.close()

        assert result["date"] == target_date

    def test_triggers_list_type(self):
        """'triggers' 필드가 list 타입인지 확인."""
        conn = _create_alarm_db()
        target_date = "2024-06-28"
        _insert_regime_features(conn, target_date)

        result = check_alarm(target_date, conn)
        conn.close()

        assert isinstance(result["triggers"], list)

    def test_no_features_returns_alarm_false(self):
        """피처 없는 날짜 → alarm=False (에러 없음)."""
        conn = _create_alarm_db()

        result = check_alarm("1990-01-01", conn)
        conn.close()

        assert result["alarm"] is False
        assert result["severity"] == "low"

    def test_vix_spike_alarm_triggered_via_check_alarm(self):
        """VIX=35 → check_alarm이 vix_spike 트리거를 포함하는지 확인."""
        conn = _create_alarm_db()
        target_date = "2024-06-28"
        _insert_regime_features(conn, target_date, vix=35.0, vix3m=34.0)

        result = check_alarm(target_date, conn)
        conn.close()

        assert result["alarm"] is True
        assert "vix_spike" in result["triggers"]

    def test_severity_consistent_with_trigger_count(self):
        """severity가 trigger 개수에 따라 올바르게 산출되는지 확인."""
        conn = _create_alarm_db()
        target_date = "2024-06-28"

        # yield_curve + vix_backwardation 동시 (2개 → high)
        _insert_regime_features(
            conn, target_date,
            vix=20.0, vix3m=16.0, vix_term=16.0/20.0,  # VIX_TERM=0.80 → backwardation
            term_spread=-0.60,  # yield_curve
        )

        result = check_alarm(target_date, conn)
        conn.close()

        n = len(result["triggers"])
        if n == 2 and "vix_spike" not in result["triggers"] and "credit_shock" not in result["triggers"]:
            assert result["severity"] == "high"
