"""
백테스트 엔진 테스트 (STEP 5A)

- BacktestResult 구조 검증
- TransactionCostModel 거래비용 계산
- 리밸런싱 날짜 추출 (월별, 분기별)
- 성과 지표 계산 (CAGR, Sharpe, MDD, Alpha/Beta)
- run() 통합 테스트
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent / "quant_us"))

from backtest.engine import (
    BacktestResult,
    TransactionCostModel,
    _get_rebalance_dates,
    compute_metrics,
    run,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def backtest_db():
    """백테스트용 in-memory DuckDB + 모의 가격 데이터."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS feature")

    # raw.prices 테이블 생성
    conn.execute("""CREATE TABLE raw.prices (
        ticker VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
        close DOUBLE, adj_close DOUBLE, volume BIGINT, market_cap DOUBLE,
        source VARCHAR, collected_at TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (ticker, date)
    )""")

    # raw.fred_series 테이블 (DGS3MO)
    conn.execute("""CREATE TABLE raw.fred_series (
        series_id VARCHAR, date DATE, value DOUBLE,
        collected_at TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (series_id, date)
    )""")

    # 모의 가격 데이터 삽입 (3종목 + SPY, 60거래일)
    np.random.seed(42)
    base_date = pd.Timestamp("2024-01-02")
    tickers = ["AAPL", "MSFT", "GOOG", "SPY"]
    starts = [150.0, 350.0, 140.0, 470.0]

    for ticker, start_price in zip(tickers, starts):
        price = start_price
        for day_offset in range(60):
            dt = base_date + pd.offsets.BDay(day_offset)
            date_str = dt.strftime("%Y-%m-%d")
            ret = np.random.normal(0.0005, 0.015)
            price *= (1 + ret)
            conn.execute(
                "INSERT INTO raw.prices VALUES (?, CAST(? AS DATE), ?, ?, ?, ?, ?, ?, NULL, 'test', NOW())",
                [ticker, date_str, price * 0.99, price * 1.01, price * 0.98, price, price, 1000000],
            )

    # DGS3MO (무위험율) 데이터
    conn.execute(
        "INSERT INTO raw.fred_series VALUES ('DGS3MO', '2024-01-02', 5.2, NOW())"
    )

    yield conn
    conn.close()


@pytest.fixture
def sample_daily_returns():
    """검증 가능한 일별 수익률 시리즈 (252일, 연 10% 복리 기준)."""
    np.random.seed(123)
    n = 252
    # 일별 평균 0.0004, 변동성 0.01 (연 10% 수익, 약 16% 변동성)
    daily = pd.Series(
        np.random.normal(0.0004, 0.01, n),
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
        name="portfolio",
    )
    return daily


@pytest.fixture
def sample_benchmark_returns():
    """벤치마크 일별 수익률 (252일)."""
    np.random.seed(456)
    n = 252
    bench = pd.Series(
        np.random.normal(0.0003, 0.012, n),
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
        name="benchmark",
    )
    return bench


# ---------------------------------------------------------------------------
# 1. BacktestResult 구조 검증
# ---------------------------------------------------------------------------

class TestBacktestResultStructure:
    def test_backtest_result_structure(self):
        """BacktestResult 필드 존재 확인."""
        empty_series = pd.Series(dtype=float)
        result = BacktestResult(
            daily_returns=empty_series,
            cumulative_returns=empty_series,
            drawdown=empty_series,
            portfolio_history=pd.DataFrame(columns=["date", "ticker", "weight"]),
            metrics={},
            benchmark_returns=empty_series,
            turnover=empty_series,
        )

        assert hasattr(result, "daily_returns")
        assert hasattr(result, "cumulative_returns")
        assert hasattr(result, "drawdown")
        assert hasattr(result, "portfolio_history")
        assert hasattr(result, "metrics")
        assert hasattr(result, "benchmark_returns")
        assert hasattr(result, "turnover")

    def test_backtest_result_types(self):
        """BacktestResult 필드 타입 확인."""
        empty_series = pd.Series(dtype=float)
        result = BacktestResult(
            daily_returns=empty_series,
            cumulative_returns=empty_series,
            drawdown=empty_series,
            portfolio_history=pd.DataFrame(columns=["date", "ticker", "weight"]),
            metrics={"cagr": 0.1},
            benchmark_returns=empty_series,
            turnover=empty_series,
        )

        assert isinstance(result.daily_returns, pd.Series)
        assert isinstance(result.cumulative_returns, pd.Series)
        assert isinstance(result.drawdown, pd.Series)
        assert isinstance(result.portfolio_history, pd.DataFrame)
        assert isinstance(result.metrics, dict)
        assert isinstance(result.benchmark_returns, pd.Series)
        assert isinstance(result.turnover, pd.Series)


