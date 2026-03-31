"""
밸류 전략 (Value Strategy)

복합 밸류 점수 산출:
- z(BM) + z(EP) + z(CFP)
- BM: Book-to-Market (Book Value / Market Cap)
- EP: Earnings-to-Price (Net Income / Market Cap)
- CFP: Cash Flow-to-Price (Operating Cash Flow / Market Cap)

룩어헤드 방지:
- SEC 재무 데이터의 filed_date 기준으로만 적용
- as_of_date 이전 filed_date 데이터만 사용

리밸런싱: 분기 1회 (3개월마다)
"""

import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
import duckdb
from scipy import stats

from utils.logger import logger
from strategies.universe import get_universe
from data.collectors.sec_collector import get_latest_financials


# ── 핵심 함수 ───────────────────────────────────────────────────────────────

def compute_signal(
    date_val: str,
    universe: List[str],
    conn: duckdb.DuckDBPyConnection,
) -> pd.Series:
    """
    기준일의 각 종목 밸류 점수 산출.

    점수 = z(BM) + z(EP) + z(CFP)

    Args:
        date_val: 기준 날짜 (YYYY-MM-DD)
        universe: 투자 유니버스 (ticker 리스트)
        conn: DuckDB 커넥션

    Returns:
        pd.Series: 신호 (인덱스=ticker, 값=signal_score)
    """
    logger.info(f"[밸류 신호] 산출 시작: {date_val}, 종목: {len(universe)}개")

    # 1. 마켓캡 & 주가 조회
    market_caps = _get_market_caps(date_val, universe, conn)
    latest_prices = _get_latest_prices(date_val, universe, conn)

    # 2. 재무 데이터 조회 (rulk-ahead 방지: filed_date <= date_val)
    financials_map = _get_financials_map(date_val, universe, conn)

    # 3. 메트릭 계산
    scores_dict: Dict[str, Tuple[float, float, float]] = {}  # {ticker: (BM, EP, CFP)}

    for ticker in universe:
        # 필요한 데이터 확인
        if ticker not in market_caps or ticker not in latest_prices:
            logger.debug(f"{ticker}: 주가 또는 마켓캡 데이터 부족")
            continue

        if ticker not in financials_map:
            logger.debug(f"{ticker}: 재무 데이터 없음")
            continue

        market_cap = market_caps[ticker]
        price = latest_prices[ticker]
        financials = financials_map[ticker]

        if market_cap <= 0 or price <= 0:
            continue

        # Book-to-Market (BM)
        equity = financials.get("stockholders_equity")
        bm = (equity / market_cap) if equity and equity > 0 else np.nan

        # Earnings-to-Price (EP)
        net_income = financials.get("net_income")
        ep = (net_income / market_cap) if net_income and net_income > 0 else np.nan

        # Cash Flow-to-Price (CFP)
        ocf = financials.get("operating_cashflow")
        cfp = (ocf / market_cap) if ocf and ocf > 0 else np.nan

        # 유효한 메트릭 저장 (최소 1개는 있어야 함)
        if not (np.isnan(bm) and np.isnan(ep) and np.isnan(cfp)):
            scores_dict[ticker] = (bm, ep, cfp)

    logger.info(f"메트릭 계산 완료: {len(scores_dict)}/{len(universe)} 종목")

    # 4. z-score 정규화
    signals = _normalize_and_combine(scores_dict)

    # pd.Series로 변환
    signals_series = pd.Series(signals)

    if len(signals_series) > 0:
        logger.info(f"[밸류 신호] 완료: {len(signals_series)}개 종목, 범위=[{signals_series.min():.4f}, {signals_series.max():.4f}], 평균={signals_series.mean():.4f}")
    else:
        logger.warning(f"[밸류 신호] 신호 없음")

    return signals_series


def _get_market_caps(
    date_val: str,
    universe: List[str],
    conn: duckdb.DuckDBPyConnection,
) -> Dict[str, float]:
    """
    기준일 기준 각 종목의 마켓캡 조회.
    (주가 * 발행주식수)

    Returns:
        {ticker: market_cap} 딕셔너리
    """
    market_caps: Dict[str, float] = {}

    # DuckDB에서 시가총액이 직접 저장되어 있는지 확인
    # 현재 raw.prices 스키마에는 시가총액이 없으므로, 가격과 주식수로 계산 필요
    # 하지만 주식수가 없으면 근사값으로 조정 종가 사용

    # 간단히 처리: 최근 조정 종가를 사용 (정확한 시가총액 대신)
    # 실무에서는 주식수 정보가 필요함
    try:
        result = conn.execute(f"""
            SELECT ticker, adj_close
            FROM raw.prices
            WHERE ticker IN ({','.join(['?' for _ in universe])})
            AND date = ?
            ORDER BY ticker
        """, [*universe, date_val]).df()

        if result.empty:
            logger.warning(f"{date_val}의 주가 데이터 없음")
            return {}

        # 간단 근사: 주가를 마켓캡 프록시로 사용
        # 더 정확한 계산을 위해서는 share_count가 필요
        for _, row in result.iterrows():
            market_caps[row["ticker"]] = float(row["adj_close"]) if row["adj_close"] else np.nan

    except Exception as e:
        logger.error(f"마켓캡 조회 실패: {e}")

    return market_caps


