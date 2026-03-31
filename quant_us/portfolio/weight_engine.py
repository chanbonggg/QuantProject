"""
포트폴리오 가중치 결정 엔진

레짐(A/B/C) + 급변 알람에 따라 4개 전략(모멘텀/퀄리티/밸류/저변동성)의
가중치와 주식 비중을 결정한 뒤 최종 결합 포트폴리오를 반환.
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.init import get_connection
from utils.logger import logger

# ---------------------------------------------------------------------------
# 전략 모듈 임포트 (실패 시 graceful 처리)
# ---------------------------------------------------------------------------

try:
    from strategies.momentum import get_portfolio as momentum_portfolio
    _MOMENTUM_AVAILABLE = True
except ImportError:
    _MOMENTUM_AVAILABLE = False
    logger.warning("[포트폴리오 가중치] strategies.momentum 임포트 실패")

try:
    from strategies.value import get_portfolio as value_portfolio
    _VALUE_AVAILABLE = True
except ImportError:
    _VALUE_AVAILABLE = False
    logger.warning("[포트폴리오 가중치] strategies.value 임포트 실패")

try:
    from strategies.quality import get_portfolio as quality_portfolio
    _QUALITY_AVAILABLE = True
except ImportError:
    _QUALITY_AVAILABLE = False
    logger.warning("[포트폴리오 가중치] strategies.quality 임포트 실패")

try:
    from strategies.low_vol import get_portfolio as low_vol_portfolio
    _LOW_VOL_AVAILABLE = True
except ImportError:
    _LOW_VOL_AVAILABLE = False
    logger.warning("[포트폴리오 가중치] strategies.low_vol 임포트 실패")

try:
    from regime.model import predict as regime_predict
    _REGIME_MODEL_AVAILABLE = True
except ImportError:
    _REGIME_MODEL_AVAILABLE = False
    logger.warning("[포트폴리오 가중치] regime.model 임포트 실패")

try:
    from regime.shock_alarm import check_alarm
    _SHOCK_ALARM_AVAILABLE = True
except ImportError:
    _SHOCK_ALARM_AVAILABLE = False
    logger.warning("[포트폴리오 가중치] regime.shock_alarm 임포트 실패")

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

# 레짐별 전략 가중치 테이블
REGIME_WEIGHTS: Dict[str, Dict[str, float]] = {
    "A": {
        "momentum": 0.40,
        "quality": 0.30,
        "value": 0.20,
        "low_vol": 0.10,
        "equity_exposure": 1.00,
    },
    "B": {
        "momentum": 0.00,
        "quality": 0.20,
        "value": 0.20,
        "low_vol": 0.60,
        "equity_exposure": 0.40,
    },
    "C": {
        "momentum": 0.10,
        "quality": 0.30,
        "value": 0.40,
        "low_vol": 0.20,
        "equity_exposure": 0.80,
    },
    "SHOCK": {
        "momentum": 0.00,
        "quality": 0.20,
        "value": 0.20,
        "low_vol": 0.60,
        "equity_exposure": 0.50,
    },
}

# Risk-off 대체자산 옵션
RISK_OFF_ASSETS: Dict[str, str] = {
    "TLT": "iShares 20+ Year Treasury Bond ETF",
    "SHY": "iShares 1-3 Year Treasury Bond ETF",
    "CASH": "현금 (수익률 0)",
}

# 유효 레짐 값
_VALID_REGIMES = frozenset({"A", "B", "C"})

# 전략 이름 → 포트폴리오 함수 매핑
_STRATEGY_FUNCS = {
    "momentum": (lambda *a, **kw: momentum_portfolio(*a, **kw)) if _MOMENTUM_AVAILABLE else None,
    "quality":  (lambda *a, **kw: quality_portfolio(*a, **kw))  if _QUALITY_AVAILABLE  else None,
    "value":    (lambda *a, **kw: value_portfolio(*a, **kw))    if _VALUE_AVAILABLE    else None,
    "low_vol":  (lambda *a, **kw: low_vol_portfolio(*a, **kw))  if _LOW_VOL_AVAILABLE  else None,
}


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

def decide_weights(
    regime: str,
    shock_alarm: bool = False,
    prev_weights: Optional[dict] = None,
    risk_off_asset: str = "SHY",
) -> dict:
    """
    레짐 + 알람 기반 전략 가중치 결정.

    shock_alarm=True 이면 SHOCK 가중치 적용.

    Args:
        regime: 현재 레짐 ('A'|'B'|'C')
        shock_alarm: 급변 알람 여부
        prev_weights: 이전 가중치 (현재 미사용, 향후 부드러운 전환용)
        risk_off_asset: Risk-off 대체자산 선택 ('TLT'|'SHY'|'CASH')

    Returns:
        dict:
            strategy_weights — {'momentum': float, 'quality': float, 'value': float, 'low_vol': float}
            equity_exposure  — float
            risk_off_asset   — str
            risk_off_weight  — float (1 - equity_exposure)
            regime_used      — str (A/B/C/SHOCK)
    """
    # 유효하지 않은 레짐은 C로 폴백
    if regime not in _VALID_REGIMES:
        logger.warning(f"[포트폴리오 가중치] 알 수 없는 레짐 '{regime}' → C 폴백")
        regime = "C"

    # 알람 발생 시 SHOCK 가중치 우선
    regime_key = "SHOCK" if shock_alarm else regime
    weights_row = REGIME_WEIGHTS[regime_key]

    # 유효하지 않은 risk_off_asset 처리
    if risk_off_asset not in RISK_OFF_ASSETS:
        logger.warning(
            f"[포트폴리오 가중치] 알 수 없는 risk_off_asset '{risk_off_asset}' → SHY 폴백"
        )
        risk_off_asset = "SHY"

    equity_exposure = weights_row["equity_exposure"]
    risk_off_weight = round(1.0 - equity_exposure, 10)

    strategy_weights = {
        "momentum": weights_row["momentum"],
        "quality":  weights_row["quality"],
        "value":    weights_row["value"],
        "low_vol":  weights_row["low_vol"],
    }

    result = {
        "strategy_weights": strategy_weights,
        "equity_exposure":  equity_exposure,
        "risk_off_asset":   risk_off_asset,
        "risk_off_weight":  risk_off_weight,
        "regime_used":      regime_key,
    }

    logger.info(
        f"[포트폴리오 가중치] decide_weights: regime={regime}, shock={shock_alarm} "
        f"→ regime_used={regime_key}, equity={equity_exposure:.2f}, "
        f"risk_off={risk_off_weight:.2f}({risk_off_asset}), "
        f"weights={strategy_weights}"
    )
    return result


def build_combined_portfolio(
    date: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    regime: Optional[str] = None,
    shock_alarm: Optional[bool] = None,
    risk_off_asset: str = "SHY",
) -> pd.DataFrame:
    """
    4개 전략 포트폴리오를 레짐 가중치로 결합.

    알고리즘:
    1. regime이 None이면 regime.model.predict 로 자동 판단
    2. shock_alarm이 None이면 regime.shock_alarm.check_alarm 으로 자동 판단
    3. decide_weights 로 전략별 가중치 결정
    4. 가중치 > 0 인 전략만 get_portfolio 호출
    5. 전략별 포트폴리오를 strategy_weight 로 결합
    6. equity_exposure 적용 후 risk_off_asset 비중 추가
    7. 최종 가중치 정규화 (합=1.0)

    Args:
        date: 기준 날짜 ('YYYY-MM-DD')
        conn: DuckDB 연결 (None이면 자동 생성)
        regime: 레짐 직접 지정 (None이면 자동 판단)
        shock_alarm: 알람 직접 지정 (None이면 자동 판단)
        risk_off_asset: Risk-off 대체자산

    Returns:
        pd.DataFrame: columns=[ticker, weight, strategy_source]
        strategy_source 는 'momentum'|'quality'|'value'|'low_vol'|'risk_off' 중 하나
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    logger.info(f"[포트폴리오 가중치] build_combined_portfolio 시작: {date}")

    try:
        # 1. 레짐 자동 판단
        if regime is None:
            if _REGIME_MODEL_AVAILABLE:
                try:
                    regime = regime_predict(date, conn)
                    logger.info(f"[포트폴리오 가중치] 레짐 자동 판단: {regime}")
                except Exception as e:
                    logger.warning(f"[포트폴리오 가중치] 레짐 판단 실패: {e} → C 폴백")
                    regime = "C"
            else:
                logger.warning("[포트폴리오 가중치] regime.model 미사용 → 레짐 C 기본값")
                regime = "C"

        # 2. 급변 알람 자동 판단
        if shock_alarm is None:
            if _SHOCK_ALARM_AVAILABLE:
                try:
                    alarm_result = check_alarm(date, conn)
                    shock_alarm = bool(alarm_result.get("alarm", False))
                    logger.info(
                        f"[포트폴리오 가중치] 급변 알람 자동 판단: {shock_alarm} "
                        f"(severity={alarm_result.get('severity', 'unknown')})"
                    )
                except Exception as e:
                    logger.warning(f"[포트폴리오 가중치] 급변 알람 판단 실패: {e} → False 폴백")
                    shock_alarm = False
            else:
                logger.warning("[포트폴리오 가중치] regime.shock_alarm 미사용 → alarm=False 기본값")
                shock_alarm = False

        # 3. 전략 가중치 결정
        weight_info = decide_weights(regime, shock_alarm, risk_off_asset=risk_off_asset)
        strategy_weights = weight_info["strategy_weights"]
        equity_exposure = weight_info["equity_exposure"]
        risk_off_weight = weight_info["risk_off_weight"]
        regime_used = weight_info["regime_used"]

        # 4. 전략별 get_portfolio 호출 및 결합
        combined: Dict[str, float] = {}
        ticker_source: Dict[str, str] = {}

        strategy_order = ["momentum", "quality", "value", "low_vol"]
        for strategy_name in strategy_order:
            strat_weight = strategy_weights.get(strategy_name, 0.0)

            # 가중치 0%인 전략은 호출 생략
            if strat_weight <= 0.0:
                logger.debug(
                    f"[포트폴리오 가중치] {strategy_name} 가중치=0 → 호출 생략"
                )
                continue

            func = _STRATEGY_FUNCS.get(strategy_name)
            if func is None:
                logger.warning(
                    f"[포트폴리오 가중치] {strategy_name} 함수 미사용 → 스킵"
                )
                continue

            try:
                portfolio_df = func(date, conn)
            except Exception as e:
                logger.error(
                    f"[포트폴리오 가중치] {strategy_name}.get_portfolio 실패: {e} → 스킵"
                )
                continue

            if portfolio_df is None or portfolio_df.empty:
                logger.warning(
                    f"[포트폴리오 가중치] {strategy_name} 포트폴리오 비어있음 → 스킵"
                )
                continue

            # 전략 내 가중치 정규화 (합=1.0 보장)
            total_w = portfolio_df["weight"].sum()
            if total_w <= 0:
                logger.warning(
                    f"[포트폴리오 가중치] {strategy_name} 가중치 합=0 → 스킵"
                )
                continue

            for _, row in portfolio_df.iterrows():
                ticker = row["ticker"]
                normalized_w = row["weight"] / total_w
                contribution = strat_weight * normalized_w

                if ticker in combined:
                    combined[ticker] += contribution
                    # 먼저 등록된 전략이 source 우선 (주 기여 전략)
                else:
                    combined[ticker] = contribution
                    ticker_source[ticker] = strategy_name

            logger.info(
                f"[포트폴리오 가중치] {strategy_name} 결합 완료: "
                f"{len(portfolio_df)}개 종목, 전략가중치={strat_weight:.2f}"
            )

        if not combined:
            logger.warning(
                f"[포트폴리오 가중치] 모든 전략 포트폴리오 비어있음 → 빈 DataFrame 반환"
            )
            return pd.DataFrame(columns=["ticker", "weight", "strategy_source"])

        # 5. equity_exposure 적용
        equity_total = sum(combined.values())
        if equity_total > 0:
            # 주식 비중을 equity_exposure 만큼 스케일 다운
            combined = {t: w * equity_exposure / equity_total for t, w in combined.items()}

        # 6. risk_off_asset 비중 추가
        if risk_off_weight > 1e-9:
            risk_off_ticker = risk_off_weight_info = weight_info["risk_off_asset"]
            if risk_off_ticker in combined:
                combined[risk_off_ticker] += risk_off_weight
            else:
                combined[risk_off_ticker] = risk_off_weight
                ticker_source[risk_off_ticker] = "risk_off"

        # 7. 최종 정규화 (합=1.0)
        total_final = sum(combined.values())
        if total_final > 0:
            combined = {t: w / total_final for t, w in combined.items()}

        # DataFrame 구성
        rows = []
        for ticker, weight in sorted(combined.items(), key=lambda x: -x[1]):
            rows.append({
                "ticker": ticker,
                "weight": weight,
                "strategy_source": ticker_source.get(ticker, "unknown"),
            })

        result_df = pd.DataFrame(rows)

        logger.info(
            f"[포트폴리오 가중치] build_combined_portfolio 완료: "
            f"date={date}, regime_used={regime_used}, shock={shock_alarm}, "
            f"종목수={len(result_df)}, 가중치합={result_df['weight'].sum():.6f}"
        )
        return result_df

    finally:
        if close_conn:
            conn.close()


