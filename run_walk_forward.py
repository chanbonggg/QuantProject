"""
Walk-Forward Analysis 실행
- train_years=2, oos_months=6
- 데이터: 2020-01-01 ~ 2026-03-27
- PostgreSQL + DuckDB postgres_scanner 사용
"""
import sys
import pickle
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "quant_us"))

from db.init import get_connection
from backtest.walk_forward import run_wfa
from portfolio.weight_engine import build_combined_portfolio
from regime.model import predict
from utils.logger import logger


def portfolio_func(date: str, conn) -> pd.DataFrame:
    # 매번 새 연결 생성 — 동시성 충돌 방지
    fresh_conn = get_connection()
    try:
        regime = predict(date, fresh_conn)
        result = build_combined_portfolio(date=date, conn=fresh_conn, regime=regime)
        return result
    except Exception as e:
        logger.error(f"포트폴리오 구성 실패 ({date}): {e}")
        return pd.DataFrame()
    finally:
        fresh_conn.close()


logger.info("=" * 70)
logger.info("Walk-Forward Analysis 시작")
logger.info("  train_years=2, oos_months=6")
logger.info("  데이터: 2020-01-01 ~ 2026-03-27")
logger.info("  DB: PostgreSQL + DuckDB postgres_scanner")
logger.info("=" * 70)

conn = get_connection()

wfa_result = run_wfa(
    strategy_func=portfolio_func,
    data_start="2020-01-01",
    data_end="2026-03-27",
    conn=conn,
    train_years=2,
    oos_months=6,
    min_oos_periods=4,
    rebalance_freq="M",
    num_strategy_trials=1,
)

conn.close()

# 결과 저장
with open("wfa_result.pkl", "wb") as f:
    pickle.dump(wfa_result, f)

# ── OOS 구간별 결과 ──────────────────────────────
logger.info("\n=== OOS 구간별 결과 ===")
for r in wfa_result.oos_results:
    m = r["metrics"]
    logger.info(
        f"  [{r['oos_start']} ~ {r['oos_end']}] "
        f"CAGR={m.get('cagr', 0)*100:.2f}% | "
        f"Sharpe={m.get('sharpe', 0):.4f} | "
        f"MDD={m.get('mdd', 0)*100:.2f}%"
    )

# ── 전체 OOS 합산 ────────────────────────────────
agg = wfa_result.aggregate_metrics
logger.info("\n=== 전체 OOS 합산 ===")
logger.info(f"  CAGR:   {agg.get('cagr', 0)*100:.2f}%")
logger.info(f"  Sharpe: {agg.get('sharpe', 0):.4f}")
logger.info(f"  MDD:    {agg.get('mdd', 0)*100:.2f}%")

# ── DSR / PBO ───────────────────────────────────
logger.info("\n=== 과적합 검증 ===")
logger.info(f"  DSR (Deflated Sharpe Ratio): {wfa_result.dsr}")
logger.info(f"  PBO (Prob. of Overfitting):  {wfa_result.pbo}")

if wfa_result.dsr is not None:
    if wfa_result.dsr > 0:
        logger.info("  DSR > 0 -> 통계적으로 유의미한 전략")
    else:
        logger.warning("  DSR <= 0 -> 과적합 의심")

if wfa_result.pbo is not None:
    if wfa_result.pbo < 0.3:
        logger.info(f"  PBO {wfa_result.pbo:.2f} < 0.3 -> 과적합 위험 낮음")
    elif wfa_result.pbo < 0.5:
        logger.warning(f"  PBO {wfa_result.pbo:.2f} -> 주의 필요")
    else:
        logger.warning(f"  PBO {wfa_result.pbo:.2f} >= 0.5 -> 과적합 위험 높음")

# ── 레짐별 성과 ──────────────────────────────────
logger.info("\n=== 레짐별 성과 ===")
for regime, perf in wfa_result.regime_performance.items():
    logger.info(
        f"  Regime {regime}: "
        f"수익률={perf.get('return', 0)*100:.2f}% | "
        f"Sharpe={perf.get('sharpe', 0):.4f} | "
        f"일수={perf.get('n_days', 0)} ({perf.get('ratio', 0)*100:.1f}%)"
    )

# ── 스트레스 테스트 ──────────────────────────────
logger.info("\n=== 스트레스 테스트 ===")
for scenario, result in wfa_result.stress_tests.items():
    if result is not None and hasattr(result, "metrics"):
        m = result.metrics
        logger.info(
            f"  {scenario}: "
            f"CAGR={m.get('cagr', 0)*100:.2f}% | "
            f"MDD={m.get('mdd', 0)*100:.2f}%"
        )

logger.info("\nWFA 완료. 결과 저장: wfa_result.pkl")
