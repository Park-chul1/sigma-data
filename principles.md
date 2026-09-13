# Project Sigma 데이터 파이프라인 기본 원칙

- 문서 상태: 작업 명세서
- 버전: 0.1
- 최종 수정일: 2026-09-13
- 기본 저장소: `Park-chul1/sigma-data`
- 현재 목표: 재현 가능하고 PIT 및 생존편향을 통제한 백테스트용 데이터셋 구축

## 1. 목적

이 문서는 Project Sigma의 데이터 처리 기본 원칙을 정의한다. 별도 합의가 없는 한 구현, 검토, 데이터 생성 및 백테스트는 이 문서를 따른다. 데이터 의미가 달라지는 변경은 문서 버전과 변경 이력에 기록한다.

## 2. 표준 파이프라인

```text
RAW -> NORMALIZED -> PIT/DERIVED -> FACTOR -> BACKTEST
```

각 계층은 하나의 책임만 가진다.

### 2.1 RAW

벤더가 제공한 사실을 가능한 한 원형에 가깝게 보존한다.

보존 대상:

- 벤더 테이블과 컬럼명
- 값, NULL, 상태 플래그
- 조회 기간, 다운로드 시각, 행 수
- 원본과 파일 메타데이터

금지 사항:

- 유니버스 필터링
- 결측치 대체 및 수익률 재구성
- 미래 corporate event를 이용한 과거 가격 조정
- 현재 종목정보의 과거 소급
- factor 계산

RAW는 불변 입력이다. 재다운로드나 교체는 명시적이고 감사 가능해야 한다.

### 2.2 NORMALIZED

경제적 사실을 바꾸지 않고 프로젝트 표준 스키마로 변환한다.

허용 사항:

- 컬럼명과 타입 표준화
- 컬럼명에 단위 명시
- 경로와 partition 표준화
- key, 날짜, 행 수, 유효기간 검증
- schema version 기록

금지 사항:

- 연구 유니버스 선택
- 상장폐지 종목 제거
- NULL 수익률을 0으로 대체
- 수정주가 및 factor 생성
- 미래정보 사용

기본 불변조건:

```text
RAW 행 수 == NORMALIZED 행 수
```

### 2.3 PIT/DERIVED

시간과 연구 규칙을 처음 적용한다.

- 과거 시점의 종목정보 결합
- 정보 이용 가능 시점 적용
- 날짜별 historical universe 구성
- corporate event 상태 반영
- 백테스트용 최종 수익률 생성
- as-of 가격지수 생성
- 상장, 거래 가능, stale price 상태 생성
- 전략별 파생 입력 생성

### 2.4 FACTOR

FACTOR는 PIT/DERIVED 데이터만 소비한다. 현재 종목정보, 미래 조정된 가격, 미래 재무제표를 직접 읽지 않는다. Winsorization과 z-score는 날짜별 당시 eligible universe에서 계산한다.

### 2.5 BACKTEST

BACKTEST는 PIT/DERIVED와 FACTOR 결과만 소비한다. 결측값이나 이벤트를 조용히 수정하지 않으며, 해결하지 못한 관측치는 품질 플래그로 남긴다.

## 3. PIT 원칙

의사결정 시각 `t`에는 그때까지 실제로 이용 가능했던 정보만 사용한다.

```text
available_at <= decision_time
```

Corporate event 날짜는 의미별로 구분한다.

- `announcement_date`: 발표일
- `available_at`: 전략이 실제 사용할 수 있게 된 시각
- `ex_date`: 권리나 분배 없이 거래되기 시작한 날짜
- `record_date`: 권리 보유자 확정일
- `payment_date`: 현금이나 증권 지급일
- `effective_date`: 증권 또는 회사 상태의 효력 발생일

의미가 다른 날짜를 하나의 generic event date로 합치지 않는다. 시각 정보 없이 날짜만 존재하면 보수적인 lag를 적용한다.

## 4. 기본 의사결정 및 체결 시점

```text
t일 장 마감까지 확정된 정보 사용
-> factor와 signal 계산 및 고정
-> t+1일 시가에 체결
```

- `t`일 종가, 거래량, 일수익률은 장 마감 후 사용한다.
- 발표 시각이 불명확하면 당일 의사결정에 사용하지 않는다.
- Forward return은 평가 label이며 feature에 사용할 수 없다.
- 다른 체결 시점은 run config에 명시한다.

## 5. 종목 식별과 과거 상태

- `PERMNO`를 기본 증권 식별자로 사용한다.
- Ticker, 회사명, CUSIP, 거래소는 시간에 따라 변하는 속성이다.
- Successor PERMNO는 관련된 별도 증권이며 이력을 자동으로 연결하지 않는다.

Historical metadata join:

