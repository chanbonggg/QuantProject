"""
포트폴리오 상태 관리 및 Drift 계산.

역할:
  - 현재 보유 포트폴리오 추적
  - 목표 vs 현재 비중 차이(Drift) 계산
  - 리밸런싱 필요 여부 판단
  - 포트폴리오 상태를 DB에 저장
"""

import json
from typing import Optional, Tuple
from datetime import datetime, timedelta

import pandas as pd
import psycopg2
import psycopg2.extensions
import duckdb

from utils.logger import logger
from db.init import get_pg_connection, get_connection


class PortfolioState:
    """포트폴리오 현재 상태 관리 및 Drift 계산."""

    def __init__(
        self,
        total_value: float,
        conn: Optional[duckdb.DuckDBPyConnection] = None,
    ):
        """
        포트폴리오 상태 초기화.

        Args:
            total_value: 총 자본금 ($)
            conn: DuckDB 읽기 연결 (None이면 자동 생성)
        """
        self.total_value = total_value
        self.conn = conn or get_connection()
        logger.debug(f"[PortfolioState] 초기화: total_value=${total_value:,.0f}")

    def compute_drift(
        self,
        target_portfolio: pd.DataFrame,
        date: str,
    ) -> Tuple[float, dict]:
        """
        목표 vs 현재 포트폴리오의 Drift 계산.

        Args:
            target_portfolio: 목표 포트폴리오 DataFrame
              columns: [ticker, weight, strategy_source]
              weight: 목표 비중 (0~1)
            date: 기준 날짜 (YYYY-MM-DD)

        Returns:
            (max_drift, drift_details)
            - max_drift: 최대 편차 % (절댓값)
            - drift_details: {
                'AAPL': {
                  'target': 0.05,
                  'current': 0.051,
                  'drift_pct': 0.1,
                  'shares': 100,
                  'value': 15200
                },
                ...
              }
        """
        logger.info(f"[Drift 계산] 시작: {date}")

        # 직전 날짜에 저장된 포트폴리오 상태 조회
        previous_state = self._get_previous_portfolio_state(date)
        if previous_state is None:
            logger.warning(f"[Drift 계산] 이전 상태 없음 → Drift = 0 반환")
            return 0.0, {}

        # 직전 목표 포트폴리오 로드
        previous_target = previous_state["target_portfolio"]
        previous_date = previous_state["date"]

        # 어제 이후 가격 변동 반영하여 현재 보유량 재계산
        current_holdings = self._compute_current_holdings(
            previous_target, previous_date, date
        )

        # 목표 비중과 현재 비중 비교
        drift_details = {}
        max_drift = 0.0

        for _, target_row in target_portfolio.iterrows():
            ticker = target_row["ticker"]
            target_weight = target_row["weight"]

            if ticker in current_holdings:
                current = current_holdings[ticker]
                current_weight = current["value"] / self.total_value
            else:
                # 신규 종목 (직전에 없던 것)
                current_weight = 0.0
                current = {"shares": 0, "value": 0.0, "price": 0.0}

            drift_pct = (current_weight - target_weight) * 100

            drift_details[ticker] = {
                "target": target_weight,
                "current": current_weight,
                "drift_pct": drift_pct,
                "shares": int(current["shares"]),
                "value": current["value"],
            }

            max_drift = max(max_drift, abs(drift_pct))

        # 직전에 있었으나 새 목표에 없는 종목 (매도 대상)
        for ticker, current in current_holdings.items():
            if not target_portfolio[
                target_portfolio["ticker"] == ticker
            ].empty:
                continue  # 이미 처리됨

            current_weight = current["value"] / self.total_value
            drift_pct = current_weight * 100  # 전부 매도해야 함

            drift_details[ticker] = {
                "target": 0.0,
                "current": current_weight,
                "drift_pct": drift_pct,
                "shares": int(current["shares"]),
                "value": current["value"],
            }

            max_drift = max(max_drift, abs(drift_pct))

        logger.info(
            f"[Drift 계산] 완료: 최대 Drift={max_drift:.2f}%, "
            f"종목={len(drift_details)}개"
        )

        return max_drift / 100, drift_details

    def get_current_holdings(
        self, date: str
    ) -> pd.DataFrame:
        """
        현재 보유 현황 조회.

        Returns:
            DataFrame columns:
              - ticker: 종목명
              - target_weight: 목표 비중
              - current_weight: 현재 비중
              - shares: 보유 주수
              - value: 현재 가치 ($)
              - drift_pct: 편차 (%)
        """
        try:
            result = self.conn.execute(
                """
                SELECT
                    target_portfolio::json as portfolio
                FROM normalized.portfolio_state
                WHERE date = ?
                ORDER BY date DESC
                LIMIT 1
                """,
                [date],
            ).fetchall()

            if not result or not result[0][0]:
                logger.warning(f"[Holdings] {date}에 저장된 상태 없음")
                return pd.DataFrame(
                    columns=[
                        "ticker",
                        "target_weight",
                        "current_weight",
                        "shares",
                        "value",
                        "drift_pct",
                    ]
                )

            portfolio_json = result[0][0]
            if isinstance(portfolio_json, str):
                target_portfolio = json.loads(portfolio_json)
            else:
                target_portfolio = portfolio_json

            # JSON → DataFrame 변환
            holdings = []
            for ticker, data in target_portfolio.items():
                holdings.append(
                    {
                        "ticker": ticker,
                        "target_weight": data.get("target", 0),
                        "current_weight": data.get("current", 0),
                        "shares": data.get("shares", 0),
                        "value": data.get("value", 0),
                        "drift_pct": data.get("drift_pct", 0),
                    }
                )

            return pd.DataFrame(holdings)

        except Exception as e:
            logger.error(f"[Holdings] 조회 실패: {e}")
            return pd.DataFrame(
                columns=[
                    "ticker",
                    "target_weight",
                    "current_weight",
                    "shares",
                    "value",
                    "drift_pct",
                ]
            )

    def save_state(
        self,
        date: str,
        target_portfolio: pd.DataFrame,
        rebalance_triggered: bool,
        reason: str,
        cash_amount: Optional[float] = None,
        max_drift: Optional[float] = None,
    ) -> None:
        """
        포트폴리오 상태를 PostgreSQL에 저장.

        Args:
            date: 기준 날짜
            target_portfolio: 목표 포트폴리오 DataFrame [ticker, weight, ...]
            rebalance_triggered: 리밸런싱 트리거 여부
            reason: 트리거 이유 ('drift', 'regime_shift', 'monthly', 'manual')
            cash_amount: 현금 ($, 옵션)
            max_drift: 최대 Drift 값 (0~1 범위, 옵션)
        """
        try:
            # 현재 보유 종목별 데이터 조회
            holdings = self._compute_current_holdings_for_save(target_portfolio, date)

            # target_portfolio를 JSON으로 직렬화
            target_dict = {}
            for _, row in target_portfolio.iterrows():
                ticker = row["ticker"]
                target_dict[ticker] = {
                    "target": float(row["weight"]),
                    "current": holdings.get(ticker, {}).get("weight", 0.0),
                    "drift_pct": (
                        holdings.get(ticker, {}).get("weight", 0.0)
                        - row["weight"]
                    )
                    * 100,
                    "shares": int(holdings.get(ticker, {}).get("shares", 0)),
                    "value": holdings.get(ticker, {}).get("value", 0.0),
                }

            target_portfolio_json = json.dumps(target_dict)

            # 계산된 지표
            equity_value = sum(h["value"] for h in holdings.values())
            if cash_amount is None:
                cash_amount = self.total_value - equity_value

            # PostgreSQL에 INSERT
            conn = get_pg_connection()
            cur = conn.cursor()

            cur.execute(
                """
                INSERT INTO normalized.portfolio_state
                (date, total_value, cash_amount, equity_value, target_portfolio,
                 current_drift, rebalance_triggered, rebalance_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (date) DO UPDATE SET
                    total_value = EXCLUDED.total_value,
                    cash_amount = EXCLUDED.cash_amount,
                    equity_value = EXCLUDED.equity_value,
                    target_portfolio = EXCLUDED.target_portfolio,
                    current_drift = EXCLUDED.current_drift,
                    rebalance_triggered = EXCLUDED.rebalance_triggered,
                    rebalance_reason = EXCLUDED.rebalance_reason
                """,
                [
                    date,
                    self.total_value,
                    cash_amount,
                    equity_value,
                    target_portfolio_json,
                    max_drift,
                    rebalance_triggered,
                    reason,
                ],
            )

            conn.commit()
            cur.close()
            conn.close()

            logger.info(
                f"[PortfolioState 저장] {date}: "
                f"종목={len(target_dict)}개, "
                f"리밸런싱={'YES' if rebalance_triggered else 'NO'} ({reason})"
            )

        except Exception as e:
            logger.error(f"[PortfolioState 저장] 실패: {e}")
            raise

    def _get_previous_portfolio_state(
        self, date: str
    ) -> Optional[dict]:
        """
        직전 영업일의 포트폴리오 상태 조회.

        Returns:
            {
              'date': '2026-03-30',
              'target_portfolio': {'AAPL': {'target': 0.05, ...}, ...},
              'total_value': 50000
            }
        """
        try:
            result = self.conn.execute(
                """
                SELECT date, target_portfolio, total_value
                FROM normalized.portfolio_state
                WHERE date < ?
                ORDER BY date DESC
                LIMIT 1
                """,
                [date],
            ).fetchall()

            if not result:
                return None

            date_prev, portfolio_json, total_value = result[0]

            if isinstance(portfolio_json, str):
                portfolio = json.loads(portfolio_json)
            else:
                portfolio = portfolio_json

            return {
                "date": str(date_prev),
                "target_portfolio": portfolio,
                "total_value": total_value,
            }

        except Exception as e:
            logger.warning(f"[이전 상태 조회] 실패: {e}")
            return None

    def _compute_current_holdings(
        self, previous_target: dict, previous_date: str, current_date: str
    ) -> dict:
        """
        직전 목표 포트폴리오에서 어제 이후 가격 변동을 반영하여 현재 보유량 계산.

        Args:
            previous_target: {'AAPL': {'target': 0.05, 'shares': 100, ...}, ...}
            previous_date: 직전 날짜
            current_date: 현재 날짜

        Returns:
            {
              'AAPL': {
                'shares': 100,
                'value': 15200,
                'price': 152.0,
                'weight': 0.304
              },
              ...
            }
        """
        holdings = {}

        try:
            for ticker, data in previous_target.items():
                shares = data.get("shares", 0)
                if shares == 0:
                    continue

                # 현재 가격 조회
                price_result = self.conn.execute(
                    """
                    SELECT close
                    FROM raw.prices
                    WHERE ticker = ? AND date <= ?
                    ORDER BY date DESC
                    LIMIT 1
                    """,
                    [ticker, current_date],
                ).fetchall()

                if price_result:
                    current_price = price_result[0][0]
                    current_value = shares * current_price

                    holdings[ticker] = {
                        "shares": shares,
                        "value": current_value,
                        "price": current_price,
                        "weight": current_value / self.total_value,
                    }

        except Exception as e:
            logger.error(f"[현재 보유량 계산] 실패: {e}")

        return holdings

    def _compute_current_holdings_for_save(
        self, target_portfolio: pd.DataFrame, date: str
    ) -> dict:
        """
        저장을 위한 현재 보유 현황 계산.

        Returns:
            {
              'AAPL': {'shares': 100, 'value': 15200, 'weight': 0.304},
              ...
            }
        """
        holdings = {}

        try:
            for _, row in target_portfolio.iterrows():
                ticker = row["ticker"]
                target_weight = row["weight"]

                # 목표 비중에서 보유 주수 역산
                target_value = self.total_value * target_weight

                # 현재 가격
                price_result = self.conn.execute(
                    """
                    SELECT close
                    FROM raw.prices
                    WHERE ticker = ? AND date <= ?
                    ORDER BY date DESC
                    LIMIT 1
                    """,
                    [ticker, date],
                ).fetchall()

                if price_result:
                    current_price = price_result[0][0]
                    shares = int(target_value / current_price)
                    current_value = shares * current_price

                    holdings[ticker] = {
                        "shares": shares,
                        "value": current_value,
                        "weight": current_value / self.total_value,
                    }

        except Exception as e:
            logger.error(f"[저장용 보유량 계산] 실패: {e}")

        return holdings
