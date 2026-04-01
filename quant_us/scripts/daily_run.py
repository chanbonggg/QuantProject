"""
일일 실행 파이프라인

EST 16:30 이후 매일 실행.
10단계로 구성:
  1. 주가 수집
  2. SEC 수집 (월 1회)
  3. FRED 수집
  4. 데이터 품질 체크
  5. 레짐 피처 산출
  6. 레짐 판단 + 급변 알람
  7. 전략 신호 산출 (리밸런싱일만)
  8. 목표 포트폴리오 산출
  9. 로그 저장
  10. Slack 알람
"""

import os
import sys
import time
import argparse
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
import psycopg2.extras

from db.init import get_connection, get_pg_connection
from utils.logger import logger

# ---------------------------------------------------------------------------
# 수집기 임포트 (개별 try-except — 하나 실패해도 나머지 실행)
# ---------------------------------------------------------------------------

try:
    from data.collectors.price_collector import collect_daily, collect_range
    _PRICE_COLLECTOR_AVAILABLE = True
except ImportError as e:
    _PRICE_COLLECTOR_AVAILABLE = False
    logger.warning(f"[파이프라인] price_collector 임포트 실패: {e}")

try:
    from data.collectors.sec_collector import collect_financials
    _SEC_COLLECTOR_AVAILABLE = True
except ImportError as e:
    _SEC_COLLECTOR_AVAILABLE = False
    logger.warning(f"[파이프라인] sec_collector 임포트 실패: {e}")

try:
    from data.collectors.fred_collector import collect_all as fred_collect_all
    _FRED_COLLECTOR_AVAILABLE = True
except ImportError as e:
    _FRED_COLLECTOR_AVAILABLE = False
    logger.warning(f"[파이프라인] fred_collector 임포트 실패: {e}")

# ---------------------------------------------------------------------------
# 레짐 모듈 임포트
# ---------------------------------------------------------------------------

try:
    from regime.features import compute_features
    _FEATURES_AVAILABLE = True
except ImportError as e:
    _FEATURES_AVAILABLE = False
    logger.warning(f"[파이프라인] regime.features 임포트 실패: {e}")

try:
    from regime.model import predict as regime_predict
    _REGIME_MODEL_AVAILABLE = True
except ImportError as e:
    _REGIME_MODEL_AVAILABLE = False
    logger.warning(f"[파이프라인] regime.model 임포트 실패: {e}")

try:
    from regime.shock_alarm import check_alarm
    _SHOCK_ALARM_AVAILABLE = True
except ImportError as e:
    _SHOCK_ALARM_AVAILABLE = False
    logger.warning(f"[파이프라인] regime.shock_alarm 임포트 실패: {e}")

# ---------------------------------------------------------------------------
# 포트폴리오 모듈 임포트
# ---------------------------------------------------------------------------

try:
    from portfolio.weight_engine import build_combined_portfolio
    _WEIGHT_ENGINE_AVAILABLE = True
except ImportError as e:
    _WEIGHT_ENGINE_AVAILABLE = False
    logger.warning(f"[파이프라인] portfolio.weight_engine 임포트 실패: {e}")

try:
    from portfolio.optimizer import optimize, apply_risk_overlay
    _OPTIMIZER_AVAILABLE = True
except ImportError as e:
    _OPTIMIZER_AVAILABLE = False
    logger.warning(f"[파이프라인] portfolio.optimizer 임포트 실패: {e}")

try:
    from portfolio.state import PortfolioState
    _PORTFOLIO_STATE_AVAILABLE = True
except ImportError as e:
    _PORTFOLIO_STATE_AVAILABLE = False
    logger.warning(f"[파이프라인] portfolio.state 임포트 실패: {e}")

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# S&P500 전체 티커 목록 (price_collector에서 가져올 수 없을 때 폴백용)
_FALLBACK_UNIVERSE = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]


# ---------------------------------------------------------------------------
# 유틸리티
# ---------------------------------------------------------------------------

