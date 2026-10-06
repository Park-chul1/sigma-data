# Corporate event 연구 경로 통합

갱신: 2026-10-06. 현재 구현과 정책의 전체 설명은
[PIT 연구 파이프라인](PIT_RESEARCH_PIPELINE.md), 원천 필드의 의미와 확인 한계는
[CRSP 원천 근거](CRSP_SOURCE_EVIDENCE.md)를 따른다.

## 실행 경로와 호환성

연구 데이터 생성기는 `src/derive/pipeline.py::build_dataset` 하나다.
`src/derive/crsp_corporate_events.py::build_corporate_event_audit`는 동일한
`ResearchConfig`와 인자를 그 생성기로 전달하는 호환 API다. 별도 수익률 SQL,
날짜 매칭, PIT 정책, 캐시 또는 파일 재사용 규칙을 갖지 않는다.

```sh
.venv-research/bin/python scripts/build_backtest_dataset.py \
  --start 2020-08-03 --end 2020-09-04 \
  --lookback 20 --training-sessions 20 --label-horizon 5

# 같은 parser, 정책, 검증, 원천/설정/코드 해시와 결과를 사용하는 호환 이름
.venv-research/bin/python scripts/build_crsp_corporate_events.py \
  --start 2020-08-03 --end 2020-09-04 \
  --lookback 20 --training-sessions 20 --label-horizon 5
```

두 명령의 기본 결과 위치는 `data/derived/crsp/research/runs/<run-id>/`다.
같은 입력과 설정은 같은 결과를 참조하고, 재사용할 때 산출물 해시를 검사한다.
원천 내용·기간·설정·코드·정책이 바뀌면 해당 실행의 식별자가 달라진다.
RAW/NORMALIZED는 읽기 전용 입력이며 출력 경로로 사용할 수 없다.

이전 `build_period(daily_pattern=..., output_file=..., force=...)` API는
명시적인 이전 안내 오류를 낸다. 기존 `--output`, `--force` 옵션과
`corporate_events/start=.../end=.../part-000.parquet` 출력 계약은 폐기했다.
이전 파일과 `_SUCCESS.json`의 존재를 새 검증 통과로 인정하지 않는다.
기존 결과는 자동 삭제하거나 덮어쓰지 않으며 새 실행은 `--output-root` 아래
검증된 run 디렉터리에 만든다.

예전 최근접 이벤트 진단 `scripts/validate_crsp_delist_events.py`도
`validate_research_run.py`의 호환 이름으로 바뀌었다. 원천 전체를 최근접 날짜로
연결하지 않고 검증할 run 디렉터리를 명시한다.

```sh
.venv-research/bin/python scripts/validate_crsp_delist_events.py \
  data/derived/crsp/research/runs/<run-id> --data-root data
```

## 수정한 오류와 수익률 정책

이전 경로는 `NULLIF(TRIM(d.delist_flag), '') IS NOT NULL`로 `'N'`까지 상장폐지로
분류했고, event date와 daily date를 직접 연결했으며, 숫자가 있는 모든 수익률을
통과시켰다. 이 SQL을 제거하여 canonical 이벤트 검증을 우회할 수 없게 했다.

- `delist_flag='N'`은 일반 일봉이고 `'Y'`는 terminal return 저장 행이다.
  NULL과 미확인 코드는 정상값으로 바꾸지 않는다. 효력 발생일의 일반 거래 행과
  terminal 저장 행은 서로 다른 관측일 수 있다.
- 검증된 연구 수익률만 `ret_total_backtest`로 제공한다. 원천 NULL, 비유한 값,
  −100% 미만 값, 숫자가 있는 `MV` 등의 불완전 값, 미확인 코드와 terminal
  대조 실패는 보존하되 검증 수익률로 통과시키지 않는다.
- 0%와 −100%는 유효한 경제적 값이다. 결측을 0%로 채우지 않고 −100% 이후
  같은 PERMNO의 누적 자산이나 feature 이력을 자동 복구하지 않는다.
- CIZ `DlyRet`의 분배 및 terminal 효과를 한 번만 사용한다. 별도 `DelRet`는
  대조 근거다. `(1 + DlyRet) * (1 + DelRet) - 1`로 재가산하지 않는다.
- `quality_policy='mark'`는 문제 행을 남기고 불완전 상태를 표시한다.
  `quality_policy='fail'`은 검토가 필요한 행을 가진 run 발행을 거부한다.
  어느 쪽도 결과가 좋지 않은 증권을 과거 universe에서 소급 삭제하지 않는다.

## 날짜와 정보의 경계

