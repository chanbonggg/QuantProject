# Quant US — 레짐 기반 멀티팩터 투자 시스템

S&P500을 대상으로 시장 상태(레짐)를 자동 판단하고,  
모멘텀·퀄리티·밸류·저변동성 4개 팩터를 결합한 포트폴리오를 매일 제안합니다.

---

## 백테스트 성과

| 기간 | CAGR | Sharpe | MDD |
|------|------|--------|-----|
| 2024 | 14.94% | 1.11 | -7.08% |
| 2024.01 ~ 2025.03 | 16.7% | 0.91 | -12.0% |
| 2025.03 ~ 2026.03 | 22.9% | 1.16 | -10.3% |

---

## 시스템 구조

```
[데이터 수집]           [레짐 판단]         [포트폴리오 구성]
  주가 (yfinance)  →  12개 피처 산출  →  레짐 A/B/C 결정
  FRED 거시지표    →  Shock 알람      →  전략 가중치 결정
  SEC 재무제표     →                  →  종목 선정 + 최적화
                                      →  Drift 계산
                                           ↓
[대시보드]                           [주문 제안]
  성과 / 레짐 / 포트폴리오 현황      Drift > 5% 시 자동 표시
```

### 레짐별 전략 가중치

| 레짐 | 설명 | 모멘텀 | 퀄리티 | 밸류 | 저변동성 | 주식비중 |
|------|------|--------|--------|------|----------|----------|
| **A** | 위험선호 (VIX < 20) | 40% | 30% | 20% | 10% | 100% |
| **B** | 위험회피 (VIX >= 25) | 0% | 20% | 20% | 60% | 40% |
| **C** | 중립 (나머지) | 10% | 30% | 40% | 20% | 80% |
| **SHOCK** | 급변 알람 발동 | 0% | 20% | 20% | 60% | 50% |

---

## 설치

### 1. 사전 요구사항

- Python 3.12+
- Docker Desktop (PostgreSQL 컨테이너용)

### 2. 패키지 설치

```bash
cd "E:\personal project\quant"
pip install -r quant_us/requirements.txt
```

### 3. 환경 변수 설정

`.env.example`을 복사해서 `.env`로 만들고 설정:

```env
# 필수
PG_DSN=postgresql://postgres:quant@127.0.0.1:5433/quant_us

# 선택
SLACK_WEBHOOK_URL=https://hooks.slack.com/...   # Slack 알람 (없으면 스킵)
```

### 4. PostgreSQL 컨테이너 실행

```bash
# 최초 1회 — 컨테이너 생성
docker run -d --name quant_pg -p 5433:5432 \
  -e POSTGRES_PASSWORD=quant \
  -e POSTGRES_DB=quant_us \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  postgres:15

# 이후 재시작할 때
docker start quant_pg
```

### 5. DB 스키마 초기화

```bash
python -c "from quant_us.db.init import init_db; init_db()"
```

---

## 사용 방법

### 처음 시작할 때 (데이터 없는 경우)

데이터가 없다면 과거 데이터를 먼저 수집해야 합니다.

```bash
# 1. 주가 수집 (2020년 ~ 현재)
python scripts/data_collection/collect_2025_2026.py

# 2. FRED 거시지표 수집
python -c "
from quant_us.data.collectors.fred_collector import collect_all
collect_all('2020-01-01')
"

# 3. SEC 재무제표 수집 (수 시간 소요)
python scripts/data_collection/collect_sec_all.py
```

---

### 매일 사용하는 방법

#### STEP 1. 데이터 업데이트 + 포트폴리오 산출

```bash
# 오늘 날짜 기준 전체 실행
python quant_us/scripts/daily_run.py

# 특정 날짜 실행 (백테스트/검증용)
python quant_us/scripts/daily_run.py --date 2026-03-31

# 수집 없이 분석만 (빠름)
python quant_us/scripts/daily_run.py --dry-run
```

**10단계 파이프라인:**
```
1. 주가 수집       → raw.prices 업데이트
2. SEC 수집        → 월 1회 자동 실행
3. FRED 수집       → raw.fred_series 업데이트
4. 데이터 품질 체크 → 결측 / 오류 확인
5. 레짐 피처 산출  → feature.regime_features 저장
6. 레짐 판단       → A/B/C + 급변 알람 확인
7. 전략 신호 산출  → 4개 팩터 신호 계산
8. 포트폴리오 산출 → Drift 계산 → 리밸런싱 결정
9. 로그 저장       → feature.pipeline_log 기록
10. Slack 알람     → 레짐 전환/수집 오류 알림
```

#### STEP 2. 대시보드 확인

```bash
streamlit run quant_us/monitor/dashboard.py
# 브라우저에서 http://localhost:8501 열기
```

**4개 탭:**
- **Tab 1 — 성과**: 누적수익률 차트, 월별 히트맵, CAGR/Sharpe/MDD
- **Tab 2 — 레짐 모니터**: VIX 차트, 현재 레짐, 크레딧 스프레드, 급변 알람 이력
- **Tab 3 — 포트폴리오**: 현재 보유 현황, Drift 히스토리, 주문 제안
- **Tab 4 — 데이터 상태**: 각 DB 테이블 최신 날짜 / 행수 확인

