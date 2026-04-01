"""
Streamlit 대시보드

레짐 기반 퀀트 시스템 모니터링:
  Tab 1: 성과 요약 (누적수익률, 월별 히트맵, 핵심 지표)
  Tab 2: 레짐 모니터 (VIX, 레짐 판단, 크레딧 스프레드, 알람)
  Tab 3: 포트폴리오 현황 (보유 종목, 전략 비중, 스트레스 테스트)
  Tab 4: 데이터 상태 (소스별 최근 업데이트, 행수)

실행: streamlit run quant_us/monitor/dashboard.py
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.init import get_connection
from portfolio.optimizer import STRESS_SHOCKS, stress_test
from portfolio.weight_engine import REGIME_WEIGHTS, decide_weights
from utils.logger import logger

# ---------------------------------------------------------------------------
# 페이지 설정
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="퀀트 시스템 대시보드",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# 사이드바
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("설정")
    selected_date = st.date_input(
        "기준 날짜",
        value=date.today(),
        help="포트폴리오 조회 기준 날짜",
    )
    selected_date_str = selected_date.strftime("%Y-%m-%d")

    lookback_days = st.slider(
        "성과 조회 기간 (일)",
        min_value=30,
        max_value=730,
        value=252,
        step=30,
        help="성과 요약 탭의 데이터 조회 기간",
    )

    st.divider()
    st.caption(f"기준일: {selected_date_str}")
    st.caption("데이터는 5분마다 갱신됩니다.")


# ---------------------------------------------------------------------------
# 데이터 조회 함수 (캐시 적용)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_price_data(start_date: str, end_date: str) -> pd.DataFrame:
    """raw.prices에서 SPY 가격 데이터 조회."""
    logger.info(f"[대시보드] 가격 데이터 조회: {start_date} ~ {end_date}")
    conn = get_connection()
    try:
        df = conn.execute(
            """
            SELECT date, adj_close
            FROM pg.raw.prices
            WHERE ticker = 'SPY'
              AND date >= CAST(? AS DATE)
              AND date <= CAST(? AS DATE)
            ORDER BY date ASC
            """,
            [start_date, end_date],
        ).df()
        logger.info(f"[대시보드] SPY 가격 조회 완료: {len(df)}행")
        return df
    except Exception as e:
        logger.error(f"[대시보드] 가격 데이터 조회 실패: {e}")
        return pd.DataFrame()
    finally:
        conn.close()


@st.cache_data(ttl=300)
def load_regime_data(start_date: str, end_date: str) -> pd.DataFrame:
    """feature.regime_labels에서 레짐 이력 조회."""
    logger.info(f"[대시보드] 레짐 데이터 조회: {start_date} ~ {end_date}")
    conn = get_connection()
    try:
        df = conn.execute(
            """
            SELECT date, regime, shock_alarm, computed_at
            FROM pg.feature.regime_labels
            WHERE date >= CAST(? AS DATE)
              AND date <= CAST(? AS DATE)
            ORDER BY date ASC
            """,
            [start_date, end_date],
        ).df()
        logger.info(f"[대시보드] 레짐 이력 조회 완료: {len(df)}행")
        return df
    except Exception as e:
        logger.error(f"[대시보드] 레짐 데이터 조회 실패: {e}")
        return pd.DataFrame()
    finally:
        conn.close()


@st.cache_data(ttl=300)
def load_regime_features(start_date: str, end_date: str) -> pd.DataFrame:
    """feature.regime_features에서 피처 이력 조회."""
    logger.info(f"[대시보드] 레짐 피처 조회: {start_date} ~ {end_date}")
    conn = get_connection()
    try:
        df = conn.execute(
            """
            SELECT date, vix, vix3m, vix_term, hy_spread, ig_spread, term_spread,
                   rv20, rv60, ma200_gap
            FROM pg.feature.regime_features
            WHERE date >= CAST(? AS DATE)
              AND date <= CAST(? AS DATE)
            ORDER BY date ASC
            """,
            [start_date, end_date],
        ).df()
        logger.info(f"[대시보드] 레짐 피처 조회 완료: {len(df)}행")
        return df
    except Exception as e:
        logger.error(f"[대시보드] 레짐 피처 조회 실패: {e}")
        return pd.DataFrame()
    finally:
        conn.close()


@st.cache_data(ttl=300)
def load_fred_data(series_ids: list, start_date: str, end_date: str) -> pd.DataFrame:
    """raw.fred_series에서 여러 시리즈 조회."""
    logger.info(f"[대시보드] FRED 데이터 조회: {series_ids}, {start_date} ~ {end_date}")
    conn = get_connection()
    try:
        placeholders = ",".join(["?" for _ in series_ids])
        df = conn.execute(
            f"""
            SELECT series_id, date, value
            FROM pg.raw.fred_series
            WHERE series_id IN ({placeholders})
              AND date >= CAST(? AS DATE)
              AND date <= CAST(? AS DATE)
            ORDER BY series_id, date ASC
            """,
            [*series_ids, start_date, end_date],
        ).df()
        logger.info(f"[대시보드] FRED 데이터 조회 완료: {len(df)}행")
        return df
    except Exception as e:
        logger.error(f"[대시보드] FRED 데이터 조회 실패: {e}")
        return pd.DataFrame()
    finally:
        conn.close()


@st.cache_data(ttl=300)
def load_data_status() -> dict:
    """각 테이블의 데이터 상태 조회."""
    logger.info("[대시보드] 데이터 상태 조회 시작")
    conn = get_connection()
    status = {}
    try:
        # raw.prices
        row = conn.execute(
            "SELECT MAX(date), COUNT(*), COUNT(DISTINCT ticker) FROM pg.raw.prices"
        ).fetchone()
        status["prices"] = {
            "latest_date": str(row[0]) if row[0] else "N/A",
            "total_rows": int(row[1]) if row[1] else 0,
            "unique_tickers": int(row[2]) if row[2] else 0,
        }

        # raw.fred_series
        row = conn.execute(
            "SELECT MAX(date), COUNT(*), COUNT(DISTINCT series_id) FROM pg.raw.fred_series"
        ).fetchone()
        status["fred"] = {
            "latest_date": str(row[0]) if row[0] else "N/A",
            "total_rows": int(row[1]) if row[1] else 0,
            "unique_series": int(row[2]) if row[2] else 0,
        }

        # raw.sec_financials
        row = conn.execute(
            "SELECT MAX(filed_date), COUNT(*), COUNT(DISTINCT ticker) FROM pg.raw.sec_financials"
        ).fetchone()
        status["sec"] = {
            "latest_date": str(row[0]) if row[0] else "N/A",
            "total_rows": int(row[1]) if row[1] else 0,
            "unique_tickers": int(row[2]) if row[2] else 0,
        }

        # feature.regime_labels
        row = conn.execute(
            "SELECT MAX(date), COUNT(*) FROM pg.feature.regime_labels"
        ).fetchone()
        status["regime_labels"] = {
            "latest_date": str(row[0]) if row[0] else "N/A",
            "total_rows": int(row[1]) if row[1] else 0,
        }

        # feature.regime_features
        row = conn.execute(
            "SELECT MAX(date), COUNT(*) FROM pg.feature.regime_features"
        ).fetchone()
        status["regime_features"] = {
            "latest_date": str(row[0]) if row[0] else "N/A",
            "total_rows": int(row[1]) if row[1] else 0,
        }

        logger.info("[대시보드] 데이터 상태 조회 완료")
        return status

    except Exception as e:
        logger.error(f"[대시보드] 데이터 상태 조회 실패: {e}")
        return {}
    finally:
        conn.close()


@st.cache_data(ttl=300)
def load_latest_regime(as_of_date: str) -> dict:
    """기준일 이전 최신 레짐 판단 결과 조회."""
    logger.info(f"[대시보드] 최신 레짐 조회: {as_of_date}")
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT date, regime, shock_alarm
            FROM pg.feature.regime_labels
            WHERE date <= CAST(? AS DATE)
            ORDER BY date DESC
            LIMIT 1
            """,
            [as_of_date],
        ).fetchone()
        if row:
            return {"date": str(row[0]), "regime": row[1], "shock_alarm": bool(row[2])}
        return {}
    except Exception as e:
        logger.error(f"[대시보드] 최신 레짐 조회 실패: {e}")
        return {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 계산 함수
# ---------------------------------------------------------------------------

def compute_monthly_returns(price_df: pd.DataFrame) -> pd.DataFrame:
    """
    SPY 가격 데이터에서 월별 수익률 계산.

    Args:
        price_df: date, adj_close 컬럼을 가진 DataFrame

    Returns:
        피벗 형태의 월별 수익률 (행=연도, 열=월)
    """
    if price_df.empty:
        return pd.DataFrame()

    df = price_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    # 월별 마지막 가격 → 월별 수익률
    monthly = df["adj_close"].resample("ME").last().pct_change().dropna()

    if monthly.empty:
        return pd.DataFrame()

    monthly_df = monthly.reset_index()
    monthly_df.columns = ["date", "return"]
    monthly_df["year"] = monthly_df["date"].dt.year
    monthly_df["month"] = monthly_df["date"].dt.month

    pivot = monthly_df.pivot(index="year", columns="month", values="return")

    # 월 번호를 영문 약칭으로 변환 (존재하는 월만 처리)
    month_names = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
    }
    pivot.columns = [month_names.get(m, str(m)) for m in pivot.columns]

    logger.info(
        f"[대시보드] 월별 수익률 계산 완료: {len(pivot)}년 × {len(pivot.columns)}월"
    )
    return pivot


