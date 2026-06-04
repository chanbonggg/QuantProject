# 운영 설정화 명세

## 목표

포트폴리오 자본금, 목표 종목 수, 대시보드 기본 자본금을 하드코딩에서 설정값으로 분리한다.

## 현재 미구현 근거

- `quant_us/scripts/daily_run.py`에 `PortfolioState(total_value=500)` 남음
- `quant_us/scripts/daily_run.py`에 `optimize(..., top_n=10)` 남음
- `.env.example` 없음

## 대상 파일

- `quant_us/scripts/daily_run.py`
- `quant_us/monitor/dashboard.py`
- `quant_us/portfolio/state.py`
- 신규 `.env.example`
- `README.md`

## 설정 명세

| 환경변수 | 타입 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `PORTFOLIO_TOTAL_VALUE` | float | `10000` | 일일 파이프라인 목표 포트폴리오 총자본금 |
| `PORTFOLIO_TOP_N` | int | `10` | 최종 포트폴리오 상위 종목 수 |
| `DASHBOARD_DEFAULT_TOTAL_VALUE` | float | `PORTFOLIO_TOTAL_VALUE` | 대시보드 입력 기본값 |

## 구현 요구사항

- 환경변수 파싱 helper를 추가한다.
- 값이 없거나 파싱 실패하면 warning 로그를 남기고 기본값을 사용한다.
- `daily_run.py`는 실제 사용한 `total_value`, `top_n`을 pipeline detail 또는 로그에 남긴다.
- 대시보드는 설정값을 기본값으로 쓰되 UI 입력으로 조정 가능해야 한다.
- 저장되는 portfolio state에는 실제 사용된 `total_value`가 계속 기록되어야 한다.

## 완료 기준

- 운영 의미의 `total_value=500`, `$500`, `top_n=10` 하드코딩이 제거된다.
- `.env.example`에 설정값 예시가 있다.
- `README.md`에 설정 방법이 있다.
- dry-run에서 설정값 기반으로 포트폴리오 상태가 생성된다.

## 검증 명령

```powershell
rg "total_value=500|\\$500|top_n=10" quant_us README.md .env.example -n
python quant_us/scripts/daily_run.py --date 2026-04-02 --dry-run
```