def _get_latest_prices(
    date_val: str,
    universe: List[str],
    conn: duckdb.DuckDBPyConnection,
) -> Dict[str, float]:
    """
    기준일 기준 각 종목의 조정 종가 조회.

    Returns:
        {ticker: adj_close} 딕셔너리
    """
    prices: Dict[str, float] = {}

    try:
        result = conn.execute(f"""
            SELECT ticker, adj_close
            FROM raw.prices
            WHERE ticker IN ({','.join(['?' for _ in universe])})
            AND date <= ?
            ORDER BY ticker, date DESC
        """, [*universe, date_val]).df()

        if result.empty:
            return {}

        # 각 ticker별 최신 가격만 선택
        for ticker in universe:
            ticker_data = result[result["ticker"] == ticker]
            if not ticker_data.empty:
                prices[ticker] = float(ticker_data.iloc[0]["adj_close"])

    except Exception as e:
        logger.error(f"주가 조회 실패: {e}")

    return prices


def _get_financials_map(
    date_val: str,
    universe: List[str],
    conn: duckdb.DuckDBPyConnection,
) -> Dict[str, Dict[str, float]]:
    """
    각 종목의 최신 재무 데이터 조회.
    룩어헤드 방지: filed_date <= date_val 조건 적용.

    Returns:
        {ticker: {key: value}} 딕셔너리
    """
    financials_map: Dict[str, Dict[str, float]] = {}

    for ticker in universe:
        financials = get_latest_financials(ticker, date_val, conn)
        if financials:
            financials_map[ticker] = {
                "stockholders_equity": financials.get("stockholders_equity"),
                "net_income": financials.get("net_income"),
                "operating_cashflow": financials.get("operating_cashflow"),
            }

    return financials_map


def _normalize_and_combine(
    scores_dict: Dict[str, Tuple[float, float, float]],
) -> Dict[str, float]:
    """
    각 메트릭 (BM, EP, CFP)을 z-score로 정규화하고,
    유효한 메트릭들의 z-score를 합산.

    Args:
        scores_dict: {ticker: (BM, EP, CFP)}

    Returns:
        {ticker: combined_signal}
    """
    # 각 메트릭 추출
    bm_values = [s[0] for s in scores_dict.values() if not np.isnan(s[0])]
    ep_values = [s[1] for s in scores_dict.values() if not np.isnan(s[1])]
    cfp_values = [s[2] for s in scores_dict.values() if not np.isnan(s[2])]

    # z-score 계산 함수
    def compute_z_score(value: float, values: List[float]) -> float:
        if not values or len(values) < 2 or np.isnan(value):
            return 0.0
        mean = np.mean(values)
        std = np.std(values)
        if std == 0:
            return 0.0
        return (value - mean) / std

    # 신호 결합
    signals: Dict[str, float] = {}
    for ticker, (bm, ep, cfp) in scores_dict.items():
        z_bm = compute_z_score(bm, bm_values)
        z_ep = compute_z_score(ep, ep_values)
        z_cfp = compute_z_score(cfp, cfp_values)

        # 유효한 메트릭만 합산
        combined = 0.0
        count = 0
        if not np.isnan(bm):
            combined += z_bm
            count += 1
        if not np.isnan(ep):
            combined += z_ep
            count += 1
        if not np.isnan(cfp):
            combined += z_cfp
            count += 1

        if count > 0:
            signals[ticker] = combined / count  # 평균 z-score

    return signals


# ── 포트폴리오 구성 ────────────────────────────────────────────────────────

