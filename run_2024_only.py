#!/usr/bin/env python
"""2024만 백테스트 실행"""
import duckdb
import pandas as pd
from pathlib import Path

from quant_us.backtest.engine import run
from quant_us.portfolio.weight_engine import build_combined_portfolio
from quant_us.utils.logger import logger

db_path = Path("data/quant_us.duckdb")

logger.info("="*70)
logger.info("2024 백테스트 실행 (독립 연결)")
logger.info("="*70)

def portfolio_func(date: str, conn) -> pd.DataFrame:
    """4개 전략 조합"""
    try:
        return build_combined_portfolio(date=date, conn=conn, regime="C")
    except Exception as e:
        logger.error(f"포트폴리오 구성 실패 ({date}): {e}")
        return pd.DataFrame()

conn = duckdb.connect(str(db_path))
result_2024 = run(
    portfolio_func=portfolio_func,
    start="2024-01-01",
    end="2024-12-31",
    conn=conn,
    rebalance_freq="M"
)
conn.close()

logger.info(f"\n[2024 결과]")
logger.info(f"  누적수익률: {result_2024.metrics['total_return']*100:.2f}%")
logger.info(f"  CAGR: {result_2024.metrics['cagr']*100:.2f}%")
logger.info(f"  Sharpe: {result_2024.metrics['sharpe']:.4f}")
logger.info(f"  MDD: {result_2024.metrics['mdd']*100:.2f}%")
logger.info(f"  Alpha: {result_2024.metrics['alpha']*100:.2f}%")

# 결과 저장 (pickle)
import pickle
with open("result_2024.pkl", "wb") as f:
    pickle.dump(result_2024, f)
logger.info("✅ 2024 결과 저장 완료: result_2024.pkl")