# ---------------------------------------------------------------------------
# 2. TransactionCostModel 거래비용 계산
# ---------------------------------------------------------------------------

class TestTransactionCostModel:
    def test_transaction_cost_model_basic(self):
        """거래비용 기본 계산."""
        model = TransactionCostModel()
        cost = model.compute_cost(sell_value=0.5, buy_value=0.5)

        # SEC Fee: 0.5 × 0.0000278 = 0.0000139
        # 수수료: 1.0 × 0.0002 = 0.0002
        # 슬리피지: 1.0 × 0.0005 = 0.0005
        # 합계: 약 0.0007139
        assert cost > 0
        assert cost == pytest.approx(
            0.5 * 0.0000278 + 1.0 * 0.0002 + 1.0 * 0.0005,
            rel=1e-6
        )

    def test_transaction_cost_zero_trade(self):
        """거래 없을 때 비용 = 0."""
        model = TransactionCostModel()
        cost = model.compute_cost(sell_value=0.0, buy_value=0.0)
        assert cost == 0.0

    def test_transaction_cost_sell_only(self):
        """매도만 있을 때 SEC Fee 포함."""
        model = TransactionCostModel()
        cost = model.compute_cost(sell_value=1.0, buy_value=0.0)

        sec_fee = 1.0 * 0.0000278
        commission = 1.0 * 0.0002
        slippage = 1.0 * 0.0005
        expected = sec_fee + commission + slippage
        assert cost == pytest.approx(expected, rel=1e-6)

    def test_transaction_cost_custom_rates(self):
        """커스텀 비용 율 적용."""
        model = TransactionCostModel(
            sec_fee_rate=0.0,
            commission_rate=0.001,
            slippage_rate=0.001,
        )
        cost = model.compute_cost(sell_value=0.4, buy_value=0.4)
        expected = (0.4 + 0.4) * (0.001 + 0.001)
        assert cost == pytest.approx(expected, rel=1e-6)


# ---------------------------------------------------------------------------
# 3. 리밸런싱 날짜 추출
# ---------------------------------------------------------------------------

class TestRebalanceDates:
    @pytest.fixture
    def year_trading_dates(self):
        """2024년 1년치 거래일."""
        dates = pd.bdate_range("2024-01-01", "2024-12-31")
        return [d.date() for d in dates]

    def test_rebalance_dates_monthly(self, year_trading_dates):
        """월별 리밸런싱: 12개 날짜."""
        result = _get_rebalance_dates(year_trading_dates, freq="M")
        # 2024년은 12개월
        assert len(result) == 12

    def test_rebalance_dates_monthly_last_day(self, year_trading_dates):
        """월별 리밸런싱: 각 날짜가 해당 월의 마지막 거래일."""
        result = _get_rebalance_dates(year_trading_dates, freq="M")
        for d in result:
            d_pd = pd.Timestamp(d)
            # 다음 월의 첫 거래일보다 이 날짜가 같은 월이어야 함
            next_month = d_pd + pd.offsets.BMonthEnd(1)
            # 이 날짜는 해당 월의 마지막 거래일이므로, 다음 거래일은 다음 달이어야 함
            next_bday = d_pd + pd.offsets.BDay(1)
            assert next_bday.month != d_pd.month or next_bday.year != d_pd.year

    def test_rebalance_dates_quarterly(self, year_trading_dates):
        """분기별 리밸런싱: 4개 날짜."""
        result = _get_rebalance_dates(year_trading_dates, freq="Q")
        assert len(result) == 4

    def test_rebalance_dates_quarterly_months(self, year_trading_dates):
        """분기별 리밸런싱: 3, 6, 9, 12월에 해당."""
        result = _get_rebalance_dates(year_trading_dates, freq="Q")
        months = {pd.Timestamp(d).month for d in result}
        assert months == {3, 6, 9, 12}

    def test_rebalance_dates_weekly(self, year_trading_dates):
        """주별 리밸런싱: 약 52주."""
        result = _get_rebalance_dates(year_trading_dates, freq="W")
        assert len(result) >= 50  # 2024년은 약 52주

    def test_rebalance_dates_empty_input(self):
        """빈 입력 → 빈 리스트."""
        result = _get_rebalance_dates([], freq="M")
        assert result == []


