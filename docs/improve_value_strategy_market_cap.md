# 밸류 전략 시가총액 개선 계획

## 목표

밸류 전략이 가격 프록시가 아니라 실제 시가총액 또는 합리적인 market cap 데이터를 사용하도록 개선한다.

## 배경

현재 `value.py`는 market cap을 실제 시가총액이 아니라 조정종가 프록시로 사용한다. 이 상태에서는 BM, EP, CFP 팩터의 경제적 의미가 약해진다.

## 대상 파일

- `quant_us/strategies/value.py`
- `quant_us/data/collectors/price_collector.py`
- `quant_us/db/init.py`
- 필요 시 신규 collector 또는 ticker master 테이블

## 구현 단계

1. 현재 `raw.prices.market_cap` 컬럼의 실제 채움 상태를 확인한다.
2. market cap 소스를 결정한다.
   - yfinance `Ticker.info` 또는 `fast_info`
   - 별도 shares outstanding 수집
   - 외부 무료 API
3. 정확한 일자별 market cap이 어렵다면 우선 최신 market cap 스냅샷 테이블을 설계한다.
4. `value.py`의 `_get_market_caps()`가 실제 market cap을 우선 사용하도록 바꾼다.
5. 데이터 없을 때 가격 프록시를 쓸지, 해당 종목을 제외할지 정책을 결정한다.
6. 테스트 fixture에 market cap을 포함한다.

## 검증

- 관련 기능의 기존 테스트를 필요한 범위만 실행한다.
- 테스트 fixture가 깨지면 market cap 입력값을 포함하도록 갱신한다.
- DB 데이터 삭제 없이 읽기 쿼리로 market cap 채움 상태를 확인한다.

진단:

```powershell
python -c "import sys; sys.path.insert(0, 'quant_us'); from db.init import get_connection; c=get_connection(); print(c.execute('SELECT COUNT(*) FROM raw.prices WHERE market_cap IS NOT NULL').fetchone()); c.close()"
```

## 주의사항

- 부정확한 market cap을 쓰는 것보다 데이터 없음으로 제외하는 편이 나을 수 있다.
- 룩어헤드 방지를 위해 기준일 이후 알게 된 shares/market cap을 과거에 적용하지 않도록 주의한다.
