"""
레짐 판단 시스템 통합 테스트

모듈별 테스트:
  - TestFeatures*: regime/features.py (피처 산출)
  - TestRuleBasedRegime, TestRegimeHysteresis, TestRegime2020Covid, TestPredictProba: regime/model.py
  - TestShockAlarm*: regime/shock_alarm.py (급변 알람)
"""

import sys
from pathlib import Path
from datetime import date

import pytest
import numpy as np
import pandas as pd
import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent / "quant_us"))

from regime.features import (
    FEATURE_COLUMNS,
    FRED_SERIES_MAP,
    _compute_rv,
    _compute_ma200_gap,
    _compute_cumret,
    compute_features,
)
from regime.model import (
    RegimeState,
    _rule_based_regime,
    _compute_percentiles,
    predict,
    predict_proba,
)
from regime.shock_alarm import (
    SAFETY_MODE_WEIGHTS,
    _check_vix_spike,
    _check_vix_backwardation,
    _check_correlation_shock,
    _check_credit_shock,
    _check_yield_curve_extreme,
    _compute_severity,
    check_alarm,
)


# ===========================================================================
# 공통 Fixtures
# ===========================================================================

@pytest.fixture
def regime_db():
    """레짐 테스트용 in-memory DuckDB."""
    conn = duckdb.connect(":memory:")

    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS feature")

    # raw.fred_series
    conn.execute("""
        CREATE TABLE raw.fred_series (
            series_id    VARCHAR,
            date         DATE,
            value        DOUBLE,
            collected_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (series_id, date)
        )
    """)

    # feature.regime_features
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

    # feature.regime_labels
    conn.execute("""
        CREATE TABLE feature.regime_labels (
            date        DATE PRIMARY KEY,
            regime      VARCHAR,
            shock_alarm BOOLEAN,
            raw_regime  VARCHAR,
            computed_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # raw.prices
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

    yield conn
    conn.close()


@pytest.fixture
def regime_db_with_fred(regime_db):
    """FRED 시계열 데이터가 삽입된 DB."""
    conn = regime_db

    # VIX 데이터 삽입 (정상 구간: 15 전후)
    vix_normal = [(f"2023-{m:02d}-01", 15.0 + m * 0.5) for m in range(1, 13)]
    for d, v in vix_normal:
        conn.execute(
            "INSERT INTO raw.fred_series VALUES ('VIXCLS', CAST(? AS DATE), ?, NOW())",
            [d, v],
        )

    # HY 스프레드 데이터 삽입 (정상 구간: 300~400 bps)
    for m in range(1, 13):
        conn.execute(
            "INSERT INTO raw.fred_series VALUES ('BAMLH0A0HYM2', CAST(? AS DATE), ?, NOW())",
            [f"2023-{m:02d}-01", 350.0 + m * 5],
        )

    # rv20 피처 삽입 (정상 구간: 0.10~0.20)
    for m in range(1, 13):
        conn.execute(
            """
            INSERT INTO feature.regime_features
                (date, vix, rv20, ma200_gap, vix_term, r12m, hy_spread, term_spread)
            VALUES (CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?)
            """,
            [f"2023-{m:02d}-01", 15.0, 0.12 + m * 0.005, 0.05, 1.1, 0.08, 350.0, 1.5],
        )

    return conn


# ===========================================================================
# 4A: 피처 산출 테스트 (regime/features.py)
# ===========================================================================

class TestFeaturesConstants:
    """피처 상수 및 구조 테스트."""

    def test_feature_columns_count(self):
        """FEATURE_COLUMNS = 13개."""
        assert len(FEATURE_COLUMNS) == 13

    def test_feature_columns_includes_vxmt(self):
        """vxmt가 FEATURE_COLUMNS에 포함."""
        assert "vxmt" in FEATURE_COLUMNS

    def test_fred_series_map_count(self):
        """FRED 시리즈 매핑 5개."""
        assert len(FRED_SERIES_MAP) == 5

    def test_fred_series_map_keys(self):
        """FRED 시리즈 매핑 키: vix, vix3m, hy_spread, ig_spread, term_spread."""
        expected = {"vix", "vix3m", "hy_spread", "ig_spread", "term_spread"}
        assert set(FRED_SERIES_MAP.keys()) == expected


class TestFeaturesComputeHelpers:
    """피처 산출 내부 헬퍼 테스트."""

    def test_compute_rv_normal(self):
        """정상 가격 → rv20 양수."""
        prices = pd.Series(
            np.random.lognormal(0, 0.01, 25),
            index=pd.date_range("2024-01-01", periods=25),
        )
        rv = _compute_rv(prices, window=20)
        assert rv is not None
        assert rv > 0

    def test_compute_rv_insufficient_data(self):
        """데이터 부족 → None."""
        prices = pd.Series([100, 101, 102])
        assert _compute_rv(prices, window=20) is None

    def test_compute_ma200_gap_above(self):
        """현재가 > MA200 → 양수."""
        # 200일간 100, 마지막 날 120
        prices_data = [100.0] * 199 + [120.0]
        prices = pd.Series(prices_data, index=pd.date_range("2023-01-01", periods=200))
        gap = _compute_ma200_gap(prices)
        assert gap is not None
        assert gap > 0

    def test_compute_ma200_gap_below(self):
        """현재가 < MA200 → 음수."""
        prices_data = [100.0] * 199 + [80.0]
        prices = pd.Series(prices_data, index=pd.date_range("2023-01-01", periods=200))
        gap = _compute_ma200_gap(prices)
        assert gap is not None
        assert gap < 0

    def test_compute_ma200_gap_insufficient_data(self):
        """200일 미만 → None."""
        prices = pd.Series([100.0] * 50)
        assert _compute_ma200_gap(prices) is None

    def test_compute_cumret_positive(self):
        """상승 → 양수 수익률."""
        prices = pd.Series(
            [100.0] + [100.0 + i for i in range(25)],
            index=pd.date_range("2024-01-01", periods=26),
        )
        ret = _compute_cumret(prices, window=21)
        assert ret is not None
        assert ret > 0

    def test_compute_cumret_insufficient(self):
        """데이터 부족 → None."""
        prices = pd.Series([100, 101, 102])
        assert _compute_cumret(prices, window=21) is None


class TestFeaturesComputeDB:
    """DB 기반 compute_features 테스트."""

    def test_compute_features_with_fred(self, regime_db):
        """FRED 데이터 있으면 vix, hy_spread 등 반환."""
        conn = regime_db

        # FRED 데이터 삽입
        conn.execute(
            "INSERT INTO raw.fred_series VALUES ('VIXCLS', '2024-01-02', 14.5, NOW())"
        )
        conn.execute(
            "INSERT INTO raw.fred_series VALUES ('BAMLH0A0HYM2', '2024-01-02', 3.5, NOW())"
        )
        conn.execute(
            "INSERT INTO raw.fred_series VALUES ('BAMLC0A0CM', '2024-01-02', 1.2, NOW())"
        )
        conn.execute(
            "INSERT INTO raw.fred_series VALUES ('T10Y2Y', '2024-01-02', -0.3, NOW())"
        )

        result = compute_features("2024-01-02", conn)

        assert isinstance(result, pd.Series)
        assert len(result) == 13
        assert result["vix"] == pytest.approx(14.5)
        assert result["hy_spread"] == pytest.approx(3.5)
        assert result["term_spread"] == pytest.approx(-0.3)

    def test_compute_features_vxmt_always_nan(self, regime_db):
        """vxmt는 항상 NaN."""
        result = compute_features("2024-01-02", regime_db)
        assert pd.isna(result["vxmt"])

    def test_compute_features_vix_term_calculation(self, regime_db):
        """vix3m / vix 정확 계산."""
        conn = regime_db
        conn.execute(
            "INSERT INTO raw.fred_series VALUES ('VIXCLS', '2024-01-02', 20.0, NOW())"
        )
        conn.execute(
            "INSERT INTO raw.fred_series VALUES ('VXVCLS', '2024-01-02', 22.0, NOW())"
        )

        result = compute_features("2024-01-02", conn)
        assert result["vix_term"] == pytest.approx(22.0 / 20.0, abs=0.01)

    def test_compute_features_no_data(self, regime_db):
        """데이터 없는 날짜 → 대부분 NaN."""
        result = compute_features("2099-01-01", regime_db)
        assert isinstance(result, pd.Series)
        assert len(result) == 13
        # FRED 데이터 없으므로 vix는 NaN
        assert pd.isna(result["vix"])

    def test_compute_features_saves_to_db(self, regime_db):
        """compute_features 결과가 DB에 저장됨."""
        conn = regime_db
        conn.execute(
            "INSERT INTO raw.fred_series VALUES ('VIXCLS', '2024-06-15', 18.0, NOW())"
        )

        compute_features("2024-06-15", conn)

        row = conn.execute(
            "SELECT vix FROM feature.regime_features WHERE date = '2024-06-15'"
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(18.0)


# ===========================================================================
# 4B: 레짐 판단 모델 테스트 (regime/model.py)
# ===========================================================================

class TestRuleBasedRegime:
    """_rule_based_regime 단위 테스트."""

    def _make_features(self, **kwargs) -> pd.Series:
        defaults = {
            "vix": 18.0, "vix3m": 19.0, "vxmt": 20.0, "vix_term": 1.05,
            "rv20": 0.12, "rv60": 0.14, "ma200_gap": 0.03, "r12m": 0.10,
            "r1m": 0.01, "avg_corr20": 0.25, "hy_spread": 350.0,
            "ig_spread": 120.0, "term_spread": 1.5,
        }
        defaults.update(kwargs)
        return pd.Series(defaults)

    def test_regime_b_all_conditions(self):
        """B 조건 4개 모두 충족 → 'B'."""
        features = self._make_features(
            vix=30.0, rv20=0.90, ma200_gap=-0.10, vix_term=0.85,
        )
        percentiles = {"rv20": {80: 0.20}, "hy_spread": {60: 450.0}}
        assert _rule_based_regime(features, percentiles) == "B"

    def test_regime_a_all_conditions(self):
        """A 조건 4개 모두 충족 → 'A'."""
        features = self._make_features(
            vix=16.0, ma200_gap=0.05, r12m=0.12, hy_spread=300.0,
        )
        percentiles = {"rv20": {80: 0.25}, "hy_spread": {60: 400.0}}
        assert _rule_based_regime(features, percentiles) == "A"

    def test_regime_c_fallback(self):
        """A/B 모두 미충족 → 'C'."""
        features = self._make_features(vix=22.0, ma200_gap=0.01, r12m=0.05, hy_spread=350.0)
        percentiles = {"rv20": {80: 0.25}, "hy_spread": {60: 400.0}}
        assert _rule_based_regime(features, percentiles) == "C"

    def test_regime_b_partial_condition_falls_to_c(self):
        """B 조건 일부만 충족 → B 아님."""
        features = self._make_features(
            vix=30.0, rv20=0.90, ma200_gap=-0.10, vix_term=1.05,
        )
        percentiles = {"rv20": {80: 0.20}, "hy_spread": {60: 450.0}}
        assert _rule_based_regime(features, percentiles) != "B"

    def test_nan_features_fallback_c(self):
        """NaN 피처 → Regime C."""
        features = pd.Series({
            "vix": float("nan"), "rv20": float("nan"), "ma200_gap": float("nan"),
            "vix_term": float("nan"), "r12m": float("nan"), "hy_spread": float("nan"),
        })
        assert _rule_based_regime(features, {}) == "C"


class TestRegimeHysteresis:
    """RegimeState 히스테리시스 동작 테스트."""

    def test_no_transition_after_1_day(self):
        """1일만 다른 레짐 → 전환 안됨."""
        state = RegimeState(initial_regime="C")
        result = state.update("A", "2024-01-02")
        assert result == "C"
        assert state.consecutive_days == 1

    def test_transition_after_2_days_from_c(self):
        """C→A: 2일 연속 → 전환."""
        state = RegimeState(initial_regime="C")
        state.update("A", "2024-01-02")
        result = state.update("A", "2024-01-03")
        assert result == "A"

    def test_no_transition_after_2_days_from_a(self):
        """A→C: 2일 연속은 부족 (EXIT_THRESHOLD=3)."""
        state = RegimeState(initial_regime="A")
        state.update("C", "2024-01-02")
        result = state.update("C", "2024-01-03")
        assert result == "A"

    def test_transition_after_3_days_from_a(self):
        """A→C: 3일 연속 → 전환."""
        state = RegimeState(initial_regime="A")
        state.update("C", "2024-01-02")
        state.update("C", "2024-01-03")
        result = state.update("C", "2024-01-04")
        assert result == "C"

    def test_candidate_reset_on_regime_change(self):
        """후보 교체 시 연속 일수 초기화."""
        state = RegimeState(initial_regime="C")
        state.update("A", "2024-01-02")
        state.update("B", "2024-01-03")
        assert state.candidate_regime == "B"
        assert state.consecutive_days == 1
        assert state.current_regime == "C"

    def test_serialization(self):
        """to_dict / from_dict 직렬화."""
        state = RegimeState(initial_regime="A")
        state.candidate_regime = "B"
        state.consecutive_days = 2

        d = state.to_dict()
        restored = RegimeState.from_dict(d)

        assert restored.current_regime == "A"
        assert restored.candidate_regime == "B"
        assert restored.consecutive_days == 2

    def test_same_regime_resets_consecutive(self):
        """현재 레짐과 동일 → 후보 초기화."""
        state = RegimeState(initial_regime="C")
        state.update("A", "2024-01-02")
        result = state.update("C", "2024-01-03")
        assert result == "C"
        assert state.consecutive_days == 0


class TestRegime2020Covid:
    """코로나 폭락 모의: VIX=82, rv20 극단 → Regime B."""

    def _covid_features(self) -> pd.Series:
        return pd.Series({
            "vix": 82.69, "vix3m": 65.0, "vxmt": 50.0, "vix_term": 0.85,
            "rv20": 0.90, "rv60": 0.60, "ma200_gap": -0.15, "r12m": -0.30,
            "r1m": -0.20, "avg_corr20": 0.85, "hy_spread": 10.87,
            "ig_spread": 3.50, "term_spread": 0.50,
        })

    def test_covid_regime_b_rule_based(self):
        """코로나 모의 → 규칙 기반 Regime B."""
        features = self._covid_features()
        percentiles = {"rv20": {80: 0.25}, "hy_spread": {60: 8.0}}
        assert _rule_based_regime(features, percentiles) == "B"

    def test_covid_predict_via_db(self, regime_db):
        """DB 기반 predict(): 2일 연속 B 조건 → Regime B."""
        conn = regime_db

        # 정상 구간 데이터 삽입 (퍼센타일 계산용)
        for i in range(50):
            conn.execute(
                """
                INSERT INTO feature.regime_features
                    (date, vix, rv20, ma200_gap, vix_term, r12m, hy_spread, term_spread)
                VALUES (CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (date) DO NOTHING
                """,
                [f"2019-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
                 15.0, 0.12 + i * 0.001, 0.05, 1.1, 0.08, 5.0, 1.5],
            )

        # 코로나 피처 2일 연속 삽입
        for d in ["2020-03-13", "2020-03-16"]:
            conn.execute(
                """
                INSERT INTO feature.regime_features
                    (date, vix, vix3m, vxmt, vix_term, rv20, rv60,
                     ma200_gap, r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread)
                VALUES (CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (date) DO NOTHING
                """,
                [d, 82.69, 65.0, 50.0, 0.85, 0.90, 0.60,
                 -0.15, -0.30, -0.20, 0.85, 10.87, 3.50, 0.50],
            )

        predict("2020-03-13", conn)
        regime_day2 = predict("2020-03-16", conn)
        assert regime_day2 == "B"


class TestPredictProba:
    """predict_proba 확률 검증."""

    def _insert_features(self, conn, date_str: str, **kwargs):
        defaults = {
            "vix": 18.0, "vix3m": 19.0, "vxmt": 20.0, "vix_term": 1.05,
            "rv20": 0.12, "rv60": 0.14, "ma200_gap": 0.03, "r12m": 0.10,
            "r1m": 0.01, "avg_corr20": 0.25, "hy_spread": 350.0,
            "ig_spread": 120.0, "term_spread": 1.5,
        }
        defaults.update(kwargs)
        conn.execute(
            """
            INSERT INTO feature.regime_features
                (date, vix, vix3m, vxmt, vix_term, rv20, rv60,
                 ma200_gap, r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread)
            VALUES (CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (date) DO NOTHING
            """,
            [
                date_str,
                defaults["vix"], defaults["vix3m"], defaults["vxmt"], defaults["vix_term"],
                defaults["rv20"], defaults["rv60"], defaults["ma200_gap"], defaults["r12m"],
                defaults["r1m"], defaults["avg_corr20"], defaults["hy_spread"],
                defaults["ig_spread"], defaults["term_spread"],
            ],
        )

    def test_proba_sum_equals_1(self, regime_db):
        conn = regime_db
        self._insert_features(conn, "2024-01-15")
        proba = predict_proba("2024-01-15", conn)
        assert set(proba.keys()) == {"A", "B", "C"}
        assert abs(sum(proba.values()) - 1.0) < 1e-6

    def test_proba_all_non_negative(self, regime_db):
        conn = regime_db
        self._insert_features(conn, "2024-02-15")
        proba = predict_proba("2024-02-15", conn)
        for prob in proba.values():
            assert prob >= 0

    def test_proba_rule_based_dominant(self, regime_db):
        """규칙 기반 최대 확률 0.8."""
        conn = regime_db
        self._insert_features(
            conn, "2024-03-15",
            vix=16.0, ma200_gap=0.05, r12m=0.12, hy_spread=300.0,
        )
        for i in range(20):
            try:
                conn.execute(
                    "INSERT INTO raw.fred_series VALUES ('BAMLH0A0HYM2', CAST(? AS DATE), ?, NOW())",
                    [f"2023-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}", 400.0 + i * 5],
                )
            except Exception:
                pass

        proba = predict_proba("2024-03-15", conn)
        max_prob = max(proba.values())
        assert max_prob == pytest.approx(0.8, abs=0.01)

    def test_proba_no_features_returns_uniform(self, regime_db):
        """피처 없음 → 균등 확률 (1/3)."""
        proba = predict_proba("2099-01-01", regime_db)
        assert abs(sum(proba.values()) - 1.0) < 1e-6
        for prob in proba.values():
            assert abs(prob - 1 / 3) < 0.01


# ===========================================================================
# 4C: 급변 알람 테스트 (regime/shock_alarm.py)
# ===========================================================================

class TestShockAlarmVixSpike:
    """VIX Spike 알람 테스트."""

    def test_vix_abs_trigger(self):
        """VIX >= 30 → 트리거."""
        features = pd.Series({"vix": 35.0})
        assert _check_vix_spike(features) is not None

    def test_vix_below_threshold(self):
        """VIX < 30, 변화율 없음 → 트리거 없음."""
        features = pd.Series({"vix": 20.0})
        assert _check_vix_spike(features) is None

    def test_vix_rate_trigger(self):
        """VIX 변화율 >= 20% → 트리거."""
        features = pd.Series({"vix": 24.0})
        prev = pd.Series({"vix": 18.0})  # 33% 상승
        assert _check_vix_spike(features, prev) is not None

    def test_vix_rate_below_threshold(self):
        """VIX 변화율 < 20% → 트리거 없음."""
        features = pd.Series({"vix": 20.0})
        prev = pd.Series({"vix": 18.0})  # 11% 상승
        assert _check_vix_spike(features, prev) is None

    def test_vix_nan(self):
        """VIX NaN → 트리거 없음."""
        features = pd.Series({"vix": float("nan")})
        assert _check_vix_spike(features) is None


class TestShockAlarmVixBackwardation:
    """VIX Backwardation 알람 테스트."""

    def test_backwardation_trigger(self):
        """VIX_TERM < 0.85 → 트리거."""
        features = pd.Series({"vix_term": 0.80})
        assert _check_vix_backwardation(features) is not None

    def test_no_backwardation(self):
        """VIX_TERM >= 0.85 → 트리거 없음."""
        features = pd.Series({"vix_term": 1.10})
        assert _check_vix_backwardation(features) is None


class TestShockAlarmCorrelation:
    """Correlation Shock 알람 테스트."""

    def test_correlation_shock_trigger(self):
        """AVG_CORR20 > 0.75, 60일전 대비 +0.15 → 트리거."""
        features = pd.Series({"avg_corr20": 0.80})
        assert _check_correlation_shock(features, avg_corr_60d_ago=0.60) is not None

    def test_correlation_shock_below_level(self):
        """AVG_CORR20 <= 0.75 → 트리거 없음."""
        features = pd.Series({"avg_corr20": 0.70})
        assert _check_correlation_shock(features, avg_corr_60d_ago=0.50) is None

    def test_correlation_shock_no_60d(self):
        """60일전 데이터 없음 → 트리거 없음."""
        features = pd.Series({"avg_corr20": 0.80})
        assert _check_correlation_shock(features, avg_corr_60d_ago=None) is None

    def test_correlation_shock_small_delta(self):
        """AVG_CORR20 > 0.75이지만 delta < 0.15 → 트리거 없음."""
        features = pd.Series({"avg_corr20": 0.78})
        assert _check_correlation_shock(features, avg_corr_60d_ago=0.70) is None


class TestShockAlarmCreditShock:
    """Credit Shock 알람 테스트."""

    def test_credit_shock_pctl_trigger(self):
        """HY > 95pctl → 트리거."""
        features = pd.Series({"hy_spread": 8.5})
        assert _check_credit_shock(features, hy_pctl_95=7.0) is not None

    def test_credit_shock_daily_change(self):
        """HY 일변화 >= 50bps → 트리거."""
        features = pd.Series({"hy_spread": 5.0})
        assert _check_credit_shock(features, hy_pctl_95=10.0, prev_hy=4.4) is not None

    def test_credit_shock_no_trigger(self):
        """HY 정상 → 트리거 없음."""
        features = pd.Series({"hy_spread": 4.0})
        assert _check_credit_shock(features, hy_pctl_95=7.0, prev_hy=3.9) is None


class TestShockAlarmYieldCurve:
    """Yield Curve Extreme 알람 테스트."""

    def test_yield_curve_extreme(self):
        """TERM_SPREAD <= -0.50 → 트리거."""
        features = pd.Series({"term_spread": -0.60})
        assert _check_yield_curve_extreme(features) is not None

    def test_yield_curve_normal(self):
        """TERM_SPREAD > -0.50 → 트리거 없음."""
        features = pd.Series({"term_spread": 1.5})
        assert _check_yield_curve_extreme(features) is None


class TestShockAlarmSeverity:
    """심각도 계산 테스트."""

    def test_severity_low(self):
        assert _compute_severity([]) == "low"

    def test_severity_medium(self):
        assert _compute_severity(["vix_spike"]) == "medium"

    def test_severity_high(self):
        assert _compute_severity(["vix_spike", "yield_curve"]) == "high"

    def test_severity_critical_3_triggers(self):
        assert _compute_severity(["vix_spike", "credit_shock", "yield_curve"]) == "critical"

    def test_severity_critical_vix_credit_combo(self):
        """VIX + Credit 동시 → critical."""
        assert _compute_severity(["vix_spike", "credit_shock"]) == "critical"


class TestShockAlarmCheckAlarm:
    """check_alarm 통합 테스트."""

    def test_no_alarm_normal(self, regime_db):
        """정상 피처 → alarm=False."""
        conn = regime_db
        conn.execute(
            """
            INSERT INTO feature.regime_features
                (date, vix, vix3m, vxmt, vix_term, rv20, rv60, ma200_gap,
                 r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread)
            VALUES ('2024-01-15', 15.0, 16.0, 17.0, 1.07, 0.12, 0.14, 0.05,
                    0.10, 0.01, 0.25, 3.5, 1.2, 1.5)
            """,
        )

        result = check_alarm("2024-01-15", conn)
        assert result["alarm"] is False
        assert result["severity"] == "low"
        assert result["triggers"] == []

    def test_alarm_vix_spike(self, regime_db):
        """VIX=35 → alarm=True."""
        conn = regime_db
        conn.execute(
            """
            INSERT INTO feature.regime_features
                (date, vix, vix3m, vxmt, vix_term, rv20, rv60, ma200_gap,
                 r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread)
            VALUES ('2024-01-15', 35.0, 30.0, 25.0, 0.86, 0.30, 0.25, -0.05,
                    -0.10, -0.05, 0.60, 5.0, 2.0, 0.5)
            """,
        )

        result = check_alarm("2024-01-15", conn)
        assert result["alarm"] is True
        assert "vix_spike" in result["triggers"]

    def test_alarm_no_features(self, regime_db):
        """피처 없음 → alarm=False."""
        result = check_alarm("2099-01-01", regime_db)
        assert result["alarm"] is False

    def test_safety_weights_on_alarm(self, regime_db):
        """alarm=True → safety_weights 반환."""
        conn = regime_db
        conn.execute(
            """
            INSERT INTO feature.regime_features
                (date, vix, vix3m, vxmt, vix_term, rv20, rv60, ma200_gap,
                 r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread)
            VALUES ('2024-02-01', 40.0, 30.0, 25.0, 0.75, 0.40, 0.30, -0.10,
                    -0.15, -0.08, 0.70, 8.0, 3.0, -0.60)
            """,
        )

        result = check_alarm("2024-02-01", conn)
        assert result["alarm"] is True
        assert result["safety_weights"]["low_vol"] == 0.60
        assert result["safety_weights"]["momentum"] == 0.0
        assert result["safety_weights"]["equity_exposure"] == 0.50

    def test_safety_weights_keys(self):
        """SAFETY_MODE_WEIGHTS 키 확인."""
        expected_keys = {"momentum", "quality", "value", "low_vol", "equity_exposure"}
        assert set(SAFETY_MODE_WEIGHTS.keys()) == expected_keys
