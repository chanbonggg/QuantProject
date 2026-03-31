"""
모멘텀 전략 (12-1 Momentum)
- S&P500 대비 초과수익률 신호
- 최근 1개월 제외 (단기 반전 회피)
- 매월 말 리밸런싱
"""

from datetime import datetime, date, timedelta
from typing import List, Optional
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
import duckdb

from utils.logger import logger
from strategies.universe import get_universe


# ─────────────────────────────────────────────────────────────────
# 헬퍼 함수
# ─────────────────────────────────────────────────────────────────

def _get_trading_dates(
    conn: duckdb.DuckDBPyConnection,
    start_date: str,
    end_date: str,
) -> List[date]:
    """
    주어진 기간의 거래일 목록 조회.

    Args:
        conn: DuckDB 연결
        start_date: 시작 날짜 (YYYY-MM-DD)
        end_date: 종료 날짜 (YYYY-MM-DD)

    Returns:
        List[date]: 거래일 목록 (오름차순 정렬)
    """
    try:
        result = conn.execute("""
            SELECT DISTINCT date FROM raw.prices
            WHERE date >= ? AND date <= ?
            ORDER BY date ASC
        """, [start_date, end_date]).df()

        return sorted(result['date'].unique().tolist())

    except Exception as e:
        logger.error(f"거래일 조회 실패: {e}")
        return []


def _get_cumulative_return(
    prices: pd.Series,
) -> float:
    """
    누적 수익률 계산: (마지막 가격 - 첫번째 가격) / 첫번째 가격.

    Args:
        prices: 정렬된 가격 Series

    Returns:
        float: 누적 수익률 (-1 <= r <= ∞)
    """
    if len(prices) < 2:
        return 0.0

    first_price = prices.iloc[0]
    last_price = prices.iloc[-1]

    if first_price <= 0:
        return 0.0

    return (last_price - first_price) / first_price


def _normalize_to_zscore(scores: pd.Series) -> pd.Series:
    """
    점수를 z-score로 정규화 후 [-1, 1] 범위로 클립.

    Args:
        scores: 원본 점수 Series (인덱스=ticker)

    Returns:
        pd.Series: 정규화된 신호 (-1 <= 값 <= 1)
    """
    mean = scores.mean()
    std = scores.std()

    if std == 0:
        # 표준편차가 0이면 모두 0으로 반환
        return pd.Series(0.0, index=scores.index)

    z = (scores - mean) / std
    # [-1, 1] 범위로 클립
    z_clipped = z.clip(-1.0, 1.0)

    return z_clipped


# ─────────────────────────────────────────────────────────────────
# 신호 계산
# ─────────────────────────────────────────────────────────────────

