"""
전략 모듈 테스트
- 모멘텀 전략 신호 및 포트폴리오
"""

import os
import sys
from pathlib import Path
from datetime import date, datetime, timedelta

import pytest
import pandas as pd
import numpy as np
import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent / "quant_us"))

from db.init import get_connection, init_db
from strategies.universe import get_universe
from strategies.momentum import compute_signal as momentum_signal, get_portfolio as momentum_portfolio
from strategies.value import (
    compute_signal as value_signal,
    get_portfolio as value_portfolio,
    diagnose_quintile,
)
from strategies.quality import compute_signal as quality_signal, get_portfolio as quality_portfolio
from strategies.low_vol import compute_signal as low_vol_signal, get_portfolio as low_vol_portfolio


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────

@pytest.fixture
def test_db():
    """테스트용 임시 DuckDB 연결."""
    db_path = ":memory:"
    conn = duckdb.connect(db_path)

    # 스키마 및 테이블 생성
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS feature")

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

    conn.execute("""
        CREATE TABLE raw.sp500_changes (
            date        DATE,
            ticker      VARCHAR,
            action      VARCHAR,
            reason      VARCHAR,
            replacement VARCHAR
        )
    """)

    conn.execute("""
        CREATE TABLE raw.ticker_events (
            ticker      VARCHAR,
            event_date  DATE,
            event_type  VARCHAR,
            details     VARCHAR
        )
    """)

    # SEC 재무 데이터 테이블 추가
    conn.execute("""
        CREATE TABLE raw.sec_financials (
            ticker              VARCHAR,
            cik                 VARCHAR,
            filing_type         VARCHAR,
            period_of_report    DATE,
            filed_date          DATE,
            revenue             DOUBLE,
            net_income          DOUBLE,
            eps_diluted         DOUBLE,
            total_assets        DOUBLE,
            stockholders_equity DOUBLE,
            total_liabilities   DOUBLE,
            operating_cashflow  DOUBLE,
            cost_of_goods_sold  DOUBLE,
            collected_at        TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (ticker, filing_type, period_of_report, filed_date)
        )
    """)

    yield conn
    conn.close()


def _insert_price_data(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    start_date: date,
    end_date: date,
    price_func=None,
) -> None:
    """
    테스트용 시가 데이터 생성 및 삽입.

    Args:
        conn: DuckDB 연결
        ticker: 티커
        start_date: 시작 날짜
        end_date: 종료 날짜
        price_func: 가격 생성 함수 (date -> float, 기본값: 100)
    """
    if price_func is None:
        price_func = lambda d: 100.0

    current_date = start_date
    while current_date <= end_date:
        # 주말 제외 (간단히 요일로 확인)
        if current_date.weekday() < 5:  # 0-4: 월-금
            price = price_func(current_date)
            conn.execute(
                """
                INSERT INTO raw.prices
                (ticker, date, open, high, low, close, adj_close, volume, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [ticker, current_date, price, price + 1, price - 1, price, price, 1000000, "test"],
            )
        current_date += timedelta(days=1)


def _insert_sp500_data(
    conn: duckdb.DuckDBPyConnection,
    tickers: list,
    add_date: date,
) -> None:
    """
    테스트용 S&P500 구성종목 데이터 삽입.

    Args:
        conn: DuckDB 연결
        tickers: 티커 목록
        add_date: 편입 날짜
    """
    for ticker in tickers:
        conn.execute(
            """
            INSERT INTO raw.sp500_changes
            (date, ticker, action, reason, replacement)
            VALUES (?, ?, ?, ?, ?)
            """,
            [add_date, ticker, "add", "initial", None],
        )


def _insert_financials_data(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    period_of_report: date,
    filed_date: date,
    revenue: float = 1e9,
    net_income: float = 1e8,
    stockholders_equity: float = 5e8,
    operating_cashflow: float = 1.5e8,
) -> None:
    """
    테스트용 SEC 재무 데이터 삽입.

    Args:
        conn: DuckDB 연결
        ticker: 티커
        period_of_report: 보고 기간말
        filed_date: 제출 날짜
        revenue: 매출
        net_income: 순이익
        stockholders_equity: 자본
        operating_cashflow: 영업 현금흐름
    """
    conn.execute(
        """
        INSERT INTO raw.sec_financials
        (ticker, cik, filing_type, period_of_report, filed_date,
         revenue, net_income, eps_diluted, total_assets,
         stockholders_equity, total_liabilities, operating_cashflow,
         cost_of_goods_sold)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ticker,
            "0000000001",  # 더미 CIK
            "10-K",
            period_of_report,
            filed_date,
            revenue,
            net_income,
            net_income / 1e8,  # EPS
            revenue * 2,  # Assets
            stockholders_equity,
            revenue,  # Liabilities
            operating_cashflow,
            revenue * 0.6,  # COGS
        ],
    )


