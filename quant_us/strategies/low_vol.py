"""
저변동성 전략 (Low Volatility Strategy)

신호: LV_i = -σ_i
- 252거래일 일간 수익률 표준편차 (연율화)
- 변동성 낮을수록 좋음 (음수로 표현)

포트폴리오:
- 변동성 하위 20% 종목
- 변동성 역가중 (1/σ 정규화)
- 섹터 편중 완화: 섹터별 최대 30%
- 추세 오버레이: SPY 200일 MA (상회=100%, 하회=50%)
- 매월 리밸런싱
"""

import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional
import pandas as pd
import numpy as np
import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import logger
from strategies.universe import get_universe


# ── 헬퍼 함수 ────────────────────────────────────────────────────────────

def _get_volatility(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of_date: str,
    lookback_days: int = 252,
) -> Optional[float]:
    """
    종목의 변동성 계산 (252거래일 기준).

    Args:
        conn: DuckDB 연결
        ticker: 티커
        as_of_date: 기준 날짜
        lookback_days: 조회 기간 (거래일)

    Returns:
        float: 변동성 (연율화된 일간 수익률 표준편차)
    """
    try:
        # 기준일 기준 252거래일 전 데이터 조회
        result = conn.execute("""
            SELECT date, adj_close
            FROM raw.prices
            WHERE ticker = ? AND date <= ?
            ORDER BY date DESC
            LIMIT ?
        """, [ticker, as_of_date, lookback_days + 10]).df()

        if len(result) < 2:
            return None

        # 오름차순 정렬
        result = result.sort_values('date')

        # 일간 수익률 계산
        prices = result['adj_close'].values
        returns = np.diff(prices) / prices[:-1]

        if len(returns) == 0:
            return None

        # 표준편차 (일간) → 연율화 (√252)
        daily_vol = np.std(returns)
        annualized_vol = daily_vol * np.sqrt(252)

        return annualized_vol if annualized_vol > 0 else None

    except Exception as e:
        logger.debug(f"{ticker}: 변동성 계산 실패 - {e}")
        return None


def _get_gics_sector(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
) -> Optional[str]:
    """
    종목의 GICS 섹터 조회 (별도 테이블 필요).
    현재는 주가 데이터의 출처 정보 활용 또는 고정 매핑.

    Args:
        conn: DuckDB 연결
        ticker: 티커

    Returns:
        str: 섹터 (또는 None)
    """
    # 임시: 고정 매핑 (실제로는 별도 마스터 테이블 필요)
    sector_map = {
        'AAPL': 'IT', 'MSFT': 'IT', 'GOOGL': 'IT', 'AMZN': 'Consumer', 'TSLA': 'Consumer',
        'BRK.B': 'Financial', 'JNJ': 'Healthcare', 'V': 'Financial', 'WMT': 'Consumer',
        'JPM': 'Financial', 'PG': 'Consumer', 'XOM': 'Energy', 'KO': 'Consumer',
    }
    return sector_map.get(ticker)


def _get_spy_trend(
    conn: duckdb.DuckDBPyConnection,
    as_of_date: str,
) -> float:
    """
    SPY 200일 이동평균 대비 현재 추세 판정.

    Returns:
        float: 1.0 (상회) 또는 0.5 (하회)
    """
    try:
        # SPY 최근 200거래일 조회
        result = conn.execute("""
            SELECT date, adj_close
            FROM raw.prices
            WHERE ticker = 'SPY' AND date <= ?
            ORDER BY date DESC
            LIMIT 200
        """, [as_of_date]).df()

        if len(result) < 200:
            logger.debug(f"SPY: 200일 데이터 부족 ({len(result)})")
            return 1.0  # 기본값: 100% 익스포저

        # 200일 MA
        ma200 = result['adj_close'].mean()
        current_price = result.iloc[-1]['adj_close']

        if current_price > ma200:
            return 1.0  # 100%
        else:
            return 0.5  # 50%

    except Exception as e:
        logger.warning(f"SPY 추세 판정 실패: {e}")
        return 1.0  # 기본값


# ── 핵심 함수 ────────────────────────────────────────────────────────────

