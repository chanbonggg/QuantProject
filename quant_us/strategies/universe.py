"""
공통 투자 유니버스 (Universe Selection)
- 해당 날짜 S&P500 구성 종목
- 5가지 필터 조건 적용
"""

from datetime import date, datetime, timedelta
from typing import List, Optional
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import duckdb

from utils.logger import logger


def get_universe(date_val: str, conn: duckdb.DuckDBPyConnection) -> List[str]:
    """
    해당 날짜 기준 S&P500 투자 유니버스 선정.

    필터 조건:
    1. 해당 날짜 S&P500 구성 종목 (편입 후, 편출 전)
    2. 상장 후 6개월 미만 제외
    3. 최근 60거래일 평균 거래대금 하위 10% 제외
    4. 주가 $5 미만 제외 (페니스톡)
    5. 상장폐지/합병 플래그 제외

    Args:
        date_val: 기준 날짜 (YYYY-MM-DD 형식 문자열)
        conn: DuckDB 연결

    Returns:
        List[str]: 필터링된 티커 목록
    """

    try:
        date_obj = pd.to_datetime(date_val).date()
        date_val = date_obj.strftime('%Y-%m-%d')  # 'YYYY-MM-DD 00:00:00' 형식 정규화
    except Exception as e:
        logger.error(f"날짜 파싱 실패: {date_val} - {e}")
        return []

    # ───────────────────────────────────────────────────────────────
    # 1. S&P500 구성 종목 결정 (편입/편출 이력 고려)
    # ───────────────────────────────────────────────────────────────

    try:
        sp500_base = conn.execute("""
            SELECT DISTINCT ticker FROM raw.sp500_changes
            WHERE date <= ?
            ORDER BY ticker
        """, [date_val]).df()

        if sp500_base.empty:
            # sp500_changes에 해당 날짜 이전 데이터 없음 → 가장 오래된 스냅샷 사용 (폴백)
            oldest = conn.execute("""
                SELECT MIN(date) FROM raw.sp500_changes
            """).fetchone()[0]
            logger.warning(f"기준일 {date_val}의 S&P500 변경 이력 없음. 가장 오래된 스냅샷({oldest}) 사용")
            sp500_base = conn.execute("""
                SELECT DISTINCT ticker FROM raw.sp500_changes
                WHERE date = (SELECT MIN(date) FROM raw.sp500_changes)
                ORDER BY ticker
            """).df()
            if sp500_base.empty:
                logger.warning(f"sp500_changes 테이블이 비어 있음")
                return []

        # 편입 이력 조회 (date 기준으로 가장 최근)
        sp500_add = conn.execute("""
            SELECT DISTINCT ticker FROM raw.sp500_changes
            WHERE action = 'add' AND date <= ?
            GROUP BY ticker
            HAVING MAX(date) <= ?
        """, [date_val, date_val]).df()

        # 편출 이력 조회 (date 기준)
        sp500_remove = conn.execute("""
            SELECT DISTINCT ticker FROM raw.sp500_changes
            WHERE action = 'remove' AND date <= ?
        """, [date_val]).df()

        # 과거 날짜(sp500_changes 최초 스냅샷 이전)면 전체 티커를 candidates로 사용
        if sp500_add.empty:
            candidates = set(sp500_base['ticker'].values)
            logger.info(f"add 이력 없음 → 전체 스냅샷 {len(candidates)}개 종목 사용")
        else:
            candidates = set(sp500_add['ticker'].values)

        removed_set = set(sp500_remove['ticker'].values) if not sp500_remove.empty else set()

        # 현재 S&P500에 속한 종목 = (편입된 것) - (편출된 것)
        sp500_tickers = list(candidates - removed_set)

        if not sp500_tickers:
            logger.warning(f"기준일 {date_val}의 유효한 S&P500 종목 없음")
            return []

        logger.info(f"S&P500 기본 종목 수: {len(sp500_tickers)} - {sp500_tickers[:5]}...")

    except Exception as e:
        logger.error(f"S&P500 구성종목 조회 실패: {e}")
        return []

    # ───────────────────────────────────────────────────────────────
    # 2. 주가 데이터 기반 필터링
    # ───────────────────────────────────────────────────────────────

    try:
        # 기준일 기준 과거 200일 범위 (6개월 이상 상장 조건 충족)
        date_obj_dt = datetime.strptime(date_val, '%Y-%m-%d')
        date_start = (date_obj_dt - timedelta(days=200)).strftime('%Y-%m-%d')

        price_df = conn.execute("""
            SELECT ticker, date, adj_close, volume
            FROM raw.prices
            WHERE ticker IN ({})
            AND date <= ?
            AND date >= ?
            ORDER BY ticker, date
        """.format(','.join(['?' for _ in sp500_tickers])),
               [*sp500_tickers, date_val, date_start]).df()

        if price_df.empty:
            logger.warning(f"기준일 {date_val}의 주가 데이터 없음")
            return []

        logger.info(f"주가 데이터 조회: {len(price_df)}개 로우, {price_df['ticker'].nunique()}개 종목")

        # 필터 2: 상장 후 6개월 미만 제외 (최초 데이터가 6개월 이전)
        min_ipo_date = pd.Timestamp(date_obj - timedelta(days=180))
        ipo_tickers = price_df.groupby('ticker')['date'].min()
        ipo_valid = set(ipo_tickers[ipo_tickers <= min_ipo_date].index)

        logger.info(f"필터 2 (상장 6개월 이상): {len(ipo_valid)}개 종목")

        # 필터 3: 최근 60거래일 평균 거래대금 하위 10% 제외
        volume_60d = price_df[price_df['ticker'].isin(ipo_valid)].copy()
        volume_60d['dollar_volume'] = volume_60d['adj_close'] * volume_60d['volume']
        avg_vol = volume_60d.groupby('ticker')['dollar_volume'].mean()

        vol_p10 = avg_vol.quantile(0.10)
        vol_valid = set(avg_vol[avg_vol >= vol_p10].index)

        logger.info(f"필터 3 (거래대금 10분위수): {len(vol_valid)}개 종목, 10분위수={vol_p10:.0f}")

        # 필터 4: 최근 종가 $5 이상
        latest_prices = (
            price_df[price_df['ticker'].isin(vol_valid)]
            .sort_values('date')
            .groupby('ticker')
            .tail(1)
        )
        price_valid = set(latest_prices[latest_prices['adj_close'] >= 5.0]['ticker'].values)

        logger.info(f"필터 4 (최근 종가 $5 이상): {len(price_valid)}개 종목")

    except Exception as e:
        logger.error(f"주가 기반 필터링 실패: {e}")
        return []

    # ───────────────────────────────────────────────────────────────
    # 5. 상장폐지/합병 제외 (ticker_events 확인)
    # ───────────────────────────────────────────────────────────────

    try:
        delisted = conn.execute("""
            SELECT DISTINCT ticker FROM raw.ticker_events
            WHERE event_type IN ('delisted', 'merger')
            AND event_date <= ?
        """, [date_val]).df()

        delisted_set = set(delisted['ticker'].values) if not delisted.empty else set()

    except Exception as e:
        logger.warning(f"상장폐지 이력 조회 실패: {e}")
        delisted_set = set()

    logger.info(f"필터 5 (상장폐지 제외): 제외 {len(delisted_set)}개 종목")

    # ───────────────────────────────────────────────────────────────
    # 최종 유니버스
    # ───────────────────────────────────────────────────────────────

    universe = list(price_valid - delisted_set)
    universe.sort()

    logger.info(f"유니버스 확정 (기준일 {date_val}): {len(universe)}개 종목 - {universe[:10]}")

    return universe
