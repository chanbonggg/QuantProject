"""
포트폴리오 최적화 모듈

build_combined_portfolio 출력을 입력으로 받아:
  1. 종목/섹터 제약 조건 적용
  2. 손절/VIX 오버레이 적용
  3. 포트폴리오 통계 산출
  4. 스트레스 테스트
"""

import sys
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.init import get_connection
from utils.logger import logger

# ---------------------------------------------------------------------------
# 제약 조건 상수
# ---------------------------------------------------------------------------

MAX_WEIGHT_PER_STOCK = 0.05       # 종목 최대 5%
MAX_WEIGHT_PER_SECTOR = 0.30      # 섹터(GICS) 최대 30%
TARGET_VOLATILITY = 0.12          # 목표 연율 변동성 12%
MIN_STOCKS = 20                   # 최소 종목 수

# 손절 정책
STOP_LOSS_THRESHOLD = -0.08       # 20거래일 누적 -8% → 50% 축소
STOP_LOSS_REDUCTION = 0.50        # 주식 50% 축소
RECOVERY_WINDOW = 10              # 10거래일 이동수익률 양전환 시 복구
VIX_OVERLAY_THRESHOLD = 35.0      # VIX > 35 → 주식 -20%p
VIX_OVERLAY_REDUCTION = 0.20      # -20%p

# Risk-off 티커
RISK_OFF_TICKERS = {"TLT", "SHY", "CASH"}

# 스트레스 시나리오
STRESS_SHOCKS = {
    "2008_gfc": {
        "equity": -0.40,
        "treasury": 0.10,
        "description": "2008 금융위기: 주식-40%, 채권+10%",
    },
    "2020_covid": {
        "equity": -0.35,
        "treasury": 0.05,
        "description": "코로나: 주식-35%, 채권+5%",
    },
    "2022_rate_hike": {
        "equity": -0.25,
        "treasury": -0.15,
        "description": "금리인상: 주식-25%, 채권-15%",
    },
    "inflation_shock": {
        "equity": -0.15,
        "treasury": -0.20,
        "description": "인플레: 주식-15%, 채권-20%",
    },
}


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _normalize_weights(portfolio: pd.DataFrame) -> pd.DataFrame:
    """가중치 합이 1.0이 되도록 정규화."""
    total = portfolio["weight"].sum()
    if total <= 0:
        logger.warning("[포트폴리오 최적화] 가중치 합=0, 정규화 불가 → 원본 반환")
        return portfolio.copy()
    result = portfolio.copy()
    result["weight"] = result["weight"] / total
    return result


def _fetch_vix(date: str, conn: duckdb.DuckDBPyConnection) -> Optional[float]:
    """date 이전 가장 가까운 VIX 값 조회."""
    result = conn.execute(
        """
        SELECT value
        FROM raw.fred_series
        WHERE series_id = 'VIXCLS'
          AND date <= CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT 1
        """,
        [date],
    ).fetchone()
    return float(result[0]) if result else None


def _fetch_portfolio_cumret(
    portfolio: pd.DataFrame,
    date: str,
    lookback: int,
    conn: duckdb.DuckDBPyConnection,
) -> Optional[float]:
    """
    포트폴리오 누적수익률 계산.

    각 보유 종목의 최근 lookback+1 거래일 adj_close를 조회하여
    가중 누적수익률 산출. risk_off 종목은 제외.
    """
    # 주식 종목만 필터링
    equity_df = portfolio[~portfolio["strategy_source"].isin(
        {"risk_off"}
    ) & ~portfolio["ticker"].isin(RISK_OFF_TICKERS)].copy()

    if equity_df.empty:
        return None

    tickers = equity_df["ticker"].tolist()
    ticker_weights = dict(zip(equity_df["ticker"], equity_df["weight"]))

    placeholders = ",".join(["?" for _ in tickers])
    prices_df = conn.execute(
        f"""
        SELECT ticker, date, adj_close
        FROM raw.prices
        WHERE ticker IN ({placeholders})
          AND date <= CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT ?
        """,
        [*tickers, date, (lookback + 2) * len(tickers)],
    ).df()

    if prices_df.empty:
        return None

    prices_df = prices_df.sort_values("date")

    # 종목별 누적수익률 계산
    total_weight = 0.0
    weighted_cumret = 0.0

    for ticker in tickers:
        t_prices = prices_df[prices_df["ticker"] == ticker]["adj_close"].values
        if len(t_prices) < lookback + 1:
            continue

        price_start = float(t_prices[-(lookback + 1)])
        price_end = float(t_prices[-1])

        if price_start <= 0:
            continue

        cumret = (price_end - price_start) / price_start
        w = ticker_weights.get(ticker, 0.0)
        weighted_cumret += cumret * w
        total_weight += w

    if total_weight <= 0:
        return None

    # 주식 비중 대비 정규화
    return weighted_cumret / total_weight


