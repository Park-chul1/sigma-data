# Project Sigma

CRSP 기반 연구 데이터 저장소입니다. 계층은 `RAW → NORMALIZED → PIT/DERIVED → FACTOR → BACKTEST`이며,
원천 데이터와 WRDS 인증정보는 Git에 넣지 않습니다. 처리 원칙은 [principles.md](principles.md)를 따릅니다.

현재 수집·정규화 코드는 `src/ingest`, `src/normalize`와 `scripts/ingest_*`, `scripts/normalize_*`에 있습니다.
정규화는 원천 컬럼의 타입·이름 변환이며, 수정주가 생성이나 엄격한 PIT 보장을 의미하지 않습니다.
기존 `build_universe_sample.py`는 2025년 1월 샘플입니다. 새 연구용 실행은 다음 CLI를 사용합니다.

## 연구 환경과 실행

기존 `.venv`의 Python 실행 파일이 손상되어 있으므로 보존하고 별도 환경을 만듭니다.
아래 의존성은 Python 3.14.4에서 검증했습니다. WRDS 연결 없이 로컬 Parquet만 읽습니다.

```sh
python3 -m venv .venv-research
.venv-research/bin/python -m pip install -r requirements-research.txt
.venv-research/bin/python scripts/build_backtest_dataset.py \
  --start 2020-08-03 --end 2020-09-04 \
  --lookback 20 --training-sessions 20 --label-horizon 5
.venv-research/bin/python -m unittest discover -s tests -v
```

결과는 `data/derived/crsp/research/runs/<run-id>/`에 생성됩니다. 전략 입력은
`features.parquet` 또는 `evaluation_features.parquet`입니다. `panel`, `events`, `labels`,
`display_indices`에는 사후 결과나 표시 정보가 있으므로 그대로 전략 입력에 넣으면 안 됩니다.

현재 지원은 **근사 PIT 연구 데이터와 vendor 총수익률 처리**입니다. 발표 시각·과거 데이터 빈티지,
정확한 지급일·분배 조건·시가가 부족하여 엄격한 PIT, 현금/주식 계좌 원장 및 다음 날 시가 체결은 지원하지 않습니다.
결측 결과가 있는 실행은 `INCOMPLETE_RETURNS`로 표시하며, 보유분 검증용 API는 해결되지 않은 손익을 실패시킵니다.

- [실행·스키마·이벤트 정책](docs/PIT_RESEARCH_PIPELINE.md)
- [공식 CRSP 근거와 로컬 데이터 검증](docs/CRSP_SOURCE_EVIDENCE.md)
- [2026-09-27 기준 초기 감사](docs/PIT_DATASET_AUDIT.md)