| 개념 | 의미 |
| --- | --- |
| `event_date` / `effective_date` | 상장폐지 등 사실이 적용되는 원천 날짜 |
| terminal return 저장일 | 일별 파일에 결과가 기록된 날; 원천 `DelDlyDt`와 구별 없이 추정하지 않음 |
| `available_at` | 연구에서 사용 가능하다고 가정한 시각; 현재 일반 관측은 거래소 세션 지연 규칙 |
| 발표일·지급일 | 공개 및 현금 지급 시점; 현재 필드가 없으면 미확인 |
| source vintage | 원천이 어느 시점의 버전인가; content hash는 식별자이지 과거 공개 증거가 아님 |
| `ingested_at` | 실제 수집 시각; 역사적 공개 시각으로 해석하지 않음 |

`DelDlyDt`가 없는 기존 extract에서는 이벤트 다음 거래소 세션 규칙이 명시적인
fallback이다. PERMNO·저장일 키·수익률·결측 상태를 대조하며 최근접 날짜로 연결하지
않는다. fallback 결과는 원천에서 확인된 날짜처럼 표현하지 않는다. 원천 저장일이
있으면 그 값과 provenance를 사용하며, 모순되거나 확인되지 않은 결과는 검토 또는
실패 상태를 유지한다. 저장일을 발표일이나 지급일로 간주할 근거는 없다.

현대 snapshot에는 역사적 수정 빈티지와 실제 공개 시각이 없다. `--pit-mode strict`는
계속 거부한다. 미래 행·이벤트 변경에도 이전 feature가 유지되는 회귀 검사는 계산의
시간 경계를 확인하지만, 과거 snapshot 수정값에 대한 strict PIT를 증명하지 않는다.

## 파일 역할과 학습 표본

| 파일 | 역할과 제한 |
| --- | --- |
| `features.parquet`, `evaluation_features.parquet` | 의사결정 시각의 입력; 이용 가능 시각·과거 이력 조건 적용 |
| `panel.parquet`, `events.parquet`, `identity_events.parquet` | 원천과 사후 이벤트 대조·감사용. 전략 feature로 직접 사용 금지 |
| `terminal_reconciliation.parquet` | terminal 원천 이벤트와 일봉 저장 행의 양방향 키·값·결측 대조. 감사 전용 |
| `labels.parquet` | 미래 vendor return 결과와 상태; cutoff 확인 없이 입력과 결합 금지 |
| `training.parquet` | feature 적격성과 label 성숙·이용 가능 조건을 모두 통과한 학습 사례 |
| `training_audit.parquet`, `training_audit_summary.parquet` | feature 후보별 제외 사유와 연도·거래소·사후 상장폐지 관련 그룹 탈락률. 감사 전용 |
| `display_indices.parquet` | 표시용 기준값 재설정. 학습·universe·체결가격 판단에 사용 금지 |
| `_SUCCESS.json` | 원천·코드·설정·정책·무결성·파일 역할과 한계 |

terminal 결과는 공개·지급 시각을 확인하지 못하므로 feature와 학습 label에 넣지
않는다. 이것이 학습 표본 누락을 없애지는 않는다. label 제외와 사후 상장폐지 관련
정보는 감사 대상으로 남기고, 과거 feature의 적격성 필터로 되돌려 사용하지 않는다.
학습 cutoff까지 미성숙한 label과 관측 자체가 누락된 label은 구별해야 한다.
CLI는 feature 기준 후보·포함·제외 건수를 출력한다. 그룹별 탈락률이 10% 이상이면
선택 편향 경고를 stderr와 manifest에 남기고, 미해결 terminal 대조 역시 경고한다.
낮은 탈락률이나 누출 검사 통과가 선택 편향 부재를 보장하지 않는다.

호환 경로 테스트는 실제 CRSP 코드 형태의 합성 fixture로 `'N'` 일반 행,
효력일/저장일 분리, −50% terminal의 중복 가산 방지, NULL 결과 보존,
strict PIT 거부, 산출물 손상 거부, 동일 canonical run 반환을 검사한다.
전체 테스트 및 실제 데이터 실행 범위·실패 건수는 [검증 기록](PIT_RESEARCH_PIPELINE.md)에
기록하며, 이 문서 자체가 모든 원천 이벤트의 검증 완료를 의미하지 않는다.

## 다음 단계

단일 모멘텀 전략 검증은 기존 입력과 label 제외 감사를 먼저 사용한다.
forward vendor return은 실제 체결 손익이 아니다. 다음 날 시가 체결을 구현하려면
시가, 이전 가격 날짜·수익률 구간, 실제 거래 가능 상태, 비용 규칙이 필요하다.
terminal 공개·지급 시각과 분배 조건을 추가 검증하기 전에는 현금/주식 원장이나
successor 포지션을 임의로 복구하지 않는다.
