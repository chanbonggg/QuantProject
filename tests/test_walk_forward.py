"""
Walk-Forward Analysis 테스트 (STEP 5B)

- OOS 구간 생성
- DSR (Deflated Sharpe Ratio)
- PBO (Probability of Backtest Overfitting)
- 레짐별 성과 분해
- 스트레스 테스트
- run_wfa 통합 테스트
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent / "quant_us"))

from backtest.walk_forward import (
    WFAResult,
    STRESS_SCENARIOS,
    _generate_oos_periods,
    _compute_regime_performance,
    compute_dsr,
    compute_pbo,
    run_stress_tests,
    run_wfa,
)
from backtest.engine import BacktestResult


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def wfa_db():
    """WFA 테스트용 in-memory DuckDB + 2년치 모의 데이터."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS feature")

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
        CREATE TABLE feature.regime_labels (
            date DATE PRIMARY KEY, regime VARCHAR,
            shock_alarm BOOLEAN, computed_at TIMESTAMP DEFAULT current_timestamp
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

    # 모의 가격 (SPY + AAPL + MSFT), 500거래일
    np.random.seed(42)
    base = pd.Timestamp("2022-01-03")
    tickers = {"SPY": 450.0, "AAPL": 170.0, "MSFT": 320.0}

    for ticker, start_price in tickers.items():
        price = start_price
        for d in range(500):
            dt = base + pd.offsets.BDay(d)
            ret = np.random.normal(0.0003, 0.012)
            price *= (1 + ret)
            conn.execute(
                "INSERT INTO raw.prices VALUES (?, CAST(? AS DATE), ?, ?, ?, ?, ?, ?, NULL, 'test', NOW())",
                [ticker, dt.strftime("%Y-%m-%d"), price*0.99, price*1.01, price*0.98, price, price, 1000000],
            )

    # DGS3MO
    conn.execute("INSERT INTO raw.fred_series VALUES ('DGS3MO', '2022-01-03', 4.5, NOW())")

    # 레짐 라벨 (랜덤 A/B/C)
    regimes = ["A", "B", "C"]
    for d in range(500):
        dt = base + pd.offsets.BDay(d)
        r = regimes[d % 3]  # 순환 할당
        conn.execute(
            "INSERT INTO feature.regime_labels VALUES (CAST(? AS DATE), ?, FALSE, NOW())",
            [dt.strftime("%Y-%m-%d"), r],
        )

    yield conn
    conn.close()


def _simple_portfolio(date_str, conn):
    """테스트용 간단 포트폴리오: AAPL 50%, MSFT 50%."""
    return pd.DataFrame({
        "ticker": ["AAPL", "MSFT"],
        "weight": [0.5, 0.5],
    })


# ===========================================================================
# OOS 구간 생성 테스트
# ===========================================================================

class TestGenerateOOSPeriods:

    def test_basic_generation(self):
        """20년 데이터 + 10년 학습 → 여러 OOS 구간."""
        periods = _generate_oos_periods("2000-01-01", "2024-12-31", train_years=10, oos_months=6)
        assert len(periods) >= 10

    def test_period_structure(self):
        """각 구간에 is_start, is_end, oos_start, oos_end 키."""
        periods = _generate_oos_periods("2000-01-01", "2024-12-31")
        for p in periods:
            assert "is_start" in p
            assert "is_end" in p
            assert "oos_start" in p
            assert "oos_end" in p

    def test_oos_start_after_train(self):
        """OOS 시작 >= data_start + train_years."""
        periods = _generate_oos_periods("2005-01-01", "2024-12-31", train_years=10)
        if periods:
            first_oos = pd.Timestamp(periods[0]["oos_start"])
            expected_min = pd.Timestamp("2015-01-01")
            assert first_oos >= expected_min

    def test_short_data_fewer_periods(self):
        """짧은 데이터 → 적은 OOS 구간."""
        periods = _generate_oos_periods("2020-01-01", "2022-12-31", train_years=2, oos_months=6)
        assert len(periods) >= 1
        assert len(periods) <= 3

    def test_very_short_data(self):
        """데이터 < 학습 기간 → 0개."""
        periods = _generate_oos_periods("2020-01-01", "2022-12-31", train_years=10)
        assert len(periods) == 0

    def test_oos_non_overlapping(self):
        """OOS 구간 비중첩."""
        periods = _generate_oos_periods("2000-01-01", "2024-12-31")
        for i in range(1, len(periods)):
            prev_end = pd.Timestamp(periods[i-1]["oos_end"])
            curr_start = pd.Timestamp(periods[i]["oos_start"])
            assert curr_start > prev_end


# ===========================================================================
# DSR 테스트
# ===========================================================================

class TestComputeDSR:

    def test_single_trial(self):
        """N=1 → DSR > 0.5 (양수 Sharpe 시)."""
        dsr = compute_dsr(1.5, num_trials=1, num_returns=252)
        assert 0 < dsr <= 1.0

    def test_many_trials_lower_dsr(self):
        """N=100 → DSR 감소 (낮은 Sharpe에서 차이 명확)."""
        dsr_1 = compute_dsr(0.2, num_trials=1, num_returns=252)
        dsr_100 = compute_dsr(0.2, num_trials=100, num_returns=252)
        assert dsr_100 < dsr_1

    def test_negative_sharpe(self):
        """음수 Sharpe → 낮은 DSR."""
        dsr = compute_dsr(-0.5, num_trials=10, num_returns=252)
        assert dsr < 0.5

    def test_high_sharpe_high_dsr(self):
        """높은 Sharpe + 적은 시행 → 높은 DSR."""
        dsr = compute_dsr(3.0, num_trials=1, num_returns=1000)
        assert dsr > 0.9

    def test_dsr_range(self):
        """DSR은 0~1 범위."""
        for sr in [-2, -1, 0, 0.5, 1.0, 2.0, 3.0]:
            for n in [1, 5, 10, 50, 100]:
                dsr = compute_dsr(sr, num_trials=n, num_returns=252)
                assert 0 <= dsr <= 1.0, f"SR={sr}, N={n}, DSR={dsr}"

    def test_zero_trials(self):
        """N=0 → DSR=0."""
        dsr = compute_dsr(1.0, num_trials=0)
        assert dsr == 0.0


# ===========================================================================
# PBO 테스트
# ===========================================================================

class TestComputePBO:

    def test_single_strategy_returns_none(self):
        """단일 전략 → None."""
        df = pd.DataFrame({"strat1": np.random.normal(0, 0.01, 100)})
        assert compute_pbo(df) is None

    def test_basic_pbo(self):
        """2개 전략 → 0~1 범위."""
        np.random.seed(42)
        df = pd.DataFrame({
            "strat1": np.random.normal(0.001, 0.01, 200),
            "strat2": np.random.normal(0.0005, 0.01, 200),
        })
        pbo = compute_pbo(df, n_partitions=6)
        assert pbo is not None
        assert 0 <= pbo <= 1.0

    def test_pbo_none_for_empty(self):
        """빈 DataFrame → None."""
        assert compute_pbo(pd.DataFrame()) is None
        assert compute_pbo(None) is None

    def test_pbo_many_strategies(self):
        """5개 전략."""
        np.random.seed(42)
        data = {f"s{i}": np.random.normal(0.001 * i, 0.01, 300) for i in range(5)}
        pbo = compute_pbo(pd.DataFrame(data), n_partitions=10)
        assert pbo is not None
        assert 0 <= pbo <= 1.0


# ===========================================================================
# 레짐별 성과 테스트
# ===========================================================================

class TestRegimePerformance:

    def test_regime_structure(self, wfa_db):
        """A/B/C 키 존재."""
        np.random.seed(42)
        dates = pd.date_range("2022-01-03", periods=100, freq="B")
        rets = pd.Series(np.random.normal(0.001, 0.01, 100), index=dates)
        bench = pd.Series(np.random.normal(0.0005, 0.01, 100), index=dates)

        result = _compute_regime_performance(
            rets, bench, "2022-01-03", "2022-06-01", wfa_db,
        )
        assert "A" in result
        assert "B" in result
        assert "C" in result

    def test_regime_has_required_fields(self, wfa_db):
        """각 레짐에 return, sharpe, n_days, ratio 키."""
        dates = pd.date_range("2022-01-03", periods=50, freq="B")
        rets = pd.Series(np.random.normal(0.001, 0.01, 50), index=dates)
        bench = pd.Series(np.zeros(50), index=dates)

        result = _compute_regime_performance(
            rets, bench, "2022-01-03", "2022-04-01", wfa_db,
        )
        for regime in ["A", "B", "C"]:
            assert "return" in result[regime]
            assert "sharpe" in result[regime]
            assert "n_days" in result[regime]
            assert "ratio" in result[regime]

    def test_regime_days_sum(self, wfa_db):
        """레짐별 일수 합 <= 전체 일수 (라벨 없는 날은 제외될 수 있음)."""
        dates = pd.date_range("2022-01-03", periods=100, freq="B")
        rets = pd.Series(np.random.normal(0, 0.01, 100), index=dates)
        bench = pd.Series(np.zeros(100), index=dates)

        result = _compute_regime_performance(
            rets, bench, "2022-01-03", "2022-06-01", wfa_db,
        )
        total_regime_days = sum(result[r]["n_days"] for r in ["A", "B", "C"])
        assert total_regime_days <= 100


# ===========================================================================
# 스트레스 시나리오 테스트
# ===========================================================================

class TestStressScenarios:

    def test_scenario_keys(self):
        """3개 시나리오 존재."""
        assert "2008_gfc" in STRESS_SCENARIOS
        assert "2020_covid" in STRESS_SCENARIOS
        assert "2022_rate_hike" in STRESS_SCENARIOS

    def test_scenario_dates_format(self):
        """각 시나리오에 start, end 키."""
        for name, period in STRESS_SCENARIOS.items():
            assert "start" in period
            assert "end" in period
            pd.Timestamp(period["start"])  # 유효한 날짜인지
            pd.Timestamp(period["end"])


# ===========================================================================
# run_wfa 통합 테스트
# ===========================================================================

class TestRunWFA:

    def test_run_wfa_basic(self, wfa_db):
        """기본 실행 + WFAResult 구조."""
        result = run_wfa(
            _simple_portfolio,
            data_start="2022-01-03",
            data_end="2023-12-29",
            conn=wfa_db,
            train_years=1,  # 짧은 학습 기간 (테스트용)
            oos_months=3,
            min_oos_periods=2,
        )
        assert isinstance(result, WFAResult)
        assert isinstance(result.oos_results, list)
        assert isinstance(result.aggregate_metrics, dict)
        assert result.dsr is not None

    def test_run_wfa_oos_count(self, wfa_db):
        """OOS 구간 수 확인."""
        result = run_wfa(
            _simple_portfolio,
            data_start="2022-01-03",
            data_end="2023-12-29",
            conn=wfa_db,
            train_years=1,
            oos_months=3,
        )
        assert len(result.oos_results) >= 1

    def test_run_wfa_aggregate_metrics_keys(self, wfa_db):
        """합산 메트릭에 핵심 키 존재."""
        result = run_wfa(
            _simple_portfolio,
            data_start="2022-01-03",
            data_end="2023-12-29",
            conn=wfa_db,
            train_years=1,
            oos_months=3,
        )
        if result.aggregate_metrics:
            assert "cagr" in result.aggregate_metrics
            assert "sharpe" in result.aggregate_metrics
            assert "mdd" in result.aggregate_metrics

    def test_run_wfa_regime_performance(self, wfa_db):
        """레짐별 성과 딕셔너리 존재."""
        result = run_wfa(
            _simple_portfolio,
            data_start="2022-01-03",
            data_end="2023-12-29",
            conn=wfa_db,
            train_years=1,
            oos_months=3,
        )
        assert "A" in result.regime_performance
        assert "B" in result.regime_performance
        assert "C" in result.regime_performance

    def test_run_wfa_no_data(self, wfa_db):
        """데이터 부족 시에도 에러 없이 반환."""
        result = run_wfa(
            _simple_portfolio,
            data_start="2022-01-03",
            data_end="2023-12-29",
            conn=wfa_db,
            train_years=10,  # 2년 데이터에 10년 학습 → OOS 0개
            oos_months=6,
        )
        assert isinstance(result, WFAResult)
        assert len(result.oos_results) == 0

    def test_run_wfa_oos_results_structure(self, wfa_db):
        """각 OOS 결과에 필수 키 존재."""
        result = run_wfa(
            _simple_portfolio,
            data_start="2022-01-03",
            data_end="2023-12-29",
            conn=wfa_db,
            train_years=1,
            oos_months=3,
        )
        for oos in result.oos_results:
            assert "oos_start" in oos
            assert "oos_end" in oos
            assert "metrics" in oos
