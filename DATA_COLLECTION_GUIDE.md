# 데이터 수집 가이드

이 문서는 다른 AI에게 복사-붙여넣기 해서 데이터 수집을 맡길 때 사용하는 가이드입니다.

---

## 프로젝트 위치

```
E:\personal project\quant\
```

## 환경변수 (.env 파일)

```
FRED_API_KEY=7f5eeffdaeef4817444aa2fbc331338c
DB_PATH=./data/quant_us.duckdb
```

- `.env` 파일은 프로젝트 루트(`E:\personal project\quant\.env`)에 있음
- DB 파일: `E:\personal project\quant\data\quant_us.duckdb`

---

## 수집기 3개 개요

| 수집기 | 파일 | 소스 | API키 필요 | 수집 대상 |
|--------|------|------|------------|-----------|
| 주가 | `quant_us/data/collectors/price_collector.py` | yfinance (무료) | 없음 | S&P500 503개 종목 OHLCV |
| FRED | `quant_us/data/collectors/fred_collector.py` | FRED API | FRED_API_KEY | 거시지표 13개 시리즈 |
| SEC | `quant_us/data/collectors/sec_collector.py` | SEC EDGAR XBRL | 없음 | 재무제표 (10-K/10-Q) |

---

## 1. 주가 수집 (price_collector.py)

### 뭘 수집하나
- S&P500 구성 종목 503개의 일별 OHLCV (시가/고가/저가/종가/수정종가/거래량)
- yfinance 라이브러리 사용 (무료, API키 불필요)
- 멀티스레딩 10개 워커로 병렬 수집
- 이미 수집된 날짜는 자동 스킵 (증분 수집)

### DB 테이블
- `raw.prices` (ticker, date, open, high, low, close, adj_close, volume, market_cap, source)

### 실행 방법

```bash
# 프로젝트 루트에서 실행
cd "E:\personal project\quant"

# 방법 1: 특정 기간 수집 (가장 많이 쓰는 방법)
python -c "
import sys; sys.path.insert(0, 'quant_us')
from data.collectors.price_collector import collect_range
collect_range('2025-01-01', '2025-03-25')
"

# 방법 2: 특정 날짜 하루만 수집
python -c "
import sys; sys.path.insert(0, 'quant_us')
from data.collectors.price_collector import collect_daily
collect_daily('2025-03-25')
"
```

### 함수 시그니처

```python
# 기간 수집 (증분: 이미 수집된 날짜 자동 스킵)
collect_range(start: str, end: str, conn=None) -> None
# 예: collect_range('2025-01-01', '2025-03-25')

# 하루 수집
collect_daily(target_date: str, conn=None) -> bool
# 예: collect_daily('2025-03-25')  → True/False 반환
```

### 소요 시간
- 하루 수집: 약 1~2분 (503종목 × 멀티스레딩)
- 1년 수집: 약 10~20분 (252 거래일)

### 주의사항
- yfinance는 가끔 타임아웃 발생 → 자동 2회 재시도 내장
- 멀티스레딩 사용 중 가끔 DuckDB 동시성 에러 발생 (503개 중 1~2개) → 재실행하면 해결됨
- 주말/공휴일은 데이터 없음 (정상)
- 거래시간 중 수집하면 당일 데이터는 불완전할 수 있음 → 미국장 마감(EST 16:00) 이후 수집 권장
- 이미 수집된 날짜는 자동 스킵되므로 같은 명령어 여러 번 실행해도 문제 없음

---

## 2. FRED 거시지표 수집 (fred_collector.py)

### 뭘 수집하나
- 미국 거시경제 지표 13개 시리즈:
  - **금리 5개**: DFF(기준금리), DGS3MO(3개월), DGS2(2년), DGS10(10년), DGS30(30년)
  - **크레딧 2개**: BAMLH0A0HYM2(하이일드 스프레드), BAMLC0A0CM(투자등급 스프레드)
  - **경기 3개**: UNRATE(실업률), CPIAUCSL(소비자물가), T10Y2Y(장단기 금리차)
  - **변동성 3개**: VIXCLS(VIX), VIXREM(VIX 3개월), VXMTSI(VXMT)
- FRED_API_KEY 필요 (.env에 설정되어 있음)
- 증분 수집: 마지막 수집일 이후만 가져옴

### DB 테이블
- `raw.fred_series` (series_id, date, value)

### 실행 방법