def compute_signal(
    date: str,
    universe: List[str],
    conn: duckdb.DuckDBPyConnection,
) -> pd.Series:
    """
    저변동성 신호 산출.

    신호 = -σ (변동성이 낮을수록 높은 신호)

    Args:
        date: 기준 날짜
        universe: 투자 유니버스
        conn: DuckDB 연결

    Returns:
        pd.Series: 신호 (인덱스=ticker, 값=음의 변동성)
    """
    logger.info(f"[저변동성 신호] 산출 시작: {date}, 종목: {len(universe)}개")

    volatilities = {}

    for ticker in universe:
        vol = _get_volatility(conn, ticker, date)
        if vol is not None and vol > 0:
            volatilities[ticker] = -vol  # 음수로 변환 (낮은 변동성 = 높은 신호)

    if not volatilities:
        logger.warning(f"{date}: 변동성 데이터 없음")
        return pd.Series(dtype=float)

    signal = pd.Series(volatilities)
    logger.info(f"[저변동성 신호] 완료: {len(signal)}개 종목, 범위=[{signal.min():.4f}, {signal.max():.4f}]")

    return signal


def get_portfolio(
    date: str,
    conn: duckdb.DuckDBPyConnection,
    equity_exposure: float = 1.0,
) -> pd.DataFrame:
    """
    저변동성 포트폴리오 구성.

    - 변동성 하위 20% 선정
    - 변동성 역가중 (1/σ)
    - 섹터 상한 30%
    - 추세 오버레이 (equity_exposure)

    Args:
        date: 기준 날짜
        conn: DuckDB 연결
        equity_exposure: 주식 익스포저 (0~1)

    Returns:
        pd.DataFrame: (ticker, weight, signal_score)
    """
    logger.info(f"저변동성 포트폴리오 구성: {date}, 익스포저={equity_exposure}")

    # 1. 유니버스 선정
    universe = get_universe(date, conn)
    if not universe:
        logger.warning(f"{date}: 유니버스 없음")
        return pd.DataFrame(columns=['ticker', 'weight', 'signal_score'])

    # 2. 신호 산출
    signal = compute_signal(date, universe, conn)
    if signal.empty:
        logger.warning(f"{date}: 신호 없음")
        return pd.DataFrame(columns=['ticker', 'weight', 'signal_score'])

    # 3. 변동성 하위 20% 선정
    quantile_20 = signal.quantile(0.2)
    selected = signal[signal <= quantile_20]

    if len(selected) == 0:
        logger.warning(f"{date}: 하위 20% 종목 없음")
        return pd.DataFrame(columns=['ticker', 'weight', 'signal_score'])

    # 4. 변동성 역가중 (1/σ)
    # signal = -σ이므로, 역가중 = 1 / (-signal) = -1/σ
    inverse_vol = 1.0 / (-selected)  # σ로 변환 후 역수
    normalized_weights = inverse_vol / inverse_vol.sum()

    # 5. 섹터 상한 30% 적용 (간단한 구현)
    sector_exposures = {}
    for ticker in normalized_weights.index:
        sector = _get_gics_sector(conn, ticker) or "Unknown"
        if sector not in sector_exposures:
            sector_exposures[sector] = 0.0
        sector_exposures[sector] += normalized_weights[ticker]

    # 섹터별 상한 조정
    adjusted_weights = normalized_weights.copy()
    for sector, exposure in sector_exposures.items():
        if exposure > 0.30:
            # 이 섹터의 종목들을 비율대로 축소
            sector_tickers = [t for t in selected.index if _get_gics_sector(conn, t) == sector]
            reduction = 0.30 / exposure
            for ticker in sector_tickers:
                adjusted_weights[ticker] *= reduction

    # 정규화 (가중치 합 = 1.0)
    adjusted_weights = adjusted_weights / adjusted_weights.sum()

    # 6. 추세 오버레이 (equity_exposure)
    equity_exposure = _get_spy_trend(conn, date) if equity_exposure == 1.0 else equity_exposure
    adjusted_weights = adjusted_weights * equity_exposure

    # 나머지는 현금 (포트폴리오에 명시하지 않음, 백테스터에서 처리)

    # 7. 결과 DataFrame
    result = pd.DataFrame({
        'ticker': adjusted_weights.index,
        'weight': adjusted_weights.values,
        'signal_score': signal[adjusted_weights.index].values,
    })

    logger.info(f"[저변동성 포트폴리오] 완료: {len(result)}개 종목, 가중치합={result['weight'].sum():.4f}")

    return result


if __name__ == "__main__":
    from db.init import get_connection

    conn = get_connection()

    # 테스트
    test_date = "2024-01-02"
    test_universe = get_universe(test_date, conn)

    if test_universe:
        signal = compute_signal(test_date, test_universe, conn)
        print(f"신호 (상위 10):\n{signal.nlargest(10)}")

        portfolio = get_portfolio(test_date, conn)
        print(f"\n포트폴리오 (상위 10):\n{portfolio.head(10)}")

    conn.close()
