# 섹터 메타데이터 명세

## 목표

전략과 포트폴리오 제약이 같은 섹터/산업 메타데이터 소스를 사용한다.

## 현재 미구현 근거

- `low_vol.py`에 고정 섹터 매핑 존재
- `quality.py`에 `normalized.ticker_info` TODO 존재
- `momentum.py`의 sector neutral 옵션이 미구현 상태
- `optimizer.py`는 입력 DataFrame에 `sector` 컬럼이 없으면 섹터 제약을 skip

## 대상 파일

- `quant_us/db/init.py`
- 신규 후보 `quant_us/data/ticker_info.py`
- `quant_us/strategies/low_vol.py`
- `quant_us/strategies/quality.py`
- `quant_us/strategies/momentum.py`
- `quant_us/portfolio/optimizer.py`
- 관련 테스트

## 스키마 명세

```sql
CREATE TABLE IF NOT EXISTS normalized.ticker_info (
    ticker TEXT PRIMARY KEY,
    sector TEXT,
    industry TEXT,
    source TEXT NOT NULL,
    as_of_date DATE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 인터페이스 명세

```python
def get_ticker_info(tickers: list[str], as_of_date: str | None = None, conn=None):
    ...

def get_ticker_sector(ticker: str, as_of_date: str | None = None, conn=None) -> str | None:
    ...
```

## 구현 요구사항

- 섹터 조회 helper를 한 곳에 둔다.
- 전략별 고정 매핑은 제거하거나 테스트 fallback으로만 둔다.
- 섹터 데이터가 없을 때의 정책을 통일한다.
  - 섹터 제약: `Unknown` 버킷 사용
  - 금융주 제외 같은 필터: sector가 없으면 제외하지 않고 warning 로그
- source와 as_of_date를 저장해 데이터 출처를 추적한다.
- 포트폴리오 optimizer 입력에 sector 컬럼이 들어오도록 상위 전략/weight engine 경로를 정리한다.

## 완료 기준

- 섹터 정보를 참조하는 전략이 같은 helper/table을 사용한다.
- 고정 매핑이 기본 경로가 아니다.
- 섹터 누락 정책이 테스트로 검증된다.

## 검증 명령

```powershell
rg "sector|industry|_get_gics_sector|ticker_info" quant_us/strategies quant_us/portfolio quant_us/db -n
python -m pytest tests/ -q -m "not integration"
```

