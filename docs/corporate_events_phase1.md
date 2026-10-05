# Corporate event PIT — 1단계: 상장폐지 수익률

## 결론부터

사용자가 제안한 `WRDS RAW -> normalize -> 기간 선택 -> PIT/event 처리 ->
backtest용 저장` 흐름은 맞다. 단, **데이터 버전별 수익률 의미를 먼저 고정**해야 한다.
이 저장소가 받는 테이블은 CRSP의 신규 CIZ 테이블
`stkdlysecurityprimarydata`이고, 이 테이블의 `DlyRet`에는 delisting return이 이미
반영된다. 따라서 별도 `stkdelists.delret`을 다시 곱하면 같은 손익을 두 번 반영한다.

이번 단계의 정책은 다음과 같다.

```text
ret_total_backtest = normalized daily.ret_total (DlyRet)
return_source = CRSP_CIZ_DAILY
stkdelists.delisting_return = audit/event metadata only
```

`DlyRet`이 없는 상장폐지 관측치는 0이나 임의의 손실률로 대체하지 않고
`return_source=MISSING`, `requires_review=true`로 격리한다. 과거 논문에서 쓰인
-30% 또는 -55% 대체법은 legacy CRSP의 누락 편향을 줄이기 위한 연구자 가정이다.
재현 가능한 민감도 분석 정책으로는 나중에 추가할 수 있지만 canonical 수익률에
조용히 적용하지 않는다.

## 이번 산출물의 흐름

1. 사용자가 `--start`, `--end`를 지정한다.
2. 해당 기간의 normalized daily만 읽는다.
3. security history는 `(permno, date)` 유효기간으로 PIT 결합한다.
4. delisting ledger는 `(permno, event_date)`로 결합하되 수익률 계산에는 쓰지 않는다.
5. 결합 때문에 일봉 행 수가 달라지면 실패한다.
6. Parquet와 `_SUCCESS.json` manifest를 atomic하게 저장한다.

```bash
python scripts/build_crsp_corporate_events.py \
  --start 2020-01-01 --end 2020-12-31
```

기본 출력은
`data/derived/crsp/corporate_events/start=.../end=.../part-000.parquet`이다.
manifest에는 기간, 정책, 입력 경로, 행 수, event 수, review 수가 남는다.

## 놓치기 쉬운 부분

- `delisting_event_date`와 수익률이 놓이는 CIZ 일봉 날짜가 항상 같다고 가정하면
  안 된다. CRSP CIZ의 convention row는 delisting date 다음 거래일에 존재할 수
  있다. 그래서 daily의 `delist_flag`도 독립적으로 보존한다.
- 현 단계의 event join은 정확한 event-date ledger다. 날짜가 다른 convention row와
  event를 하나의 행으로 강제 이동하지 않는다.
- successor PERMNO는 식별 정보일 뿐이다. 합병/주식교환 조건을 확인하지 않고 기존
  포지션을 successor로 자동 이전하지 않는다.
- 이번 출력은 **수익률과 감사 가능한 이벤트 상태를 확정하는 첫 레이어**다. 다음
  단계에서 listing/trading 상태와 base-100 index를 추가한 뒤 factor/backtest가 이
  데이터만 소비하도록 한다.

## 연구 방법론 검토

Shumway (1997)는 누락된 상장폐지 손익을 버리면 특히 실패 기업과 소형주의 성과가
상향 편향된다는 점을 보였다. Shumway and Warther (1999)는 NASDAQ의
performance-related missing delisting return에 -55% 보정을 제안했다. 이는
상장폐지 종목을 표본에서 제거하면 안 된다는 현재 설계의 근거다.

하지만 신규 CIZ에서는 WRDS의 공식 event-study 예제도 delisting return이 이미
일별 수익률에 포함되었다고 명시한다. 따라서 legacy SIZ 코드의
`(1 + RET) * (1 + DLRET) - 1` 관행을 CIZ에 그대로 이식하는 것은 잘못이다.

권장 검증은 canonical CIZ 결과와 별도 sensitivity run을 구분하는 것이다.

1. canonical: vendor CIZ `DlyRet`, 무대체
2. sensitivity: missing performance delist에 명시적 가정 적용
3. 두 결과의 universe/portfolio 차이를 run manifest와 함께 비교

## 다음 이벤트 순서

1. 상장폐지 convention date와 마지막 거래일 상태 검증
2. 현금배당/특별배당 (`DlyRet` 대 `DlyRetx`, 중복 현금흐름 방지)
3. split/역분할 (raw price 보존, 수량 상태 별도)
4. 합병/주식교환 (cash와 successor shares 분리)
5. spin-off와 rights (불완전 조건은 review 격리)

## 참고 자료

- Tyler Shumway, *The Delisting Bias in CRSP Data*, Journal of Finance (1997),
  DOI: `10.1111/j.1540-6261.1997.tb03818.x`
- Tyler Shumway and Vincent Warther, *The Delisting Bias in CRSP's Nasdaq Data
  and Its Implications for the Size Effect*, Journal of Finance (1999)
- WRDS, *Run an Event Study (CIZ Format)* — CIZ 수익률에 delisting return이 이미
  포함된다는 공식 예제
- CRSP, *SIZ to CIZ Mapping, Delisting Convention Daily Changes* — convention
  필드의 일별 배치 설명