def _retry_with_backoff(func, *args, max_retries: int = 3, base_delay: float = 1.0, **kwargs):
    """지수 백오프로 재시도."""
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            func_name = getattr(func, "__name__", repr(func))
            logger.warning(
                f"[파이프라인] {func_name} 실패 (attempt {attempt + 1}/{max_retries}): {e}, "
                f"{delay:.1f}초 후 재시도"
            )
            time.sleep(delay)
    raise last_error  # type: ignore[misc]


def _send_slack_alert(message: str, level: str = "INFO") -> bool:
    """Slack webhook으로 알림 발송. 환경변수 없으면 스킵."""
    if not SLACK_WEBHOOK_URL:
        logger.info(f"[파이프라인] Slack webhook 미설정 → 알림 스킵: [{level}] {message}")
        return False

    emoji = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🚨", "CRITICAL": "🔴"}.get(level, "📌")
    payload = {"text": f"{emoji} *[{level}]* {message}"}

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        success = resp.status_code == 200
        if success:
            logger.debug(f"[파이프라인] Slack 발송 성공: [{level}] {message[:50]}")
        else:
            logger.warning(f"[파이프라인] Slack 발송 실패 (status={resp.status_code}): {message[:50]}")
        return success
    except Exception as e:
        logger.error(f"[파이프라인] Slack 발송 예외: {e}")
        return False


def _is_rebalance_date(date_str: str) -> bool:
    """매월 마지막 영업일인지 확인."""
    dt = pd.Timestamp(date_str)
    month_end = dt + pd.offsets.BMonthEnd(0)
    return dt.date() == month_end.date()


def _is_regime_shift(date_str: str, conn) -> bool:
    """어제와 오늘 레짐이 변경되었거나 shock_alarm이 발동했는지 확인."""
    try:
        result = conn.execute(
            """
            SELECT regime, shock_alarm
            FROM feature.regime_labels
            WHERE date <= ?
            ORDER BY date DESC
            LIMIT 2
            """,
            [date_str],
        ).fetchall()

        if not result or len(result) < 2:
            return False

        today_regime, today_alarm = result[0]
        yesterday_regime = result[1][0] if len(result) > 1 else None

        # 레짐 변경 또는 shock_alarm 발동
        is_shift = (today_regime != yesterday_regime) or (today_alarm is True)
        return is_shift

    except Exception as e:
        logger.warning(f"[Regime Shift 체크] 실패: {e}")
        return False


def _make_step_result(name: str, status: str, detail: str) -> dict:
    """단계 결과 딕셔너리 생성."""
    return {"name": name, "status": status, "detail": detail}


# ---------------------------------------------------------------------------
# 파이프라인 각 단계
# ---------------------------------------------------------------------------

def _step_collect_prices(date_str: str, conn, dry_run: bool) -> dict:
    """1단계: 주가 수집."""
    if dry_run:
        logger.info("[파이프라인] [1/10] 주가 수집 스킵 (dry-run)")
        return _make_step_result("주가수집", "skipped", "dry-run 모드")

    if not _PRICE_COLLECTOR_AVAILABLE:
        return _make_step_result("주가수집", "error", "price_collector 임포트 실패")

    logger.info(f"[파이프라인] [1/10] 주가 수집 시작: {date_str}")
    try:
        rows = _retry_with_backoff(collect_daily, date_str, conn, max_retries=3, base_delay=1.0)
        msg = f"{rows}행 저장"
        logger.info(f"[파이프라인] 주가 수집 완료: {msg}")
        return _make_step_result("주가수집", "success", msg)
    except Exception as e:
        detail = f"3회 재시도 실패: {e}"
        logger.error(f"[파이프라인] 주가 수집 실패: {detail}")
        _send_slack_alert(f"주가 수집 실패 ({date_str}): {e}", level="ERROR")
        return _make_step_result("주가수집", "error", detail)


