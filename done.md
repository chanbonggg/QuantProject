# 완료 이력 및 보관된 구현 계획

이 파일은 `AGENTS.md`에서 분리한 완료된 구현 이력, 과거 계획, 과거 운영 기준을 보관한다. 현재 작업 기준은 `AGENTS.md`를 우선한다.

---

## 백테스트 결과 (2026-03-27 완료)

| 기간 | CAGR | Sharpe | MDD | Alpha | Beta | Calmar | Turnover |
| ---- | ---- | ------ | --- | ----- | ---- | ------ | -------- |
| 2024 (1년) | 14.94% | 1.11 | -7.08% | -3.52% | 0.73 | 2.11 | 12.89%/월 |
| 2024-01 ~ 2025-03 | 16.7% | 0.91 | -12.0% | - | - | - | - |
| 2025-03 ~ 2026-03 | 22.9% | 1.16 | -10.3% | - | - | - | - |

### DB 현황 (2026-03-27 기준)

- `raw.prices`: 777,094행 | 2020-01-02 ~ 2026-03-26 | 506개 티커
- `raw.fred_series`: 16,373행 | 2020-01-01 ~ 2026-03-26 | 12개 시리즈
- `raw.sec_financials`: 38,859행 | 501개 티커

### 주요 관찰

- 2024년 전체 기간 Regime C (Range) — Regime A/B 미발동
- 2024년 S&P500 약 25% 강세장 대비 Value 중심 전략 언더퍼폼 (Alpha -3.52%)
- 2025~2026 구간 CAGR 22.9%로 개선

---

## STEP 5: 백테스트 프레임워크 (완료, 2026-03-22)

### 구현 완료

- 5A: `backtest/engine.py` — 백테스트 엔진 + 거래비용 모델 + 성과 지표
- 5B: `backtest/walk_forward.py` — Walk-Forward + DSR + PBO + 레짐별 성과 + 스트레스 테스트
- `pytest tests/test_backtest.py` 34개 통과
- `pytest tests/test_walk_forward.py` 27개 통과
- 당시 전체 테스트 135개 통과

### 5A: 기본 백테스터

- 미국 거래비용 모델 (`TransactionCostModel`)
  - SEC Fee: 매도 x 0.00278%
  - 위탁수수료: 왕복 2bp
  - 슬리피지: 편도 5bp
- 성과 지표 산출 (`compute_metrics`)
  - CAGR, Sharpe, MDD, Calmar, Alpha/Beta, Information Ratio, Turnover
- `run(portfolio_func, start, end, conn) -> BacktestResult`
  - 월/분기/주 리밸런싱
  - 드리프트 가중치 + 거래비용 차감
  - `portfolio_func` 실패 시 이전 포트폴리오 유지

### 5B: Walk-Forward + 과적합 검정

- `run_wfa`
  - 학습 구간: 10년 롤링
  - OOS 테스트 스텝: 6개월
  - `_generate_oos_periods()` 구현
- `compute_dsr`
  - Bailey & Lopez de Prado (2014)
  - N에 따라 Sharpe Ratio 허들 조정
- `compute_pbo`
  - CSCV 기반
  - 단일 전략은 `None` 반환
- 레짐별 성과 분해
- 스트레스 테스트 시나리오
  - `2008_gfc`
  - `2020_covid`
  - `2022_rate_hike`

---

## STEP 6: 포트폴리오 결합 엔진 + 리스크 관리 (완료, 2026-03-22)

### 구현 완료

- 6A: `portfolio/weight_engine.py` — 레짐별 전략 가중치 + 결합 포트폴리오 구성
- 6B: `portfolio/optimizer.py` — 제약 조건 최적화 + 리스크 오버레이 + 스트레스 테스트
- `pytest tests/test_portfolio.py` 31개 통과

### 6A: 가중치 결정 엔진

| 레짐 | 모멘텀 | 퀄리티 | 밸류 | 저변동성 | 주식비중 |
| ---- | ------ | ------ | ---- | -------- | -------- |
| A (Risk-on) | 40% | 30% | 20% | 10% | 100% |
| B (Risk-off) | 0% | 20% | 20% | 60% | 40% |
| C (Range) | 10% | 30% | 40% | 20% | 80% |
| SHOCK | 0% | 20% | 20% | 60% | 50% |

- Risk-off 대체자산: SHY, TLT, CASH
- `decide_weights(regime, shock_alarm, risk_off_asset) -> dict`
- `build_combined_portfolio(date, regime, conn)`

### 6B: 포트폴리오 최적화

- 종목 최대 비중: 5%
- 섹터 최대 비중: 30%
- 최소 종목 수: 20개
- VIX > 35이면 주식 비중 -20%p 축소
- 스트레스 테스트 시나리오
  - `2008_gfc`
  - `2020_covid`
  - `2022_rate_hike`
  - `inflation_shock`
- `compute_portfolio_stats(portfolio, date)`

---

## STEP 7: 모니터링 대시보드 + 운영 자동화 (완료, 2026-03-22)

### 구현 완료

- 7A: `monitor/dashboard.py` — Streamlit 4탭 대시보드
- 7B: `scripts/daily_run.py` — 10단계 일일 파이프라인 + Slack 알람 + dry-run 모드
- `pytest tests/test_monitor.py` 31개 통과
- `pytest tests/test_daily_run.py` 24개 통과
- 당시 전체 테스트 221개 통과

### 7A: Streamlit 대시보드

- Tab 1: 성과 요약
  - 누적수익률 차트
  - 월별 수익률 히트맵
  - CAGR, Sharpe, MDD, Calmar