def _get_moving_return(
    portfolio: pd.DataFrame,
    date: str,
    window: int,
    conn: duckdb.DuckDBPyConnection,
) -> Optional[float]:
    """최근 window 거래일 이동수익률 계산."""
    return _fetch_portfolio_cumret(portfolio, date, window, conn)


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

def optimize(
    portfolio: pd.DataFrame,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    max_weight: float = MAX_WEIGHT_PER_STOCK,
    max_sector_weight: float = MAX_WEIGHT_PER_SECTOR,
    min_stocks: int = MIN_STOCKS,
    top_n: Optional[int] = None,
) -> pd.DataFrame:
    """
    포트폴리오 제약 조건 적용.

    알고리즘 (규칙 기반):
    1. 종목 최대 비중 클리핑
    2. 섹터 최대 비중 적용 (섹터 정보 없으면 스킵)
    3. 최소 종목 수 미달 시 균등분배 종목 추가
    4. 최종 정규화 (합=1.0)
    5. top_n이 지정되면 상위 N개만 선택 후 재정규화

    Args:
        portfolio: columns=[ticker, weight, strategy_source]
        conn: DuckDB 연결
        max_weight: 종목 최대 비중 (기본 5%)
        max_sector_weight: 섹터 최대 비중 (기본 30%)
        min_stocks: 최소 종목 수 (기본 20)
        top_n: 상위 N개만 선택 (None이면 모든 종목 사용)

    Returns:
        pd.DataFrame: columns=[ticker, weight, strategy_source]
    """
    logger.info(
        f"[포트폴리오 최적화] optimize 시작: 입력 {len(portfolio)}개 종목, "
        f"max_weight={max_weight:.2%}, max_sector={max_sector_weight:.2%}, min_stocks={min_stocks}, top_n={top_n}"
    )

    if portfolio.empty:
        logger.warning("[포트폴리오 최적화] 입력 포트폴리오 비어있음 → 빈 DataFrame 반환")
        return pd.DataFrame(columns=["ticker", "weight", "strategy_source"])

    result = portfolio.copy()

    # risk_off 종목과 주식 종목 분리
    is_risk_off = result["strategy_source"] == "risk_off"
    equity_df = result[~is_risk_off].copy()
    risk_off_df = result[is_risk_off].copy()

    # 1. 종목 최대 비중 클리핑
    before_clip_over = int((equity_df["weight"] > max_weight).sum())
    equity_df["weight"] = equity_df["weight"].clip(upper=max_weight)

    if before_clip_over > 0:
        logger.info(
            f"[포트폴리오 최적화] 종목 비중 클리핑: {before_clip_over}개 종목 → {max_weight:.2%} 상한 적용"
        )

    # 2. 섹터 최대 비중 적용
    if "sector" in equity_df.columns:
        equity_df = _apply_sector_constraint(equity_df, max_sector_weight)
    else:
        logger.debug("[포트폴리오 최적화] sector 컬럼 없음 → 섹터 제약 스킵")

    # 3. 최소 종목 수 확인
    n_equity = len(equity_df[equity_df["weight"] > 0])
    if n_equity < min_stocks:
        logger.warning(
            f"[포트폴리오 최적화] 주식 종목 수 {n_equity}개 < 최소 {min_stocks}개 "
            f"→ 하한 비중 종목 수 부족 (데이터 확인 필요)"
        )

    # 4. 주식 가중치 재정규화 (equity 합을 원래 equity 비중으로 복원, 단 max_weight 준수)
    equity_total_before = result[~is_risk_off]["weight"].sum()
    equity_total_after = equity_df["weight"].sum()

    if equity_total_after > 0 and equity_total_before > 0:
        scale = equity_total_before / equity_total_after
        # 스케일링 후에도 max_weight를 초과하지 않도록 반복 클리핑
        for _ in range(10):
            equity_df["weight"] = equity_df["weight"] * scale
            over_mask = equity_df["weight"] > max_weight
            if not over_mask.any():
                break
            equity_df.loc[over_mask, "weight"] = max_weight
            new_total = equity_df["weight"].sum()
            if new_total > 0:
                scale = equity_total_before / new_total
            else:
                break

    # 5. 결합 및 최종 정규화 (max_weight 준수)
    combined = pd.concat([equity_df, risk_off_df], ignore_index=True)
    combined = combined[combined["weight"] > 0].copy()
    combined = _normalize_weights(combined)

    # 정규화 후 max_weight 초과 종목이 있으면 반복 클리핑 + 초과분을 risk_off으로 이전
    for _ in range(10):
        is_ro = combined["ticker"].isin(RISK_OFF_TICKERS)
        over = (~is_ro) & (combined["weight"] > max_weight + 1e-9)
        if not over.any():
            break
        excess = (combined.loc[over, "weight"] - max_weight).sum()
        combined.loc[over, "weight"] = max_weight
        # 초과분을 risk_off 종목에 균등 분배
        ro_count = is_ro.sum()
        if ro_count > 0:
            combined.loc[is_ro, "weight"] += excess / ro_count
        else:
            # risk_off 없으면 전체 재정규화
            combined = _normalize_weights(combined)

    combined = combined.sort_values("weight", ascending=False).reset_index(drop=True)

    # top_n 필터링: 자본 제약 시 상위 N개만 선택 (균등 가중치)
    if top_n is not None and len(combined) > top_n:
        is_ro = combined["ticker"].isin(RISK_OFF_TICKERS)
        risk_off_combined = combined[is_ro]
        equity_combined = combined[~is_ro].head(top_n)

        # 주식은 균등 가중치, risk_off는 나머지
        equity_weight = (1.0 - risk_off_combined["weight"].sum()) / len(equity_combined)
        equity_combined["weight"] = equity_weight

        combined = pd.concat([equity_combined, risk_off_combined], ignore_index=True).reset_index(drop=True)
        combined = combined.sort_values("weight", ascending=False).reset_index(drop=True)

        n_equity = len(equity_combined)
        logger.info(f"[포트폴리오 최적화] top_n 필터링: {n_equity}개 주식 균등배치 (각 {equity_weight:.2%}) + risk_off {risk_off_combined['weight'].sum():.2%}")

    logger.info(
        f"[포트폴리오 최적화] optimize 완료: {len(combined)}개 종목, "
        f"가중치합={combined['weight'].sum():.6f}, "
        f"최대비중={combined['weight'].max():.4f}"
    )
    return combined