# ─────────────────────────────────────────────────────────────────
# 테스트: 모멘텀 신호
# ─────────────────────────────────────────────────────────────────

def test_momentum_signal_range(test_db):
    """모멘텀 신호가 [-1, 1] 범위인지 확인."""

    # 테스트 데이터: 10개 종목, 각각 400일 데이터 (약 16개월)
    test_tickers = [f"TEST{i:02d}" for i in range(10)]

    # 시작 날짜: 2년 전
    base_date = date(2023, 1, 1)
    price_start = base_date - timedelta(days=400)
    price_end = base_date

    # S&P500 데이터 (상승 추세)
    def sp500_price_func(d):
        days_since_start = (d - price_start).days
        return 100.0 + days_since_start * 0.05  # 연 5% 정도 상승

    _insert_price_data(test_db, "^GSPC", price_start, price_end, sp500_price_func)

    # 종목별 다양한 수익률로 데이터 생성
    for i, ticker in enumerate(test_tickers):
        # 종목: 상승, 보통, 하락 등 다양한 패턴
        trend = (i - 5) * 0.1  # -0.5 ~ 0.4
        price_func = lambda d, t=trend: 100.0 * (1.0 + t) ** ((d - price_start).days / 365.0)
        _insert_price_data(test_db, ticker, price_start, price_end, price_func)

    # S&P500 S&P500 편입 데이터
    _insert_sp500_data(test_db, test_tickers, base_date - timedelta(days=400))

    # 신호 계산
    signal = momentum_signal(str(base_date), test_tickers, test_db)

    # 검증
    assert not signal.empty, "신호가 비어있음"
    assert len(signal) == len(test_tickers), "신호 개수 불일치"
    assert (signal >= -1.0).all(), f"신호 최솟값 {signal.min()} < -1"
    assert (signal <= 1.0).all(), f"신호 최댓값 {signal.max()} > 1"

    print(f"✓ 모멘텀 신호 범위 검증 통과")
    print(f"  신호 범위: [{signal.min():.4f}, {signal.max():.4f}]")
    print(f"  평균: {signal.mean():.4f}, 표준편차: {signal.std():.4f}")


def test_momentum_signal_values(test_db):
    """모멘텀 신호가 초과수익률을 정확히 반영하는지 확인."""

    # 간단한 테스트: 3개 종목
    # 1. GOOD: S&P500 대비 +20% 초과수익률 (상승)
    # 2. NEUTRAL: S&P500과 동일 (0% 초과수익률)
    # 3. BAD: S&P500 대비 -20% 초과수익률 (하락)

    base_date = date(2023, 6, 30)
    price_start = base_date - timedelta(days=400)
    price_end = base_date - timedelta(days=30)  # t-1: 30일 전까지

    # S&P500: 100 -> 120 (20% 상승)
    def sp500_price_func(d):
        days_since_start = (d - price_start).days
        total_days = (price_end - price_start).days
        return 100.0 + 20.0 * days_since_start / total_days

    _insert_price_data(test_db, "^GSPC", price_start, price_end, sp500_price_func)

    # GOOD: 100 -> 144 (44% 상승 = 20% S&P500 + 24% 초과)
    def good_price_func(d):
        days_since_start = (d - price_start).days
        total_days = (price_end - price_start).days
        return 100.0 + 44.0 * days_since_start / total_days

    # NEUTRAL: 100 -> 120 (20% 상승 = S&P500과 동일)
    def neutral_price_func(d):
        days_since_start = (d - price_start).days
        total_days = (price_end - price_start).days
        return 100.0 + 20.0 * days_since_start / total_days

    # BAD: 100 -> 100 (0% = -20% 초과수익률)
    def bad_price_func(d):
        return 100.0

    _insert_price_data(test_db, "GOOD", price_start, price_end, good_price_func)
    _insert_price_data(test_db, "NEUTRAL", price_start, price_end, neutral_price_func)
    _insert_price_data(test_db, "BAD", price_start, price_end, bad_price_func)

    _insert_sp500_data(test_db, ["GOOD", "NEUTRAL", "BAD"], price_start)

    # 신호 계산
    signal = momentum_signal(str(base_date), ["GOOD", "NEUTRAL", "BAD"], test_db)

    # 검증: GOOD > NEUTRAL > BAD
    assert signal["GOOD"] > signal["NEUTRAL"], "GOOD가 NEUTRAL보다 높아야 함"
    assert signal["NEUTRAL"] > signal["BAD"], "NEUTRAL이 BAD보다 높아야 함"

    print(f"✓ 모멘텀 신호 순위 검증 통과")
    print(f"  GOOD={signal['GOOD']:.4f} > NEUTRAL={signal['NEUTRAL']:.4f} > BAD={signal['BAD']:.4f}")


