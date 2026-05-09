# 운영 하드코딩 제거 계획

## 목표

포트폴리오 자본금, 상위 종목 수, 대시보드 기본값 등 운영에 직접 영향을 주는 하드코딩 값을 설정값 또는 UI 입력으로 분리한다.

## 배경

현재 일부 코드에 개인 테스트용 값이 남아 있다.

- `PortfolioState(total_value=500)`
- `optimize(..., top_n=10)`
- 대시보드 총자본금 `$500`

이 값들은 실계좌/페이퍼 트레이딩 규모와 다를 수 있어 운영 결과를 왜곡한다.

## 대상 파일

- `quant_us/scripts/daily_run.py`
- `quant_us/monitor/dashboard.py`
- `quant_us/portfolio/state.py`
- 필요 시 `.env.example`, `README.md`

## 구현 단계

1. `.env` 또는 설정 상수로 `PORTFOLIO_TOTAL_VALUE` 기본값을 정의한다.
2. `daily_run.py`에서 `PortfolioState(total_value=...)` 값을 설정에서 읽는다.
3. `top_n=10`이 실제 운영 정책인지 확인한다.
4. `top_n`이 필요하면 설정값으로 분리하고, 필요 없으면 기본 제약 최적화 흐름으로 되돌린다.
5. 대시보드에서는 총자본금을 입력 위젯 또는 설정값으로 제공한다.
6. 테스트 fixture는 명시적인 total_value를 사용하도록 조정한다.

## 검증

- 관련 기능의 기존 테스트를 필요한 범위만 실행한다.
- 테스트 fixture가 깨지면 명시적인 total_value 기준으로 갱신한다.
- DB 데이터 삭제 없이 dry-run으로 운영 흐름을 확인한다.

운영 dry-run:

```powershell
python quant_us/scripts/daily_run.py --date 2026-04-02 --dry-run
```

## 주의사항

- 기존 `normalized.portfolio_state` 데이터는 삭제하지 않는다.
- 설정 기본값 변경 시 과거 drift 기록과 비교가 어려워질 수 있으므로 기록에 total_value를 계속 저장한다.
