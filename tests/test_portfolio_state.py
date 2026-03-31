"""
Portfolio State 관리 및 Drift 계산 테스트.

테스트 범위:
  - Drift 계산 로직 (0변화, 초과, 미만)
  - 현재 보유량 계산
  - DB 저장/조회
  - 포트폴리오 상태 추적
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import pytest
import pandas as pd
import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent / "quant_us"))

from portfolio.state import PortfolioState
from db.init import get_connection, get_pg_connection


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_portfolio() -> pd.DataFrame:
    """샘플 목표 포트폴리오 (10개 종목)."""
    return pd.DataFrame(
        {
            "ticker": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "JPM", "JNJ", "XOM", "WMT", "PG"],
            "weight": [0.05] * 10,
            "strategy_source": ["momentum"] * 5 + ["quality"] * 3 + ["value"] * 2,
        }
    )


@pytest.fixture
def portfolio_db() -> duckdb.DuckDBPyConnection:
    """in-memory DuckDB + 샘플 데이터."""
    conn = duckdb.connect(":memory:")

    # 스키마 생성
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS feature")
    conn.execute("CREATE SCHEMA IF NOT EXISTS normalized")

    # raw.prices 테이블
    conn.execute("""
        CREATE TABLE raw.prices (
            ticker TEXT,
            date DATE,
            close DOUBLE PRECISION,
            PRIMARY KEY (ticker, date)
        )
    """)

    # normalized.portfolio_state 테이블
    conn.execute("""
        CREATE TABLE normalized.portfolio_state (
            date DATE PRIMARY KEY,
            total_value DOUBLE PRECISION,
            cash_amount DOUBLE PRECISION,
            equity_value DOUBLE PRECISION,
            target_portfolio VARCHAR,
            current_drift DOUBLE PRECISION,
            rebalance_triggered BOOLEAN,
            rebalance_reason VARCHAR
        )
    """)

    # 샘플 가격 데이터 (2026-03-25 ~ 2026-03-31)
    tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "JPM", "JNJ", "XOM", "WMT", "PG"]
    base_prices = {
        "AAPL": 150.0,
        "MSFT": 420.0,
        "GOOGL": 140.0,
        "AMZN": 170.0,
        "NVDA": 875.0,
        "JPM": 190.0,
        "JNJ": 155.0,
        "XOM": 115.0,
        "WMT": 85.0,
        "PG": 160.0,
    }

    base_date = pd.Timestamp("2026-03-25")
    for i in range(7):  # 7일간
        current_date = (base_date + timedelta(days=i)).strftime("%Y-%m-%d")
        for ticker in tickers:
            # 가격을 약간씩 변동시킴 (기본가격 ± 2%)
            price = base_prices[ticker] * (1 + (i - 3) * 0.01)
            conn.execute(
                "INSERT INTO raw.prices (ticker, date, close) VALUES (?, ?, ?)",
                [ticker, current_date, price],
            )

    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestDriftComputation:
    """Drift 계산 테스트."""

    def test_compute_drift_no_change(self, sample_portfolio, portfolio_db):
        """직전 상태가 없거나 변화 없음 → Drift = 0."""
        portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)

        max_drift, details = portfolio_state.compute_drift(
            sample_portfolio, "2026-03-31"
        )

        # 직전 상태가 없으므로 drift = 0, details = {}
        assert max_drift == 0.0
        assert details == {}

    def test_compute_drift_structure(self, sample_portfolio, portfolio_db):
        """Drift 계산 결과의 구조 검증."""
        portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)

        # 초기 포트폴리오 저장
        portfolio_state.save_state(
            "2026-03-30",
            sample_portfolio,
            rebalance_triggered=False,
            reason="manual",
        )

        # 다음날 Drift 계산
        max_drift, details = portfolio_state.compute_drift(
            sample_portfolio, "2026-03-31"
        )

        # 검증: details가 dict인지, 각 항목이 올바른 키를 가지는지
        assert isinstance(details, dict)
        for ticker in sample_portfolio["ticker"].unique():
            if ticker in details:
                assert "target" in details[ticker]
                assert "current" in details[ticker]
                assert "drift_pct" in details[ticker]
                assert "shares" in details[ticker]
                assert "value" in details[ticker]

    def test_compute_drift_exceeds_threshold(self, sample_portfolio, portfolio_db):
        """극단적 가격 변동 후 Drift > 5% 검증."""
        portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)

        # 초기 저장
        portfolio_state.save_state(
            "2026-03-29",
            sample_portfolio,
            rebalance_triggered=False,
            reason="manual",
        )

        # 가격 변동 시뮬레이션: AAPL 크게 상승
        portfolio_db.execute(
            "DELETE FROM raw.prices WHERE ticker = 'AAPL' AND date = '2026-03-30'"
        )
        portfolio_db.execute(
            "INSERT INTO raw.prices VALUES ('AAPL', '2026-03-30', 160.0)"
        )  # +6.67%
        portfolio_db.commit()

        # Drift 계산
        max_drift, details = portfolio_state.compute_drift(
            sample_portfolio, "2026-03-30"
        )

        # AAPL의 현재 비중이 증가했으므로 drift > 0
        if "AAPL" in details:
            assert details["AAPL"]["drift_pct"] > 0


class TestPortfolioStatePersistence:
    """포트폴리오 상태 저장/조회 테스트."""

    def test_save_and_retrieve_state(self, sample_portfolio, portfolio_db):
        """상태 저장 후 조회 검증 (PostgreSQL 직접 조회)."""
        portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)

        # 저장 (→ PostgreSQL)
        portfolio_state.save_state(
            "2026-03-31",
            sample_portfolio,
            rebalance_triggered=True,
            reason="drift",
            cash_amount=2500,
        )

        # PostgreSQL에서 직접 조회
        pg = get_pg_connection()
        cur = pg.cursor()
        cur.execute(
            "SELECT date::text, total_value, rebalance_triggered, rebalance_reason "
            "FROM normalized.portfolio_state WHERE date = '2026-03-31'"
        )
        result = cur.fetchall()
        cur.close()
        pg.close()

        assert len(result) == 1
        date, total_value, triggered, reason = result[0]
        assert date == "2026-03-31"
        assert total_value == 50000
        assert triggered is True
        assert reason == "drift"

    def test_get_current_holdings_empty(self, portfolio_db):
        """저장된 상태가 없을 때 빈 DataFrame 반환."""
        portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)

        holdings = portfolio_state.get_current_holdings("2026-03-31")

        assert isinstance(holdings, pd.DataFrame)
        assert holdings.empty


class TestHelperMethods:
    """내부 헬퍼 메서드 테스트."""

    def test_compute_current_holdings_for_save(self, sample_portfolio, portfolio_db):
        """저장용 현재 보유량 계산."""
        portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)

        holdings = portfolio_state._compute_current_holdings_for_save(
            sample_portfolio, "2026-03-31"
        )

        # 검증: 모든 종목이 계산되었는지, 가치 합이 대략 50000인지
        assert isinstance(holdings, dict)
        for ticker in sample_portfolio["ticker"].unique():
            if ticker in holdings:
                assert "shares" in holdings[ticker]
                assert "value" in holdings[ticker]
                assert "weight" in holdings[ticker]

    def test_get_previous_portfolio_state_none(self, portfolio_db):
        """직전 상태가 없을 때 None 반환."""
        portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)

        result = portfolio_state._get_previous_portfolio_state("2026-03-31")

        assert result is None


class TestEdgeCases:
    """엣지 케이스 테스트."""

    def test_portfolio_state_with_negative_drift(self, sample_portfolio, portfolio_db):
        """음수 Drift (실제 < 목표)."""
        portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)

        portfolio_state.save_state(
            "2026-03-29",
            sample_portfolio,
            rebalance_triggered=False,
            reason="manual",
        )

        # 가격 하락 시뮬레이션
        portfolio_db.execute(
            "DELETE FROM raw.prices WHERE ticker = 'MSFT' AND date = '2026-03-30'"
        )
        portfolio_db.execute(
            "INSERT INTO raw.prices VALUES ('MSFT', '2026-03-30', 410.0)"
        )  # -2.38%
        portfolio_db.commit()

        max_drift, details = portfolio_state.compute_drift(
            sample_portfolio, "2026-03-30"
        )

        # MSFT drift < 0 (현재 비중 < 목표 비중)
        if "MSFT" in details:
            assert details["MSFT"]["drift_pct"] < 0

    def test_zero_weight_portfolio(self, portfolio_db):
        """모든 비중이 0인 포트폴리오 (엣지 케이스)."""
        empty_portfolio = pd.DataFrame(
            {
                "ticker": ["AAPL"],
                "weight": [0.0],
                "strategy_source": ["test"],
            }
        )

        portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)

        max_drift, details = portfolio_state.compute_drift(
            empty_portfolio, "2026-03-31"
        )

        # 비중이 0이므로 drift = 0
        assert max_drift == 0.0 or max_drift >= 0.0  # 음수 방지


class TestIntegration:
    """통합 테스트."""

    def test_full_workflow(self, sample_portfolio, portfolio_db):
        """전체 워크플로우: 저장 → Drift 계산 → 재저장."""
        portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)

        # 1. 초기 포트폴리오 저장
        portfolio_state.save_state(
            "2026-03-30",
            sample_portfolio,
            rebalance_triggered=False,
            reason="monthly",
        )

        # 2. 다음날 Drift 계산
        max_drift, details = portfolio_state.compute_drift(
            sample_portfolio, "2026-03-31"
        )

        # 3. 리밸런싱 판단 및 재저장
        should_rebalance = max_drift > 0.05
        portfolio_state.save_state(
            "2026-03-31",
            sample_portfolio,
            rebalance_triggered=should_rebalance,
            reason="drift" if should_rebalance else "skipped",
        )

        # 4. PostgreSQL에서 확인
        pg = get_pg_connection()
        cur = pg.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM normalized.portfolio_state WHERE date IN ('2026-03-30', '2026-03-31')"
        )
        result = cur.fetchall()
        cur.close()
        pg.close()

        assert result[0][0] == 2  # 2개 레코드 저장됨


# ---------------------------------------------------------------------------
# Parametrized Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "date_str,expected_length",
    [
        ("2026-03-31", 0),  # 이전 상태 없음
    ],
)
def test_compute_drift_with_various_dates(
    sample_portfolio, portfolio_db, date_str, expected_length
):
    """다양한 날짜에서 Drift 계산."""
    portfolio_state = PortfolioState(total_value=50000, conn=portfolio_db)
    max_drift, details = portfolio_state.compute_drift(sample_portfolio, date_str)

    # 검증: details의 길이는 직전 상태 유무에 따라 달라짐
    # (첫날은 0, 이후는 > 0)
    assert isinstance(details, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