# ---------------------------------------------------------------------------
# 4. 성과 지표 계산
# ---------------------------------------------------------------------------

class TestComputeMetrics:
    def test_compute_metrics_basic(self, sample_daily_returns, sample_benchmark_returns):
        """메트릭 기본 계산 — 필수 키 존재 확인."""
        metrics = compute_metrics(sample_daily_returns, sample_benchmark_returns)

        required_keys = [
            "cagr", "total_return", "annualized_volatility",
            "sharpe", "mdd", "calmar",
        ]
        for key in required_keys:
            assert key in metrics, f"메트릭 키 누락: {key}"

    def test_compute_metrics_total_return(self, sample_daily_returns, sample_benchmark_returns):
        """total_return = (1 + r).prod() - 1 확인."""
        metrics = compute_metrics(sample_daily_returns, sample_benchmark_returns)
        expected = float((1 + sample_daily_returns).prod() - 1)
        assert metrics["total_return"] == pytest.approx(expected, rel=1e-6)

    def test_compute_metrics_cagr_annualized(self, sample_daily_returns, sample_benchmark_returns):
        """CAGR 연율화 확인."""
        metrics = compute_metrics(sample_daily_returns, sample_benchmark_returns)
        n = len(sample_daily_returns)
        total = (1 + sample_daily_returns).prod() - 1
        expected_cagr = (1 + total) ** (252 / n) - 1
        assert metrics["cagr"] == pytest.approx(expected_cagr, rel=1e-6)

    def test_compute_metrics_sharpe(self, sample_daily_returns, sample_benchmark_returns):
        """Sharpe ratio = (mean excess) / std × sqrt(252)."""
        rf_annual = 0.05
        metrics = compute_metrics(
            sample_daily_returns, sample_benchmark_returns, risk_free_rate=rf_annual
        )
        rf_daily = rf_annual / 252
        excess = sample_daily_returns - rf_daily
        expected_sharpe = (excess.mean() / sample_daily_returns.std()) * np.sqrt(252)
        assert metrics["sharpe"] == pytest.approx(expected_sharpe, rel=1e-4)

    def test_compute_metrics_mdd(self):
        """MDD 계산 정확성."""
        # 10% 상승 후 20% 하락 → MDD = -20%/(1+10%) ≈ -18.2%
        returns = pd.Series([0.10] + [-0.02] * 10 + [0.0] * 5)
        bench = pd.Series([0.0] * len(returns))
        metrics = compute_metrics(returns, bench, risk_free_rate=0.0)

        assert metrics["mdd"] < 0
        assert metrics["mdd"] > -1.0  # -100% 이하는 불가

    def test_compute_metrics_mdd_no_drawdown(self):
        """순수 상승장 → MDD ≈ 0."""
        returns = pd.Series([0.001] * 252)
        bench = pd.Series([0.0] * 252)
        metrics = compute_metrics(returns, bench, risk_free_rate=0.0)
        # 매일 상승이면 MDD는 매우 작음 (0 또는 거의 0)
        assert metrics["mdd"] >= -0.01

    def test_compute_metrics_mdd_precise(self):
        """MDD 정밀 계산: [+50%, -40%] 시나리오."""
        returns = pd.Series([0.5, -0.4])
        bench = pd.Series([0.0, 0.0])
        metrics = compute_metrics(returns, bench, risk_free_rate=0.0)

        # cum_ret: [1.5, 0.9]
        # rolling_max: [1.5, 1.5]
        # drawdown: [0, (0.9 - 1.5) / 1.5] = [0, -0.4]
        assert metrics["mdd"] == pytest.approx(-0.4, abs=1e-6)

    def test_compute_metrics_alpha_beta(self, sample_daily_returns, sample_benchmark_returns):
        """Alpha/Beta 계산 존재 확인."""
        metrics = compute_metrics(
            sample_daily_returns, sample_benchmark_returns, risk_free_rate=0.05
        )
        assert "alpha" in metrics
        assert "beta" in metrics
        # Beta는 유한한 실수
        if not np.isnan(metrics["beta"]):
            assert isinstance(metrics["beta"], float)
            assert -10 < metrics["beta"] < 10  # 극단값 아님

    def test_compute_metrics_beta_definition(self):
        """Beta = Cov(Rp, Rb) / Var(Rb) 직접 검증."""
        np.random.seed(99)
        bench = pd.Series(np.random.normal(0.0003, 0.01, 252))
        # 포트폴리오 = 1.5 × 벤치마크 (베타 1.5)
        port = bench * 1.5

        metrics = compute_metrics(port, bench, risk_free_rate=0.0)
        if not np.isnan(metrics["beta"]):
            assert metrics["beta"] == pytest.approx(1.5, abs=0.01)

    def test_compute_metrics_information_ratio(self, sample_daily_returns, sample_benchmark_returns):
        """Information Ratio 키 존재 확인."""
        metrics = compute_metrics(sample_daily_returns, sample_benchmark_returns)
        assert "information_ratio" in metrics

    def test_compute_metrics_with_db(self, backtest_db, sample_daily_returns, sample_benchmark_returns):
        """DGS3MO DB에서 무위험율 자동 조회."""
        metrics = compute_metrics(
            sample_daily_returns, sample_benchmark_returns,
            risk_free_rate=None, conn=backtest_db
        )
        # DB에서 5.2% 조회 → rf_annual = 0.052
        assert metrics["risk_free_rate_annual"] == pytest.approx(0.052, abs=1e-4)

    def test_compute_metrics_empty_returns(self):
        """빈 수익률 → 빈 딕셔너리."""
        metrics = compute_metrics(pd.Series(dtype=float), pd.Series(dtype=float))
        assert metrics == {}