def _step_collect_sec(date_str: str, conn, dry_run: bool) -> dict:
    """2단계: SEC 수집 (월 1회, 매월 1일)."""
    if dry_run:
        logger.info("[파이프라인] [2/10] SEC 수집 스킵 (dry-run)")
        return _make_step_result("SEC수집", "skipped", "dry-run 모드")

    dt = pd.Timestamp(date_str)
    if dt.day != 1:
        logger.info(f"[파이프라인] [2/10] SEC 수집 스킵: 월 1일이 아님 (day={dt.day})")
        return _make_step_result("SEC수집", "skipped", f"월 1일 아님 (day={dt.day})")

    if not _SEC_COLLECTOR_AVAILABLE:
        return _make_step_result("SEC수집", "error", "sec_collector 임포트 실패")

    logger.info(f"[파이프라인] [2/10] SEC 수집 시작: {date_str}")
    try:
        # SEC는 전체 유니버스 대상 증분 수집 (실패 시 로그만 남기고 계속)
        start_year = dt.year - 2
        total_rows = 0
        try:
            from data.collectors.price_collector import SP500_TICKERS
            universe = SP500_TICKERS
        except ImportError:
            universe = _FALLBACK_UNIVERSE

        for ticker in universe[:10]:  # 일일 파이프라인에서는 샘플만 수집 (전체 수집은 별도 배치)
            try:
                rows = collect_financials(ticker, start_year=start_year, conn=None)
                total_rows += rows
            except Exception as ticker_err:
                logger.warning(f"[파이프라인] SEC 수집 개별 실패 ({ticker}): {ticker_err}")

        msg = f"{total_rows}행 저장 (샘플 10종목)"
        logger.info(f"[파이프라인] SEC 수집 완료: {msg}")
        return _make_step_result("SEC수집", "success", msg)
    except Exception as e:
        detail = f"수집 실패 (스킵): {e}"
        logger.error(f"[파이프라인] SEC 수집 실패: {detail}")
        return _make_step_result("SEC수집", "error", detail)


def _step_collect_fred(date_str: str, conn, dry_run: bool) -> dict:
    """3단계: FRED 거시지표 수집."""
    if dry_run:
        logger.info("[파이프라인] [3/10] FRED 수집 스킵 (dry-run)")
        return _make_step_result("FRED수집", "skipped", "dry-run 모드")

    if not _FRED_COLLECTOR_AVAILABLE:
        return _make_step_result("FRED수집", "error", "fred_collector 임포트 실패")

    logger.info(f"[파이프라인] [3/10] FRED 수집 시작: {date_str}")
    try:
        # FRED는 마지막 수집일 이후만 증분 수집 (쓰기 작업이므로 PG conn 사용)
        rows = _retry_with_backoff(fred_collect_all, start=date_str, conn=None, max_retries=3, base_delay=1.0)
        msg = f"{rows}행 저장"
        logger.info(f"[파이프라인] FRED 수집 완료: {msg}")
        return _make_step_result("FRED수집", "success", msg)
    except Exception as e:
        detail = f"3회 재시도 실패: {e}"
        logger.error(f"[파이프라인] FRED 수집 실패: {detail}")
        _send_slack_alert(f"FRED 수집 실패 ({date_str}): {e}", level="ERROR")
        return _make_step_result("FRED수집", "error", detail)


def _check_data_quality(date_str: str, conn) -> dict:
    """데이터 품질 체크 결과 반환."""
    result = {"price_rows": 0, "missing_rate": 1.0, "has_data": False}

    try:
        # 당일 가격 데이터 존재 확인
        row = conn.execute(
            "SELECT COUNT(*) FROM raw.prices WHERE date = CAST(? AS DATE)",
            [date_str],
        ).fetchone()
        price_rows = int(row[0]) if row else 0
        result["price_rows"] = price_rows
        result["has_data"] = price_rows > 0

        # 결측률 체크 (adj_close 기준)
        if price_rows > 0:
            null_row = conn.execute(
                "SELECT COUNT(*) FROM raw.prices WHERE date = CAST(? AS DATE) AND adj_close IS NULL",
                [date_str],
            ).fetchone()
            null_count = int(null_row[0]) if null_row else 0
            result["missing_rate"] = null_count / price_rows
        else:
            result["missing_rate"] = 1.0

        logger.info(
            f"[파이프라인] 데이터 품질: {date_str} → {price_rows}행, "
            f"결측률={result['missing_rate']:.2%}"
        )
    except Exception as e:
        logger.error(f"[파이프라인] 데이터 품질 체크 실패: {e}")

    return result


