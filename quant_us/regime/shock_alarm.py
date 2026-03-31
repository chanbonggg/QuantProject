"""
급변 알람 모듈 (Shock Alarm)

6개 알람 조건을 체크하여 시장 급변 여부를 판단.

알람 조건:
  1. VIX Spike         — VIX 변화율 >= +20% OR VIX >= 30
  2. VIX Backwardation — VIX_TERM < 0.85 (근월물이 원월물보다 비쌈 = 극단 공포)
  3. Correlation Shock — AVG_CORR20 > 0.75 AND 60일전 대비 +0.15
  4. Credit Shock      — HY_SPREAD 일변화 >= +50bps OR HY_SPREAD > 5년 95pctl
  5. Trend Break       — SPY 일간 수익률 <= -3% AND 거래량 >= 20일평균 × 2
  6. Yield Curve Extreme — TERM_SPREAD <= -0.50%
"""

import sys
from pathlib import Path
from typing import List, Optional

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.init import get_connection
from utils.logger import logger

# features.py 동시 구현 중 — 임포트 실패 허용
try:
    from regime.features import compute_features
    _FEATURES_AVAILABLE = True
except ImportError:
    _FEATURES_AVAILABLE = False
    logger.warning("[급변 알람] regime.features 모듈 없음 — compute_features 직접 조회로 대체")

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

# 안전모드 전략 가중치 (alarm=True 시 권장)
SAFETY_MODE_WEIGHTS: dict = {
    "momentum": 0.0,
    "quality": 0.20,
    "value": 0.20,
    "low_vol": 0.60,
    "equity_exposure": 0.50,
}

# 알람 임계값
_VIX_SPIKE_RATE = 0.20       # VIX 변화율 임계값 (20%)
_VIX_SPIKE_ABS = 30.0        # VIX 절대값 임계값
_VIX_BACKWARDATION = 0.85    # VIX_TERM 하한 (vix3m/vix)
_CORR_SHOCK_LEVEL = 0.75     # AVG_CORR20 절대 임계값
_CORR_SHOCK_DELTA = 0.15     # AVG_CORR20 60일 증가 임계값
_CREDIT_SHOCK_BPS = 50.0     # HY_SPREAD 일변화 임계값 (bps)
_CREDIT_PCTL_THRESHOLD = 0.95  # HY_SPREAD 5년 퍼센타일
_SPY_TREND_BREAK = -0.03     # SPY 일간 수익률 임계값 (-3%)
_SPY_VOL_MULTIPLE = 2.0      # 거래량 배수 (20일 평균 × 2)
_YIELD_CURVE_EXTREME = -0.50  # TERM_SPREAD 임계값 (%)

# severity 기준 (트리거 개수)
_SEVERITY_MAP = {
    0: "low",
    1: "medium",
    2: "high",
}


# ---------------------------------------------------------------------------
# 보조 데이터 조회 헬퍼
# ---------------------------------------------------------------------------

def _get_features_from_db(date: str, conn: duckdb.DuckDBPyConnection) -> Optional[pd.Series]:
    """feature.regime_features에서 해당 날짜 피처 조회."""
    result = conn.execute(
        """
        SELECT vix, vix3m, vxmt, vix_term, rv20, rv60, ma200_gap,
               r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread
        FROM feature.regime_features
        WHERE date = CAST(? AS DATE)
        LIMIT 1
        """,
        [date],
    ).fetchone()

    if result is None:
        return None

    columns = [
        "vix", "vix3m", "vxmt", "vix_term", "rv20", "rv60", "ma200_gap",
        "r12m", "r1m", "avg_corr20", "hy_spread", "ig_spread", "term_spread",
    ]
    return pd.Series(dict(zip(columns, result)), dtype=float)


def _resolve_features(date: str, conn: duckdb.DuckDBPyConnection) -> Optional[pd.Series]:
    """
    피처 Series 반환.

    1. feature.regime_features DB에서 먼저 조회
    2. 없으면 compute_features 호출 (사용 가능한 경우)
    3. 둘 다 없으면 None
    """
    # 1. DB에서 먼저 조회 (빠름)
    features = _get_features_from_db(date, conn)
    if features is not None:
        return features

    # 2. compute_features 호출 (모듈 사용 가능 시)
    if _FEATURES_AVAILABLE:
        try:
            features = compute_features(date, conn)
            return features
        except Exception as e:
            logger.warning(f"[급변 알람] compute_features 호출 실패: {e}")

    return None


