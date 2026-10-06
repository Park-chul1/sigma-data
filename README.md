# Project Sigma

CRSP 기반 연구 데이터 저장소입니다. 계층은 `RAW → NORMALIZED → PIT/DERIVED → FACTOR → BACKTEST`이며,
원천 데이터와 WRDS 인증정보는 Git에 넣지 않습니다. 처리 원칙은 [principles.md](principles.md)를 따릅니다.

현재 수집·정규화 코드는 `src/ingest`, `src/normalize`와 `scripts/ingest_*`, `scripts/normalize_*`에 있습니다.
정규화는 원천 컬럼의 타입·이름 변환이며, 수정주가 생성이나 엄격한 PIT 보장을 의미하지 않습니다.
기존 `build_universe_sample.py`는 2025년 1월 샘플입니다. 새 연구용 실행은 다음 CLI를 사용합니다.

## 연구 환경과 실행

기존 `.venv`의 Python 실행 파일은 손상되어 보존했습니다. 로컬에는 동작하는 `.venv-research`가 있으며, 새 checkout에서는 아래처럼 별도 환경을 만듭니다.
아래 의존성은 Python 3.14.4에서 검증했습니다. WRDS 연결 없이 로컬 Parquet만 읽습니다.

```sh
python3 -m venv .venv-research
.venv-research/bin/python -m pip install -r requirements-research.txt
.venv-research/bin/python scripts/build_backtest_dataset.py \
  --start 2020-08-03 --end 2020-09-04 \
  --lookback 20 --training-sessions 20 --label-horizon 5
.venv-research/bin/python -m unittest discover -s tests -v
```

연구 생성 경로는 `src/derive/pipeline.py` 하나입니다. `build_crsp_corporate_events.py`도 동일 CLI/정책/캐시를
사용하는 호환 진입점입니다. 이전 `build_period` API와 기간 경로의 `part-000.parquet` 출력은 폐기했습니다.

결과는 `data/derived/crsp/research/runs/<run-id>/`에 생성됩니다. 전략 입력은
`features.parquet` 또는 `evaluation_features.parquet`입니다. `panel`, `events`, `labels`,
`display_indices`, `terminal_reconciliation`, `training_audit*`에는 사후 결과나 표시 정보가 있으므로 전략 입력에 넣으면 안 됩니다.
학습 후보·포함·제외 사유와 연도/거래소/사후 상장폐지 그룹별 탈락률을 감사하며, 10% 이상 탈락 그룹은 경고합니다.

현재 지원은 **근사 PIT 연구 데이터와 vendor 총수익률 처리**입니다. 발표 시각·과거 데이터 빈티지,
정확한 지급일·분배 조건·시가가 부족하여 엄격한 PIT, 현금/주식 계좌 원장 및 다음 날 시가 체결은 지원하지 않습니다.
결측 결과가 있는 실행은 `INCOMPLETE_RETURNS`로 표시하며, 보유분 검증용 API는 해결되지 않은 손익을 실패시킵니다.

- [실행·스키마·이벤트 정책](docs/PIT_RESEARCH_PIPELINE.md)
- [공식 CRSP 근거와 로컬 데이터 검증](docs/CRSP_SOURCE_EVIDENCE.md)
- [2026-09-27 기준 초기 감사](docs/PIT_DATASET_AUDIT.md)

검증은 `.github/workflows/research.yml`에서 문법 검사와 synthetic unittest로 자동 실행됩니다.
WRDS 접속과 라이선스 데이터는 필요하지 않습니다. 원천 빈티지의 과거 수정은 미래 행 불변성 검사로 해결되지 않습니다.
2026-10-06 로컬 검증: synthetic 테스트 78개, 실제 입력 감사 24개, 대표·경계 run 독립 검사 각 80개 통과.
미해결 수익률은 삭제 없이 `INCOMPLETE_RETURNS`로 남습니다. 원격 CI는 아직 실행하지 않았습니다.
최신 감사 결과와 실제 검증 명령은 [연구 파이프라인 문서](docs/PIT_RESEARCH_PIPELINE.md)의 검증 절을 참고합니다.
