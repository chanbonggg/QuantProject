# 밸류 전략 Market Cap 명세

## 목표

밸류 전략이 `adj_close` 가격 프록시가 아니라 실제 market cap 또는 룩어헤드 방지 정책을 만족하는 market cap 데이터를 사용한다.

## 현재 미구현 근거

- `quant_us/strategies/value.py`의 `_get_market_caps()`가 `adj_close`를 market cap 프록시로 사용
- `normalized.ticker_market_cap` 같은 기준일별 market cap 테이블 없음

## 대상 파일

- `quant_us/strategies/value.py`
- `quant_us/data/collectors/price_collector.py`
- `quant_us/db/init.py`
- 관련 테스트

## 데이터 정책

우선순위:

1. 기준일에 유효한 일자별 market cap
2. 기준일에 유효한 shares outstanding과 가격으로 계산한 market cap
3. 최신 snapshot은 운영 현재값 표시에는 허용하되 과거 백테스트에는 사용 금지
4. 데이터가 없으면 해당 ticker를 value universe에서 제외

금지:

- `adj_close` 단독 market cap 프록시
- 기준일 이후에 알게 된 최신 snapshot을 과거 계산에 적용

## 스키마 후보

```sql
CREATE TABLE IF NOT EXISTS normalized.ticker_market_cap (
    ticker TEXT NOT NULL,
    as_of_date DATE NOT NULL,
    market_cap NUMERIC,
    shares_outstanding NUMERIC,
    source TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, as_of_date, source)
);
```

## 구현 요구사항

- `_get_market_caps()`는 실제 market cap 데이터를 우선 조회한다.
- 기준일 데이터가 없으면 해당 ticker를 제외한다.
- 제외 수와 제외 사유를 로그로 남긴다.
- 테스트 fixture에 market cap 있음/없음 케이스를 포함한다.
- 현재 `raw.prices.market_cap` 채움 상태를 읽기 쿼리로 확인한다.

## 완료 기준

- `value.py`에서 `adj_close` market cap 프록시가 제거된다.
- 데이터 없는 ticker는 명시적으로 제외된다.
- 밸류 팩터 테스트가 market cap fixture 기준으로 통과한다.

## 검증 명령

```powershell
python -c "import sys; sys.path.insert(0, 'quant_us'); from db.init import get_pg_connection; c=get_pg_connection(); cur=c.cursor(); cur.execute('SELECT COUNT(*) FROM raw.prices WHERE market_cap IS NOT NULL'); print(cur.fetchone()); c.close()"
python -m pytest tests/ -q -m "not integration"
```

