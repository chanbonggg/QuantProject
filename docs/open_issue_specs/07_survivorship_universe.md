# 서바이버십 편향 검증 명세

## 목표

`raw.sp500_changes`와 `raw.ticker_events` 기반 과거 S&P500 유니버스 복원이 백테스트에 충분한지 검증한다.

## 현재 미구현 근거

- 대표 날짜별 유니버스 크기 검증 결과가 문서화되어 있지 않음
- 편입/편출/티커변경 fixture 테스트 필요
- fallback 사용 비율과 데이터 부족 영향이 명확히 기록되어 있지 않음

## 대상 파일

- `quant_us/strategies/universe.py`
- `quant_us/data/collectors/price_collector.py`
- `quant_us/db/init.py`
- 관련 테스트

## 검증 요구사항

### 1. DB 상태 진단

- `raw.sp500_changes` 행 수
- 날짜 범위
- `action` 분포
- `raw.ticker_events` 이벤트 타입 분포

### 2. 대표 날짜별 유니버스 크기 확인

- 2020-01-02
- 2021-01-04
- 2024-01-02
- 최신 가격 데이터 기준일

### 3. 로직 테스트

- 편입 전 ticker는 universe에 포함되지 않는다.
- 편입 후 ticker는 포함된다.
- 편출 후 ticker는 제외된다.
- 티커 변경/합병 이벤트가 fixture 기준으로 반영된다.
- 변경 이력 부족 시 현재 S&P500 fallback이 과도하게 작동하지 않는다.

## 완료 기준

- 대표 날짜별 유니버스 크기가 문서화되어 있다.
- 편입/편출/티커변경 fixture 테스트가 있다.
- fallback 발생 조건 또는 사용 비율이 로그로 확인 가능하다.
- 데이터 부족으로 코드만으로 해결할 수 없는 부분은 데이터 보강 이슈로 분리되어 있다.

## 검증 명령

```powershell
python -c "import sys; sys.path.insert(0, 'quant_us'); from db.init import get_pg_connection; c=get_pg_connection(); cur=c.cursor(); cur.execute('SELECT COUNT(*), MIN(date), MAX(date) FROM raw.sp500_changes'); print(cur.fetchone()); cur.execute('SELECT action, COUNT(*) FROM raw.sp500_changes GROUP BY action ORDER BY action'); print(cur.fetchall()); cur.execute('SELECT event_type, COUNT(*) FROM raw.ticker_events GROUP BY event_type ORDER BY event_type'); print(cur.fetchall()); c.close()"
python -m pytest tests/ -q -m "not integration"
```