def _step_data_quality(date_str: str, conn) -> dict:
    """4단계: 데이터 품질 체크."""
    logger.info(f"[파이프라인] [4/10] 데이터 품질 체크: {date_str}")
    quality = _check_data_quality(date_str, conn)

    if not quality["has_data"]:
        detail = f"당일 가격 데이터 없음 (rows=0)"
        logger.warning(f"[파이프라인] {detail}")
        return _make_step_result("데이터품질", "error", detail)

    if quality["missing_rate"] > 0.20:
        detail = f"결측률 과다: {quality['missing_rate']:.2%} (임계=20%)"
        logger.warning(f"[파이프라인] {detail}")
        return _make_step_result("데이터품질", "error", detail)

    detail = f"rows={quality['price_rows']}, 결측률={quality['missing_rate']:.2%}"
    return _make_step_result("데이터품질", "success", detail)


def _step_compute_features(date_str: str, conn) -> tuple[dict, Optional[pd.Series]]:
    """5단계: 레짐 피처 산출."""
    logger.info(f"[파이프라인] [5/10] 레짐 피처 산출: {date_str}")

    if not _FEATURES_AVAILABLE:
        return _make_step_result("레짐피처", "error", "regime.features 임포트 실패"), None

    try:
        features = compute_features(date_str, conn)
        non_null = int(features.notna().sum())
        detail = f"피처 {non_null}개 산출 (전체={len(features)})"
        logger.info(f"[파이프라인] 레짐 피처 완료: {detail}")
        return _make_step_result("레짐피처", "success", detail), features
    except Exception as e:
        detail = f"피처 산출 실패: {e}"
        logger.error(f"[파이프라인] {detail}")
        return _make_step_result("레짐피처", "error", detail), None


def _step_regime_and_alarm(date_str: str, conn, alerts: list) -> tuple[dict, Optional[str], Optional[dict]]:
    """6단계: 레짐 판단 + 급변 알람."""
    logger.info(f"[파이프라인] [6/10] 레짐 판단 + 급변 알람: {date_str}")

    regime: Optional[str] = None
    alarm_result: Optional[dict] = None

    if not _REGIME_MODEL_AVAILABLE:
        return _make_step_result("레짐판단", "error", "regime.model 임포트 실패"), None, None

    try:
        # 레짐 판단
        regime = regime_predict(date_str, conn)
        logger.info(f"[파이프라인] 레짐: {regime}")

        # 이전 레짐 조회 (전환 감지용)
        prev_row = conn.execute(
            """
            SELECT regime FROM feature.regime_labels
            WHERE date < CAST(? AS DATE)
            ORDER BY date DESC LIMIT 1
            """,
            [date_str],
        ).fetchone()
        prev_regime = prev_row[0] if prev_row else None

        if prev_regime and prev_regime != regime:
            msg = f"레짐 전환: {prev_regime} → {regime} ({date_str})"
            alerts.append({"level": "INFO", "message": msg})
            _send_slack_alert(msg, level="INFO")
            logger.info(f"[파이프라인] {msg}")

        # 급변 알람
        if _SHOCK_ALARM_AVAILABLE:
            alarm_result = check_alarm(date_str, conn)
            alarm_flag = alarm_result.get("alarm", False)
            severity = alarm_result.get("severity", "low")
            triggers = alarm_result.get("triggers", [])

            if alarm_flag:
                msg = f"급변 알람 발동: severity={severity}, triggers={triggers} ({date_str})"
                level = "ERROR" if severity in ("critical", "high") else "WARNING"
                alerts.append({"level": level, "message": msg})
                _send_slack_alert(msg, level=level)
                logger.warning(f"[파이프라인] {msg}")

        detail = f"레짐={regime}, alarm={alarm_result.get('alarm', False) if alarm_result else 'N/A'}"
        return _make_step_result("레짐판단", "success", detail), regime, alarm_result

    except Exception as e:
        detail = f"레짐 판단 실패: {e}"
        logger.error(f"[파이프라인] {detail}")
        return _make_step_result("레짐판단", "error", detail), regime, alarm_result