def get_portfolio(
    date_val: str,
    conn: duckdb.DuckDBPyConnection,
    top_percentile: float = 0.2,
) -> pd.DataFrame:
    """
    기준일 기준 밸류 전략 포트폴리오 구성.

    상위 top_percentile % 롱 포지션, 동일가중.

    Args:
        date_val: 기준 날짜 (YYYY-MM-DD)
        conn: DuckDB 커넥션
        top_percentile: 상위 백분위수 (기본 0.2 = 상위 20%)

    Returns:
        pd.DataFrame: 포트폴리오 (컬럼: ticker, weight, signal_score)
    """
    logger.info(f"[밸류 포트폴리오] 기준일: {date_val}, 상위: {top_percentile*100:.0f}%")

    # 1. 유니버스 선정
    universe = get_universe(date_val, conn)
    if not universe:
        logger.warning(f"유니버스 선정 실패: {date_val}")
        return pd.DataFrame(columns=['ticker', 'weight', 'signal_score'])

    # 2. 신호 산출
    signals = compute_signal(date_val, universe, conn)
    if signals.empty:
        logger.warning(f"신호 산출 실패: {date_val}")
        return pd.DataFrame(columns=['ticker', 'weight', 'signal_score'])

    # 3. 상위 20% 선정
    n_total = len(signals)
    n_top = max(1, int(np.ceil(n_total * top_percentile)))
    top_tickers = signals.nlargest(n_top).index.tolist()
    top_signals = signals[top_tickers]

    logger.info(f"상위 {top_percentile*100:.0f}% 종목 수: {n_top}/{n_total}")

    # 4. 동일가중
    n_selected = len(top_tickers)
    weights = pd.Series(1.0 / n_selected, index=top_tickers)

    # 5. DataFrame 구성
    portfolio = pd.DataFrame({
        'ticker': top_tickers,
        'weight': weights.values,
        'signal_score': top_signals.values,
    }).reset_index(drop=True)

    logger.info(
        f"[밸류 포트폴리오] 완료: {len(portfolio)}개 종목, "
        f"가중치합={portfolio['weight'].sum():.6f}, "
        f"신호범위=[{portfolio['signal_score'].min():.4f}, {portfolio['signal_score'].max():.4f}]"
    )

    return portfolio


# ── 진단 함수 ────────────────────────────────────────────────────────────

def diagnose_quintile(
    start_date: str,
    end_date: str,
    conn: duckdb.DuckDBPyConnection,
) -> Dict[str, any]:  # type: ignore
    """
    기간 내 밸류 신호의 분포 및 포트폴리오 성과 진단.

    Returns:
        진단 결과 딕셔너리
    """
    logger.info(f"밸류 퀸틸 진단: {start_date} ~ {end_date}")

    # 조회 날짜 목록 (분기말)
    try:
        dates = conn.execute(f"""
            SELECT DISTINCT date FROM raw.prices
            WHERE date >= ? AND date <= ?
            ORDER BY date
        """, [start_date, end_date]).df()

        if dates.empty:
            logger.warning("조회 기간의 주가 데이터 없음")
            return {}

        sample_dates = [dates.iloc[i]["date"] for i in range(0, len(dates), len(dates) // 5)]

    except Exception as e:
        logger.error(f"진단 날짜 조회 실패: {e}")
        return {}

    # 각 시점별 신호 분포 계산
    diagnostics: Dict[str, any] = {"dates": [], "signals": []}

    for sample_date in sample_dates:
        date_str = str(sample_date)
        universe = get_universe(date_str, conn)
        if universe:
            signals = compute_signal(date_str, universe, conn)
            if signals is not None and len(signals) > 0:
                diagnostics["dates"].append(date_str)
                diagnostics["signals"].append({
                    "count": len(signals),
                    "mean": np.mean(signals.values),
                    "std": np.std(signals.values),
                    "q1": np.percentile(signals.values, 25),
                    "median": np.percentile(signals.values, 50),
                    "q3": np.percentile(signals.values, 75),
                })

    logger.info(f"진단 완료: {len(diagnostics['dates'])}개 시점")

    return diagnostics


if __name__ == "__main__":
    from db.init import get_connection

    # 테스트 예시
    conn = get_connection()
    try:
        test_date = "2024-01-02"
        universe = get_universe(test_date, conn)
        signals = compute_signal(test_date, universe, conn)
        portfolio = get_portfolio(test_date, conn)

        print(f"\n[밸류 전략 테스트] {test_date}")
        print(f"유니버스: {len(universe)}개 종목")
        print(f"신호 산출: {len(signals)}개 종목")
        print(f"포트폴리오: {len(portfolio)}개 종목")
        if not portfolio.empty:
            print(f"상위 5개 가중치:")
            for idx, row in portfolio.head(5).iterrows():
                print(f"  {row['ticker']}: {row['weight']:.4f} (신호: {row['signal_score']:.4f})")
    finally:
        conn.close()