def compute_cumulative_returns(price_df: pd.DataFrame) -> pd.Series:
    """가격 데이터에서 누적수익률 계산 (1.0 시작)."""
    if price_df.empty:
        return pd.Series(dtype=float)

    df = price_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    prices = df["adj_close"]
    daily_ret = prices.pct_change().fillna(0)
    cum_ret = (1 + daily_ret).cumprod()
    return cum_ret


def compute_performance_metrics(price_df: pd.DataFrame) -> dict:
    """
    가격 데이터에서 핵심 성과 지표 계산.

    Returns:
        dict: cagr, sharpe, mdd, calmar, total_return
    """
    if price_df.empty or len(price_df) < 2:
        return {}

    df = price_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    prices = df["adj_close"]
    daily_ret = prices.pct_change().dropna()
    n = len(daily_ret)

    if n == 0:
        return {}

    total_return = (1 + daily_ret).prod() - 1
    cagr = (1 + total_return) ** (252 / n) - 1
    vol = daily_ret.std() * np.sqrt(252)
    sharpe = (cagr - 0.05) / vol if vol > 0 else 0.0

    cum_ret = (1 + daily_ret).cumprod()
    rolling_max = cum_ret.cummax()
    drawdown = (cum_ret - rolling_max) / rolling_max
    mdd = float(drawdown.min())
    calmar = cagr / abs(mdd) if mdd != 0 else 0.0

    logger.info(
        f"[대시보드] 성과 지표 계산 완료: "
        f"CAGR={cagr:.4f}, Sharpe={sharpe:.4f}, MDD={mdd:.4f}"
    )
    return {
        "cagr": cagr,
        "sharpe": sharpe,
        "mdd": mdd,
        "calmar": calmar,
        "total_return": total_return,
        "n_days": n,
    }