- Tab 2: 레짐 모니터
  - VIX 시계열
  - VIX Term Structure
  - 현재 레짐
  - 크레딧 스프레드
  - 급변 알람 이력
- Tab 3: 포트폴리오 현황
  - 상위 보유 종목
  - 전략별 비중
  - 스트레스 테스트
  - 포트폴리오 통계
- Tab 4: 데이터 상태
  - 가격/FRED/SEC 최신 날짜와 행 수
  - 레짐 피처/라벨 최신 날짜

### 7B: 일일 실행 파이프라인

1. yfinance 일별 시세 수집
2. SEC EDGAR 증분 수집
3. FRED 거시지표 업데이트
4. 데이터 품질 체크
5. 레짐 피처 산출
6. 레짐 판단 + 급변 알람
7. 전략 신호 산출
8. 목표 포트폴리오 산출
9. `feature.pipeline_log` 저장
10. Slack webhook 알람 발송

---

## 코드 정리 및 폴더 분리 (2026-05-16)

### 삭제

- `verify_quality.py`: 오래된 단일 검증 스크립트. 삭제된 `quant_us/tests`만 참조하던 코드라 제거.
- `run_2024_only.py`: 현재 운영 기준(PostgreSQL + DuckDB in-memory)과 맞지 않는 구식 DuckDB 파일 직접 백테스트라 제거.
- `quant_us/tests/`: 루트 `tests/`와 중복되는 오래된 테스트 묶음이라 제거.
- `sp500_tickers_list.py`: `price_collector.py`의 `SP500_TICKERS`와 중복되고 외부 참조가 없어 제거.
- 실행 산출물/캐시: `__pycache__`, `.pytest_cache`, `quant_us/logs`, `scripts/*.log`, `wfa.log`, `wfa_result.pkl` 제거.

### 폴더 분리

- 분석 스크립트: `scripts/analysis/run_walk_forward.py`
- 데이터 수집 스크립트:
  - `scripts/data_collection/collect_2025_2026.py`
  - `scripts/data_collection/collect_sec_all.py`
  - `scripts/data_collection/recollect_sec_all.py`

### 엉킨 코드 정리

- 이동한 스크립트의 프로젝트 루트 계산을 `Path(__file__).resolve().parents[2]` 기준으로 수정.
- SEC 전체 수집 스크립트가 중복 티커 파일 대신 `data.collectors.price_collector.SP500_TICKERS`를 사용하도록 변경.
- SEC 재수집 스크립트는 `raw.sec_financials` row count를 먼저 확인하고, `--confirm-delete` 없이는 삭제하지 않도록 보호.
- `run_pipeline(date, conn=...)`가 전달받은 DuckDB 연결을 실제 단계 실행에 사용하도록 수정.
- 레짐 피처/라벨 저장은 PostgreSQL 저장 실패 시에도 주입된 DuckDB 테스트 연결에 best-effort 저장하도록 정리.
- `PortfolioState.save_state()`도 PostgreSQL 저장 실패 시 주입된 DuckDB 연결에 저장해 테스트/분석 경로가 동작하도록 개선.
- 비리밸런싱일에는 `전략신호` 단계를 모듈 import 전에 `skipped` 처리.
- `compute_portfolio_stats()`는 DB 연결이 없어도 기본 통계(HHI, top5 등)를 계산하고, 변동성 추정만 스킵하도록 분리.

### 검증

- `python -m py_compile` 대상 스크립트/수정 파일 통과.
- `python -m pytest tests/ -q`: 230개 통과, 2개 실패.
- 남은 2개 실패는 `tests/test_portfolio_state.py`가 실제 `get_pg_connection()`으로 로컬 PostgreSQL에 직접 접속하는 케이스이며, 현재 환경에서 psycopg2 연결이 `UnicodeDecodeError`로 실패한다. 코드 주입 연결 경로는 통과.

---

## 과거 Docker/PostgreSQL 운영 기준 (보관)

2026-03-28 마이그레이션 당시에는 Docker PostgreSQL을 기본으로 사용했다.

```bash
docker run -d --name quant_pg -p 5433:5432 \
  -e POSTGRES_PASSWORD=quant -e POSTGRES_DB=quant_us \
  -e POSTGRES_HOST_AUTH_METHOD=trust postgres:15
```

당시 `.env` 예시:

```env
PG_DSN=postgresql://postgres:quant@127.0.0.1:5433/quant_us
```

현재는 이 기준을 사용하지 않는다. 현재 운영 기준은 `AGENTS.md`의 로컬 Windows PostgreSQL `5432` 설정이다.

---

## 병렬 에이전트 실패 회고 (2025-03-21, STEP 2)

### 원인 1: 공통 인터페이스 명세 미전달

- `conn` 파라미터 패턴이 price_collector 프롬프트에만 있었고 sec/fred에는 없었음
- 각 에이전트가 독립 설계하여 인터페이스 불일치 발생

### 원인 2: 상호 파일 참조 지시 없음

- sec/fred 에이전트가 price_collector 패턴을 참고했다면 맞출 수 있었음

### 원인 3: 체크리스트 항목과 구현 방법 혼동

- "증분 수집"이 체크리스트에는 있었지만 구현 방법이 프롬프트에 없어서 skip

### 교훈

- 병렬 에이전트 실행 전 공통 인터페이스 명세를 모든 프롬프트에 포함
- 기존 파일을 읽고 패턴을 통일하라고 명시
- 모호한 요구사항은 코드 예시까지 포함