```sql
daily.permno = security_info.permno
AND daily.date >= security_info.valid_from
AND daily.date <= COALESCE(security_info.valid_to, DATE '9999-12-31')
```

PIT join 후 일봉 행 수가 증가하면 유효기간 중복이나 모호한 매칭으로 판단하고 실패시킨다.

## 6. 가격과 수익률

경제적 성과는 가격 차이가 아니라 수익률과 누적비율로 측정한다. 체결, 유동성, 가격 필터를 위해 원시 가격은 보존한다.

표준 값:

- `price_raw`: 당시 실제 관측가격
- `market_cap_kusd`: 당시 시가총액, 단위 천 달러
- `ret_total_vendor`: 벤더 총수익률
- `ret_ex_div`: 분배 제외 가격수익률
- `ret_total_backtest`: 검증 후 백테스트 수익률
- `return_source`: 최종 수익률의 출처와 생성 방식

### 6.1 기하적 누적

```text
gross_return_t = 1 + return_t
index_t = index_(t-1) * gross_return_t
index_t = 100 * product(1 + return_s)
geometric_mean = product(1 + return_t)^(1 / n) - 1
```

누적 자산을 계산할 때 수익률을 단순 합산하지 않는다.

### 6.2 두 종류의 지수

- `total_return_index`: `ret_total_backtest` 누적, 총성과와 momentum에 사용
- `price_return_index`: `ret_ex_div` 누적, 순수가격 추세에 사용

지수는 파생 결과이며 일별 수익률이 영구 기반 데이터다.

### 6.3 Rebase

영구적인 단일 기준일을 두지 않는다. 백테스트 또는 분석 기간마다 관련 시작일을 100으로 설정한다. Rebased index는 절대가격, 기업 규모, 유동성 비교에 사용할 수 없다.

### 6.4 원시 가격의 용도

`price_raw`는 체결가격, slippage, bid-ask, 유동성, 최소가격 조건 및 거래대금 계산에 사용한다. 기업 규모 비교에는 시가총액을 사용한다.

### 6.5 결측과 -100% 수익률

NULL 수익률을 자동으로 0으로 채우지 않는다. 비거래, 거래정지, 최초 관측, 상장폐지, 가격 결측, 벤더 계산 불가와 오류를 구분한다.

수익률 `-1.0`은 지수를 0으로 만들며 해당 PERMNO의 계열은 이후 회복할 수 없다.

## 7. 백테스트 기간별 데이터 생성

RAW와 NORMALIZED는 재사용한다. PIT/DERIVED, FACTOR, BACKTEST는 사용자가 지정한 기간과 설정에 맞춰 run별로 생성한다.

날짜 구분:

- `requested_start`: 성과 기록 시작일
- `process_start`: lookback과 lag를 확보하기 위한 실제 처리 시작일
- `requested_end`: 백테스트 종료일

`process_start`는 최대 factor lookback, 정보 lag, calendar buffer를 포함해 자동 계산한다.

미래정보 차단:

- 횡단면 표준화는 같은 날짜의 eligible universe에서 계산한다.
- 시계열 추정은 해당 날짜와 과거만 사용한다.
- Forward return은 label로만 사용한다.
- 전체 기간 통계로 과거 feature를 정규화하지 않는다.

포트폴리오 지수는 `requested_start`에서 100으로 시작해 실현 수익률을 기하적으로 누적한다.

각 run은 기간, 설정, lookback, 체결 시점, return/event policy, schema version, source coverage, Git commit, config hash와 생성 시각을 manifest에 기록한다.

## 8. Corporate event 공통 원칙

이벤트가 미치는 네 대상을 분리한다.

1. 당시 실제 시장가격
2. 투자자가 얻은 경제적 수익률
3. 종목 ID와 포지션 상태
4. Signal이 이용할 수 있었던 정보

하나의 수정주가로 네 목적을 모두 처리하지 않는다.

### 8.1 주식분할 및 역분할

- `price_raw`를 보존한다.
- 포지션 수량은 비율에 따라 조정한다.
- 경제적 성과에는 검증된 total return을 사용한다.
- 가격 수준 feature에는 as-of price index를 사용한다.
- 미래 split을 과거에 미리 반영하지 않는다.
- 역분할 단주 현금정산은 별도 정책으로 처리한다.

### 8.2 현금배당

- 총성과에는 total return을 사용한다.
- 순수가격 분석에는 ex-dividend return을 사용한다.
- Total return에 포함된 배당을 다시 더하지 않는다.
- 가격효과에는 ex-date, cash ledger에는 payment date를 사용한다.

### 8.3 특별배당, 자본환급, 청산분배