def _step_strategy_signals(date_str: str, conn) -> tuple[dict, Optional[pd.DataFrame]]:
    """7단계: 전략 신호 산출 (매일 생성, 리밸런싱 선택)."""
    logger.info(f"[파이프라인] [7/10] 전략 신호 산출: {date_str}")

    if not _WEIGHT_ENGINE_AVAILABLE:
        return _make_step_result("전략신호", "error", "portfolio.weight_engine 임포트 실패"), None

    try:
        portfolio = build_combined_portfolio(date=date_str, conn=conn)
        n_stocks = len(portfolio[~portfolio["ticker"].isin({"SHY", "TLT", "CASH"})])
        detail = f"{n_stocks}개 종목 신호 산출"
        logger.info(f"[파이프라인] 전략 신호 완료: {detail}")
        return _make_step_result("전략신호", "success", detail), portfolio
    except Exception as e:
        detail = f"전략 신호 실패: {e}"
        logger.error(f"[파이프라인] {detail}")
        return _make_step_result("전략신호", "error", detail), None


def _step_build_portfolio(
    date_str: str,
    conn,
    raw_portfolio: Optional[pd.DataFrame],
    alerts: list,
) -> tuple[dict, Optional[pd.DataFrame]]:
    """8단계: 목표 포트폴리오 산출 + Drift 계산 + Rebalancing 판단 + 저장."""
    if raw_portfolio is None:
        logger.info("[파이프라인] [8/10] 포트폴리오 산출 스킵: 전략 신호 없음")
        return _make_step_result("포트폴리오산출", "skipped", "전략 신호 없음"), None

    if not _OPTIMIZER_AVAILABLE or not _PORTFOLIO_STATE_AVAILABLE:
        missing = []
        if not _OPTIMIZER_AVAILABLE:
            missing.append("portfolio.optimizer")
        if not _PORTFOLIO_STATE_AVAILABLE:
            missing.append("portfolio.state")
        return _make_step_result(
            "포트폴리오산출",
            "error",
            f"{', '.join(missing)} 임포트 실패",
        ), None

    logger.info(f"[파이프라인] [8/10] 포트폴리오 산출 + Drift 계산: {date_str}")
    try:
        # 1. Drift 계산 및 리밸런싱 판단
        portfolio_state = PortfolioState(total_value=500, conn=conn)

        # 직전 포트폴리오 상태 확인
        prev_state = portfolio_state._get_previous_portfolio_state(date_str)

        if prev_state is None:
            # 첫 번째 실행: drift = 0
            max_drift = 0.0
            drift_details = {}
            logger.info("[Drift 계산] 첫 실행이므로 drift=0")
        else:
            # 이전 상태가 있으면 drift 계산
            max_drift, drift_details = portfolio_state.compute_drift(raw_portfolio, date_str)

        # 2. 리밸런싱 필요 여부 판단
        should_rebalance = (
            max_drift > 0.05  # Drift > 5%
            or _is_regime_shift(date_str, conn)  # 레짐 급변
            or _is_rebalance_date(date_str)  # 월말
        )

        # 3. 리밸런싱 시에만 최적화 + 리스크 오버레이
        if should_rebalance:
            final_portfolio = optimize(raw_portfolio, conn=conn, top_n=10)
            final_portfolio = apply_risk_overlay(final_portfolio, date=date_str, conn=conn)
            reason = (
                "drift"
                if max_drift > 0.05
                else "regime_shift"
                if _is_regime_shift(date_str, conn)
                else "monthly"
            )
        else:
            # 리밸런싱 안 함 → 직전 포트폴리오 유지
            final_portfolio = raw_portfolio.copy()
            reason = "skipped"

        # 4. 상태 저장
        portfolio_state.save_state(
            date_str, final_portfolio, should_rebalance, reason, max_drift=max_drift
        )

        # 5. 결과 로깅
        n_final = len(final_portfolio[final_portfolio["weight"] > 0])
        total_weight = final_portfolio["weight"].sum()

        detail = (
            f"{n_final}개 종목, drift={max_drift*100:.2f}%, "
            f"리밸런싱={'YES' if should_rebalance else 'NO'} ({reason}), "
            f"가중치합={total_weight:.4f}"
        )

        logger.info(f"[파이프라인] 포트폴리오 산출 완료: {detail}")

        if max_drift > 0.05:
            alerts.append(
                {
                    "level": "WARNING",
                    "message": f"Drift 초과: {max_drift*100:.2f}% > 5% → 리밸런싱 필요",
                }
            )

        return _make_step_result("포트폴리오산출", "success", detail), final_portfolio

    except Exception as e:
        detail = f"포트폴리오 산출 실패: {e}"
        logger.error(f"[파이프라인] {detail}")
        return _make_step_result("포트폴리오산출", "error", detail), None