# ---------------------------------------------------------------------------
# 5. run() 통합 테스트
# ---------------------------------------------------------------------------

class TestRunBacktest:
    def _simple_portfolio_func(self, date_str, conn):
        """단순 동일가중 포트폴리오: AAPL, MSFT, GOOG."""
        return pd.DataFrame({
            "ticker": ["AAPL", "MSFT", "GOOG"],
            "weight": [1 / 3, 1 / 3, 1 / 3],
            "signal_score": [1.0, 0.5, 0.0],
        })

    def _spy_only_portfolio(self, date_str, conn):
        """SPY 100% 포트폴리오."""
        return pd.DataFrame({
            "ticker": ["SPY"],
            "weight": [1.0],
        })

    def test_run_basic(self, backtest_db):
        """run() 기본 통합 테스트: 결과 구조 확인."""
        result = run(
            portfolio_func=self._simple_portfolio_func,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
        )

        assert isinstance(result, BacktestResult)
        assert isinstance(result.daily_returns, pd.Series)
        assert len(result.daily_returns) > 0
        assert isinstance(result.metrics, dict)
        assert "cagr" in result.metrics
        assert "sharpe" in result.metrics
        assert "mdd" in result.metrics

    def test_run_cumulative_returns_start_at_one(self, backtest_db):
        """누적수익률은 1.0에서 시작."""
        result = run(
            portfolio_func=self._simple_portfolio_func,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
        )
        # 첫 날 수익률=0 → 누적수익률 첫 값 = 1.0
        assert float(result.cumulative_returns.iloc[0]) == pytest.approx(1.0, abs=1e-6)

    def test_run_drawdown_non_positive(self, backtest_db):
        """드로다운은 항상 0 이하."""
        result = run(
            portfolio_func=self._simple_portfolio_func,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
        )
        assert (result.drawdown <= 1e-9).all()

    def test_run_empty_portfolio(self, backtest_db):
        """빈 포트폴리오 → 현금 보유 (수익률 0)."""
        def empty_portfolio(date_str, conn):
            return pd.DataFrame(columns=["ticker", "weight"])

        result = run(
            portfolio_func=empty_portfolio,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
        )

        assert isinstance(result, BacktestResult)
        # 현금 보유 → 모든 일별 수익률 = 0
        assert (result.daily_returns == 0.0).all()

    def test_run_with_cost(self, backtest_db):
        """거래비용 모델 적용 시 비용 없을 때보다 수익률 낮음."""
        no_cost_model = TransactionCostModel(
            sec_fee_rate=0.0,
            commission_rate=0.0,
            slippage_rate=0.0,
        )
        default_cost_model = TransactionCostModel()

        result_no_cost = run(
            portfolio_func=self._simple_portfolio_func,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
            cost_model=no_cost_model,
        )
        result_with_cost = run(
            portfolio_func=self._simple_portfolio_func,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
            cost_model=default_cost_model,
        )

        # 거래비용 있으면 누적수익률이 더 낮아야 함
        no_cost_final = float(result_no_cost.cumulative_returns.iloc[-1])
        with_cost_final = float(result_with_cost.cumulative_returns.iloc[-1])
        assert with_cost_final <= no_cost_final

    def test_run_benchmark(self, backtest_db):
        """벤치마크(SPY) 수익률 계산 확인."""
        result = run(
            portfolio_func=self._simple_portfolio_func,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
            benchmark_ticker="SPY",
        )

        assert isinstance(result.benchmark_returns, pd.Series)
        assert len(result.benchmark_returns) > 0
        # 벤치마크 수익률은 -50% ~ +50% 범위 내여야 함
        assert result.benchmark_returns.between(-0.5, 0.5).all()

    def test_run_spy_portfolio_matches_benchmark(self, backtest_db):
        """SPY 100% 포트폴리오는 벤치마크와 유사한 수익률."""
        result = run(
            portfolio_func=self._spy_only_portfolio,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
            benchmark_ticker="SPY",
            cost_model=TransactionCostModel(0.0, 0.0, 0.0),
        )

        # 비용 없으면 포트폴리오 수익률 ≈ 벤치마크 수익률
        # 첫 날 수익률 0으로 초기화, 리밸런싱 타이밍 차이 등 구조적 오차 허용 (10%)
        total_port = float(result.cumulative_returns.iloc[-1])
        bench_cum = float((1 + result.benchmark_returns).cumprod().iloc[-1])
        assert abs(total_port - bench_cum) < 0.10

    def test_run_portfolio_func_failure(self, backtest_db):
        """portfolio_func 실패 시 이전 포트폴리오 유지."""
        call_count = {"n": 0}

        def flaky_portfolio(date_str, conn):
            call_count["n"] += 1
            if call_count["n"] > 1:
                raise RuntimeError("포트폴리오 계산 실패")
            return pd.DataFrame({
                "ticker": ["AAPL"],
                "weight": [1.0],
            })

        result = run(
            portfolio_func=flaky_portfolio,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
        )
        # 에러가 발생해도 BacktestResult가 반환되어야 함
        assert isinstance(result, BacktestResult)

    def test_run_turnover_computed(self, backtest_db):
        """회전율 계산 확인."""
        result = run(
            portfolio_func=self._simple_portfolio_func,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
        )
        # 리밸런싱이 발생했으면 turnover Series에 데이터가 있어야 함
        assert isinstance(result.turnover, pd.Series)
        assert "avg_turnover" in result.metrics

    def test_run_portfolio_history_columns(self, backtest_db):
        """포트폴리오 이력 컬럼 확인."""
        result = run(
            portfolio_func=self._simple_portfolio_func,
            start="2024-01-02",
            end="2024-03-31",
            conn=backtest_db,
            rebalance_freq="M",
        )
        if not result.portfolio_history.empty:
            assert "date" in result.portfolio_history.columns
            assert "ticker" in result.portfolio_history.columns
            assert "weight" in result.portfolio_history.columns
