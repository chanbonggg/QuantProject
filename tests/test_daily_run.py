"""
일일 파이프라인 테스트 (STEP 7B)

- _is_rebalance_date: 월말 영업일 판단
- _retry_with_backoff: 재시도 로직
- _send_slack_alert: Slack 웹훅 미설정 스킵
- _check_data_quality: 데이터 품질 체크
- run_pipeline(dry_run=True): dry-run 모드
- run_pipeline 결과 구조 검증
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent / "quant_us"))

from scripts.daily_run import (
    _is_rebalance_date,
    _retry_with_backoff,
    _send_slack_alert,
    _check_data_quality,
    _make_step_result,
    run_pipeline,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pipeline_db():
    """일일 파이프라인 테스트용 in-memory DuckDB."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS feature")

    # raw.prices
    conn.execute("""
        CREATE TABLE raw.prices (
            ticker VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, adj_close DOUBLE, volume BIGINT, market_cap DOUBLE,
            source VARCHAR, collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (ticker, date)
        )
    """)

    # raw.fred_series
    conn.execute("""
        CREATE TABLE raw.fred_series (
            series_id VARCHAR, date DATE, value DOUBLE,
            collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (series_id, date)
        )
    """)

    # raw.sec_financials
    conn.execute("""
        CREATE TABLE raw.sec_financials (
            ticker VARCHAR, cik VARCHAR, filing_type VARCHAR,
            period_of_report DATE, filed_date DATE,
            revenue DOUBLE, net_income DOUBLE, eps_diluted DOUBLE,
            total_assets DOUBLE, stockholders_equity DOUBLE,
            total_liabilities DOUBLE, operating_cashflow DOUBLE,
            cost_of_goods_sold DOUBLE,
            collected_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # feature.regime_features
    conn.execute("""
        CREATE TABLE feature.regime_features (
            date DATE PRIMARY KEY,
            vix DOUBLE, vix3m DOUBLE, vxmt DOUBLE,
            vix_term DOUBLE, rv20 DOUBLE, rv60 DOUBLE,
            ma200_gap DOUBLE, r12m DOUBLE, r1m DOUBLE,
            avg_corr20 DOUBLE, hy_spread DOUBLE, ig_spread DOUBLE,
            term_spread DOUBLE,
            computed_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # feature.regime_labels
    conn.execute("""
        CREATE TABLE feature.regime_labels (
            date DATE PRIMARY KEY,
            regime VARCHAR,
            shock_alarm BOOLEAN,
            computed_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # 모의 가격 데이터 삽입 (3종목 + SPY, 30거래일)
    np.random.seed(42)
    base_date = pd.Timestamp("2024-11-01")
    tickers = ["AAPL", "MSFT", "SPY"]
    for ticker in tickers:
        price = 150.0
        for i in range(30):
            trade_date = base_date + pd.offsets.BDay(i)
            price *= (1 + np.random.normal(0.0003, 0.01))
            conn.execute(
                """
                INSERT OR IGNORE INTO raw.prices
                (ticker, date, open, high, low, close, adj_close, volume, market_cap, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [ticker, trade_date.date(), price, price * 1.01, price * 0.99,
                 price, price, 1000000, price * 1e6, "test"],
            )

    # FRED: VIX 데이터
    for i in range(30):
        trade_date = base_date + pd.offsets.BDay(i)
        conn.execute(
            "INSERT OR IGNORE INTO raw.fred_series (series_id, date, value) VALUES (?, ?, ?)",
            ["VIXCLS", trade_date.date(), 18.0 + np.random.normal(0, 1)],
        )

    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 테스트: _is_rebalance_date
# ---------------------------------------------------------------------------

class TestIsRebalanceDate:
    def test_month_end_business_day(self):
        """월말 영업일이면 True."""
        # 2024-11-29 (금요일) 가 11월 마지막 영업일
        assert _is_rebalance_date("2024-11-29") is True

    def test_month_end_friday_december(self):
        """2024-12-31 (화요일)이 12월 마지막 영업일."""
        assert _is_rebalance_date("2024-12-31") is True

    def test_not_month_end(self):
        """월중 날짜는 False."""
        assert _is_rebalance_date("2024-11-15") is False

    def test_first_of_month(self):
        """월초는 False."""
        assert _is_rebalance_date("2024-11-01") is False

    def test_second_to_last_business_day(self):
        """마지막 영업일 바로 전날은 False."""
        # 2024-11-28 (목요일)은 마지막 영업일이 아님
        assert _is_rebalance_date("2024-11-28") is False


# ---------------------------------------------------------------------------
# 테스트: _retry_with_backoff
# ---------------------------------------------------------------------------

class TestRetryWithBackoff:
    def test_success_on_first_attempt(self):
        """첫 시도에 성공."""
        mock_func = MagicMock(return_value=42)
        result = _retry_with_backoff(mock_func, "arg1", max_retries=3, base_delay=0.01)
        assert result == 42
        assert mock_func.call_count == 1

    def test_success_on_second_attempt(self):
        """첫 시도 실패, 두 번째 성공."""
        mock_func = MagicMock(side_effect=[ValueError("실패"), 99])
        result = _retry_with_backoff(mock_func, max_retries=3, base_delay=0.01)
        assert result == 99
        assert mock_func.call_count == 2

    def test_failure_after_max_retries(self):
        """max_retries 모두 실패 시 예외 발생."""
        mock_func = MagicMock(side_effect=ValueError("항상 실패"))
        with pytest.raises(ValueError, match="항상 실패"):
            _retry_with_backoff(mock_func, max_retries=3, base_delay=0.01)
        assert mock_func.call_count == 3

    def test_passes_args_and_kwargs(self):
        """args, kwargs가 함수에 전달됨."""
        mock_func = MagicMock(return_value="ok")
        _retry_with_backoff(mock_func, "pos_arg", max_retries=2, base_delay=0.01, key="val")
        mock_func.assert_called_once_with("pos_arg", key="val")


# ---------------------------------------------------------------------------
# 테스트: _send_slack_alert
# ---------------------------------------------------------------------------

class TestSendSlackAlert:
    def test_no_webhook_returns_false(self):
        """SLACK_WEBHOOK_URL 미설정 시 False 반환."""
        with patch("scripts.daily_run.SLACK_WEBHOOK_URL", ""):
            result = _send_slack_alert("테스트 메시지", level="INFO")
        assert result is False

    def test_with_webhook_calls_requests(self):
        """웹훅 URL 있으면 requests.post 호출."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("scripts.daily_run.SLACK_WEBHOOK_URL", "https://hooks.slack.com/test"), \
             patch("scripts.daily_run.requests.post", return_value=mock_resp) as mock_post:
            result = _send_slack_alert("테스트", level="WARNING")
        assert result is True
        mock_post.assert_called_once()

    def test_request_exception_returns_false(self):
        """requests 예외 발생 시 False 반환."""
        with patch("scripts.daily_run.SLACK_WEBHOOK_URL", "https://hooks.slack.com/test"), \
             patch("scripts.daily_run.requests.post", side_effect=ConnectionError("연결 실패")):
            result = _send_slack_alert("테스트", level="ERROR")
        assert result is False


# ---------------------------------------------------------------------------
# 테스트: _check_data_quality
# ---------------------------------------------------------------------------

class TestCheckDataQuality:
    def test_data_exists(self, pipeline_db):
        """당일 가격 데이터 있으면 has_data=True."""
        # 2024-11-01 데이터가 fixture에 삽입됨
        result = _check_data_quality("2024-11-01", pipeline_db)
        assert result["has_data"] is True
        assert result["price_rows"] > 0
        assert result["missing_rate"] <= 1.0

    def test_no_data_returns_false(self, pipeline_db):
        """데이터 없는 날짜면 has_data=False."""
        result = _check_data_quality("2020-01-01", pipeline_db)
        assert result["has_data"] is False
        assert result["price_rows"] == 0
        assert result["missing_rate"] == 1.0

    def test_missing_rate_calculation(self, pipeline_db):
        """adj_close NULL이 있으면 결측률 계산."""
        # NULL adj_close 데이터 삽입
        pipeline_db.execute("""
            INSERT INTO raw.prices
            (ticker, date, open, high, low, close, adj_close, volume, market_cap, source)
            VALUES ('TEST', '2024-11-01', 100, 101, 99, 100, NULL, 1000, 1000000, 'test')
        """)
        result = _check_data_quality("2024-11-01", pipeline_db)
        assert result["missing_rate"] > 0.0


# ---------------------------------------------------------------------------
# 테스트: run_pipeline (dry-run 모드)
# ---------------------------------------------------------------------------

class TestPipelineDryRun:
    def test_dry_run_skips_collection_steps(self, pipeline_db):
        """dry-run 모드: 수집 단계(주가/SEC/FRED)가 skipped 상태."""
        # regime.features, regime.model 등 실제 모듈이 없을 수 있으므로 모킹
        with patch("scripts.daily_run._FEATURES_AVAILABLE", False), \
             patch("scripts.daily_run._REGIME_MODEL_AVAILABLE", False), \
             patch("scripts.daily_run._SHOCK_ALARM_AVAILABLE", False), \
             patch("scripts.daily_run._WEIGHT_ENGINE_AVAILABLE", False), \
             patch("scripts.daily_run._OPTIMIZER_AVAILABLE", False):
            result = run_pipeline(date="2024-11-01", conn=pipeline_db, dry_run=True)

        step_map = {s["name"]: s for s in result["steps"]}

        # 수집 단계는 skipped
        assert step_map["주가수집"]["status"] == "skipped"
        assert step_map["SEC수집"]["status"] == "skipped"
        assert step_map["FRED수집"]["status"] == "skipped"

    def test_dry_run_runs_analysis_steps(self, pipeline_db):
        """dry-run 모드: 분석 단계(데이터품질 등)는 실행."""
        with patch("scripts.daily_run._FEATURES_AVAILABLE", False), \
             patch("scripts.daily_run._REGIME_MODEL_AVAILABLE", False), \
             patch("scripts.daily_run._SHOCK_ALARM_AVAILABLE", False), \
             patch("scripts.daily_run._WEIGHT_ENGINE_AVAILABLE", False), \
             patch("scripts.daily_run._OPTIMIZER_AVAILABLE", False):
            result = run_pipeline(date="2024-11-01", conn=pipeline_db, dry_run=True)

        step_map = {s["name"]: s for s in result["steps"]}
        # 데이터 품질 체크는 실행됨
        assert "데이터품질" in step_map


# ---------------------------------------------------------------------------
# 테스트: run_pipeline 결과 구조
# ---------------------------------------------------------------------------

class TestPipelineStepResults:
    def test_result_has_required_keys(self, pipeline_db):
        """파이프라인 결과에 필수 키가 모두 존재."""
        with patch("scripts.daily_run._FEATURES_AVAILABLE", False), \
             patch("scripts.daily_run._REGIME_MODEL_AVAILABLE", False), \
             patch("scripts.daily_run._SHOCK_ALARM_AVAILABLE", False), \
             patch("scripts.daily_run._WEIGHT_ENGINE_AVAILABLE", False), \
             patch("scripts.daily_run._OPTIMIZER_AVAILABLE", False):
            result = run_pipeline(date="2024-11-01", conn=pipeline_db, dry_run=True)

        assert "date" in result
        assert "steps" in result
        assert "alerts" in result
        assert "portfolio" in result

    def test_result_date_matches_input(self, pipeline_db):
        """결과의 date가 입력 날짜와 일치."""
        with patch("scripts.daily_run._FEATURES_AVAILABLE", False), \
             patch("scripts.daily_run._REGIME_MODEL_AVAILABLE", False), \
             patch("scripts.daily_run._SHOCK_ALARM_AVAILABLE", False), \
             patch("scripts.daily_run._WEIGHT_ENGINE_AVAILABLE", False), \
             patch("scripts.daily_run._OPTIMIZER_AVAILABLE", False):
            result = run_pipeline(date="2024-11-15", conn=pipeline_db, dry_run=True)

        assert result["date"] == "2024-11-15"

    def test_each_step_has_required_fields(self, pipeline_db):
        """각 단계 결과에 name, status, detail 필드 존재."""
        with patch("scripts.daily_run._FEATURES_AVAILABLE", False), \
             patch("scripts.daily_run._REGIME_MODEL_AVAILABLE", False), \
             patch("scripts.daily_run._SHOCK_ALARM_AVAILABLE", False), \
             patch("scripts.daily_run._WEIGHT_ENGINE_AVAILABLE", False), \
             patch("scripts.daily_run._OPTIMIZER_AVAILABLE", False):
            result = run_pipeline(date="2024-11-01", conn=pipeline_db, dry_run=True)

        for step in result["steps"]:
            assert "name" in step
            assert "status" in step
            assert "detail" in step
            assert step["status"] in ("success", "error", "skipped")

    def test_steps_list_is_not_empty(self, pipeline_db):
        """파이프라인 실행 후 단계 목록이 비어있지 않음."""
        with patch("scripts.daily_run._FEATURES_AVAILABLE", False), \
             patch("scripts.daily_run._REGIME_MODEL_AVAILABLE", False), \
             patch("scripts.daily_run._SHOCK_ALARM_AVAILABLE", False), \
             patch("scripts.daily_run._WEIGHT_ENGINE_AVAILABLE", False), \
             patch("scripts.daily_run._OPTIMIZER_AVAILABLE", False):
            result = run_pipeline(date="2024-11-01", conn=pipeline_db, dry_run=True)

        assert len(result["steps"]) >= 8  # 최소 8개 단계

    def test_alerts_is_list(self, pipeline_db):
        """alerts 필드는 항상 리스트."""
        with patch("scripts.daily_run._FEATURES_AVAILABLE", False), \
             patch("scripts.daily_run._REGIME_MODEL_AVAILABLE", False), \
             patch("scripts.daily_run._SHOCK_ALARM_AVAILABLE", False), \
             patch("scripts.daily_run._WEIGHT_ENGINE_AVAILABLE", False), \
             patch("scripts.daily_run._OPTIMIZER_AVAILABLE", False):
            result = run_pipeline(date="2024-11-01", conn=pipeline_db, dry_run=True)

        assert isinstance(result["alerts"], list)

    def test_log_saved_to_db(self, pipeline_db):
        """9단계 로그 저장: PostgreSQL에 쓰기, 결과 딕셔너리에 steps 포함."""
        mock_pg = MagicMock()
        mock_cur = MagicMock()
        mock_pg.cursor.return_value = mock_cur

        with patch("scripts.daily_run._FEATURES_AVAILABLE", False), \
             patch("scripts.daily_run._REGIME_MODEL_AVAILABLE", False), \
             patch("scripts.daily_run._SHOCK_ALARM_AVAILABLE", False), \
             patch("scripts.daily_run._WEIGHT_ENGINE_AVAILABLE", False), \
             patch("scripts.daily_run._OPTIMIZER_AVAILABLE", False), \
             patch("scripts.daily_run.get_pg_connection", return_value=mock_pg):
            result = run_pipeline(date="2024-11-01", conn=pipeline_db, dry_run=True)

        # pipeline_log INSERT가 PostgreSQL에 호출됐는지 확인
        assert mock_cur.execute.called
        # 결과에 steps 포함 확인
        assert "steps" in result
        assert len(result["steps"]) > 0

    def test_non_rebalance_date_skips_portfolio(self, pipeline_db):
        """리밸런싱일 아닌 날은 전략 신호 + 포트폴리오 단계가 skipped."""
        with patch("scripts.daily_run._FEATURES_AVAILABLE", False), \
             patch("scripts.daily_run._REGIME_MODEL_AVAILABLE", False), \
             patch("scripts.daily_run._SHOCK_ALARM_AVAILABLE", False), \
             patch("scripts.daily_run._WEIGHT_ENGINE_AVAILABLE", False), \
             patch("scripts.daily_run._OPTIMIZER_AVAILABLE", False):
            # 2024-11-15는 리밸런싱일 아님
            result = run_pipeline(date="2024-11-15", conn=pipeline_db, dry_run=True)

        step_map = {s["name"]: s for s in result["steps"]}
        assert step_map["전략신호"]["status"] == "skipped"
        assert step_map["포트폴리오산출"]["status"] == "skipped"
