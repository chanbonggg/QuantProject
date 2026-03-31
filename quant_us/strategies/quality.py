"""
퀄리티 전략 (`strategies/quality.py`)

신호: Q_i = z(ROE) - z(부채비율) - z(이익변동성)
- ROE = 순이익 / 자기자본 (높을수록 좋음)
- 부채비율 = 총부채 / 자기자본 (낮을수록 좋음, 500% 초과 윈저라이징)
- 이익변동성 = 최근 8분기 EPS 표준편차 (낮을수록 좋음)

특수 처리:
- 금융주(Financials): 부채비율 기준 별도 처리 또는 제외
- NaN 처리: 필요한 재무 데이터 부족 시 NaN

포트폴리오: 상위 20% 종목, 동일가중, 분기별 리밸런싱

룩어헤드 방지: filed_date 기준으로만 재무 데이터 사용
"""

import sys
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import numpy as np
import duckdb
from scipy import stats

from db.init import get_connection
from utils.logger import logger


# ── 유틸리티 ────────────────────────────────────────────────────────────

def _zscore_normalize(series: pd.Series) -> pd.Series:
    """
    Z-score 정규화: (x - mean) / std
    NaN 처리: 계산에서 제외
    """
    valid = series.dropna()
    if len(valid) < 2:
        return pd.Series(np.nan, index=series.index)

    mean = valid.mean()
    std = valid.std()

    if std == 0:
        return pd.Series(0.0, index=series.index)

    return (series - mean) / std


def _winsorize_value(value: float, lower: float = 0.01, upper: float = 0.99) -> float:
    """
    Winsorize: 극값 제한
    부채비율 500% 이상 → 500%로 제한
    """
    if pd.isna(value):
        return np.nan

    # 특정 범위 제한 (부채비율의 경우)
    if value > 5.0:  # 500%
        return 5.0

    return value


def _get_gics_sector(ticker: str, conn: duckdb.DuckDBPyConnection) -> Optional[str]:
    """
    ticker의 GICS 섹터를 조회한다.
    (향후 normalized 테이블에 섹터 정보 추가 시 사용)
    """
    # TODO: normalized.ticker_info 테이블 추가 후 구현
    # 현재는 None 반환 (금융주 필터 선택적)
    return None


# ── 신호 산출 ────────────────────────────────────────────────────────────