# ─────────────────────────────────────────────────────────────────
# 테스트: 포트폴리오
# ─────────────────────────────────────────────────────────────────

def test_momentum_portfolio_weights(test_db):
    """포트폴리오 가중치 합이 1.0인지 확인."""

    test_tickers = [f"TEST{i:02d}" for i in range(20)]

    base_date = date(2023, 1, 1)
    price_start = base_date - timedelta(days=400)
    price_end = base_date

    # 더미 데이터 생성
    for ticker in test_tickers + ["^GSPC"]:
        _insert_price_data(test_db, ticker, price_start, price_end)

    _insert_sp500_data(test_db, test_tickers, price_start)

    # 포트폴리오 구성
    portfolio = momentum_portfolio(str(base_date), test_db)

    # 검증
    assert not portfolio.empty, "포트폴리오가 비어있음"
    assert "ticker" in portfolio.columns, "ticker 컬럼 없음"
    assert "weight" in portfolio.columns, "weight 컬럼 없음"
    assert "signal_score" in portfolio.columns, "signal_score 컬럼 없음"

    weight_sum = portfolio['weight'].sum()
    assert abs(weight_sum - 1.0) < 1e-6, f"가중치 합 {weight_sum} != 1.0"

    # 상위 20% (약 4개 종목)
    expected_count = max(1, int(np.ceil(len(test_tickers) * 0.20)))
    assert len(portfolio) == expected_count, f"포트폴리오 종목 수 {len(portfolio)} != {expected_count}"

    print(f"✓ 포트폴리오 가중치 검증 통과")
    print(f"  종목 수: {len(portfolio)}, 가중치 합: {weight_sum:.6f}")
    print(f"  Top 3 위치: {portfolio.head(3)[['ticker', 'weight']].to_string()}")


def test_momentum_portfolio_equal_weighted(test_db):
    """포트폴리오가 동일가중인지 확인."""

    test_tickers = [f"TEST{i:02d}" for i in range(20)]

    base_date = date(2023, 1, 1)
    price_start = base_date - timedelta(days=400)
    price_end = base_date

    # 더미 데이터
    for ticker in test_tickers + ["^GSPC"]:
        _insert_price_data(test_db, ticker, price_start, price_end)

    _insert_sp500_data(test_db, test_tickers, price_start)

    # 포트폴리오 구성
    portfolio = momentum_portfolio(str(base_date), test_db)

    # 검증: 모든 가중치가 동일
    if len(portfolio) > 0:
        expected_weight = 1.0 / len(portfolio)
        assert (np.isclose(portfolio['weight'].values, expected_weight)).all(), \
            f"가중치가 동일하지 않음: {portfolio['weight'].unique()}"

    print(f"✓ 동일가중 검증 통과")
    print(f"  각 종목 가중치: {1.0 / len(portfolio):.6f}")


# ─────────────────────────────────────────────────────────────────
# 테스트: Universe
# ─────────────────────────────────────────────────────────────────

def test_universe_basic(test_db):
    """기본 유니버스 선정 검증."""

    test_tickers = [f"TEST{i:02d}" for i in range(10)]

    base_date = date(2023, 1, 1)
    price_start = base_date - timedelta(days=250)
    price_end = base_date

    # 데이터 생성
    for ticker in test_tickers + ["^GSPC"]:
        _insert_price_data(test_db, ticker, price_start, price_end)

    _insert_sp500_data(test_db, test_tickers, price_start)

    # 유니버스 선정
    universe = get_universe(str(base_date), test_db)

    # 검증
    assert len(universe) > 0, "유니버스가 비어있음"
    assert len(universe) <= len(test_tickers), "유니버스가 원본보다 큼"
    assert all(ticker in test_tickers for ticker in universe), "유니버스에 잘못된 티커 포함"

    print(f"✓ 유니버스 선정 검증 통과: {len(universe)}/{len(test_tickers)} 종목")


# ─────────────────────────────────────────────────────────────────
# 테스트: 밸류 신호 (Value Strategy)
# ─────────────────────────────────────────────────────────────────