def _step_save_log(date_str: str, conn, steps: list) -> dict:
    """9단계: 파이프라인 실행 결과를 DB에 저장."""
    logger.info(f"[파이프라인] [9/10] 로그 저장: {date_str}")

    try:
        # 각 단계 결과 저장 (ON CONFLICT upsert — psycopg2)
        run_date = pd.Timestamp(date_str).date()
        pg_conn = get_pg_connection()
        cur = pg_conn.cursor()

        for step in steps:
            cur.execute(
                """
                INSERT INTO feature.pipeline_log (run_date, step_name, status, detail, logged_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (run_date, step_name) DO UPDATE
                    SET status    = EXCLUDED.status,
                        detail    = EXCLUDED.detail,
                        logged_at = NOW()
                """,
                (run_date, step["name"], step["status"], step["detail"]),
            )

        pg_conn.commit()
        cur.close()
        pg_conn.close()

        logger.info(f"[파이프라인] 로그 저장 완료: {len(steps)}개 단계")
        return _make_step_result("로그저장", "success", f"{len(steps)}개 단계 저장")

    except Exception as e:
        detail = f"로그 저장 실패: {e}"
        logger.error(f"[파이프라인] {detail}")
        return _make_step_result("로그저장", "error", detail)


def _step_slack_summary(date_str: str, steps: list, alerts: list, portfolio: Optional[pd.DataFrame]) -> dict:
    """10단계: Slack 요약 알람."""
    logger.info(f"[파이프라인] [10/10] Slack 요약 알람: {date_str}")

    # 성공/실패/스킵 집계
    counts = {"success": 0, "error": 0, "skipped": 0}
    for step in steps:
        status = step.get("status", "unknown")
        if status in counts:
            counts[status] += 1

    # 포트폴리오 요약
    portfolio_summary = "없음"
    if portfolio is not None and not portfolio.empty:
        n = len(portfolio[portfolio["weight"] > 0])
        portfolio_summary = f"{n}개 종목"

    summary = (
        f"일일 파이프라인 완료 ({date_str})\n"
        f"성공: {counts['success']}, 실패: {counts['error']}, 스킵: {counts['skipped']}\n"
        f"포트폴리오: {portfolio_summary}"
    )

    level = "ERROR" if counts["error"] > 0 else "INFO"
    _send_slack_alert(summary, level=level)

    logger.info(f"[파이프라인] Slack 요약 발송: {counts}")
    return _make_step_result("Slack알람", "success", f"success={counts['success']}, failed={counts['error']}")


# ---------------------------------------------------------------------------
# 메인 파이프라인
# ---------------------------------------------------------------------------