def _get_prev_features(date: str, conn: duckdb.DuckDBPyConnection) -> Optional[pd.Series]:
    """feature.regime_features에서 date 직전 거래일 피처 조회."""
    result = conn.execute(
        """
        SELECT date, vix, vix3m, vxmt, vix_term, rv20, rv60, ma200_gap,
               r12m, r1m, avg_corr20, hy_spread, ig_spread, term_spread
        FROM feature.regime_features
        WHERE date < CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT 1
        """,
        [date],
    ).fetchone()

    if result is None:
        return None

    columns = [
        "date", "vix", "vix3m", "vxmt", "vix_term", "rv20", "rv60", "ma200_gap",
        "r12m", "r1m", "avg_corr20", "hy_spread", "ig_spread", "term_spread",
    ]
    row = dict(zip(columns, result))
    # date 컬럼 제외
    row.pop("date", None)
    return pd.Series(row, dtype=float)


def _get_avg_corr_60d_ago(date: str, conn: duckdb.DuckDBPyConnection) -> Optional[float]:
    """feature.regime_features에서 60거래일 전 avg_corr20 조회."""
    result = conn.execute(
        """
        SELECT avg_corr20
        FROM feature.regime_features
        WHERE date < CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT 1
        OFFSET 59
        """,
        [date],
    ).fetchone()

    if result is None or result[0] is None:
        return None

    return float(result[0])


def _get_hy_95pctl(conn: duckdb.DuckDBPyConnection, lookback_years: int = 5) -> Optional[float]:
    """
    raw.fred_series BAMLH0A0HYM2의 최근 lookback_years년 95퍼센타일.

    데이터가 lookback_years 미만이면 가용 전체 데이터로 계산.
    """
    result = conn.execute(
        """
        SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS pctl_95
        FROM raw.fred_series
        WHERE series_id = 'BAMLH0A0HYM2'
          AND date >= CURRENT_DATE - INTERVAL (? * 365) DAY
        """,
        [lookback_years],
    ).fetchone()

    if result and result[0] is not None:
        return float(result[0])

    # 5년치 데이터 없으면 전체 데이터로 계산
    result_all = conn.execute(
        """
        SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS pctl_95
        FROM raw.fred_series
        WHERE series_id = 'BAMLH0A0HYM2'
        """
    ).fetchone()

    return float(result_all[0]) if result_all and result_all[0] is not None else None


# ---------------------------------------------------------------------------
# 6개 알람 조건 체크
# ---------------------------------------------------------------------------

def _check_vix_spike(
    features: pd.Series,
    prev_features: Optional[pd.Series] = None,
) -> Optional[str]:
    """
    VIX Spike 체크.

    조건: VIX 변화율 >= +20% OR VIX >= 30
    prev_features 없으면 절대값 조건(VIX >= 30)만 체크.

    Returns:
        트리거 메시지 또는 None
    """
    vix = features.get("vix")
    if vix is None or pd.isna(vix):
        return None

    # 절대값 조건
    if vix >= _VIX_SPIKE_ABS:
        return f"VIX={vix:.1f} (임계값 {_VIX_SPIKE_ABS})"

    # 변화율 조건 (전일 데이터 있을 때만)
    if prev_features is not None:
        prev_vix = prev_features.get("vix")
        if prev_vix is not None and not pd.isna(prev_vix) and prev_vix > 0:
            change_rate = (vix - prev_vix) / prev_vix
            if change_rate >= _VIX_SPIKE_RATE:
                return f"VIX 급등: {prev_vix:.1f} → {vix:.1f} (+{change_rate*100:.1f}%)"

    return None


def _check_vix_backwardation(features: pd.Series) -> Optional[str]:
    """
    VIX Backwardation 체크.

    조건: VIX_TERM < 0.85 (vix3m/vix < 0.85 = 근월물이 더 비쌈 = 극단 공포)

    Returns:
        트리거 메시지 또는 None
    """
    vix_term = features.get("vix_term")
    if vix_term is None or pd.isna(vix_term):
        return None

    if vix_term < _VIX_BACKWARDATION:
        return f"VIX_TERM={vix_term:.3f} (임계값 {_VIX_BACKWARDATION}, 백워데이션)"

    return None


def _check_correlation_shock(
    features: pd.Series,
    avg_corr_60d_ago: Optional[float] = None,
) -> Optional[str]:
    """
    Correlation Shock 체크.

    조건: AVG_CORR20 > 0.75 AND 60일 전 대비 +0.15
    60일전 없으면 조건 스킵.

    Returns:
        트리거 메시지 또는 None
    """
    avg_corr = features.get("avg_corr20")
    if avg_corr is None or pd.isna(avg_corr):
        return None

    if avg_corr <= _CORR_SHOCK_LEVEL:
        return None

    # 60일전 데이터 없으면 스킵
    if avg_corr_60d_ago is None:
        return None

    delta = avg_corr - avg_corr_60d_ago
    if delta >= _CORR_SHOCK_DELTA:
        return (
            f"AVG_CORR20={avg_corr:.3f} (임계값 {_CORR_SHOCK_LEVEL}), "
            f"60일전={avg_corr_60d_ago:.3f} (+{delta:.3f})"
        )

    return None


