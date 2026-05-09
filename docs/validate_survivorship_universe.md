# 유니버스 및 서바이버십 검증 계획

## 목표

S&P500 과거 구성종목 복원, 편입/편출, 상장폐지/합병 처리 로직이 백테스트에서 서바이버십 편향을 충분히 줄이는지 검증한다.

## 배경

현재 `strategies/universe.py`는 `raw.sp500_changes`와 `raw.ticker_events`를 활용하지만, 과거 특정 날짜의 구성종목 복원이 충분히 정확한지는 별도 검증이 필요하다.

## 대상 파일

- `quant_us/strategies/universe.py`
- `quant_us/data/collectors/price_collector.py`
- `quant_us/db/init.py`

## 구현 단계

1. `raw.sp500_changes` 데이터 행 수, 날짜 범위, action 분포를 확인한다.
2. 특정 과거 날짜 몇 개를 골라 구성종목 수가 현실적인지 검증한다.
3. 편입 이후/편출 이전 조건이 정확히 적용되는지 단위 테스트를 작성한다.
4. `raw.ticker_events`의 delisted/merger/ticker_change 활용도를 확인한다.
5. 티커 변경 및 합병 케이스를 fixture로 만들어 테스트한다.
6. 데이터가 부족하면 수집 방식 또는 별도 데이터 소스를 결정한다.

## 검증

- 관련 기능의 기존 테스트를 필요한 범위만 실행한다.
- 테스트 fixture가 깨지면 현재 S&P500 변경 이력 구조에 맞게 갱신한다.
- DB 데이터 삭제 없이 읽기 쿼리로 구성종목/이벤트 분포를 확인한다.

DB 상태 확인:

```powershell
python -c "import sys; sys.path.insert(0, 'quant_us'); from db.init import get_connection; c=get_connection(); print(c.execute('SELECT action, COUNT(*) FROM raw.sp500_changes GROUP BY action').fetchall()); print(c.execute('SELECT COUNT(*) FROM raw.ticker_events').fetchone()); c.close()"
```

## 주의사항

- 구성종목 데이터가 부족하면 코드만으로 서바이버십 편향을 해결할 수 없다.
- 데이터 삭제 없이 검증 쿼리와 테스트 fixture부터 작성한다.