def run_pipeline(
    date: Optional[str] = None,
    conn=None,
    dry_run: bool = False,
) -> dict:
    """
    일일 파이프라인 실행.

    Args:
        date: 기준일 (None이면 오늘)
        conn: DuckDB 연결 (None이면 자동 생성)
        dry_run: True이면 수집(1~3단계) 스킵, 분석(4~10단계)만 실행

    Returns:
        dict:
            'date': str
            'steps': [{'name': str, 'status': 'success'|'failed'|'skipped', 'detail': str}]
            'alerts': [{'level': str, 'message': str}]
            'portfolio': pd.DataFrame or None
    """
    # 기준일 설정
    date_str = date or datetime.today().strftime("%Y-%m-%d")
    logger.info(f"[파이프라인] 시작: {date_str} (dry_run={dry_run})")

    # DB 연결
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    steps: list = []
    alerts: list = []
    final_portfolio: Optional[pd.DataFrame] = None

    try:
        # 1. 주가 수집
        steps.append(_step_collect_prices(date_str, conn, dry_run))

        # 2. SEC 수집 (월 1회)
        steps.append(_step_collect_sec(date_str, conn, dry_run))

        # 3. FRED 수집
        steps.append(_step_collect_fred(date_str, conn, dry_run))

        # 4. 데이터 품질 체크
        steps.append(_step_data_quality(date_str, conn))

        # 5. 레짐 피처 산출
        feature_step, _ = _step_compute_features(date_str, conn)
        steps.append(feature_step)

        # 6. 레짐 판단 + 급변 알람
        regime_step, regime, alarm_result = _step_regime_and_alarm(date_str, conn, alerts)
        steps.append(regime_step)

        # 7. 전략 신호 산출 (리밸런싱일만)
        signal_step, raw_portfolio = _step_strategy_signals(date_str, conn)
        steps.append(signal_step)

        # 8. 목표 포트폴리오 산출
        portfolio_step, final_portfolio = _step_build_portfolio(
            date_str, conn, raw_portfolio, alerts
        )
        steps.append(portfolio_step)

        # 9. 로그 저장
        log_step = _step_save_log(date_str, conn, steps)
        steps.append(log_step)

        # 10. Slack 요약 알람
        slack_step = _step_slack_summary(date_str, steps, alerts, final_portfolio)
        steps.append(slack_step)

    except Exception as e:
        logger.error(f"[파이프라인] 예상치 못한 오류: {e}")
        _send_slack_alert(f"파이프라인 비정상 종료: {e}", level="CRITICAL")
        steps.append(_make_step_result("비정상종료", "error", str(e)))
    finally:
        if close_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # 결과 집계
    success_count = sum(1 for s in steps if s["status"] == "success")
    failed_count = sum(1 for s in steps if s["status"] == "error")
    logger.info(
        f"[파이프라인] 완료: {date_str} → "
        f"success={success_count}, failed={failed_count}, 총={len(steps)}단계"
    )

    return {
        "date": date_str,
        "steps": steps,
        "alerts": alerts,
        "portfolio": final_portfolio,
    }


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="일일 파이프라인 실행")
    parser.add_argument("--date", type=str, default=None, help="기준일 (YYYY-MM-DD, 기본=오늘)")
    parser.add_argument("--dry-run", action="store_true", help="수집 없이 분석만 실행")
    args = parser.parse_args()

    result = run_pipeline(date=args.date, dry_run=args.dry_run)

    # 결과 출력
    print(f"\n{'=' * 60}")
    print(f"파이프라인 실행 결과: {result['date']}")
    print(f"{'=' * 60}")
    for step in result["steps"]:
        icon = {"success": "O", "error": "X", "skipped": "-"}.get(step["status"], "?")
        print(f"  [{icon}] {step['name']:15s} {step['status']:8s}  {step['detail']}")

    if result["alerts"]:
        print(f"\n알람 ({len(result['alerts'])}개):")
        for alert in result["alerts"]:
            print(f"  [{alert['level']}] {alert['message']}")

    if result["portfolio"] is not None:
        n = len(result["portfolio"][result["portfolio"]["weight"] > 0])
        print(f"\n최종 포트폴리오: {n}개 종목")