```bash
cd "E:\personal project\quant"

# 전체 시리즈 수집 (증분: 마지막 수집일 이후만)
python -c "
import sys; sys.path.insert(0, 'quant_us')
from dotenv import load_dotenv; load_dotenv()
from data.collectors.fred_collector import collect_all
result = collect_all('2024-01-01')
print(f'총 {result}행 수집 완료')
"

# 특정 시리즈의 특정 날짜 값 조회 (수집이 아닌 조회)
python -c "
import sys; sys.path.insert(0, 'quant_us')
from dotenv import load_dotenv; load_dotenv()
from data.collectors.fred_collector import get_series
vix = get_series('VIXCLS', '2024-12-31')
print(f'VIX: {vix}')
"
```

### 함수 시그니처

```python
# 전체 시리즈 수집 (증분)
collect_all(start: str = "2000-01-01", conn=None) -> int
# 예: collect_all('2024-01-01')  → 삽입된 행 수 반환

# 특정 시리즈 값 조회 (DB에서)
get_series(series_id: str, as_of_date: str, conn=None) -> Optional[float]
# 예: get_series('VIXCLS', '2024-12-31')  → 17.35
```

### 소요 시간
- 전체 수집: 약 1~2분 (13개 시리즈, 요청간 0.5초 딜레이)

### 주의사항
- **FRED_API_KEY 필수** — .env 파일에 설정되어 있어야 함
- `load_dotenv()` 호출 필요 (또는 환경변수로 직접 설정)
- 일부 시리즈(UNRATE, CPIAUCSL)는 월간 데이터 (매일 값이 있지 않음)
- **VIXREM, VXMTSI 2개는 FRED에서 폐지됨** — 수집 시 에러 나지만 정상임 (11/13 성공이 정상 결과)
- 에러 로그에 `Bad Request. The series does not exist.` 나오면 무시해도 됨

---

## 3. SEC EDGAR 재무제표 수집 (sec_collector.py)

### 뭘 수집하나
- S&P500 종목의 10-K(연간)/10-Q(분기) 재무제표
- SEC XBRL API에서 직접 수집 (API키 불필요)
- 수집 항목 15개: 매출(3종), 순이익, EPS, 총자산, 자기자본(2종), 부채(2종), 영업현금흐름(2종), 매출원가(2종)
- `filed_date`(SEC 제출일) 기준으로 저장 → 룩어헤드 방지

### DB 테이블
- `raw.sec_financials` (ticker, cik, filing_type, period_of_report, filed_date, fiscal_year, fiscal_period, revenue, net_income, eps_diluted, total_assets, stockholders_equity, total_liabilities, operating_cashflow, cost_of_goods_sold)

### 실행 방법

```bash
cd "E:\personal project\quant"

# 특정 종목 1개 수집
python -c "
import sys; sys.path.insert(0, 'quant_us')
from data.collectors.sec_collector import collect_financials
result = collect_financials('AAPL', start_year=2020)
print(f'AAPL: {result}행 수집')
"

# S&P500 전체 수집 (오래 걸림!)
python -c "
import sys; sys.path.insert(0, 'quant_us')
from data.collectors.sec_collector import collect_financials
from data.collectors.price_collector import SP500_TICKERS

total = 0
for i, ticker in enumerate(SP500_TICKERS):
    try:
        n = collect_financials(ticker, start_year=2020)
        total += n
        if (i+1) % 50 == 0:
            print(f'진행: {i+1}/{len(SP500_TICKERS)}, 누적 {total}행')
    except Exception as e:
        print(f'{ticker} 실패: {e}')

print(f'전체 완료: {total}행')
"
```

### 함수 시그니처

```python
# 단일 종목 재무제표 수집
collect_financials(ticker: str, start_year: int = 2010, conn=None) -> int
# 예: collect_financials('AAPL', start_year=2020)  → 삽입된 행 수 반환

# 특정 종목의 특정 날짜 기준 최신 재무 데이터 조회
get_latest_financials(ticker: str, as_of_date: str, conn=None) -> Optional[dict]
# 예: get_latest_financials('AAPL', '2024-12-31')
```

### 소요 시간
- 단일 종목: 2~5초
- S&P500 전체 (503종목): 약 30~60분 (SEC Rate Limit: 요청당 0.12초 딜레이)