# ---------------------------------------------------------------------------
# 탭 렌더링 함수
# ---------------------------------------------------------------------------

def render_tab_performance(start_date: str, end_date: str) -> None:
    """Tab 1: 성과 요약."""
    st.header("성과 요약")

    price_df = load_price_data(start_date, end_date)

    if price_df.empty:
        st.warning("가격 데이터가 없습니다. 데이터를 먼저 수집하세요.")
        return

    # 핵심 지표 카드
    metrics = compute_performance_metrics(price_df)

    if metrics:
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.metric("CAGR", f"{metrics['cagr']:.2%}")
        with col2:
            st.metric("Sharpe", f"{metrics['sharpe']:.2f}")
        with col3:
            st.metric("MDD", f"{metrics['mdd']:.2%}")
        with col4:
            st.metric("Calmar", f"{metrics['calmar']:.2f}")
        with col5:
            st.metric("총 수익률", f"{metrics['total_return']:.2%}")

    st.divider()

    # 누적수익률 차트
    cum_ret = compute_cumulative_returns(price_df)

    if not cum_ret.empty:
        st.subheader("SPY 누적수익률")
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=cum_ret.index,
                y=cum_ret.values,
                mode="lines",
                name="SPY",
                line={"color": "#2563EB", "width": 2},
            )
        )
        fig.update_layout(
            xaxis_title="날짜",
            yaxis_title="누적수익률 (1.0 시작)",
            hovermode="x unified",
            margin={"l": 0, "r": 0, "t": 30, "b": 0},
        )
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # 월별 수익률 히트맵
    monthly_ret = compute_monthly_returns(price_df)

    if not monthly_ret.empty:
        st.subheader("월별 수익률 히트맵 (SPY 벤치마크)")
        fig_hm = go.Figure(
            data=go.Heatmap(
                z=monthly_ret.values * 100,
                x=monthly_ret.columns.tolist(),
                y=monthly_ret.index.tolist(),
                colorscale=[
                    [0.0, "#EF4444"],
                    [0.5, "#FFFFFF"],
                    [1.0, "#22C55E"],
                ],
                zmid=0,
                text=np.round(monthly_ret.values * 100, 2),
                texttemplate="%{text:.2f}%",
                textfont={"size": 11},
                hovertemplate="월: %{x}<br>연도: %{y}<br>수익률: %{z:.2f}%<extra></extra>",
            )
        )
        fig_hm.update_layout(
            xaxis_title="월",
            yaxis_title="연도",
            margin={"l": 0, "r": 0, "t": 30, "b": 0},
        )
        st.plotly_chart(fig_hm, use_container_width=True)