def get_weight_transition_log(
    dates: List[str],
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> pd.DataFrame:
    """
    여러 날짜의 가중치 전환 이력 조회.

    각 날짜별로 regime, shock_alarm, strategy_weights 를 기록한 DataFrame 반환.

    Args:
        dates: 조회할 날짜 목록 ('YYYY-MM-DD' 형식)
        conn: DuckDB 연결 (None이면 자동 생성)

    Returns:
        pd.DataFrame:
            columns=[date, regime, shock_alarm, momentum, quality, value, low_vol, equity_exposure]
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    logger.info(
        f"[포트폴리오 가중치] get_weight_transition_log 시작: {len(dates)}개 날짜"
    )

    records = []

    try:
        for date_str in dates:
            # 레짐 판단
            regime = "C"
            if _REGIME_MODEL_AVAILABLE:
                try:
                    regime = regime_predict(date_str, conn)
                except Exception as e:
                    logger.warning(
                        f"[포트폴리오 가중치] {date_str} 레짐 판단 실패: {e} → C"
                    )

            # 급변 알람 판단
            shock = False
            if _SHOCK_ALARM_AVAILABLE:
                try:
                    alarm_result = check_alarm(date_str, conn)
                    shock = bool(alarm_result.get("alarm", False))
                except Exception as e:
                    logger.warning(
                        f"[포트폴리오 가중치] {date_str} 급변 알람 판단 실패: {e} → False"
                    )

            # 가중치 결정
            weight_info = decide_weights(regime, shock)
            sw = weight_info["strategy_weights"]

            records.append({
                "date":            date_str,
                "regime":          regime,
                "shock_alarm":     shock,
                "momentum":        sw["momentum"],
                "quality":         sw["quality"],
                "value":           sw["value"],
                "low_vol":         sw["low_vol"],
                "equity_exposure": weight_info["equity_exposure"],
                "regime_used":     weight_info["regime_used"],
            })

    finally:
        if close_conn:
            conn.close()

    df = pd.DataFrame(records)

    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

    logger.info(
        f"[포트폴리오 가중치] get_weight_transition_log 완료: {len(df)}개 레코드"
    )
    return df