def compute_signal(
    date_val: str,
    universe: List[str],
    conn: duckdb.DuckDBPyConnection,
) -> pd.Series:
    """
    12-1 모멘텀 신호 계산: R(t-12:t-1) - R_SP500(t-12:t-1).

    신호 = 12개월 누적 수익률(최근 1개월 제외) - S&P500 12개월 누적 수익률
    z-score 정규화 → [-1, 1] 범위

    Args:
        date_val: 기준 날짜 (YYYY-MM-DD)
        universe: 종목 목록
        conn: DuckDB 연결

    Returns:
        pd.Series: 신호 (인덱스=ticker, 값=-1~1)
    """

    logger.info(f"[모멘텀 신호 산출] 기준일: {date_val}, 종목: {len(universe)}개")

    try:
        date_obj = pd.to_datetime(date_val).date()
    except Exception as e:
        logger.error(f"날짜 파싱 실패: {date_val} - {e}")
        return pd.Series(dtype=float)

    if not universe:
        logger.warning("유니버스가 비어있음")
        return pd.Series(dtype=float)

    # ───────────────────────────────────────────────────────────────
    # 1. 기간 설정
    # ───────────────────────────────────────────────────────────────

    # 12개월 이전 ~ 1개월 이전 (recent 1개월 제외)
    period_end = date_obj - timedelta(days=30)  # t-1: 최근 1개월 전
    period_start = period_end - timedelta(days=365)  # t-12: 12개월 이전

    logger.debug(
        f"모멘텀 신호 기간: {period_start} ~ {period_end} (기준일: {date_obj})"
    )

    # ───────────────────────────────────────────────────────────────
    # 2. 종목별 누적 수익률 계산
    # ───────────────────────────────────────────────────────────────

    stock_prices = conn.execute("""
        SELECT ticker, date, adj_close
        FROM raw.prices
        WHERE ticker IN ({})
        AND date >= ? AND date <= ?
        ORDER BY ticker, date
    """.format(','.join(['?' for _ in universe])),
           [*universe, period_start, period_end]).df()

    if stock_prices.empty:
        logger.warning(f"기간 {period_start}~{period_end}의 주가 데이터 없음")
        return pd.Series(dtype=float)

    stock_returns = {}
    for ticker in universe:
        ticker_data = stock_prices[stock_prices['ticker'] == ticker]
        if ticker_data.empty:
            stock_returns[ticker] = 0.0
        else:
            ticker_data = ticker_data.sort_values('date')
            stock_returns[ticker] = _get_cumulative_return(ticker_data['adj_close'])

    stock_returns_series = pd.Series(stock_returns)

    logger.debug(f"종목 누적 수익률 계산 완료: {len(stock_returns)} 종목")

    # ───────────────────────────────────────────────────────────────
    # 3. S&P500 누적 수익률 계산
    # ───────────────────────────────────────────────────────────────

    sp500_data = conn.execute("""
        SELECT date, adj_close FROM raw.prices
        WHERE ticker = '^GSPC'
        AND date >= ? AND date <= ?
        ORDER BY date
    """, [period_start, period_end]).df()

    if sp500_data.empty:
        logger.warning(f"기간 {period_start}~{period_end}의 S&P500 데이터 없음")
        sp500_return = 0.0
    else:
        sp500_data = sp500_data.sort_values('date')
        sp500_return = _get_cumulative_return(sp500_data['adj_close'])

    logger.debug(f"S&P500 12개월 누적 수익률: {sp500_return:.4f}")

    # ───────────────────────────────────────────────────────────────
    # 4. 초과수익률 계산 및 정규화
    # ───────────────────────────────────────────────────────────────

    excess_returns = stock_returns_series - sp500_return

    # z-score 정규화 ([-1, 1] 범위)
    signal = _normalize_to_zscore(excess_returns)

    logger.info(
        f"[모멘텀 신호] 계산 완료: {len(signal)}개 종목, "
        f"평균={signal.mean():.4f}, 표준편차={signal.std():.4f}, "
        f"범위=[{signal.min():.4f}, {signal.max():.4f}]"
    )

    return signal


# ─────────────────────────────────────────────────────────────────
# 포트폴리오 구성
# ─────────────────────────────────────────────────────────────────

def get_portfolio(
    date_val: str,
    conn: duckdb.DuckDBPyConnection,
    sector_neutral: bool = False,
) -> pd.DataFrame:
    """
    모멘텀 포트폴리오 구성: 상위 20% 종목, 동일가중.

    Args:
        date_val: 기준 날짜 (YYYY-MM-DD)
        conn: DuckDB 연결
        sector_neutral: True면 섹터 중립 모멘텀 적용 (현재 미구현)

    Returns:
        pd.DataFrame: 포트폴리오 (컬럼: ticker, weight, signal_score)
    """

    logger.info(f"[모멘텀 포트폴리오] 기준일: {date_val}")

    # 1. 유니버스 선정
    universe = get_universe(date_val, conn)
    if not universe:
        logger.warning(f"기준일 {date_val}의 유니버스가 비어있음")
        return pd.DataFrame(columns=['ticker', 'weight', 'signal_score'])

    # 2. 신호 계산
    signal = compute_signal(date_val, universe, conn)
    if signal.empty:
        logger.warning(f"신호 계산 실패")
        return pd.DataFrame(columns=['ticker', 'weight', 'signal_score'])

    # 3. 상위 20% 선정
    n_total = len(signal)
    n_top = max(1, int(np.ceil(n_total * 0.20)))

    top_tickers = signal.nlargest(n_top).index.tolist()
    top_signal = signal[top_tickers]

    logger.info(
        f"상위 20% 종목 선정: {n_total}개 중 {n_top}개"
    )

    # 4. 동일가중
    n_selected = len(top_tickers)
    weights = pd.Series(1.0 / n_selected, index=top_tickers)

    # 5. DataFrame 구성
    portfolio = pd.DataFrame({
        'ticker': top_tickers,
        'weight': weights.values,
        'signal_score': top_signal.values,
    }).reset_index(drop=True)

    logger.info(
        f"[모멘텀 포트폴리오] 완료: {len(portfolio)}개 종목 (상위 20%), "
        f"가중치합={portfolio['weight'].sum():.6f}, "
        f"신호범위=[{portfolio['signal_score'].min():.4f}, {portfolio['signal_score'].max():.4f}]"
    )

    return portfolio
