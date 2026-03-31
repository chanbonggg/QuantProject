"""
Walk-Forward Analysis + 과적합 검정

- run_wfa(): OOS 구간별 백테스트 + 레짐별 성과 분해
- compute_dsr(): Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014)
- compute_pbo(): Probability of Backtest Overfitting (CSCV 기반)
- run_stress_tests(): 4개 시나리오 스트레스 테스트
"""

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path
from typing import Callable, Dict, List, Optional

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.init import get_connection
from utils.logger import logger
from backtest.engine import (
    BacktestResult,
    TransactionCostModel,
    compute_metrics,
    run,
)


# ---------------------------------------------------------------------------
# 데이터클래스
# ---------------------------------------------------------------------------

@dataclass
class WFAResult:
    """Walk-Forward Analysis 결과."""

    oos_results: List[dict]          # OOS 구간별 {start, end, metrics, regime_breakdown}
    aggregate_metrics: dict          # 전체 OOS 합산 성과 지표
    dsr: Optional[float]             # Deflated Sharpe Ratio
    pbo: Optional[float]             # Probability of Backtest Overfitting
    stress_tests: dict               # {scenario_name: BacktestResult}
    regime_performance: dict         # {A: metrics, B: metrics, C: metrics}


# ---------------------------------------------------------------------------
# 스트레스 테스트 시나리오
# ---------------------------------------------------------------------------

STRESS_SCENARIOS: Dict[str, dict] = {
    "2008_gfc":       {"start": "2008-09-01", "end": "2009-03-31"},
    "2020_covid":     {"start": "2020-02-01", "end": "2020-03-31"},
    "2022_rate_hike": {"start": "2022-01-01", "end": "2022-10-31"},
}


# ---------------------------------------------------------------------------
# OOS 구간 생성
# ---------------------------------------------------------------------------

def _generate_oos_periods(
    data_start: str,
    data_end: str,
    train_years: int = 10,
    oos_months: int = 6,
) -> List[dict]:
    """
    Walk-Forward OOS 구간 목록 생성.

    Args:
        data_start: 전체 데이터 시작일
        data_end: 전체 데이터 종료일
        train_years: 학습 구간 (년)
        oos_months: OOS 테스트 구간 (월)

    Returns:
        List[dict]: [{is_start, is_end, oos_start, oos_end}, ...]
    """
    start_dt = pd.Timestamp(data_start)
    end_dt = pd.Timestamp(data_end)

    periods: List[dict] = []
    oos_start = start_dt + pd.DateOffset(years=train_years)

    while oos_start < end_dt:
        oos_end = oos_start + pd.DateOffset(months=oos_months) - pd.Timedelta(days=1)
        if oos_end > end_dt:
            oos_end = end_dt

        # OOS 구간이 최소 30일 이상이어야 유의미
        if (oos_end - oos_start).days < 30:
            break

        is_start = oos_start - pd.DateOffset(years=train_years)
        is_end = oos_start - pd.Timedelta(days=1)

        periods.append({
            "is_start": is_start.strftime("%Y-%m-%d"),
            "is_end": is_end.strftime("%Y-%m-%d"),
            "oos_start": oos_start.strftime("%Y-%m-%d"),
            "oos_end": oos_end.strftime("%Y-%m-%d"),
        })

        oos_start = oos_end + pd.Timedelta(days=1)

    logger.info(f"[WFA] OOS 구간 생성: {len(periods)}개 (train={train_years}Y, oos={oos_months}M)")
    return periods


# ---------------------------------------------------------------------------
# 레짐별 성과 분해
# ---------------------------------------------------------------------------