def test_value_signal_basic(test_db):
    """밸류 신호 기본 테스트."""

    test_tickers = [f"TEST{i:02d}" for i in range(10)]

    base_date = date(2023, 1, 1)
    price_start = base_date - timedelta(days=250)
    price_end = base_date

    # 더미 데이터 생성
    for ticker in test_tickers + ["^GSPC"]:
        _insert_price_data(test_db, ticker, price_start, price_end)

    _insert_sp500_data(test_db, test_tickers, price_start)

    # 재무 데이터 추가
    for i, ticker in enumerate(test_tickers):
        # 재무 데이터: 종목별로 다른 재무비율
        revenue = 1e9 * (1 + i * 0.1)
        net_income = 1e8 * (1 + i * 0.05)
        equity = 5e8 * (1 + i * 0.15)
        _insert_financials_data(
            test_db,
            ticker,
            base_date - timedelta(days=90),
            base_date - timedelta(days=30),
            revenue=revenue,
            net_income=net_income,
            stockholders_equity=equity,
        )

    # 신호 계산
    universe = get_universe(str(base_date), test_db)
    if universe:
        signals = value_signal(str(base_date), universe, test_db)

        # 검증: 신호가 pd.Series
        assert isinstance(signals, pd.Series), f"신호는 pd.Series여야 함, 실제: {type(signals)}"
        # 재무 데이터가 있으므로 신호가 있어야 함
        if not signals.empty:
            assert all(isinstance(s, (int, float, np.number)) for s in signals.values), "신호는 숫자여야 함"
            print(f"✓ 밸류 신호 기본 검증 통과: {len(signals)}개 종목")


def test_value_portfolio_basic(test_db):
    """밸류 포트폴리오 기본 테스트."""

    test_tickers = [f"TEST{i:02d}" for i in range(20)]

    base_date = date(2023, 1, 1)
    price_start = base_date - timedelta(days=250)
    price_end = base_date

    # 더미 데이터 생성
    for ticker in test_tickers + ["^GSPC"]:
        _insert_price_data(test_db, ticker, price_start, price_end)

    _insert_sp500_data(test_db, test_tickers, price_start)

    # 재무 데이터 추가
    for i, ticker in enumerate(test_tickers):
        revenue = 1e9 * (1 + i * 0.1)
        net_income = 1e8 * (1 + i * 0.05)
        equity = 5e8 * (1 + i * 0.15)
        _insert_financials_data(
            test_db,
            ticker,
            base_date - timedelta(days=90),
            base_date - timedelta(days=30),
            revenue=revenue,
            net_income=net_income,
            stockholders_equity=equity,
        )

    # 포트폴리오 구성
    portfolio = value_portfolio(str(base_date), test_db)

    # 검증: 포트폴리오가 pd.DataFrame
    assert isinstance(portfolio, pd.DataFrame), f"포트폴리오는 pd.DataFrame이어야 함, 실제: {type(portfolio)}"
    if not portfolio.empty:
        # 필수 컬럼 확인
        assert "ticker" in portfolio.columns, "ticker 컬럼 필요"
        assert "weight" in portfolio.columns, "weight 컬럼 필요"
        assert "signal_score" in portfolio.columns, "signal_score 컬럼 필요"

        # 가중치 합 검증
        weight_sum = portfolio['weight'].sum()
        assert abs(weight_sum - 1.0) < 1e-6, f"가중치 합: {weight_sum}"

        # 모든 가중치가 양수
        assert (portfolio['weight'] > 0).all(), "모든 가중치는 양수여야 함"

        # 동일가중 확인
        expected_weight = 1.0 / len(portfolio)
        assert (np.isclose(portfolio['weight'].values, expected_weight)).all(), "가중치가 동일하지 않음"

        print(f"✓ 밸류 포트폴리오 검증 통과: {len(portfolio)}개 종목")


def test_value_quintile_basic(test_db):
    """밸류 퀸틸 진단 기본 테스트."""

    test_tickers = [f"TEST{i:02d}" for i in range(15)]

    base_date = date(2023, 6, 30)
    price_start = base_date - timedelta(days=300)
    price_end = base_date

    # 더미 데이터 생성
    for ticker in test_tickers + ["^GSPC"]:
        _insert_price_data(test_db, ticker, price_start, price_end)

    _insert_sp500_data(test_db, test_tickers, price_start)

    # 재무 데이터 추가
    for i, ticker in enumerate(test_tickers):
        revenue = 1e9 * (1 + i * 0.1)
        net_income = 1e8 * (1 + i * 0.05)
        equity = 5e8 * (1 + i * 0.15)
        _insert_financials_data(
            test_db,
            ticker,
            base_date - timedelta(days=90),
            base_date - timedelta(days=30),
            revenue=revenue,
            net_income=net_income,
            stockholders_equity=equity,
        )

    # 진단 실행
    diagnostics = diagnose_quintile(
        str(base_date - timedelta(days=100)),
        str(base_date),
        test_db,
    )

    # 검증
    assert isinstance(diagnostics, dict), "진단 결과는 딕셔너리여야 함"
    if diagnostics:
        assert "dates" in diagnostics, "dates 키 필요"
        assert "signals" in diagnostics, "signals 키 필요"
        print(f"✓ 밸류 진단 검증 통과: {len(diagnostics.get('dates', []))}개 시점")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