def render_tab_regime(start_date: str, end_date: str, as_of_date: str) -> None:
    """Tab 2: 레짐 모니터."""
    st.header("레짐 모니터")

    # 현재 레짐 상태
    latest_regime = load_latest_regime(as_of_date)

    if latest_regime:
        regime = latest_regime.get("regime", "N/A")
        shock = latest_regime.get("shock_alarm", False)
        regime_date = latest_regime.get("date", "N/A")

        regime_labels = {"A": "Risk-on (A)", "B": "Risk-off (B)", "C": "Range (C)"}
        regime_colors = {"A": "normal", "B": "inverse", "C": "off"}

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("현재 레짐", regime_labels.get(regime, regime))
        with col2:
            alarm_text = "발동 중" if shock else "정상"
            st.metric("급변 알람", alarm_text, delta="주의" if shock else None)
        with col3:
            st.metric("레짐 기준일", regime_date)

        # 레짐별 전략 가중치
        if regime in REGIME_WEIGHTS:
            st.subheader("현재 레짐 전략 가중치")
            weight_info = decide_weights(regime, shock)
            sw = weight_info["strategy_weights"]
            equity = weight_info["equity_exposure"]

            wcol1, wcol2, wcol3, wcol4, wcol5 = st.columns(5)
            wcol1.metric("모멘텀", f"{sw['momentum']:.0%}")
            wcol2.metric("퀄리티", f"{sw['quality']:.0%}")
            wcol3.metric("밸류", f"{sw['value']:.0%}")
            wcol4.metric("저변동성", f"{sw['low_vol']:.0%}")
            wcol5.metric("주식 비중", f"{equity:.0%}")
    else:
        st.warning("레짐 판단 데이터가 없습니다. 레짐 모델을 먼저 실행하세요.")

    st.divider()

    features_df = load_regime_features(start_date, end_date)

    # VIX 시계열 + Term Structure
    if not features_df.empty and "vix" in features_df.columns:
        st.subheader("VIX 시계열")
        fig_vix = go.Figure()
        fig_vix.add_trace(
            go.Scatter(
                x=pd.to_datetime(features_df["date"]),
                y=features_df["vix"],
                mode="lines",
                name="VIX",
                line={"color": "#EF4444", "width": 2},
            )
        )
        if "vix3m" in features_df.columns:
            fig_vix.add_trace(
                go.Scatter(
                    x=pd.to_datetime(features_df["date"]),
                    y=features_df["vix3m"],
                    mode="lines",
                    name="VIX3M",
                    line={"color": "#F97316", "width": 1.5, "dash": "dash"},
                )
            )
        fig_vix.add_hline(y=20, line_dash="dot", line_color="gray", annotation_text="20")
        fig_vix.add_hline(y=25, line_dash="dot", line_color="orange", annotation_text="25")
        fig_vix.add_hline(y=35, line_dash="dot", line_color="red", annotation_text="35")
        fig_vix.update_layout(
            xaxis_title="날짜",
            yaxis_title="VIX",
            hovermode="x unified",
            margin={"l": 0, "r": 0, "t": 30, "b": 0},
        )
        st.plotly_chart(fig_vix, use_container_width=True)
    else:
        st.warning("레짐 피처 데이터가 없습니다.")

    # 크레딧 스프레드
    fred_df = load_fred_data(
        ["BAMLH0A0HYM2", "BAMLC0A0CM"],
        start_date,
        end_date,
    )

    if not fred_df.empty:
        st.subheader("크레딧 스프레드")
        fig_credit = go.Figure()

        hy_df = fred_df[fred_df["series_id"] == "BAMLH0A0HYM2"]
        ig_df = fred_df[fred_df["series_id"] == "BAMLC0A0CM"]

        if not hy_df.empty:
            fig_credit.add_trace(
                go.Scatter(
                    x=pd.to_datetime(hy_df["date"]),
                    y=hy_df["value"],
                    mode="lines",
                    name="HY OAS",
                    line={"color": "#DC2626", "width": 2},
                )
            )
        if not ig_df.empty:
            fig_credit.add_trace(
                go.Scatter(
                    x=pd.to_datetime(ig_df["date"]),
                    y=ig_df["value"],
                    mode="lines",
                    name="IG OAS",
                    line={"color": "#2563EB", "width": 2},
                )
            )
        fig_credit.update_layout(
            xaxis_title="날짜",
            yaxis_title="스프레드 (%)",
            hovermode="x unified",
            margin={"l": 0, "r": 0, "t": 30, "b": 0},
        )
        st.plotly_chart(fig_credit, use_container_width=True)

    # 급변 알람 이력
    regime_df = load_regime_data(start_date, end_date)

    if not regime_df.empty:
        shock_df = regime_df[regime_df["shock_alarm"] == True]
        if not shock_df.empty:
            st.subheader(f"급변 알람 이력 (최근 {len(shock_df)}건)")
            st.dataframe(
                shock_df[["date", "regime", "shock_alarm"]].tail(10),
                use_container_width=True,
            )
        else:
            st.info("조회 기간 내 급변 알람 없음")