def _check_credit_shock(
    features: pd.Series,
    hy_pctl_95: Optional[float] = None,
    prev_hy: Optional[float] = None,
) -> Optional[str]:
    """
    Credit Shock 체크.

    조건: HY_SPREAD 일변화 >= +50bps OR HY_SPREAD > 5년 95pctl

    Returns:
        트리거 메시지 또는 None
    """
    hy_spread = features.get("hy_spread")
    if hy_spread is None or pd.isna(hy_spread):
        return None

    # 1. 퍼센타일 조건
    if hy_pctl_95 is not None and hy_spread > hy_pctl_95:
        return f"HY_SPREAD={hy_spread:.1f} > 5년 95pctl({hy_pctl_95:.1f})"

    # 2. 일변화 조건
    if prev_hy is not None and not pd.isna(prev_hy):
        delta_bps = (hy_spread - prev_hy) * 100  # % → bps 변환 (이미 % 단위)
        # BAMLH0A0HYM2는 % 단위이므로 0.50 = 50bps
        delta_bps_raw = hy_spread - prev_hy  # 실제 단위 그대로
        if delta_bps_raw >= (_CREDIT_SHOCK_BPS / 100):  # 50bps = 0.50%p
            return (
                f"HY_SPREAD 급등: {prev_hy:.2f} → {hy_spread:.2f} "
                f"(+{delta_bps_raw*100:.1f}bps)"
            )

    return None


def _check_trend_break(date: str, conn: duckdb.DuckDBPyConnection) -> Optional[str]:
    """
    Trend Break 체크.

    조건: SPY 일간 수익률 <= -3% AND 거래량 >= 20일평균 × 2

    Returns:
        트리거 메시지 또는 None
    """
    # 최근 21거래일 SPY 데이터 조회 (당일 포함, 수익률 계산용 +1)
    spy_df = conn.execute(
        """
        SELECT date, adj_close, volume
        FROM raw.prices
        WHERE ticker = 'SPY'
          AND date <= CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT 22
        """,
        [date],
    ).df()

    if spy_df.empty or len(spy_df) < 2:
        return None

    spy_df = spy_df.sort_values("date").reset_index(drop=True)

    # 당일 데이터 확인
    today_row = spy_df.iloc[-1]
    today_date_str = str(today_row["date"])[:10]

    # date가 DB에 없는 경우 (거래일 아님) 스킵
    if today_date_str != date:
        return None

    prev_row = spy_df.iloc[-2]

    # 일간 수익률 계산
    if prev_row["adj_close"] is None or prev_row["adj_close"] <= 0:
        return None

    daily_ret = (today_row["adj_close"] - prev_row["adj_close"]) / prev_row["adj_close"]

    if daily_ret > _SPY_TREND_BREAK:
        return None

    # 거래량 조건: 20일 평균 거래량 계산 (당일 제외)
    vol_history = spy_df.iloc[:-1]["volume"].dropna()
    if len(vol_history) < 5:
        return None

    avg_volume = vol_history.mean()
    today_volume = today_row["volume"]

    if avg_volume <= 0 or today_volume is None:
        return None

    vol_ratio = today_volume / avg_volume

    if vol_ratio >= _SPY_VOL_MULTIPLE:
        return (
            f"SPY 급락: {daily_ret*100:.1f}%, "
            f"거래량={today_volume:,.0f} (평균 대비 {vol_ratio:.1f}배)"
        )

    return None


def _check_yield_curve_extreme(features: pd.Series) -> Optional[str]:
    """
    Yield Curve Extreme 체크.

    조건: TERM_SPREAD <= -0.50%

    Returns:
        트리거 메시지 또는 None
    """
    term_spread = features.get("term_spread")
    if term_spread is None or pd.isna(term_spread):
        return None

    if term_spread <= _YIELD_CURVE_EXTREME:
        return f"TERM_SPREAD={term_spread:.2f}% (임계값 {_YIELD_CURVE_EXTREME}%, 역전)"

    return None


# ---------------------------------------------------------------------------
# 심각도 계산
# ---------------------------------------------------------------------------

