"""
백테스트 엔진

- 미국 거래비용 모델 (SEC Fee, 위탁수수료, 슬리피지)
- 성과 지표 산출: CAGR, Sharpe, MDD, Calmar, Alpha/Beta, IR, Turnover
- run(portfolio_func, start, end, conn) → BacktestResult
"""

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.init import get_connection
from utils.logger import logger


# ---------------------------------------------------------------------------
# 데이터클래스
# ---------------------------------------------------------------------------

@dataclass
class TransactionCostModel:
    """미국 주식 거래비용 모델."""

    sec_fee_rate: float = 0.0000278   # SEC Fee: 매도금액 × 0.00278%
    commission_rate: float = 0.0002   # 위탁수수료: 왕복 2bp (편도 1bp)
    slippage_rate: float = 0.0005     # 슬리피지: 편도 5bp (보수적)

    def compute_cost(self, sell_value: float, buy_value: float) -> float:
        """
        리밸런싱 시 총 거래비용.

        Args:
            sell_value: 매도 금액 (포트폴리오 가치 대비 비율)
            buy_value: 매수 금액 (포트폴리오 가치 대비 비율)

        Returns:
            float: 총 거래비용 (포트폴리오 가치 대비 비율)
        """
        sec_fee = sell_value * self.sec_fee_rate
        commission = (sell_value + buy_value) * self.commission_rate
        slippage = (sell_value + buy_value) * self.slippage_rate
        return sec_fee + commission + slippage


@dataclass
class BacktestResult:
    """백테스트 결과."""

    daily_returns: pd.Series           # 인덱스=date, 값=일별 수익률
    cumulative_returns: pd.Series      # 인덱스=date, 값=누적수익률 (1.0 시작)
    drawdown: pd.Series                # 인덱스=date, 값=드로다운 (음수)
    portfolio_history: pd.DataFrame    # 인덱스=date, 컬럼=[ticker, weight, ...]
    metrics: dict                      # 성과 지표 딕셔너리
    benchmark_returns: pd.Series       # SPY 일별 수익률 (벤치마크)
    turnover: pd.Series                # 인덱스=리밸런싱일, 값=회전율


# ---------------------------------------------------------------------------
# 리밸런싱 날짜 유틸리티
# ---------------------------------------------------------------------------

def _get_rebalance_dates(
    trading_dates: List[date],
    freq: str = "M",
) -> List[date]:
    """
    거래일 목록에서 리밸런싱 날짜 추출.

    Args:
        trading_dates: 정렬된 거래일 목록
        freq: "M"(월말), "Q"(분기말), "W"(주말)

    Returns:
        List[date]: 리밸런싱 날짜 목록
    """
    if not trading_dates:
        return []

    dates_series = pd.Series(trading_dates)
    dates_pd = pd.to_datetime(dates_series)

    if freq == "W":
        # 각 주의 마지막 거래일 (isocalendar().week 기준)
        df = pd.DataFrame({"date": dates_pd, "orig": trading_dates})
        df["week"] = dates_pd.dt.isocalendar().week.values
        df["year"] = dates_pd.dt.isocalendar().year.values
        result = df.groupby(["year", "week"])["orig"].last().tolist()

    elif freq == "Q":
        # 3, 6, 9, 12월의 마지막 거래일
        df = pd.DataFrame({"date": dates_pd, "orig": trading_dates})
        df["month"] = dates_pd.dt.month
        df["year"] = dates_pd.dt.year
        quarter_months = {3, 6, 9, 12}
        df_q = df[df["month"].isin(quarter_months)]
        result = df_q.groupby(["year", "month"])["orig"].last().tolist()

    else:
        # 월말 (기본)
        df = pd.DataFrame({"date": dates_pd, "orig": trading_dates})
        df["month"] = dates_pd.dt.month
        df["year"] = dates_pd.dt.year
        result = df.groupby(["year", "month"])["orig"].last().tolist()

    result_sorted = sorted(result)
    logger.debug(f"[백테스트] 리밸런싱 날짜 추출: {len(result_sorted)}개 ({freq})")
    return result_sorted


# ---------------------------------------------------------------------------
# 성과 지표
# ---------------------------------------------------------------------------