- Distribution type을 보존하고 total return 포함 여부를 확인한다.
- Ex-date 가격 하락을 원인 없는 손실로 처리하지 않는다.
- 예측 signal에는 발표정보가 이용 가능해진 이후만 사용한다.

### 8.4 주식배당 및 권리배정

- Split과 구분해 원본 이벤트를 보존한다.
- 수량, 권리 행사, 매도, 소멸과 추가 자본투입을 구분한다.
- 전략에 행사 규칙이 없으면 권리를 자동 행사하지 않는다.
- 단순 수익률 백테스트에서는 검증된 vendor total return을 우선한다.

### 8.5 Spin-off

- 모회사와 분사회사 이력을 합치지 않는다.
- 신규 PERMNO를 별도 증권으로 취급한다.
- 분배 가치의 total return 포함 여부를 확인한다.
- 조건을 정확히 재현할 수 있을 때만 신규 포지션을 만든다.

### 8.6 인수합병

- 기존 PERMNO를 이벤트 시점에 종료한다.
- 현금 대가는 cash ledger에 기록한다.
- 주식교환은 조건과 비율이 확인된 경우에만 successor 포지션을 만든다.
- 현금과 successor shares를 분리한다.
- 피인수 종목 이력을 인수회사에 연결하지 않는다.
- 조건이 불완전하면 추정하지 않고 review 대상으로 둔다.

### 8.7 공개매수 및 교환 제안

- 전략 규칙 없이 자발적 참여를 가정하지 않는다.
- 최종 강제 거래는 효력 발생 시점에 반영한다.
- 발표 signal에는 availability time을 적용한다.

### 8.8 파산, 청산 및 상장폐지

- 실제 eligibility를 잃기 전까지 historical universe에 유지한다.
- 최종 경제적 손실을 포함한다.
- 효력 발생 후에만 universe에서 제외한다.
- 결측 최종수익률을 0으로 채우지 않는다.
- Reason, status, payment, missing-return flag를 보존한다.
- 자발적 상장폐지와 비공개 전환은 파산과 구분한다.

### 8.9 거래정지

- 포지션을 즉시 청산할 수 있다고 가정하지 않는다.
- 결측 return을 자동으로 0으로 처리하지 않는다.
- Stale price와 마지막 거래 이후 일수를 기록한다.
- 거래 불가능한 날에는 진입과 청산을 막는다.
- 거래 재개일 gap return 또는 최종 delisting result를 반영한다.

### 8.10 Ticker, 회사명, 거래소, 증권 유형 변경

- PERMNO를 ID로 유지한다.
- 날짜별 유효한 속성과 eligibility를 사용한다.
- 현재 속성을 과거에 소급하지 않는다.
- ADR ratio나 share-class 변화의 기계적 가격변화를 손익으로 오인하지 않는다.

### 8.11 IPO 및 신규 상장

- 첫 eligible date 이전에는 universe에 포함하지 않는다.
- 가격이나 factor history를 backfill하지 않는다.
- Factor별 최소 과거 관측치를 요구한다.
- `is_listed`, `is_trading`, `has_required_history`, `is_investable`을 구분한다.

### 8.12 유상증자 및 자사주 매입

- 당시 시가총액과 주식 수를 보존한다.
- 현재 shares outstanding을 과거에 소급하지 않는다.
- 매입 승인 발표와 실제 매입을 구분한다.
- 발표된 최대 매입 한도를 실제 매입량으로 간주하지 않는다.

### 8.13 기타 조직개편

- 원본 이벤트와 조건을 보존한다.
- PERMNO 연속 여부를 확인한다.
- 누락된 전환 조건을 추정하지 않는다.
- 경제적 성과에는 검증된 vendor total return을 우선한다.
- 모호한 사례는 `requires_review`로 격리한다.

## 9. Delisting return 정책

다음을 분리한다.

```text
ret_total_vendor
delisting_return
ret_total_backtest
return_source
return_was_reconstructed
requires_review
```

규칙:

1. Daily total return에 delisting 효과가 포함됐으면 그대로 사용한다.
2. 두 return이 독립 성분인지 검증하기 전에는 결합하지 않는다.
3. 분리된 성분임이 확인된 경우에만 다음을 사용한다.

   ```text
   (1 + daily_return) * (1 + delisting_return) - 1
   ```

4. 유효한 delisting return만 존재하면 출처를 명시하고 사용할 수 있다.
5. 둘 다 유효하지 않으면 NULL과 review 상태를 유지한다.
6. 최종 경제적 수익률 반영 후 기존 PERMNO 계열을 종료한다.

권장 `return_source`:

```text
CRSP_DAILY
CRSP_DELIST
COMBINED_VERIFIED
RECONSTRUCTED
MISSING
```

## 10. 권장 PIT 일봉 출력