def _compute_severity(triggers: List[str]) -> str:
    """
    트리거 개수와 조합에 따라 심각도 반환.

    - 0개: 'low'
    - 1개: 'medium'
    - 2개: 'high'
    - 3개+ OR (vix_spike AND credit_shock 동시): 'critical'
    """
    n = len(triggers)

    if n == 0:
        return "low"
    if n == 1:
        return "medium"
    if n == 2:
        # vix_spike + credit_shock 동시 → critical
        if "vix_spike" in triggers and "credit_shock" in triggers:
            return "critical"
        return "high"
    # 3개 이상 → critical
    return "critical"


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

def check_alarm(
    date: str,
    conn: duckdb.DuckDBPyConnection = None,
) -> dict:
    """
    지정 날짜의 급변 알람 체크.

    6개 조건을 독립적으로 평가하여 알람 여부와 심각도 반환.
    DB 저장은 수행하지 않음 (model.py가 담당).

    Args:
        date: 기준 날짜 (YYYY-MM-DD)
        conn: DuckDB 연결 (None이면 자동 생성)

    Returns:
        {
            'alarm': bool,
            'triggers': List[str],      # ['vix_spike', 'credit_shock', ...]
            'severity': str,            # 'low'|'medium'|'high'|'critical'
            'details': List[str],       # 각 트리거의 상세 메시지
            'safety_weights': dict,     # alarm=True일 때 SAFETY_MODE_WEIGHTS
            'date': str,
        }
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    result = {
        "alarm": False,
        "triggers": [],
        "severity": "low",
        "details": [],
        "safety_weights": {},
        "date": date,
    }

    try:
        # ── 1. 피처 조회 ─────────────────────────────────────────────────
        features = _resolve_features(date, conn)
        if features is None:
            logger.warning(f"[급변 알람] {date}: 피처 없음 — 알람 체크 불가")
            return result

        # ── 2. 보조 데이터 조회 ───────────────────────────────────────────
        prev_features = _get_prev_features(date, conn)
        avg_corr_60d_ago = _get_avg_corr_60d_ago(date, conn)
        hy_pctl_95 = _get_hy_95pctl(conn)

        prev_hy = (
            prev_features.get("hy_spread")
            if prev_features is not None
            else None
        )
        if prev_hy is not None and pd.isna(prev_hy):
            prev_hy = None

        # ── 3. 6개 알람 조건 체크 ────────────────────────────────────────
        checks = [
            ("vix_spike",        _check_vix_spike(features, prev_features)),
            ("vix_backwardation", _check_vix_backwardation(features)),
            ("correlation_shock", _check_correlation_shock(features, avg_corr_60d_ago)),
            ("credit_shock",      _check_credit_shock(features, hy_pctl_95, prev_hy)),
            ("trend_break",       _check_trend_break(date, conn)),
            ("yield_curve",       _check_yield_curve_extreme(features)),
        ]

        triggers: List[str] = []
        details: List[str] = []

        for alarm_name, message in checks:
            if message is not None:
                triggers.append(alarm_name)
                details.append(f"[{alarm_name}] {message}")

        # ── 4. 심각도 산출 ────────────────────────────────────────────────
        alarm = len(triggers) > 0
        severity = _compute_severity(triggers)

        result.update({
            "alarm": alarm,
            "triggers": triggers,
            "severity": severity,
            "details": details,
            "safety_weights": SAFETY_MODE_WEIGHTS if alarm else {},
            "date": date,
        })

        logger.info(
            f"[급변 알람] {date}: alarm={alarm}, "
            f"triggers={triggers}, severity={severity}"
        )

        return result

    finally:
        if close_conn:
            conn.close()


def get_alarm_history(
    start: str,
    end: str,
    conn: duckdb.DuckDBPyConnection = None,
) -> pd.DataFrame:
    """
    feature.regime_labels에서 shock_alarm=True인 날짜 목록 조회.

    Args:
        start: 시작 날짜 (YYYY-MM-DD, 포함)
        end: 종료 날짜 (YYYY-MM-DD, 포함)
        conn: DuckDB 연결 (None이면 자동 생성)

    Returns:
        pd.DataFrame: 컬럼=[date, regime, shock_alarm, computed_at]
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        df = conn.execute(
            """
            SELECT date, regime, shock_alarm, computed_at
            FROM feature.regime_labels
            WHERE shock_alarm = TRUE
              AND date >= CAST(? AS DATE)
              AND date <= CAST(? AS DATE)
            ORDER BY date ASC
            """,
            [start, end],
        ).df()

        logger.info(
            f"[급변 알람 이력] {start} ~ {end}: {len(df)}건 조회"
        )
        return df

    finally:
        if close_conn:
            conn.close()
