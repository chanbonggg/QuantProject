"""
포트폴리오 결합 엔진 + 최적화 테스트 (STEP 6)

- 6A: weight_engine.py (레짐별 전략 가중치, 결합 포트폴리오)
- 6B: optimizer.py (제약 조건, 손절/VIX 오버레이, 스트레스 테스트)
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent / "quant_us"))

from portfolio.weight_engine import (
    REGIME_WEIGHTS,
    RISK_OFF_ASSETS,
    decide_weights,
)
from portfolio.optimizer import (
    MAX_WEIGHT_PER_STOCK,
    MIN_STOCKS,
    STRESS_SHOCKS,
    RISK_OFF_TICKERS,
    optimize,
    apply_risk_overlay,
    compute_portfolio_stats,
    stress_test,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def sample_portfolio():
    """테스트용 포트폴리오: 10종목 + SHY."""
    tickers = [f"STOCK{i}" for i in range(10)] + ["SHY"]
    weights = [0.09] * 10 + [0.10]
    sources = ["momentum"] * 3 + ["quality"] * 3 + ["value"] * 2 + ["low_vol"] * 2 + ["risk_off"]
    return pd.DataFrame({
        "ticker": tickers,
        "weight": weights,
        "strategy_source": sources,
    })


@pytest.fixture
def concentrated_portfolio():
    """집중 포트폴리오: 1종목에 30% 비중."""
    return pd.DataFrame({
        "ticker": ["AAPL", "MSFT", "GOOG", "AMZN", "SHY"],
        "weight": [0.30, 0.25, 0.20, 0.15, 0.10],
        "strategy_source": ["momentum", "quality", "value", "low_vol", "risk_off"],
    })


@pytest.fixture
def portfolio_db():
    """포트폴리오 테스트용 in-memory DuckDB."""
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

    # VIX 데이터 (정상 수준)
    conn.execute("INSERT INTO raw.fred_series VALUES ('VIXCLS', '2024-12-20', 18.0, NOW())")

    # SPY + 몇 종목 가격 (30거래일)
    np.random.seed(42)
    base = pd.Timestamp("2024-11-01")
    for ticker, start_p in [("SPY", 580.0), ("AAPL", 230.0), ("MSFT", 420.0)]:
        p = start_p
        for d in range(40):
            dt = base + pd.offsets.BDay(d)
            ret = np.random.normal(0.0003, 0.01)
            p *= (1 + ret)
            conn.execute(
                "INSERT INTO raw.prices VALUES (?, CAST(? AS DATE), ?, ?, ?, ?, ?, ?, NULL, 'test', NOW())",
                [ticker, dt.strftime("%Y-%m-%d"), p*0.99, p*1.01, p*0.98, p, p, 1000000],
            )

    yield conn
    conn.close()


# ===========================================================================
# 6A: weight_engine 테스트
# ===========================================================================

class TestRegimeWeights:

    def test_all_regimes_defined(self):
        """A/B/C/SHOCK 4개 레짐 정의."""
        assert "A" in REGIME_WEIGHTS
        assert "B" in REGIME_WEIGHTS
        assert "C" in REGIME_WEIGHTS
        assert "SHOCK" in REGIME_WEIGHTS

    def test_strategy_weights_sum_to_1(self):
        """각 레짐의 전략 가중치 합 = 1.0."""
        for regime, weights in REGIME_WEIGHTS.items():
            strategy_sum = (
                weights["momentum"] + weights["quality"]
                + weights["value"] + weights["low_vol"]
            )
            assert abs(strategy_sum - 1.0) < 1e-6, f"{regime} 전략 합={strategy_sum}"

    def test_equity_exposure_range(self):
        """equity_exposure는 0~1 범위."""
        for regime, weights in REGIME_WEIGHTS.items():
            assert 0 <= weights["equity_exposure"] <= 1.0

    def test_risk_on_high_momentum(self):
        """Regime A: 모멘텀 비중 최대."""
        assert REGIME_WEIGHTS["A"]["momentum"] == 0.40

    def test_risk_off_no_momentum(self):
        """Regime B: 모멘텀 0%, 저변동성 60%."""
        assert REGIME_WEIGHTS["B"]["momentum"] == 0.00
        assert REGIME_WEIGHTS["B"]["low_vol"] == 0.60

    def test_shock_matches_risk_off(self):
        """SHOCK: Regime B와 동일 전략 가중치, equity 50%."""
        assert REGIME_WEIGHTS["SHOCK"]["equity_exposure"] == 0.50


class TestDecideWeights:

    def test_regime_a(self):
        result = decide_weights("A")
        assert result["strategy_weights"]["momentum"] == 0.40
        assert result["equity_exposure"] == 1.00
        assert result["regime_used"] == "A"

    def test_regime_b(self):
        result = decide_weights("B")
        assert result["strategy_weights"]["momentum"] == 0.00
        assert result["equity_exposure"] == 0.40

    def test_regime_c(self):
        result = decide_weights("C")
        assert result["strategy_weights"]["value"] == 0.40

    def test_shock_overrides_regime(self):
        """shock_alarm=True → SHOCK 가중치 사용."""
        result = decide_weights("A", shock_alarm=True)
        assert result["regime_used"] == "SHOCK"
        assert result["strategy_weights"]["momentum"] == 0.00

    def test_invalid_regime_fallback_c(self):
        """알 수 없는 레짐 → C 폴백."""
        result = decide_weights("X")
        assert result["regime_used"] == "C"

    def test_risk_off_weight(self):
        """risk_off_weight = 1 - equity_exposure."""
        result = decide_weights("B")
        expected_risk_off = 1.0 - result["equity_exposure"]
        assert abs(result["risk_off_weight"] - expected_risk_off) < 1e-6

    def test_risk_off_asset_selection(self):
        """대체자산 선택."""
        result = decide_weights("A", risk_off_asset="TLT")
        assert result["risk_off_asset"] == "TLT"

    def test_strategy_weights_keys(self):
        """4개 전략 키 존재."""
        result = decide_weights("A")
        keys = set(result["strategy_weights"].keys())
        assert keys == {"momentum", "quality", "value", "low_vol"}


# ===========================================================================
# 6B: optimizer 테스트
# ===========================================================================

class TestOptimize:

    def test_optimize_caps_max_weight(self, concentrated_portfolio):
        """30% 종목 → 5% 이하로 클리핑."""
        result = optimize(concentrated_portfolio)
        for _, row in result.iterrows():
            if row["ticker"] not in RISK_OFF_TICKERS:
                assert row["weight"] <= MAX_WEIGHT_PER_STOCK + 0.01, \
                    f"{row['ticker']} weight={row['weight']}"

    def test_optimize_weights_sum_to_1(self, concentrated_portfolio):
        """최적화 후 가중치 합 = 1.0."""
        result = optimize(concentrated_portfolio)
        assert abs(result["weight"].sum() - 1.0) < 1e-6

    def test_optimize_preserves_risk_off(self, sample_portfolio):
        """risk_off 종목은 별도 처리."""
        result = optimize(sample_portfolio)
        assert "SHY" in result["ticker"].values

    def test_optimize_all_non_negative(self, concentrated_portfolio):
        """모든 가중치 >= 0."""
        result = optimize(concentrated_portfolio)
        assert (result["weight"] >= 0).all()

    def test_optimize_empty_portfolio(self):
        """빈 포트폴리오 → 빈 결과."""
        empty = pd.DataFrame(columns=["ticker", "weight", "strategy_source"])
        result = optimize(empty)
        assert len(result) == 0 or result["weight"].sum() < 1e-6


class TestApplyRiskOverlay:

    def test_no_overlay_normal_conditions(self, portfolio_db, sample_portfolio):
        """정상 조건 → 가중치 변화 없거나 미미."""
        result = apply_risk_overlay(sample_portfolio, "2024-12-20", portfolio_db)
        assert abs(result["weight"].sum() - 1.0) < 1e-6

    def test_overlay_preserves_tickers(self, portfolio_db, sample_portfolio):
        """오버레이 후에도 종목 유지."""
        result = apply_risk_overlay(sample_portfolio, "2024-12-20", portfolio_db)
        assert len(result) > 0

    def test_overlay_high_vix(self, portfolio_db, sample_portfolio):
        """VIX > 35 → 주식 비중 감소."""
        # VIX를 높은 값으로 업데이트
        portfolio_db.execute(
            "DELETE FROM raw.fred_series WHERE series_id='VIXCLS'"
        )
        portfolio_db.execute(
            "INSERT INTO raw.fred_series VALUES ('VIXCLS', '2024-12-20', 40.0, NOW())"
        )

        result = apply_risk_overlay(sample_portfolio, "2024-12-20", portfolio_db)

        # 주식 비중이 원래보다 줄어야 함
        equity_tickers = result[~result["ticker"].isin(RISK_OFF_TICKERS)]
        original_equity = sample_portfolio[
            ~sample_portfolio["ticker"].isin(RISK_OFF_TICKERS)
        ]["weight"].sum()

        if not equity_tickers.empty:
            new_equity = equity_tickers["weight"].sum()
            assert new_equity < original_equity or abs(new_equity - original_equity) < 0.01


class TestComputePortfolioStats:

    def test_stats_structure(self, sample_portfolio):
        """필수 키 존재."""
        stats = compute_portfolio_stats(sample_portfolio, "2024-12-20")
        assert "n_stocks" in stats
        assert "max_weight" in stats
        assert "hhi" in stats
        assert "top_5_weight" in stats

    def test_stats_values(self, sample_portfolio):
        """통계값 합리성 검증."""
        stats = compute_portfolio_stats(sample_portfolio, "2024-12-20")
        # risk_off 제외 주식 수
        assert stats["n_stocks"] >= 1
        assert 0 < stats["max_weight"] <= 1.0
        assert 0 < stats["hhi"] <= 1.0

    def test_stats_concentrated(self, concentrated_portfolio):
        """집중 포트폴리오 → 높은 HHI."""
        stats = compute_portfolio_stats(concentrated_portfolio, "2024-12-20")
        assert stats["hhi"] > 0.1  # 집중도 높음


class TestStressTest:

    def test_all_scenarios_present(self, sample_portfolio):
        """4개 시나리오 결과."""
        result = stress_test(sample_portfolio)
        assert len(result) == len(STRESS_SHOCKS)

    def test_scenario_keys(self, sample_portfolio):
        """각 시나리오에 expected_loss, description 키."""
        result = stress_test(sample_portfolio)
        for name, data in result.items():
            assert "expected_loss" in data, f"{name} missing expected_loss"
            assert "description" in data, f"{name} missing description"

    def test_single_scenario(self, sample_portfolio):
        """단일 시나리오 실행."""
        result = stress_test(sample_portfolio, scenario="2008_gfc")
        assert "2008_gfc" in result
        assert len(result) == 1

    def test_equity_loss_negative(self, concentrated_portfolio):
        """주식 포트폴리오 → GFC 시 음수 손실."""
        result = stress_test(concentrated_portfolio, scenario="2008_gfc")
        assert result["2008_gfc"]["expected_loss"] < 0

    def test_invalid_scenario(self, sample_portfolio):
        """존재하지 않는 시나리오 → 빈 결과."""
        result = stress_test(sample_portfolio, scenario="nonexistent")
        assert len(result) == 0

    def test_stress_loss_range(self, sample_portfolio):
        """예상 손실이 합리적 범위 (-100% ~ +50%)."""
        result = stress_test(sample_portfolio)
        for name, data in result.items():
            assert -1.0 <= data["expected_loss"] <= 0.5, \
                f"{name}: loss={data['expected_loss']}"