```text
permno
date
price_raw
market_cap_kusd
volume_raw_shares
ret_total_vendor
ret_ex_div
ticker_asof
exchange_asof
security_type_asof
trading_status_asof
is_split_day
is_dividend_day
is_delisting_day
is_halted
successor_permno
ret_total_backtest
return_source
total_return_index
price_return_index
is_listed
is_trading
has_required_history
is_investable
return_was_reconstructed
price_is_stale
event_terms_incomplete
requires_review
```

재현 비용이 낮은 컬럼은 물리적으로 저장하지 않아도 되지만 정의와 provenance는 고정되어야 한다.

## 11. 필수 불변조건

1. PERMNO를 기본 증권 key로 사용한다.
2. Ticker를 안정적인 join key로 사용하지 않는다.
3. 현재 종목정보를 과거에 소급하지 않는다.
4. 미래 corporate event로 raw historical price를 덮어쓰지 않는다.
5. 하나의 경제적 효과를 return과 cash flow에 이중 반영하지 않는다.
6. 상장폐지 종목을 historical universe에서 소급 제거하지 않는다.
7. 알 수 없는 return을 조용히 0으로 대체하지 않는다.
8. 서로 다른 PERMNO 이력을 자동으로 연결하지 않는다.
9. 재구성하거나 선택한 return의 provenance를 보존한다.
10. 모호한 이벤트 조건을 추정하지 않는다.
11. PIT join은 행 폭증을 일으키면 안 된다.
12. FACTOR와 BACKTEST는 PIT-safe 입력만 소비한다.
13. 누적성과는 기하적으로 계산한다.
14. Rebased index를 가격, 시가총액, 유동성 대신 사용하지 않는다.
15. 모든 백테스트는 코드, 데이터, 설정 버전으로 재현 가능해야 한다.

## 12. 현재 구현 상태

구현됨:

- CRSP 일봉 월별 RAW ingest
- RAW success manifest 및 atomic commit
- CRSP security history와 delisting ingest
- 일봉, security history, delisting strict normalization
- 일봉 key, 행 수, 유효기간 검증
- Sample PIT metadata join 검증
- Sample baseline investable universe 생성

미완성:

- 전체 기간 `security_daily_pit`
- 통합 corporate-event ledger
- 상세 CRSP corporate-action 필드
- 검증된 delisting return 결합
- 전체 기간 historical investable universe
- Run별 backtest dataset builder
- Total-return 및 price-return index builder
- Compustat publication-date PIT join
- CRSP-Compustat link history
- 신규 데이터 기반 factor 및 transaction-cost backtest

## 13. 구현 순서

1. 전체 RAW와 NORMALIZED coverage 검증
2. 전체 기간 security-history PIT join 구현
3. CRSP 상세 일봉 및 delisting 필드 확장
4. Daily와 delisting return 처리 검증
5. `ret_total_backtest`와 provenance 생성
6. 전체 기간 listing, trading, investability 상태 생성
7. 자동 lookback을 포함한 run별 PIT dataset builder 생성
8. Run별 base-100 total-return 및 price-return index 생성
9. Split, dividend, merger, spin-off, rights, reorganization 추가
10. Compustat 및 CRSP-Compustat PIT 결합
11. Factor, execution, cost 및 diagnostics 추가

## 14. 미확정 항목

- CRSP daily total return에 포함된 delisting return 범위
- Corporate-event ledger의 최종 필드와 원본 테이블
- Split 및 조직개편 시 단주 처리
- Rights offering 행사 정책
- Spin-off 신규 포지션 정책
- 해결되지 않은 delisting return 기본 처리
- 시각 없이 날짜만 있을 때의 availability lag
- Rebased index 저장 여부
- Cache 구조와 canonical run ID
- Factor별 최소 과거 관측치

## 15. 변경 절차

1. 영향을 받는 계층을 명시한다.
2. PIT와 look-ahead 영향을 설명한다.
3. Key, 날짜, 타입, 단위, NULL 동작을 정의한다.
4. 실패 및 격리 동작을 정의한다.
5. 검증 테스트를 추가하거나 수정한다.
6. 의미가 달라지는 변경은 문서 버전을 올린다.
7. 변경 이력에 기록한다.

## 16. 변경 이력

### Version 0.1 - 2026-09-13

- 표준 계층형 파이프라인 확정
- PIT 정보 이용 시점과 기본 체결 시점 정의
- 수익률과 기하 누적지수를 성과 표현의 기본으로 채택
- 체결과 유동성 분석을 위해 raw price 보존
- Run별 데이터 처리와 base-100 rebase 원칙 채택
- Corporate event 및 delisting 기본 정책 정의
- 현재 구현 상태와 미확정 항목 기록