def render_tab_portfolio(as_of_date: str) -> None:
    """Tab 3: 포트폴리오 현황."""
    st.header("포트폴리오 현황")

    latest_regime = load_latest_regime(as_of_date)
    regime = latest_regime.get("regime", "C") if latest_regime else "C"
    shock = latest_regime.get("shock_alarm", False) if latest_regime else False

    weight_info = decide_weights(regime, shock)
    sw = weight_info["strategy_weights"]
    equity = weight_info["equity_exposure"]
    risk_off_w = weight_info["risk_off_weight"]
    risk_off_asset = weight_info["risk_off_asset"]

    # 섹션 0: 현재 포트폴리오 (신규)
    st.subheader("현재 포트폴리오 현황")
    total_value = 500  # $500 자본금

    # Drift 데이터 조회
    try:
        from portfolio.state import PortfolioState

        portfolio_state = PortfolioState(total_value=total_value, conn=None)
        holdings = portfolio_state.get_current_holdings(as_of_date)

        if not holdings.empty:
            holdings_display = holdings[
                ["ticker", "target_weight", "current_weight", "drift_pct", "shares", "value"]
            ].copy()
            holdings_display["target_weight"] = holdings_display["target_weight"].apply(
                lambda x: f"{x*100:.2f}%"
            )
            holdings_display["current_weight"] = holdings_display["current_weight"].apply(
                lambda x: f"{x*100:.2f}%"
            )
            holdings_display["drift_pct"] = holdings_display["drift_pct"].apply(
                lambda x: f"{x:.2f}%"
            )
            holdings_display["value"] = holdings_display["value"].apply(
                lambda x: f"${x:,.0f}"
            )

            st.dataframe(
                holdings_display.sort_values("drift_pct", key=abs, ascending=False),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("아직 저장된 포트폴리오 상태가 없습니다.")
    except Exception as e:
        logger.warning(f"[대시보드] 현재 포트폴리오 조회 실패: {e}")
        st.warning(f"현재 포트폴리오 조회 오류: {e}")

    st.divider()

    # 전략별 비중 파이 차트
    st.subheader("전략별 비중 (레짐 기반)")
    strategy_labels = ["모멘텀", "퀄리티", "밸류", "저변동성", f"Risk-off ({risk_off_asset})"]
    strategy_values = [
        sw["momentum"] * equity,
        sw["quality"] * equity,
        sw["value"] * equity,
        sw["low_vol"] * equity,
        risk_off_w,
    ]

    fig_pie = px.pie(
        names=strategy_labels,
        values=strategy_values,
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    fig_pie.update_traces(textposition="inside", textinfo="percent+label")
    fig_pie.update_layout(margin={"l": 0, "r": 0, "t": 30, "b": 0})
    st.plotly_chart(fig_pie, use_container_width=True)

    st.divider()

    # 섹션 1: Drift 히트맵 (신규)
    st.subheader("Drift 히스토리 (최근 30일)")
    try:
        conn = get_connection()
        drift_result = conn.execute(
            """
            SELECT
                date,
                COALESCE(current_drift, 0) as drift_pct
            FROM pg.normalized.portfolio_state
            WHERE date >= CURRENT_DATE - INTERVAL '30 days'
            ORDER BY date ASC
            """
        ).fetchall()

        drift_history = pd.DataFrame(drift_result, columns=["date", "drift_pct"]) if drift_result else pd.DataFrame()

        if not drift_history.empty:
            drift_history["date"] = pd.to_datetime(drift_history["date"])
            drift_history["drift_pct"] = drift_history["drift_pct"] * 100

            fig_drift = px.line(
                drift_history,
                x="date",
                y="drift_pct",
                title="일일 Drift 변화",
                markers=True,
            )
            fig_drift.add_hline(
                y=5,
                line_dash="dash",
                line_color="red",
                annotation_text="리밸런싱 임계값 (5%)",
            )
            fig_drift.update_layout(
                yaxis_title="Drift (%)",
                xaxis_title="날짜",
                hovermode="x unified",
            )
            st.plotly_chart(fig_drift, use_container_width=True)
        else:
            st.info("Drift 히스토리가 없습니다.")
    except Exception as e:
        logger.warning(f"[대시보드] Drift 히트맵 실패: {e}")
        st.warning(f"Drift 히트맵 조회 오류: {e}")

    st.divider()

    # 섹션 2: 주문 제안 (신규)
    st.subheader("주문 제안")
    try:
        conn = get_connection()
        latest_state = pd.read_sql_query(
            """
            SELECT
                date,
                current_drift,
                rebalance_triggered,
                rebalance_reason,
                target_portfolio
            FROM pg.normalized.portfolio_state
            WHERE date = ?
            ORDER BY date DESC
            LIMIT 1
            """,
            conn,
            params=[as_of_date],
        )

        if not latest_state.empty:
            row = latest_state.iloc[0]
            max_drift = row["current_drift"] if row["current_drift"] else 0
            triggered = row["rebalance_triggered"]
            reason = row["rebalance_reason"]

            if max_drift > 0.05 or triggered:
                st.warning(
                    f"🚨 리밸런싱 필요 (최대 Drift: {max_drift*100:.2f}%, 사유: {reason})"
                )

                # target_portfolio JSON 파싱
                import json

                if isinstance(row["target_portfolio"], str):
                    target_dict = json.loads(row["target_portfolio"])
                else:
                    target_dict = row["target_portfolio"]

                # 주문 제안 생성
                orders = []
                for ticker, data in target_dict.items():
                    drift_pct = data.get("drift_pct", 0)
                    if abs(drift_pct) > 0.1:  # 0.1% 이상만 표시
                        action = "매도" if drift_pct > 0 else "매수"
                        orders.append(
                            f"- {ticker}: {action} (현재 {data['current']*100:.2f}% → 목표 {data['target']*100:.2f}%)"
                        )

                if orders:
                    st.write("**제안 주문:**")
                    for order in orders:
                        st.write(order)
                else:
                    st.info("0.1% 이상 편차인 종목이 없습니다.")
            else:
                st.success(
                    f"✅ 포트폴리오 정렬 상태 (Drift: {max_drift*100:.2f}% < 5%)"
                )
        else:
            st.info("저장된 포트폴리오 상태가 없습니다.")
    except Exception as e:
        logger.warning(f"[대시보드] 주문 제안 실패: {e}")
        st.warning(f"주문 제안 조회 오류: {e}")

    st.divider()

    # 레짐별 전략 가중치 비교 테이블
    st.subheader("레짐별 전략 가중치 참조 테이블")
    regime_table = []
    regime_name_map = {
        "A": "Risk-on (A)",
        "B": "Risk-off (B)",
        "C": "Range (C)",
        "SHOCK": "급변 (SHOCK)",
    }
    for regime_key, weights in REGIME_WEIGHTS.items():
        regime_table.append({
            "레짐": regime_name_map.get(regime_key, regime_key),
            "모멘텀": f"{weights['momentum']:.0%}",
            "퀄리티": f"{weights['quality']:.0%}",
            "밸류": f"{weights['value']:.0%}",
            "저변동성": f"{weights['low_vol']:.0%}",
            "주식 비중": f"{weights['equity_exposure']:.0%}",
        })
    st.dataframe(pd.DataFrame(regime_table), use_container_width=True, hide_index=True)

    st.divider()

    # 스트레스 테스트 결과
    st.subheader("스트레스 테스트 (현재 레짐 가중치 기준)")

    # 더미 포트폴리오로 스트레스 테스트 실행
    dummy_portfolio = pd.DataFrame([
        {"ticker": "EQUITY", "weight": equity, "strategy_source": "equity"},
        {"ticker": risk_off_asset, "weight": risk_off_w, "strategy_source": "risk_off"},
    ])

    try:
        stress_results = stress_test(dummy_portfolio)
        stress_rows = []
        for scenario_name, result in stress_results.items():
            stress_rows.append({
                "시나리오": result["description"],
                "예상 손실": f"{result['expected_loss']:.2%}",
                "주식 비중": f"{result['equity_weight']:.2%}",
                "채권 비중": f"{result['treasury_weight']:.2%}",
            })
        if stress_rows:
            st.dataframe(
                pd.DataFrame(stress_rows),
                use_container_width=True,
                hide_index=True,
            )
    except Exception as e:
        logger.error(f"[대시보드] 스트레스 테스트 실패: {e}")
        st.error(f"스트레스 테스트 계산 오류: {e}")


def render_tab_data_status() -> None:
    """Tab 4: 데이터 상태."""
    st.header("데이터 상태")

    status = load_data_status()

    if not status:
        st.error("데이터 상태를 조회할 수 없습니다.")
        return

    # raw.prices
    st.subheader("주가 데이터 (raw.prices)")
    prices_stat = status.get("prices", {})
    col1, col2, col3 = st.columns(3)
    col1.metric("최근 날짜", prices_stat.get("latest_date", "N/A"))
    col2.metric("총 행수", f"{prices_stat.get('total_rows', 0):,}")
    col3.metric("유니크 티커 수", f"{prices_stat.get('unique_tickers', 0):,}")

    st.divider()

    # raw.fred_series
    st.subheader("FRED 거시지표 (raw.fred_series)")
    fred_stat = status.get("fred", {})
    col1, col2, col3 = st.columns(3)
    col1.metric("최근 날짜", fred_stat.get("latest_date", "N/A"))
    col2.metric("총 행수", f"{fred_stat.get('total_rows', 0):,}")
    col3.metric("유니크 시리즈 수", f"{fred_stat.get('unique_series', 0):,}")

    st.divider()

    # raw.sec_financials
    st.subheader("SEC 재무 데이터 (raw.sec_financials)")
    sec_stat = status.get("sec", {})
    col1, col2, col3 = st.columns(3)
    col1.metric("최근 제출일", sec_stat.get("latest_date", "N/A"))
    col2.metric("총 행수", f"{sec_stat.get('total_rows', 0):,}")
    col3.metric("유니크 티커 수", f"{sec_stat.get('unique_tickers', 0):,}")

    st.divider()

    # feature 테이블
    st.subheader("레짐 피처/라벨 (feature 스키마)")
    col1, col2 = st.columns(2)

    labels_stat = status.get("regime_labels", {})
    with col1:
        st.metric(
            "regime_labels 최근 날짜",
            labels_stat.get("latest_date", "N/A"),
        )
        st.caption(f"총 {labels_stat.get('total_rows', 0):,}행")

    features_stat = status.get("regime_features", {})
    with col2:
        st.metric(
            "regime_features 최근 날짜",
            features_stat.get("latest_date", "N/A"),
        )
        st.caption(f"총 {features_stat.get('total_rows', 0):,}행")


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main() -> None:
    """대시보드 메인 진입점."""
    st.title("레짐 기반 퀀트 시스템 대시보드")

    logger.info(f"[대시보드] 렌더링 시작: 기준일={selected_date_str}")

    # 조회 시작 날짜 계산
    start_date = (selected_date - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end_date = selected_date_str

    tab1, tab2, tab3, tab4 = st.tabs(
        ["성과 요약", "레짐 모니터", "포트폴리오 현황", "데이터 상태"]
    )

    with tab1:
        try:
            render_tab_performance(start_date, end_date)
        except Exception as e:
            logger.error(f"[대시보드] 성과 요약 탭 오류: {e}")
            st.error(f"성과 요약 탭 오류: {e}")

    with tab2:
        try:
            render_tab_regime(start_date, end_date, selected_date_str)
        except Exception as e:
            logger.error(f"[대시보드] 레짐 모니터 탭 오류: {e}")
            st.error(f"레짐 모니터 탭 오류: {e}")

    with tab3:
        try:
            render_tab_portfolio(selected_date_str)
        except Exception as e:
            logger.error(f"[대시보드] 포트폴리오 현황 탭 오류: {e}")
            st.error(f"포트폴리오 현황 탭 오류: {e}")

    with tab4:
        try:
            render_tab_data_status()
        except Exception as e:
            logger.error(f"[대시보드] 데이터 상태 탭 오류: {e}")
            st.error(f"데이터 상태 탭 오류: {e}")

    logger.info("[대시보드] 렌더링 완료")


if __name__ == "__main__":
    main()
