"""
전략 단위 테스트

test_strategies.py:
- test_quality_signal: 퀄리티 신호 기본 동작
- test_quality_winsorize: 부채비율 윈저라이징
- test_quality_portfolio: 포트폴리오 구성
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import pandas as pd
import numpy as np
from unittest.mock import Mock, patch, MagicMock

from strategies.quality import (
    compute_signal,
    get_portfolio,
    _calculate_quality_metrics,
    _winsorize_value,
    _zscore_normalize,
    _get_all_recent_financials,
)
from db.init import get_connection


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_conn():
    """Mock DuckDB 커넥션."""
    return MagicMock()


@pytest.fixture
def sample_financials():
    """샘플 재무 데이터."""
    return [
        {
            "ticker": "AAPL",
            "cik": "0000320193",
            "filing_type": "10-Q",
            "period_of_report": "2024-03-31",
            "filed_date": "2024-05-03",
            "revenue": 90.7e9,
            "net_income": 25.4e9,
            "eps_diluted": 1.64,
            "total_assets": 345.1e9,
            "stockholders_equity": 63.1e9,
            "total_liabilities": 282.0e9,
            "operating_cashflow": 28.0e9,
            "cost_of_goods_sold": 46.2e9,
        },
    ]


@pytest.fixture
def sample_universe():
    """샘플 유니버스."""
    return ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]


# ── Test: Winsorize ────────────────────────────────────────────────────────

def test_quality_winsorize_normal():
    """정상 값: 윈저라이징 적용 안 함."""
    value = 1.5  # 150%
    assert _winsorize_value(value) == 1.5


def test_quality_winsorize_extreme():
    """극값: 500% 이상 제한."""
    value = 6.0  # 600%
    assert _winsorize_value(value) == 5.0


def test_quality_winsorize_nan():
    """NaN 처리."""
    value = np.nan
    assert pd.isna(_winsorize_value(value))


def test_quality_winsorize_boundary():
    """경계값: 500% 정확히."""
    value = 5.0
    assert _winsorize_value(value) == 5.0


# ── Test: Z-score Normalize ────────────────────────────────────────────────

def test_zscore_normalize_basic():
    """기본 정규화."""
    data = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = _zscore_normalize(data)

    # 평균 = 3, 표준편차 = sqrt(2)
    assert abs(result.mean()) < 1e-10  # 평균 ≈ 0
    assert abs(result.std() - 1.0) < 0.01  # 표준편차 ≈ 1


def test_zscore_normalize_with_nan():
    """NaN 포함 정규화."""
    data = pd.Series([1.0, 2.0, np.nan, 4.0, 5.0])
    result = _zscore_normalize(data)

    # 유효한 값만 사용: [1, 2, 4, 5] 평균 3
    assert len(result) == 5  # 인덱스는 유지
    assert abs(result.dropna().mean()) < 1e-10


def test_zscore_normalize_zero_std():
    """표준편차 0: 상수 시리즈."""
    data = pd.Series([2.0, 2.0, 2.0, 2.0])
    result = _zscore_normalize(data)

    # 모두 같은 값이므로 표준편차 = 0
    assert result.iloc[0] == 0.0


def test_zscore_normalize_empty():
    """비어있거나 유효한 값 없음."""
    data = pd.Series([np.nan, np.nan])
    result = _zscore_normalize(data)

    assert result.isna().all()


# ── Test: Calculate Quality Metrics ────────────────────────────────────────

def test_calculate_quality_metrics_basic(mock_conn, sample_financials):
    """기본 퀄리티 메트릭 계산."""
    f = sample_financials[0]

    with patch(
        "strategies.quality._get_all_recent_financials",
        return_value=sample_financials,
    ):
        roe, debt_ratio, eps_vol = _calculate_quality_metrics(
            "AAPL", "2024-05-05", mock_conn
        )

    # ROE = 25.4B / 63.1B ≈ 0.403
    assert abs(roe - (25.4e9 / 63.1e9)) < 0.01

    # 부채비율 = 282.0B / 63.1B ≈ 4.47
    assert abs(debt_ratio - (282.0e9 / 63.1e9)) < 0.01

    # EPS 변동성 = 단일 데이터 = NaN
    assert pd.isna(eps_vol)


def test_calculate_quality_metrics_no_data(mock_conn):
    """재무 데이터 없음."""
    with patch(
        "strategies.quality._get_all_recent_financials",
        return_value=[],
    ):
        roe, debt_ratio, eps_vol = _calculate_quality_metrics(
            "XXXX", "2024-05-05", mock_conn
        )

    assert roe is None
    assert debt_ratio is None
    assert eps_vol is None


def test_calculate_quality_metrics_zero_equity(mock_conn):
    """자기자본 0: division by zero."""
    bad_financials = [
        {
            "ticker": "TEST",
            "net_income": 1e9,
            "stockholders_equity": 0,  # 0!
            "total_liabilities": 1e9,
            "eps_diluted": 1.0,
        }
    ]

    with patch(
        "strategies.quality._get_all_recent_financials",
        return_value=bad_financials,
    ):
        roe, debt_ratio, eps_vol = _calculate_quality_metrics(
            "TEST", "2024-05-05", mock_conn
        )

    # 0으로 나누는 경우 None 반환
    assert roe is None
    assert debt_ratio is None


def test_calculate_quality_metrics_eps_volatility(mock_conn):
    """EPS 변동성 계산."""
    multi_financials = [
        {"eps_diluted": 1.0},
        {"eps_diluted": 1.1},
        {"eps_diluted": 0.9},
        {"eps_diluted": 1.2},
        {"net_income": None, "stockholders_equity": None, "total_liabilities": None},
    ]

    with patch(
        "strategies.quality._get_all_recent_financials",
        return_value=multi_financials,
    ):
        roe, debt_ratio, eps_vol = _calculate_quality_metrics(
            "TEST", "2024-05-05", mock_conn
        )

    # eps_vol = std([1.0, 1.1, 0.9, 1.2]) ≈ 0.118
    assert abs(eps_vol - np.std([1.0, 1.1, 0.9, 1.2])) < 0.01


# ── Test: Compute Signal ────────────────────────────────────────────────────

def test_compute_signal_basic(mock_conn):
    """기본 신호 계산."""
    universe = ["AAPL", "MSFT"]

    mock_metrics = [
        (0.4, 2.0, 0.1),  # AAPL: ROE, debt, eps_vol
        (0.35, 1.8, 0.12),  # MSFT
    ]

    with patch(
        "strategies.quality._calculate_quality_metrics",
        side_effect=mock_metrics,
    ):
        signal = compute_signal("2024-05-05", universe, mock_conn)

    assert len(signal) == 2
    assert "AAPL" in signal.index
    assert "MSFT" in signal.index

    # 신호 = z(ROE) - z(debt) - z(eps_vol)
    # ROE: [0.4, 0.35] → zscore
    # debt: [2.0, 1.8] → zscore
    # eps_vol: [0.1, 0.12] → zscore
    # 계산 검증 (정확한 값은 정규화 후이므로 생략)
    assert not signal.isna().all()


def test_compute_signal_partial_nan(mock_conn):
    """일부 NaN: 유효한 데이터만 사용."""
    universe = ["AAPL", "MSFT", "GOOGL"]

    mock_metrics = [
        (0.4, 2.0, 0.1),  # AAPL: 유효
        (None, None, None),  # MSFT: NaN
        (0.35, 1.8, 0.12),  # GOOGL: 유효
    ]

    with patch(
        "strategies.quality._calculate_quality_metrics",
        side_effect=mock_metrics,
    ):
        signal = compute_signal("2024-05-05", universe, mock_conn)

    # MSFT는 제외, AAPL, GOOGL만 포함
    assert len(signal) == 2
    assert "MSFT" not in signal.index


def test_compute_signal_all_nan(mock_conn):
    """모두 NaN: 빈 Series 반환."""
    universe = ["XXXX", "YYYY"]

    with patch(
        "strategies.quality._calculate_quality_metrics",
        return_value=(None, None, None),
    ):
        signal = compute_signal("2024-05-05", universe, mock_conn)

    assert signal.empty


# ── Test: Get Portfolio ────────────────────────────────────────────────────

def test_get_portfolio_basic(mock_conn):
    """기본 포트폴리오 구성."""
    mock_signal = pd.Series(
        [0.5, 0.3, 0.7, 0.2, 0.6],
        index=["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"],
    )

    with patch("strategies.quality._get_universe") as mock_universe, \
         patch("strategies.quality.compute_signal", return_value=mock_signal) as mock_sig:

        mock_universe.return_value = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]

        portfolio = get_portfolio("2024-05-05", mock_conn)

    # 상위 20% = 1개 종목 (5의 20%)
    assert len(portfolio) == 1
    assert portfolio.iloc[0]["ticker"] == "GOOGL"  # 신호 최고
    assert abs(portfolio.iloc[0]["weight"] - 1.0) < 0.01  # 동일가중 = 100%


def test_get_portfolio_equal_weight(mock_conn):
    """동일가중 검증."""
    mock_signal = pd.Series(
        [0.5] * 10,
        index=[f"TICK{i}" for i in range(10)],
    )

    with patch("strategies.quality._get_universe") as mock_universe, \
         patch("strategies.quality.compute_signal", return_value=mock_signal):

        tickers = [f"TICK{i}" for i in range(10)]
        mock_universe.return_value = tickers

        portfolio = get_portfolio("2024-05-05", mock_conn)

    # 상위 20% = 2개 (10의 20%)
    assert len(portfolio) == 2

    # 동일가중 = 1/2 = 0.5
    assert all(abs(w - 0.5) < 0.01 for w in portfolio["weight"])


def test_get_portfolio_empty_universe(mock_conn):
    """빈 유니버스: 빈 DataFrame 반환."""
    with patch("strategies.quality._get_universe", return_value=[]):
        portfolio = get_portfolio("2024-05-05", mock_conn)

    assert portfolio.empty


def test_get_portfolio_columns(mock_conn):
    """포트폴리오 컬럼 검증."""
    mock_signal = pd.Series([0.5, 0.3], index=["AAPL", "MSFT"])

    with patch("strategies.quality._get_universe") as mock_universe, \
         patch("strategies.quality.compute_signal", return_value=mock_signal):

        mock_universe.return_value = ["AAPL", "MSFT"]

        portfolio = get_portfolio("2024-05-05", mock_conn)

    assert "ticker" in portfolio.columns
    assert "weight" in portfolio.columns
    assert "signal_score" in portfolio.columns


# ── Test: Integration ──────────────────────────────────────────────────────

def test_quality_strategy_end_to_end(mock_conn):
    """End-to-end: 신호 → 포트폴리오."""
    universe = ["A", "B", "C", "D", "E"]

    mock_metrics = [
        (0.4, 2.0, 0.1),
        (0.35, 1.8, 0.12),
        (0.45, 1.9, 0.09),
        (0.30, 2.2, 0.15),
        (0.50, 1.7, 0.08),
    ]

    with patch("strategies.quality._get_universe", return_value=universe), \
         patch("strategies.quality._calculate_quality_metrics",
               side_effect=mock_metrics):

        # 신호 계산
        signal = compute_signal("2024-05-05", universe, mock_conn)
        assert not signal.empty

        # 포트폴리오 구성
        portfolio = get_portfolio("2024-05-05", mock_conn)

        # 상위 20% = 1개
        assert len(portfolio) == 1
        assert portfolio["weight"].sum() == pytest.approx(1.0)


# ── CLI/Manual Test ────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