def compute_signal(
    date: str,
    universe: list[str],
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> pd.Series:
    """
    퀄리티 신호 산출.

    Args:
        date: 기준일 ('YYYY-MM-DD')
        universe: 종목 리스트
        conn: DuckDB 커넥션 (None이면 자동 생성)

    Returns:
        pd.Series(인덱스=티커, 값=퀄리티 점수)
        점수 = z(ROE) - z(부채비율) - z(이익변동성)
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        # 1. 각 종목별 최신 재무 데이터 조회
        roe_scores = []
        debt_scores = []
        eps_vol_scores = []
        tickers_valid = []

        for ticker in universe:
            roe, debt_ratio, eps_vol = _calculate_quality_metrics(
                ticker, date, conn
            )

            # NaN 체크: 모든 메트릭이 필요
            if pd.notna(roe) and pd.notna(debt_ratio) and pd.notna(eps_vol):
                roe_scores.append(roe)
                debt_scores.append(debt_ratio)
                eps_vol_scores.append(eps_vol)
                tickers_valid.append(ticker)

        if not tickers_valid:
            logger.warning(f"퀄리티 신호: 유효한 데이터 없음 ({date})")
            return pd.Series(dtype=float)

        logger.info(
            f"[퀄리티 신호] 산출: {len(tickers_valid)}/{len(universe)} 종목 ({date})"
        )

        # 2. Z-score 정규화
        roe_z = _zscore_normalize(pd.Series(roe_scores, index=tickers_valid))
        debt_z = _zscore_normalize(pd.Series(debt_scores, index=tickers_valid))
        eps_vol_z = _zscore_normalize(
            pd.Series(eps_vol_scores, index=tickers_valid)
        )

        # 3. 신호 합산: z(ROE) - z(부채비율) - z(이익변동성)
        signal = roe_z - debt_z - eps_vol_z

        return signal

    except Exception as e:
        logger.error(f"퀄리티 신호 산출 실패 ({date}): {e}")
        return pd.Series(dtype=float)
    finally:
        if close_conn:
            conn.close()


def _calculate_quality_metrics(
    ticker: str,
    date: str,
    conn: duckdb.DuckDBPyConnection,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    단일 종목의 ROE, 부채비율, EPS변동성을 계산한다.

    XBRL 데이터는 같은 기간에도 여러 filing에 분산될 수 있으므로,
    각 필드별로 최신값을 개별 조회하여 가용 데이터를 최대화한다.

    Returns:
        (ROE, 부채비율, EPS변동성)
    """
    # 1. 각 필드별 최신값 조회 (필드가 다른 filing에 분산되어도 수집 가능)
    net_income = _get_latest_field(ticker, date, "net_income", conn)
    stockholders_equity = _get_latest_field(ticker, date, "stockholders_equity", conn)
    total_liabilities = _get_latest_field(ticker, date, "total_liabilities", conn)
    total_assets = _get_latest_field(ticker, date, "total_assets", conn)

    # 2. ROE 계산: 순이익 / 자기자본
    roe = None
    if pd.notna(net_income) and pd.notna(stockholders_equity) and stockholders_equity != 0:
        roe = net_income / stockholders_equity

    # 3. 부채비율 계산: 총부채 / 자기자본
    # total_liabilities가 없으면 total_assets - stockholders_equity로 추정
    if pd.isna(total_liabilities) and pd.notna(total_assets) and pd.notna(stockholders_equity):
        total_liabilities = total_assets - stockholders_equity

    debt_ratio = None
    if pd.notna(total_liabilities) and pd.notna(stockholders_equity) and stockholders_equity != 0:
        debt_ratio = total_liabilities / stockholders_equity
        # 윈저라이징: 500% 이상 제한
        debt_ratio = _winsorize_value(debt_ratio)

    # 4. EPS 변동성: 최근 8분기 EPS 표준편차
    eps_values = _get_recent_eps(ticker, date, conn, limit=8)
    eps_vol = None
    if len(eps_values) >= 2:
        eps_vol = np.std(eps_values)

    return roe, debt_ratio, eps_vol


def _get_latest_field(
    ticker: str,
    date: str,
    field: str,
    conn: duckdb.DuckDBPyConnection,
) -> Optional[float]:
    """ticker의 filed_date <= date인 최신 field 값을 반환."""
    try:
        row = conn.execute(
            f"""
            SELECT {field}
            FROM raw.sec_financials
            WHERE ticker = ? AND filed_date <= ? AND {field} IS NOT NULL
            ORDER BY filed_date DESC, period_of_report DESC
            LIMIT 1
            """,
            [ticker.upper(), date],
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _get_recent_eps(
    ticker: str,
    date: str,
    conn: duckdb.DuckDBPyConnection,
    limit: int = 8,
) -> list[float]:
    """ticker의 최근 EPS 값들을 반환 (최대 limit개)."""
    try:
        rows = conn.execute(
            """
            SELECT eps_diluted
            FROM raw.sec_financials
            WHERE ticker = ? AND filed_date <= ? AND eps_diluted IS NOT NULL
            ORDER BY filed_date DESC, period_of_report DESC
            LIMIT ?
            """,
            [ticker.upper(), date, limit],
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _get_all_recent_financials(
    ticker: str,
    date: str,
    conn: duckdb.DuckDBPyConnection,
    limit: int = 9,
) -> list[dict]:
    """
    ticker의 최근 재무 데이터 (filed_date <= date) 조회.

    Rules:
    - filed_date <= date (룩어헤드 방지)
    - 최신순 정렬

    Returns:
        [{ticker, net_income, stockholders_equity, total_liabilities, eps_diluted, ...}]
    """
    try:
        rows = conn.execute(
            """
            SELECT
                ticker, cik, filing_type, period_of_report, filed_date,
                revenue, net_income, eps_diluted, total_assets,
                stockholders_equity, total_liabilities, operating_cashflow,
                cost_of_goods_sold
            FROM raw.sec_financials
            WHERE ticker = ?
              AND filed_date <= ?
            ORDER BY filed_date DESC, period_of_report DESC
            LIMIT ?
            """,
            [ticker.upper(), date, limit],
        ).fetchall()

        columns = [
            "ticker", "cik", "filing_type", "period_of_report", "filed_date",
            "revenue", "net_income", "eps_diluted", "total_assets",
            "stockholders_equity", "total_liabilities", "operating_cashflow",
            "cost_of_goods_sold",
        ]
        return [dict(zip(columns, row)) for row in rows]

    except Exception as e:
        logger.error(f"재무 데이터 조회 실패 ({ticker}, {date}): {e}")
        return []


# ── 포트폴리오 구성 ────────────────────────────────────────────────────────

def get_portfolio(
    date: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> pd.DataFrame:
    """
    퀄리티 전략 포트폴리오 구성.

    Rules:
    - 신호 상위 20% 종목 선정
    - 동일가중 (Equal-Weight)
    - 분기별 리밸런싱 (3/6/9/12월 말)

    Args:
        date: 기준일 ('YYYY-MM-DD')
        conn: DuckDB 커넥션 (None이면 자동 생성)

    Returns:
        DataFrame([ticker, weight, signal_score])
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        # 1. 유니버스 조회
        universe = _get_universe(date, conn)
        if not universe:
            logger.warning(f"퀄리티 포트폴리오: 유니버스 없음 ({date})")
            return pd.DataFrame(columns=["ticker", "weight", "signal_score"])

        # 2. 신호 계산
        signal = compute_signal(date, universe, conn)
        if signal.empty:
            return pd.DataFrame(columns=["ticker", "weight", "signal_score"])

        # 3. 상위 20% 선정
        n_top = max(1, int(len(signal) * 0.20))
        top_tickers = signal.nlargest(n_top).index.tolist()

        # 4. 동일가중
        weight = 1.0 / len(top_tickers)

        portfolio = pd.DataFrame({
            "ticker": top_tickers,
            "weight": weight,
            "signal_score": signal[top_tickers].values,
        })

        logger.info(
            f"[퀄리티 포트폴리오] 완료: {len(portfolio)}개 종목, "
            f"가중치합={portfolio['weight'].sum():.6f}"
        )

        return portfolio

    except Exception as e:
        logger.error(f"퀄리티 포트폴리오 구성 실패 ({date}): {e}")
        return pd.DataFrame(columns=["ticker", "weight", "signal_score"])
    finally:
        if close_conn:
            conn.close()


# ── 진단 함수 ──────────────────────────────────────────────────────────────

def diagnose_quintile(
    start: str,
    end: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> pd.DataFrame:
    """
    기간별 퀀타일 성과 분석 (선택).

    퀄리티 점수를 5개 그룹(퀀타일)으로 나누고,
    각 그룹의 평균 수익률을 계산.

    Args:
        start: 시작일 ('YYYY-MM-DD')
        end: 종료일 ('YYYY-MM-DD')
        conn: DuckDB 커넥션

    Returns:
        DataFrame([quintile, avg_return, n_stocks, start_date, end_date])
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        logger.info(f"퀄리티 퀀타일 진단: {start} ~ {end}")
        # TODO: 실현 수익률과 함께 분석
        logger.warning("diagnose_quintile: 미구현 (백테스트 단계에서 활용)")
        return pd.DataFrame()

    except Exception as e:
        logger.error(f"퀀타일 진단 실패: {e}")
        return pd.DataFrame()
    finally:
        if close_conn:
            conn.close()


# ── 유니버스 헬퍼 ──────────────────────────────────────────────────────────

def _get_universe(date: str, conn: duckdb.DuckDBPyConnection) -> list[str]:
    """
    기본 필터를 적용한 투자 유니버스 조회.

    Rules (3-0 참조):
    1. 해당 날짜 S&P500 구성 종목
    2. 상장 후 6개월 미만 제외
    3. 최근 60거래일 평균 거래대금 하위 10% 제외
    4. 주가 $5 미만 제외
    5. 상장폐지/합병 플래그 제외

    Note: strategies/universe.py의 get_universe() 사용.
    """
    try:
        from strategies.universe import get_universe as get_filtered_universe
        return get_filtered_universe(date, conn)
    except ImportError:
        logger.warning("strategies.universe 임포트 실패, 기본 필터만 적용")
        # Fallback: 간단한 필터
        try:
            result = conn.execute(
                """
                SELECT DISTINCT ticker
                FROM raw.prices
                WHERE date = ?
                  AND adj_close > 5.0
                ORDER BY ticker
                """,
                [date],
            ).fetchall()

            universe = [r[0] for r in result]
            logger.debug(f"유니버스 (Fallback): {len(universe)}개 종목 ({date})")
            return universe

        except Exception as e:
            logger.error(f"유니버스 조회 실패: {e}")
            return []


# ── CLI 진입점 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="퀄리티 전략")
    parser.add_argument("--date", type=str, required=True, help="기준일 (YYYY-MM-DD)")
    parser.add_argument(
        "--universe",
        type=str,
        default="sp500",
        help="유니버스 (기본: sp500)",
    )
    args = parser.parse_args()

    conn = get_connection()
    try:
        # 1. 신호 계산
        signal = compute_signal(args.date, _get_universe(args.date, conn), conn)
        print(f"\n{args.date} 퀄리티 신호 (상위 10):")
        print(signal.nlargest(10))

        # 2. 포트폴리오
        portfolio = get_portfolio(args.date, conn)
        print(f"\n포트폴리오 ({len(portfolio)}개 종목):")
        print(portfolio.head(10))

    finally:
        conn.close()