### 주의사항
- **SEC Rate Limit 엄격** — 요청간 0.12초 딜레이 자동 적용, 위반시 429 에러
- User-Agent 헤더 필수 (코드에 이미 설정됨)
- **증분 수집**: 이미 수집된 종목은 0행 반환 (정상) — 새 filing이 있을 때만 추가됨
- 같은 명령어 여러 번 실행해도 중복 저장 안 됨
- 외국 기업(ASML, AZN 등)은 SEC에 filing이 없을 수 있음 (정상)
- 증분 수집: 마지막 filed_date 이후 filing만 수집

---

## 현재 DB에 있는 데이터 확인

```bash
cd "E:\personal project\quant"

python -c "
import sys; sys.path.insert(0, 'quant_us')
from db.init import get_connection

conn = get_connection()

# 주가
r = conn.execute('SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(date), MAX(date) FROM raw.prices').fetchone()
print(f'주가: {r[0]}행, {r[1]}종목, {r[2]} ~ {r[3]}')

# FRED
r = conn.execute('SELECT COUNT(*), COUNT(DISTINCT series_id), MIN(date), MAX(date) FROM raw.fred_series').fetchone()
print(f'FRED: {r[0]}행, {r[1]}시리즈, {r[2]} ~ {r[3]}')

# SEC
r = conn.execute('SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(filed_date), MAX(filed_date) FROM raw.sec_financials').fetchone()
print(f'SEC: {r[0]}행, {r[1]}종목, {r[2]} ~ {r[3]}')

conn.close()
"
```

---

## 전체 수집 한번에 하기 (복사-붙여넣기용)

아래를 통째로 실행하면 3가지 데이터를 순차적으로 수집합니다:

```bash
cd "E:\personal project\quant"

python -c "
import sys; sys.path.insert(0, 'quant_us')
from dotenv import load_dotenv; load_dotenv()

# 1. 주가 수집 (2025년)
print('=== 1/3: 주가 수집 시작 ===')
from data.collectors.price_collector import collect_range
collect_range('2025-01-01', '2025-03-25')

# 2. FRED 거시지표 수집
print('=== 2/3: FRED 수집 시작 ===')
from data.collectors.fred_collector import collect_all
fred_rows = collect_all('2025-01-01')
print(f'FRED: {fred_rows}행 수집')

# 3. SEC 재무제표 수집 (시간 오래 걸림)
print('=== 3/3: SEC 수집 시작 ===')
from data.collectors.sec_collector import collect_financials
from data.collectors.price_collector import SP500_TICKERS
total = 0
for i, ticker in enumerate(SP500_TICKERS):
    try:
        n = collect_financials(ticker, start_year=2024)
        total += n
    except:
        pass
    if (i+1) % 100 == 0:
        print(f'SEC 진행: {i+1}/503, 누적 {total}행')
print(f'SEC 완료: {total}행')

print('=== 전체 수집 완료 ===')
"
```

---

## 일일 파이프라인으로 수집하기 (가장 쉬운 방법)

위 3개를 자동으로 해주는 파이프라인이 이미 있습니다:

```bash
cd "E:\personal project\quant"

# 오늘 날짜 기준 수집 + 분석 전체 실행
python quant_us/scripts/daily_run.py

# 특정 날짜 지정
python quant_us/scripts/daily_run.py --date 2025-03-25

# 수집 없이 분석만 (이미 데이터가 있을 때)
python quant_us/scripts/daily_run.py --date 2024-12-31 --dry-run
```

파이프라인은 10단계를 순서대로 실행합니다:
1. 주가 수집 → 2. SEC 수집(월1회) → 3. FRED 수집 → 4. 품질체크 → 5. 레짐피처 → 6. 레짐판단 → 7. 전략신호 → 8. 포트폴리오 → 9. 로그저장 → 10. Slack알람

---

## 트러블슈팅

| 문제 | 원인 | 해결 |
|------|------|------|
| `ModuleNotFoundError: No module named 'yfinance'` | 패키지 미설치 | `pip install -r quant_us/requirements.txt` |
| `FRED API key not found` | 환경변수 미설정 | `.env` 파일에 `FRED_API_KEY=...` 확인, `load_dotenv()` 호출 |
| SEC 수집 429 에러 | Rate Limit 초과 | 자동 재시도 내장, 심하면 잠시 후 재실행 |
| yfinance 타임아웃 | 네트워크 문제 | 자동 2회 재시도 내장, VPN 확인 |
| DB 파일 없음 | 초기화 안됨 | `python quant_us/db/init.py` 실행 |
| `DuckDB IOException` | DB 파일 다른 프로세스가 사용 중 | 대시보드나 다른 파이썬 프로세스 종료 후 재실행 |