def _compute_regime_performance(
    daily_returns: pd.Series,
    benchmark_returns: pd.Series,
    start: str,
    end: str,
    conn: duckdb.DuckDBPyConnection,
) -> dict:
    """
    feature.regime_labels 기반 레짐별 성과 분해.

    Returns:
        {
            'A': {'return': float, 'sharpe': float, 'n_days': int, 'ratio': float},
            'B': {...}, 'C': {...},
        }
    """
    result: dict = {}

    try:
        labels_df = conn.execute("""
            SELECT date, regime FROM feature.regime_labels
            WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
            ORDER BY date ASC
        """, [start, end]).df()
    except Exception as e:
        logger.warning(f"[WFA] regime_labels 조회 실패: {e}")
        labels_df = pd.DataFrame()

    if labels_df.empty:
        for regime in ["A", "B", "C"]:
            result[regime] = {"return": 0.0, "sharpe": 0.0, "n_days": 0, "ratio": 0.0}
        return result

    labels_df["date"] = pd.to_datetime(labels_df["date"])
    labels_df = labels_df.set_index("date")

    total_days = len(daily_returns)

    for regime in ["A", "B", "C"]:
        regime_dates = labels_df[labels_df["regime"] == regime].index
        regime_rets = daily_returns[daily_returns.index.isin(regime_dates)]
        n_days = len(regime_rets)

        if n_days == 0:
            result[regime] = {"return": 0.0, "sharpe": 0.0, "n_days": 0, "ratio": 0.0}
            continue

        total_ret = float((1 + regime_rets).prod() - 1)

        # 연율화 Sharpe (기간이 짧으면 단순 비율)
        if regime_rets.std() > 0 and n_days > 1:
            sharpe = float(regime_rets.mean() / regime_rets.std() * np.sqrt(252))
        else:
            sharpe = 0.0

        result[regime] = {
            "return": total_ret,
            "sharpe": sharpe,
            "n_days": n_days,
            "ratio": n_days / total_days if total_days > 0 else 0.0,
        }

    logger.info(
        f"[WFA] 레짐별 성과: "
        + ", ".join(f"{r}={result[r]['n_days']}일({result[r]['return']:.2%})" for r in ["A", "B", "C"])
    )
    return result


# ---------------------------------------------------------------------------
# DSR (Deflated Sharpe Ratio)
# ---------------------------------------------------------------------------

def compute_dsr(
    observed_sharpe: float,
    num_trials: int = 1,
    returns_skewness: float = 0.0,
    returns_kurtosis: float = 3.0,
    num_returns: int = 252,
) -> float:
    """
    Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

    전략 후보 수(num_trials)에 따라 Sharpe 허들 상향 조정.
    DSR = P(SR > SR*) where SR* = expected max SR given N trials.

    Args:
        observed_sharpe: 관측된 연율화 Sharpe ratio
        num_trials: 테스트한 전략 후보 수
        returns_skewness: 수익률 왜도 (기본 0)
        returns_kurtosis: 수익률 첨도 (기본 3, 정규분포)
        num_returns: 수익률 관측 수 (기본 252)

    Returns:
        float: 0~1 확률 (높을수록 과적합 가능성 낮음)
    """
    if num_trials < 1:
        return 0.0

    T = num_returns
    sr = observed_sharpe

    # Sharpe ratio의 분산 추정 (Lo 2002, Bailey & Lopez de Prado 2014)
    # Var(SR) ≈ (1 - skew*SR + ((kurt-3)/4)*SR^2) / T
    sr_var = (1 - returns_skewness * sr + ((returns_kurtosis - 3) / 4) * sr**2) / T
    sr_std = np.sqrt(max(sr_var, 1e-10))

    # Expected maximum SR given N independent trials (Euler-Mascheroni approx)
    if num_trials <= 1:
        sr_star = 0.0
    else:
        log_n = np.log(num_trials)
        if log_n <= 0:
            sr_star = 0.0
        else:
            euler_gamma = 0.5772156649
            sr_star = sr_std * (
                np.sqrt(2 * log_n)
                - (np.log(np.pi) + np.log(log_n))
                / (2 * np.sqrt(2 * log_n))
            )

    # DSR = P(true SR > SR*) = Φ((SR - SR*) / σ_SR)
    dsr = float(stats.norm.cdf((sr - sr_star) / sr_std))

    logger.debug(
        f"[WFA] DSR: SR={sr:.3f}, SR*={sr_star:.3f}, "
        f"σ_SR={sr_std:.3f}, N={num_trials}, DSR={dsr:.4f}"
    )
    return dsr


# ---------------------------------------------------------------------------
# PBO (Probability of Backtest Overfitting)
# ---------------------------------------------------------------------------