def compute_metrics(
    daily_returns: pd.Series,
    benchmark_returns: pd.Series,
    risk_free_rate: Optional[float] = None,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> dict:
    """
    성과 지표 산출.

    Args:
        daily_returns: 일별 수익률 (인덱스=date)
        benchmark_returns: 벤치마크 일별 수익률
        risk_free_rate: 연율화 무위험율 (None이면 DGS3MO 자동 조회)
        conn: DuckDB 연결 (무위험율 조회 시 사용)

    Returns:
        dict: 성과 지표 딕셔너리
    """
    logger.info(f"[백테스트] 성과 지표 산출: {len(daily_returns)}일")

    n = len(daily_returns)
    if n == 0:
        logger.warning("[백테스트] 수익률 데이터 없음 — 빈 메트릭 반환")
        return {}

    # 1. 무위험율 조회 (연율화)
    rf_annual = risk_free_rate
    if rf_annual is None and conn is not None:
        try:
            row = conn.execute("""
                SELECT AVG(value) FROM raw.fred_series
                WHERE series_id = 'DGS3MO' AND value IS NOT NULL
            """).fetchone()
            if row and row[0] is not None:
                rf_annual = float(row[0]) / 100.0
        except Exception as e:
            logger.warning(f"[백테스트] DGS3MO 조회 실패: {e}")

    if rf_annual is None:
        rf_annual = 0.05  # 기본값 5%

    rf_daily = rf_annual / 252

    # 2. 기본 지표
    total_return = (1 + daily_returns).prod() - 1
    cagr = (1 + total_return) ** (252 / n) - 1
    annualized_vol = daily_returns.std() * np.sqrt(252)

    # 3. Sharpe ratio
    excess_daily = daily_returns - rf_daily
    sharpe_raw = excess_daily.mean() / daily_returns.std() if daily_returns.std() > 0 else 0.0
    sharpe = sharpe_raw * np.sqrt(252)

    # 4. 누적수익률 및 MDD
    cum_ret = (1 + daily_returns).cumprod()
    rolling_max = cum_ret.cummax()
    drawdown_series = (cum_ret - rolling_max) / rolling_max
    mdd = float(drawdown_series.min())

    # 5. Calmar
    calmar = cagr / abs(mdd) if mdd != 0 else np.nan

    # 6. Alpha / Beta (벤치마크 대비)
    alpha = np.nan
    beta = np.nan
    if len(benchmark_returns) > 1:
        aligned = pd.DataFrame({
            "port": daily_returns,
            "bench": benchmark_returns,
        }).dropna()

        if len(aligned) > 1:
            bench_excess = aligned["bench"] - rf_daily
            port_excess = aligned["port"] - rf_daily

            bench_var = bench_excess.var()
            if bench_var > 0:
                beta = float(port_excess.cov(bench_excess) / bench_var)
                alpha = float(
                    (port_excess.mean() - beta * bench_excess.mean()) * 252
                )

    # 7. Information Ratio
    if len(benchmark_returns) > 1:
        aligned_ir = pd.DataFrame({
            "port": daily_returns,
            "bench": benchmark_returns,
        }).dropna()
        active_returns = aligned_ir["port"] - aligned_ir["bench"]
        tracking_error = active_returns.std() * np.sqrt(252)
        ir = (active_returns.mean() * 252) / tracking_error if tracking_error > 0 else np.nan
    else:
        ir = np.nan

    metrics = {
        "cagr": float(cagr),
        "total_return": float(total_return),
        "annualized_volatility": float(annualized_vol),
        "sharpe": float(sharpe),
        "mdd": float(mdd),
        "calmar": float(calmar) if not np.isnan(calmar) else np.nan,
        "alpha": float(alpha) if not np.isnan(alpha) else np.nan,
        "beta": float(beta) if not np.isnan(beta) else np.nan,
        "information_ratio": float(ir) if not np.isnan(ir) else np.nan,
        "risk_free_rate_annual": float(rf_annual),
    }

    logger.info(
        f"[백테스트] 지표 완료: CAGR={cagr:.2%}, Sharpe={sharpe:.2f}, "
        f"MDD={mdd:.2%}, Alpha={alpha:.2%}" if not np.isnan(alpha) else
        f"[백테스트] 지표 완료: CAGR={cagr:.2%}, Sharpe={sharpe:.2f}, MDD={mdd:.2%}"
    )
    return metrics


# ---------------------------------------------------------------------------
# 가격 데이터 조회
# ---------------------------------------------------------------------------

def _load_prices(
    tickers: List[str],
    start: str,
    end: str,
    conn: duckdb.DuckDBPyConnection,
) -> pd.DataFrame:
    """
    기간 내 종목들의 adj_close 조회.

    Returns:
        pd.DataFrame: pivot (인덱스=date, 컬럼=ticker, 값=adj_close)
    """
    if not tickers:
        return pd.DataFrame()

    placeholders = ",".join(["?" for _ in tickers])
    try:
        df = conn.execute(f"""
            SELECT ticker, date, adj_close
            FROM raw.prices
            WHERE ticker IN ({placeholders})
              AND date >= CAST(? AS DATE)
              AND date <= CAST(? AS DATE)
            ORDER BY date ASC
        """, [*tickers, start, end]).df()
    except Exception as e:
        logger.error(f"[백테스트] 가격 조회 실패: {e}")
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"])
    pivot = df.pivot(index="date", columns="ticker", values="adj_close")
    return pivot


def _load_trading_dates(
    start: str,
    end: str,
    conn: duckdb.DuckDBPyConnection,
    benchmark_ticker: str = "SPY",
) -> List[date]:
    """
    벤치마크 종목의 거래일 목록 조회.

    Returns:
        List[date]: 정렬된 거래일 목록
    """
    try:
        df = conn.execute("""
            SELECT DISTINCT date FROM raw.prices
            WHERE ticker = ?
              AND date >= CAST(? AS DATE)
              AND date <= CAST(? AS DATE)
            ORDER BY date ASC
        """, [benchmark_ticker, start, end]).df()

        if df.empty:
            # 벤치마크 없으면 전체 거래일
            df = conn.execute("""
                SELECT DISTINCT date FROM raw.prices
                WHERE date >= CAST(? AS DATE)
                  AND date <= CAST(? AS DATE)
                ORDER BY date ASC
            """, [start, end]).df()

        return df["date"].tolist()
    except Exception as e:
        logger.error(f"[백테스트] 거래일 조회 실패: {e}")
        return []


# ---------------------------------------------------------------------------
# 회전율 계산
# ---------------------------------------------------------------------------

def _compute_turnover(
    prev_weights: dict,
    new_weights: dict,
) -> float:
    """
    포트폴리오 회전율 = Σ|new_w - prev_w| / 2.

    Returns:
        float: 0~1 사이 회전율
    """
    all_tickers = set(prev_weights.keys()) | set(new_weights.keys())
    total_change = sum(
        abs(new_weights.get(t, 0.0) - prev_weights.get(t, 0.0))
        for t in all_tickers
    )
    return total_change / 2.0


# ---------------------------------------------------------------------------
# 메인 백테스트 실행
# ---------------------------------------------------------------------------

def run(
    portfolio_func: Callable,
    start: str,
    end: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    rebalance_freq: str = "M",
    cost_model: Optional[TransactionCostModel] = None,
    benchmark_ticker: str = "SPY",
) -> BacktestResult:
    """
    백테스트 실행.

    Args:
        portfolio_func: (date_str, conn) → pd.DataFrame (컬럼=[ticker, weight])
        start: 백테스트 시작일 (YYYY-MM-DD)
        end: 백테스트 종료일 (YYYY-MM-DD)
        conn: DuckDB 연결 (None이면 자동 생성)
        rebalance_freq: 리밸런싱 빈도 ("M"=월, "Q"=분기, "W"=주)
        cost_model: 거래비용 모델 (None이면 기본값 사용)
        benchmark_ticker: 벤치마크 티커 (기본 "SPY")

    Returns:
        BacktestResult: 백테스트 결과
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    if cost_model is None:
        cost_model = TransactionCostModel()

    logger.info(f"[백테스트] 시작: {start} ~ {end}, 리밸런싱={rebalance_freq}")

    try:
        # 1. 거래일 및 리밸런싱 날짜 확정
        trading_dates = _load_trading_dates(start, end, conn, benchmark_ticker)
        if not trading_dates:
            logger.error("[백테스트] 거래일 데이터 없음")
            return _empty_result()

        rebalance_dates_raw = _get_rebalance_dates(trading_dates, rebalance_freq)
        rebalance_set = set(str(d) for d in rebalance_dates_raw)
        logger.info(
            f"[백테스트] 거래일: {len(trading_dates)}일, "
            f"리밸런싱: {len(rebalance_dates_raw)}회"
        )

        # 2. 벤치마크 가격 로드
        bench_prices = _load_prices([benchmark_ticker], start, end, conn)

        # 3. 백테스트 루프
        portfolio_weights: dict = {}          # {ticker: weight}
        price_cache: dict = {}                # {ticker: 직전 가격}
        daily_returns_list: List[float] = []
        daily_dates_list: List = []
        bench_returns_list: List[float] = []
        portfolio_history_rows: List[dict] = []
        turnover_dict: dict = {}

        prev_weights: dict = {}

        for i, dt in enumerate(trading_dates):
            dt_str = str(dt)

            # 리밸런싱 날짜 처리
            if dt_str in rebalance_set:
                # 새 포트폴리오 요청
                try:
                    new_port_df = portfolio_func(dt_str, conn)
                except Exception as e:
                    logger.warning(f"[백테스트] portfolio_func 실패 ({dt_str}): {e} — 이전 포트폴리오 유지")
                    new_port_df = None

                if new_port_df is not None and not new_port_df.empty and "ticker" in new_port_df.columns and "weight" in new_port_df.columns:
                    new_weights = dict(zip(new_port_df["ticker"], new_port_df["weight"]))
                else:
                    new_weights = {}

                # 회전율 계산
                turnover = _compute_turnover(prev_weights, new_weights)
                turnover_dict[dt_str] = turnover

                # 거래비용 차감 (전날 포트폴리오 기준으로 전환 시)
                if i > 0 and prev_weights:
                    sell_value = sum(
                        max(0.0, prev_weights.get(t, 0.0) - new_weights.get(t, 0.0))
                        for t in set(prev_weights) | set(new_weights)
                    )
                    buy_value = sum(
                        max(0.0, new_weights.get(t, 0.0) - prev_weights.get(t, 0.0))
                        for t in set(prev_weights) | set(new_weights)
                    )
                    cost = cost_model.compute_cost(sell_value, buy_value)

                    if daily_returns_list:
                        # 거래비용을 리밸런싱 당일 수익률에서 차감
                        daily_returns_list[-1] -= cost
                    logger.debug(f"[백테스트] {dt_str} 거래비용: {cost:.6f} (회전율={turnover:.2%})")

                portfolio_weights = new_weights
                prev_weights = dict(new_weights)

                # 포트폴리오 이력 저장
                for ticker, w in portfolio_weights.items():
                    portfolio_history_rows.append({
                        "date": dt_str,
                        "ticker": ticker,
                        "weight": w,
                    })

            # 일별 수익률 계산
            if i == 0:
                # 첫 날은 수익률 계산 불가
                daily_returns_list.append(0.0)
                daily_dates_list.append(dt)
                bench_returns_list.append(0.0)
                continue

            prev_dt = trading_dates[i - 1]

            # 포트폴리오 수익률 계산
            port_ret = _compute_portfolio_return(
                portfolio_weights, dt, prev_dt, conn
            )
            daily_returns_list.append(port_ret)
            daily_dates_list.append(dt)

            # 벤치마크 수익률 계산
            bench_ret = _compute_ticker_return(
                benchmark_ticker, dt, prev_dt, bench_prices
            )
            bench_returns_list.append(bench_ret)

        # 4. 결과 Series 구성
        daily_returns = pd.Series(
            daily_returns_list, index=pd.to_datetime(daily_dates_list), name="portfolio"
        )
        benchmark_returns = pd.Series(
            bench_returns_list, index=pd.to_datetime(daily_dates_list), name="benchmark"
        )

        cumulative_returns = (1 + daily_returns).cumprod()
        rolling_max = cumulative_returns.cummax()
        drawdown = (cumulative_returns - rolling_max) / rolling_max

        turnover_series = pd.Series(turnover_dict, name="turnover")
        avg_turnover = float(turnover_series.mean()) if not turnover_series.empty else 0.0

        # 5. 성과 지표
        metrics = compute_metrics(daily_returns, benchmark_returns, conn=conn)
        metrics["avg_turnover"] = avg_turnover

        # 6. 포트폴리오 이력 DataFrame
        if portfolio_history_rows:
            portfolio_history = pd.DataFrame(portfolio_history_rows)
        else:
            portfolio_history = pd.DataFrame(columns=["date", "ticker", "weight"])

        result = BacktestResult(
            daily_returns=daily_returns,
            cumulative_returns=cumulative_returns,
            drawdown=drawdown,
            portfolio_history=portfolio_history,
            metrics=metrics,
            benchmark_returns=benchmark_returns,
            turnover=turnover_series,
        )

        logger.info(
            f"[백테스트] 완료: CAGR={metrics.get('cagr', 0):.2%}, "
            f"Sharpe={metrics.get('sharpe', 0):.2f}, "
            f"MDD={metrics.get('mdd', 0):.2%}, "
            f"평균회전율={avg_turnover:.2%}"
        )
        return result

    finally:
        if close_conn:
            conn.close()


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _compute_portfolio_return(
    weights: dict,
    dt: date,
    prev_dt: date,
    conn: duckdb.DuckDBPyConnection,
) -> float:
    """
    포트폴리오 일별 수익률 계산: Σ(weight_i × return_i).

    Args:
        weights: {ticker: weight}
        dt: 현재 날짜
        prev_dt: 직전 거래일
        conn: DuckDB 연결

    Returns:
        float: 포트폴리오 수익률
    """
    if not weights:
        return 0.0

    tickers = list(weights.keys())
    placeholders = ",".join(["?" for _ in tickers])

    try:
        df = conn.execute(f"""
            SELECT ticker, date, adj_close
            FROM raw.prices
            WHERE ticker IN ({placeholders})
              AND date IN (CAST(? AS DATE), CAST(? AS DATE))
            ORDER BY ticker, date ASC
        """, [*tickers, str(prev_dt), str(dt)]).df()
    except Exception as e:
        logger.debug(f"[백테스트] 가격 조회 실패 ({dt}): {e}")
        return 0.0

    if df.empty:
        return 0.0

    port_ret = 0.0
    for ticker, w in weights.items():
        ticker_data = df[df["ticker"] == ticker].sort_values("date")
        if len(ticker_data) < 2:
            continue
        prev_price = ticker_data.iloc[0]["adj_close"]
        curr_price = ticker_data.iloc[1]["adj_close"]
        if prev_price and prev_price > 0:
            ticker_ret = (curr_price - prev_price) / prev_price
            port_ret += w * ticker_ret

    return port_ret


def _compute_ticker_return(
    ticker: str,
    dt: date,
    prev_dt: date,
    price_pivot: pd.DataFrame,
) -> float:
    """
    단일 티커 일별 수익률 계산 (피벗 DataFrame 사용).

    Args:
        ticker: 티커
        dt: 현재 날짜
        prev_dt: 직전 거래일
        price_pivot: pivot (인덱스=datetime, 컬럼=ticker, 값=adj_close)

    Returns:
        float: 일별 수익률
    """
    if price_pivot.empty or ticker not in price_pivot.columns:
        return 0.0

    dt_pd = pd.Timestamp(dt)
    prev_dt_pd = pd.Timestamp(prev_dt)

    if dt_pd not in price_pivot.index or prev_dt_pd not in price_pivot.index:
        return 0.0

    curr_price = price_pivot.loc[dt_pd, ticker]
    prev_price = price_pivot.loc[prev_dt_pd, ticker]

    if pd.isna(prev_price) or prev_price <= 0 or pd.isna(curr_price):
        return 0.0

    return float((curr_price - prev_price) / prev_price)


def _empty_result() -> BacktestResult:
    """빈 BacktestResult 반환."""
    empty_series = pd.Series(dtype=float)
    return BacktestResult(
        daily_returns=empty_series,
        cumulative_returns=empty_series,
        drawdown=empty_series,
        portfolio_history=pd.DataFrame(columns=["date", "ticker", "weight"]),
        metrics={},
        benchmark_returns=empty_series,
        turnover=empty_series,
    )