#### STEP 3. 주문 제안 확인 및 수동 매매

Tab 3에서 Drift 상태를 확인합니다:

```
✅ 포트폴리오 정렬 상태 (Drift: 2.3% < 5%)  → 매매 불필요
🚨 리밸런싱 필요 (최대 Drift: 6.1% > 5%)   → 주문 제안 표시
```

주문 제안 예시:
```
- AAPL: 매도 (현재 5.8% → 목표 5.0%)
- NVDA: 매수 (현재 4.1% → 목표 5.0%)
- MSFT: 유지 (편차 0.3% — 임계값 미만)
```

> 자동 주문은 없습니다. 제안을 보고 본인이 직접 매매합니다.

---

### 리밸런싱 조건 (Drift-based)

매일 자동으로 계산하며, 아래 조건 중 하나 충족 시 리밸런싱 제안:

| 조건 | 설명 |
|------|------|
| **Drift > 5%** | 특정 종목의 현재 비중이 목표 비중에서 5%p 이상 벗어남 |
| **Regime Shift** | 레짐 전환 또는 급변 알람 발동 |
| **월말** | 매월 마지막 영업일 (기본 리밸런싱 기준) |

---

## 파일 구조

```
quant/
├── quant_us/
│   ├── data/collectors/
│   │   ├── price_collector.py   # 주가 수집 (yfinance)
│   │   ├── fred_collector.py    # 거시지표 수집 (FRED API)
│   │   └── sec_collector.py     # 재무제표 수집 (SEC EDGAR)
│   │
│   ├── regime/
│   │   ├── features.py          # 12개 피처 산출 (VIX, 변동성 등)
│   │   ├── model.py             # 레짐 판단 (A/B/C + 히스테리시스)
│   │   └── shock_alarm.py       # 급변 알람 (6개 조건)
│   │
│   ├── strategies/
│   │   ├── universe.py          # 투자 유니버스 필터링
│   │   ├── momentum.py          # 12-1 모멘텀 팩터
│   │   ├── value.py             # 복합 밸류 팩터
│   │   ├── quality.py           # 퀄리티 팩터
│   │   └── low_vol.py           # 저변동성 팩터
│   │
│   ├── portfolio/
│   │   ├── weight_engine.py     # 레짐별 전략 가중치 결정
│   │   ├── optimizer.py         # 제약조건 최적화 + 리스크 오버레이
│   │   └── state.py             # Drift 계산 + 포트폴리오 상태 저장
│   │
│   ├── backtest/
│   │   ├── engine.py            # 백테스트 엔진 (거래비용 포함)
│   │   └── walk_forward.py      # Walk-Forward + DSR/PBO 검증
│   │
│   ├── monitor/
│   │   └── dashboard.py         # Streamlit 대시보드
│   │
│   ├── scripts/
│   │   └── daily_run.py         # 일일 파이프라인 (메인 실행 파일)
│   │
│   └── db/
│       └── init.py              # DB 연결 + 스키마 초기화
│
├── tests/                       # 테스트 (221개 케이스)
├── scripts/                     # 초기 데이터 수집 스크립트
└── .env                         # 환경 변수
```

---

## DB 구조

| DB | 역할 | 연결 |
|----|------|------|
| **PostgreSQL** (포트 5433) | 데이터 저장 (쓰기) | `get_pg_connection()` |
| **DuckDB** (in-memory) | 데이터 분석 (읽기) | `get_connection()` |

**주요 테이블:**

| 테이블 | 내용 |
|--------|------|
| `raw.prices` | 주가 OHLCV (506개 티커, 2020~현재) |
| `raw.fred_series` | 12개 거시지표 (VIX, VIX3M, HY, IG 등) |
| `raw.sec_financials` | 재무제표 (501개 티커) |
| `feature.regime_features` | 12개 레짐 피처 |
| `feature.regime_labels` | 레짐 판단 결과 (A/B/C) |
| `feature.pipeline_log` | 일일 파이프라인 실행 로그 |
| `normalized.portfolio_state` | 포트폴리오 상태 + Drift 기록 |

---

## 테스트

```bash
# 전체 테스트
python -m pytest tests/ -v

# 모듈별 테스트
python -m pytest tests/test_portfolio_state.py -v   # 포트폴리오 상태
python -m pytest tests/test_portfolio.py -v          # 포트폴리오 구성
python -m pytest tests/test_regime.py -v             # 레짐 판단
python -m pytest tests/test_backtest.py -v           # 백테스트 엔진
```

---

## 핵심 설계 원칙

- **서바이버십 편향 방지** — S&P500 과거 편출 종목까지 포함
- **룩어헤드 방지** — SEC `filed_date` 기준으로만 재무 데이터 사용
- **과적합 방지** — Walk-Forward + DSR + PBO 3중 검증
- **거래비용 현실화** — SEC 수수료, 위탁수수료 2bp, 슬리피지 5bp
- **자동 주문 없음** — 신호만 제공, 최종 결정은 본인