def compute_pbo(
    returns_matrix: pd.DataFrame,
    n_partitions: int = 10,
) -> Optional[float]:
    """
    Probability of Backtest Overfitting (Bailey et al. 2015, CSCV 기반).

    단일 전략이면 PBO 계산 불가 → None.

    Args:
        returns_matrix: 컬럼=전략 변형, 인덱스=날짜, 값=일별 수익률
        n_partitions: 시계열 분할 수 (기본 10, 짝수 필요)

    Returns:
        float: 0~1 (높을수록 과적합), None (전략 1개 이하)
    """
    if returns_matrix is None or returns_matrix.shape[1] < 2:
        logger.info("[WFA] PBO: 전략 2개 미만 — 계산 불가")
        return None

    n_strategies = returns_matrix.shape[1]
    n_rows = returns_matrix.shape[0]

    # 짝수로 조정
    if n_partitions % 2 != 0:
        n_partitions += 1
    n_partitions = min(n_partitions, n_rows)
    if n_partitions < 4:
        logger.warning("[WFA] PBO: 분할 수 부족 — 계산 불가")
        return None

    half = n_partitions // 2

    # 시계열을 n_partitions 블록으로 분할
    block_size = n_rows // n_partitions
    if block_size < 1:
        return None

    blocks = []
    for i in range(n_partitions):
        start_idx = i * block_size
        end_idx = start_idx + block_size if i < n_partitions - 1 else n_rows
        blocks.append(returns_matrix.iloc[start_idx:end_idx])

    # C(n, n/2) 조합 (최대 50개 샘플링)
    all_combos = list(combinations(range(n_partitions), half))
    if len(all_combos) > 50:
        rng = np.random.RandomState(42)
        combo_indices = rng.choice(len(all_combos), 50, replace=False)
        combos = [all_combos[i] for i in combo_indices]
    else:
        combos = all_combos

    overfit_count = 0
    total_count = 0

    for is_indices in combos:
        oos_indices = tuple(i for i in range(n_partitions) if i not in is_indices)

        # IS/OOS 수익률 합산
        is_returns = pd.concat([blocks[i] for i in is_indices])
        oos_returns = pd.concat([blocks[i] for i in oos_indices])

        # IS에서 최고 Sharpe 전략 선택
        is_sharpes = is_returns.mean() / is_returns.std()
        best_strategy = is_sharpes.idxmax()

        # OOS에서 해당 전략의 Sharpe
        oos_best_sharpe = (
            oos_returns[best_strategy].mean() / oos_returns[best_strategy].std()
            if oos_returns[best_strategy].std() > 0 else 0.0
        )

        # OOS Sharpe < IS 중앙값 → 과적합
        oos_all_sharpes = oos_returns.mean() / oos_returns.std()
        oos_median = oos_all_sharpes.median()

        if oos_best_sharpe < oos_median:
            overfit_count += 1
        total_count += 1

    pbo = overfit_count / total_count if total_count > 0 else 0.0

    logger.info(f"[WFA] PBO: {pbo:.4f} ({overfit_count}/{total_count} 조합에서 과적합)")
    return float(pbo)


# ---------------------------------------------------------------------------
# 스트레스 테스트
# ---------------------------------------------------------------------------

