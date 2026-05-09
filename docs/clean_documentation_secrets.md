# 문서 및 비밀정보 정리 계획

## 목표

문서에 남아 있는 API 키, DB 비밀번호, 오래된 Docker 기준 등 민감하거나 혼동을 줄 수 있는 정보를 정리한다.

## 배경

현재 개인 로컬 운영을 위해 `AGENTS.md`에는 PG_DSN 기준을 기록해 두었지만, 장기적으로는 문서에서 비밀번호/API 키 노출을 줄이는 편이 안전하다. 또한 Docker 5433 기준과 로컬 PostgreSQL 5432 기준이 문서에 섞여 있을 수 있다.

## 대상 파일

- `AGENTS.md`
- `done.md`
- `README.md`
- `DATA_COLLECTION_GUIDE.md`
- `.env.example`
- 기타 `rg "PG_DSN|FRED_API_KEY|rlacksdud|quant@127|5433|VIXREM|VXMTSI"`에 잡히는 문서

## 구현 단계

1. 문서 전체에서 민감정보/오래된 기준을 검색한다.
2. 실제 `.env`는 유지하되, 문서에는 가능한 placeholder를 사용한다.
3. `AGENTS.md`의 로컬 운영 기준은 필요 최소한으로 유지할지 결정한다.
4. Docker 기준은 `done.md`의 과거 이력으로만 남기고 운영 문서에서는 제거한다.
5. `DATA_COLLECTION_GUIDE.md`의 API 키 노출을 제거한다.
6. `.env.example`을 현재 운영 기준에 맞게 갱신한다.

## 검증

```powershell
rg "rlacksdud|FRED_API_KEY=.*[A-Za-z0-9]{10,}|postgres:quant@|127.0.0.1:5433|VIXREM|VXMTSI" AGENTS.md done.md README.md DATA_COLLECTION_GUIDE.md quant_us -n
```

## 주의사항

- 실제 `.env` 파일은 사용자의 로컬 운영에 필요하므로 무단 삭제하지 않는다.
- 공개 저장소에 올릴 가능성이 있으면 비밀번호/API 키는 반드시 제거한다.