def _apply_sector_constraint(
    equity_df: pd.DataFrame,
    max_sector_weight: float,
) -> pd.DataFrame:
    """섹터 최대 비중 제약 적용 (내부 헬퍼)."""
    result = equity_df.copy()
    sector_totals = result.groupby("sector")["weight"].sum()
    over_sectors = sector_totals[sector_totals > max_sector_weight]

    if over_sectors.empty:
        return result

    for sector, total_w in over_sectors.items():
        scale = max_sector_weight / total_w
        mask = result["sector"] == sector
        result.loc[mask, "weight"] = result.loc[mask, "weight"] * scale
        logger.info(
            f"[포트폴리오 최적화] 섹터 '{sector}' 비중 축소: "
            f"{total_w:.2%} → {max_sector_weight:.2%} (scale={scale:.4f})"
        )

    return result


def apply_risk_overlay(
    portfolio: pd.DataFrame,
    date: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> pd.DataFrame:
    """
    손절/VIX 오버레이 적용.

    1. 20거래일 포트폴리오 누적수익률 계산
       - cumret <= STOP_LOSS_THRESHOLD → 주식 비중 * STOP_LOSS_REDUCTION
    2. 회복 확인: 10거래일 이동수익률 > 0 → 정상 복구 (재축소 방지)
    3. VIX > VIX_OVERLAY_THRESHOLD → equity -= VIX_OVERLAY_REDUCTION
    4. risk_off 자산 비중 조정으로 전체 합 = 1.0 유지

    Args:
        portfolio: columns=[ticker, weight, strategy_source]
        date: 기준 날짜 (YYYY-MM-DD)
        conn: DuckDB 연결

    Returns:
        pd.DataFrame: columns=[ticker, weight, strategy_source]
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    logger.info(f"[포트폴리오 최적화] apply_risk_overlay 시작: {date}")

    try:
        if portfolio.empty:
            logger.warning("[포트폴리오 최적화] 입력 포트폴리오 비어있음 → 원본 반환")
            return portfolio.copy()

        result = portfolio.copy()
        is_risk_off = (result["strategy_source"] == "risk_off") | result["ticker"].isin(RISK_OFF_TICKERS)
        equity_weight_total = result.loc[~is_risk_off, "weight"].sum()
        risk_off_weight_total = result.loc[is_risk_off, "weight"].sum()

        equity_scale = 1.0  # 최종 주식 비중 스케일 팩터

        # 1. 20거래일 누적수익률 계산
        cumret_20d = _fetch_portfolio_cumret(result, date, lookback=20, conn=conn)

        if cumret_20d is not None:
            logger.info(f"[포트폴리오 최적화] 20거래일 누적수익률: {cumret_20d:.4f}")

            if cumret_20d <= STOP_LOSS_THRESHOLD:
                # 2. 회복 확인: 10거래일 이동수익률
                cumret_10d = _fetch_portfolio_cumret(result, date, lookback=10, conn=conn)
                is_recovering = cumret_10d is not None and cumret_10d > 0

                if not is_recovering:
                    equity_scale *= STOP_LOSS_REDUCTION
                    logger.warning(
                        f"[포트폴리오 최적화] 손절 발동: 20거래일 누적수익률={cumret_20d:.4f} "
                        f"<= 임계값={STOP_LOSS_THRESHOLD:.4f} → 주식 비중 {STOP_LOSS_REDUCTION:.0%}로 축소"
                    )
                else:
                    logger.info(
                        f"[포트폴리오 최적화] 손절 임계 도달했으나 10거래일 수익률={cumret_10d:.4f} > 0 "
                        f"→ 회복 중, 손절 미적용"
                    )
        else:
            logger.debug("[포트폴리오 최적화] 20거래일 누적수익률 계산 불가 → 손절 스킵")

        # 3. VIX 오버레이
        vix = _fetch_vix(date, conn)

        if vix is not None:
            logger.info(f"[포트폴리오 최적화] VIX: {vix:.2f}")

            if vix > VIX_OVERLAY_THRESHOLD:
                vix_reduction_ratio = 1.0 - (VIX_OVERLAY_REDUCTION / max(equity_scale, 1e-9))
                # equity_scale에 직접 -20%p 적용 (비율이 아닌 절대값)
                current_equity_w = equity_weight_total * equity_scale
                reduced_equity_w = max(current_equity_w - VIX_OVERLAY_REDUCTION, 0.0)

                if current_equity_w > 0:
                    equity_scale *= reduced_equity_w / current_equity_w
                else:
                    equity_scale = 0.0

                logger.warning(
                    f"[포트폴리오 최적화] VIX 오버레이 발동: VIX={vix:.2f} > {VIX_OVERLAY_THRESHOLD} "
                    f"→ 주식 비중 -{VIX_OVERLAY_REDUCTION:.0%}p 추가 축소 "
                    f"(equity_scale={equity_scale:.4f})"
                )
        else:
            logger.debug("[포트폴리오 최적화] VIX 조회 실패 → VIX 오버레이 스킵")

        # 4. 비중 적용
        if abs(equity_scale - 1.0) < 1e-6:
            logger.info("[포트폴리오 최적화] 오버레이 미적용 (equity_scale=1.0) → 원본 반환")
            return result

        # 주식 비중 조정
        result.loc[~is_risk_off, "weight"] = result.loc[~is_risk_off, "weight"] * equity_scale

        new_equity_total = result.loc[~is_risk_off, "weight"].sum()
        added_to_risk_off = equity_weight_total - new_equity_total

        # risk_off 비중을 줄어든 주식 비중만큼 증가
        if risk_off_weight_total > 0 and added_to_risk_off > 1e-9:
            risk_off_scale = (risk_off_weight_total + added_to_risk_off) / risk_off_weight_total
            result.loc[is_risk_off, "weight"] = result.loc[is_risk_off, "weight"] * risk_off_scale
        elif added_to_risk_off > 1e-9:
            # risk_off 종목이 없으면 SHY를 추가
            risk_off_row = pd.DataFrame([{
                "ticker": "SHY",
                "weight": added_to_risk_off,
                "strategy_source": "risk_off",
            }])
            result = pd.concat([result, risk_off_row], ignore_index=True)
            logger.info(
                f"[포트폴리오 최적화] SHY 추가: weight={added_to_risk_off:.4f} "
                f"(risk_off 종목 없음)"
            )

        # 최종 정규화
        result = _normalize_weights(result)
        result = result[result["weight"] > 1e-9].copy()
        result = result.sort_values("weight", ascending=False).reset_index(drop=True)

        final_equity = result.loc[
            ~((result["strategy_source"] == "risk_off") | result["ticker"].isin(RISK_OFF_TICKERS)),
            "weight"
        ].sum()

        logger.info(
            f"[포트폴리오 최적화] apply_risk_overlay 완료: {date}, "
            f"equity_scale={equity_scale:.4f}, "
            f"최종 주식비중={final_equity:.4f}, "
            f"종목수={len(result)}, "
            f"가중치합={result['weight'].sum():.6f}"
        )
        return result

    finally:
        if close_conn:
            conn.close()


def compute_portfolio_stats(
    portfolio: pd.DataFrame,
    date: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> dict:
    """
    포트폴리오 통계 산출.

    Args:
        portfolio: columns=[ticker, weight] (strategy_source 옵션)
        date: 기준 날짜 (YYYY-MM-DD)
        conn: DuckDB 연결

    Returns:
        {
            'n_stocks': int,
            'max_weight': float,
            'hhi': float,          # Herfindahl-Hirschman Index (집중도)
            'top_5_weight': float, # 상위 5종목 합산 비중
            'estimated_annual_vol': float | None,  # 추정 연율 변동성
        }
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    logger.info(f"[포트폴리오 최적화] compute_portfolio_stats 시작: {date}")

    try:
        if portfolio.empty:
            return {
                "n_stocks": 0,
                "max_weight": 0.0,
                "hhi": 0.0,
                "top_5_weight": 0.0,
                "estimated_annual_vol": None,
            }

        weights = portfolio["weight"].values
        n_stocks = int((weights > 0).sum())
        max_weight = float(weights.max())

        # HHI (Herfindahl-Hirschman Index): 가중치 제곱합
        hhi = float(np.sum(weights ** 2))

        # 상위 5종목 비중 합
        top_5_weight = float(np.sort(weights)[::-1][:5].sum())

        # 추정 연율 변동성 (가용 데이터 있는 경우)
        estimated_vol = _estimate_portfolio_vol(portfolio, date, conn)

        stats = {
            "n_stocks": n_stocks,
            "max_weight": max_weight,
            "hhi": hhi,
            "top_5_weight": top_5_weight,
            "estimated_annual_vol": estimated_vol,
        }

        vol_str = f"{estimated_vol:.4f}" if estimated_vol else "N/A"
        logger.info(
            f"[포트폴리오 최적화] 통계 산출 완료: "
            f"종목수={n_stocks}, max_weight={max_weight:.4f}, "
            f"HHI={hhi:.4f}, top5={top_5_weight:.4f}, "
            f"vol={vol_str}"
        )
        return stats

    finally:
        if close_conn:
            conn.close()


def _estimate_portfolio_vol(
    portfolio: pd.DataFrame,
    date: str,
    conn: duckdb.DuckDBPyConnection,
    lookback: int = 60,
) -> Optional[float]:
    """
    포트폴리오 추정 연율 변동성 계산 (간략화: 가중 개별 변동성 합산).

    상관관계를 모두 계산하면 연산이 과도하므로
    가중 개별 변동성을 합산하는 근사치 사용.
    """
    # risk_off 및 CASH 제외
    equity_df = portfolio[
        ~portfolio["ticker"].isin(RISK_OFF_TICKERS) &
        (portfolio.get("strategy_source", pd.Series([""] * len(portfolio))) != "risk_off")
    ].copy()

    if equity_df.empty or equity_df["weight"].sum() <= 0:
        return None

    tickers = equity_df["ticker"].tolist()
    ticker_weights = dict(zip(equity_df["ticker"], equity_df["weight"]))

    placeholders = ",".join(["?" for _ in tickers])
    prices_df = conn.execute(
        f"""
        SELECT ticker, date, adj_close
        FROM raw.prices
        WHERE ticker IN ({placeholders})
          AND date <= CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT ?
        """,
        [*tickers, date, (lookback + 2) * len(tickers)],
    ).df()

    if prices_df.empty:
        return None

    prices_df = prices_df.sort_values("date")
    pivot = prices_df.pivot_table(index="date", columns="ticker", values="adj_close")

    if len(pivot) < lookback + 1:
        return None

    rets = pivot.pct_change().dropna()
    if len(rets) < lookback:
        return None

    rets_tail = rets.iloc[-lookback:]
    weighted_vol = 0.0
    total_w = 0.0

    for ticker in tickers:
        if ticker not in rets_tail.columns:
            continue
        ticker_rets = rets_tail[ticker].dropna()
        if len(ticker_rets) < 20:
            continue
        vol = float(ticker_rets.std() * np.sqrt(252))
        w = ticker_weights.get(ticker, 0.0)
        weighted_vol += vol * w
        total_w += w

    if total_w <= 0:
        return None

    return weighted_vol / total_w


def stress_test(
    portfolio: pd.DataFrame,
    scenario: Optional[str] = None,
) -> dict:
    """
    시나리오별 예상 손실 계산.

    주식 종목 → equity shock 적용
    risk_off 자산(TLT/SHY) → treasury shock 적용
    CASH → 0

    Args:
        portfolio: columns=[ticker, weight, strategy_source]
        scenario: 시나리오명 (None이면 전체 실행)

    Returns:
        {scenario_name: {'expected_loss': float, 'description': str}}
    """
    logger.info(
        f"[포트폴리오 최적화] stress_test 시작: "
        f"scenario={scenario or '전체'}, 종목수={len(portfolio)}"
    )

    if portfolio.empty:
        logger.warning("[포트폴리오 최적화] 입력 포트폴리오 비어있음 → 빈 결과 반환")
        return {}

    # 시나리오 목록 결정
    if scenario is not None:
        if scenario not in STRESS_SHOCKS:
            logger.warning(f"[포트폴리오 최적화] 알 수 없는 시나리오: {scenario}")
            return {}
        scenarios_to_run = {scenario: STRESS_SHOCKS[scenario]}
    else:
        scenarios_to_run = STRESS_SHOCKS

    # 종목 분류
    is_risk_off_source = portfolio["strategy_source"] == "risk_off"
    is_risk_off_ticker = portfolio["ticker"].isin({"TLT", "SHY"})
    is_cash = portfolio["ticker"] == "CASH"

    equity_weight = portfolio.loc[
        ~is_risk_off_source & ~is_risk_off_ticker & ~is_cash,
        "weight"
    ].sum()

    treasury_weight = portfolio.loc[
        (is_risk_off_source | is_risk_off_ticker) & ~is_cash,
        "weight"
    ].sum()

    cash_weight = portfolio.loc[is_cash, "weight"].sum()

    logger.info(
        f"[포트폴리오 최적화] 종목 분류: "
        f"주식={equity_weight:.4f}, 채권={treasury_weight:.4f}, 현금={cash_weight:.4f}"
    )

    results = {}

    for scen_name, shock in scenarios_to_run.items():
        equity_shock = shock["equity"]
        treasury_shock = shock["treasury"]

        expected_loss = (
            equity_weight * equity_shock
            + treasury_weight * treasury_shock
            # CASH는 0
        )

        results[scen_name] = {
            "expected_loss": round(expected_loss, 6),
            "description": shock["description"],
            "equity_weight": round(equity_weight, 4),
            "treasury_weight": round(treasury_weight, 4),
            "cash_weight": round(cash_weight, 4),
            "equity_shock": equity_shock,
            "treasury_shock": treasury_shock,
        }

        logger.info(
            f"[포트폴리오 최적화] 스트레스 테스트 '{scen_name}': "
            f"예상손실={expected_loss:.4f} ({shock['description']})"
        )

    logger.info(
        f"[포트폴리오 최적화] stress_test 완료: {len(results)}개 시나리오"
    )
    return results