def run_stress_tests(
    strategy_func: Callable,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    slippage_multiplier: float = 1.0,
) -> dict:
    """
    각 스트레스 시나리오 기간에 engine.run() 실행.

    Args:
        strategy_func: (date_str, conn) -> pd.DataFrame
        conn: DuckDB 연결
        slippage_multiplier: 슬리피지 배수 (3.0 → slippage_3x)

    Returns:
        {scenario_name: BacktestResult}
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    cost_model = TransactionCostModel(
        slippage_rate=0.0005 * slippage_multiplier,
    )

    results: dict = {}

    try:
        for name, period in STRESS_SCENARIOS.items():
            logger.info(f"[WFA 스트레스] {name}: {period['start']} ~ {period['end']}")
            try:
                bt_result = run(
                    strategy_func,
                    start=period["start"],
                    end=period["end"],
                    conn=conn,
                    cost_model=cost_model,
                )
                results[name] = bt_result
                logger.info(
                    f"[WFA 스트레스] {name} 완료: "
                    f"return={bt_result.metrics.get('total_return', 0):.2%}, "
                    f"MDD={bt_result.metrics.get('mdd', 0):.2%}"
                )
            except Exception as e:
                logger.error(f"[WFA 스트레스] {name} 실패: {e}")
                results[name] = None

        # slippage_3x 시나리오 (전체 기간에 슬리피지 3배)
        if slippage_multiplier > 1.0:
            logger.info(f"[WFA 스트레스] slippage_{slippage_multiplier:.0f}x 시나리오 적용됨")

        return results

    finally:
        if close_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Walk-Forward Analysis 메인
# ---------------------------------------------------------------------------

def run_wfa(
    strategy_func: Callable,
    data_start: str,
    data_end: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    train_years: int = 10,
    oos_months: int = 6,
    min_oos_periods: int = 10,
    rebalance_freq: str = "M",
    num_strategy_trials: int = 1,
) -> WFAResult:
    """
    Walk-Forward Analysis 실행.

    Args:
        strategy_func: (date_str, conn) -> pd.DataFrame
        data_start: 전체 데이터 시작일
        data_end: 전체 데이터 종료일
        conn: DuckDB 연결
        train_years: 학습 구간 (년)
        oos_months: OOS 테스트 구간 (월)
        min_oos_periods: 최소 OOS 구간 수 (경고용)
        rebalance_freq: 리밸런싱 빈도
        num_strategy_trials: DSR 계산용 전략 후보 수

    Returns:
        WFAResult
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    logger.info(
        f"[WFA] 시작: {data_start} ~ {data_end}, "
        f"train={train_years}Y, oos={oos_months}M"
    )

    try:
        # 1. OOS 구간 생성
        periods = _generate_oos_periods(data_start, data_end, train_years, oos_months)
        if len(periods) < min_oos_periods:
            logger.warning(
                f"[WFA] OOS 구간 {len(periods)}개 < 최소 {min_oos_periods}개 "
                f"(데이터 기간 부족)"
            )

        # 2. 각 OOS 구간 백테스트
        oos_results: List[dict] = []
        all_oos_returns: List[pd.Series] = []
        all_oos_bench: List[pd.Series] = []

        for i, period in enumerate(periods):
            logger.info(
                f"[WFA] OOS {i+1}/{len(periods)}: "
                f"{period['oos_start']} ~ {period['oos_end']}"
            )
            try:
                bt_result = run(
                    strategy_func,
                    start=period["oos_start"],
                    end=period["oos_end"],
                    conn=conn,
                    rebalance_freq=rebalance_freq,
                )

                # 레짐별 분해
                regime_breakdown = _compute_regime_performance(
                    bt_result.daily_returns,
                    bt_result.benchmark_returns,
                    period["oos_start"],
                    period["oos_end"],
                    conn,
                )

                oos_results.append({
                    "period_idx": i,
                    "is_start": period["is_start"],
                    "is_end": period["is_end"],
                    "oos_start": period["oos_start"],
                    "oos_end": period["oos_end"],
                    "metrics": bt_result.metrics,
                    "regime_breakdown": regime_breakdown,
                })

                all_oos_returns.append(bt_result.daily_returns)
                all_oos_bench.append(bt_result.benchmark_returns)

            except Exception as e:
                logger.error(f"[WFA] OOS {i+1} 실패: {e}")
                oos_results.append({
                    "period_idx": i,
                    "oos_start": period["oos_start"],
                    "oos_end": period["oos_end"],
                    "metrics": {},
                    "regime_breakdown": {},
                    "error": str(e),
                })

        # 3. 전체 OOS 합산 메트릭
        if all_oos_returns:
            combined_returns = pd.concat(all_oos_returns)
            combined_bench = pd.concat(all_oos_bench)
            aggregate_metrics = compute_metrics(combined_returns, combined_bench, conn=conn)
        else:
            aggregate_metrics = {}
            combined_returns = pd.Series(dtype=float)
            combined_bench = pd.Series(dtype=float)

        # 4. DSR
        observed_sharpe = aggregate_metrics.get("sharpe", 0.0)
        n_rets = len(combined_returns) if not combined_returns.empty else 252
        skew = float(combined_returns.skew()) if len(combined_returns) > 2 else 0.0
        kurt = float(combined_returns.kurtosis()) + 3 if len(combined_returns) > 3 else 3.0
        dsr = compute_dsr(
            observed_sharpe,
            num_trials=num_strategy_trials,
            returns_skewness=skew,
            returns_kurtosis=kurt,
            num_returns=n_rets,
        )

        # 5. 레짐별 전체 성과
        if not combined_returns.empty:
            regime_performance = _compute_regime_performance(
                combined_returns, combined_bench, data_start, data_end, conn,
            )
        else:
            regime_performance = {r: {"return": 0.0, "sharpe": 0.0, "n_days": 0, "ratio": 0.0}
                                  for r in ["A", "B", "C"]}

        # 6. 스트레스 테스트
        stress_tests = run_stress_tests(strategy_func, conn=conn)

        result = WFAResult(
            oos_results=oos_results,
            aggregate_metrics=aggregate_metrics,
            dsr=dsr,
            pbo=None,  # 단일 전략이면 None
            stress_tests=stress_tests,
            regime_performance=regime_performance,
        )

        logger.info(
            f"[WFA] 완료: OOS {len(oos_results)}개, "
            f"DSR={dsr:.4f}, "
            f"합산 Sharpe={aggregate_metrics.get('sharpe', 0):.2f}"
        )
        return result

    finally:
        if close_conn:
            conn.close()
