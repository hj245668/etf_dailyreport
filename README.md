# KODEX AI전력핵심설비 일일 리포트 (텔레그램 자동 전송)

평일 장 마감 후 GitHub Actions가 `report.py`를 실행해 일일 리포트를 텔레그램으로 보냅니다.
규칙 충족 여부를 점검하는 용도이며 매수·매도 추천이 아닙니다.

## 폴더 구성

```
report.py                         메인 스크립트 (데이터 수집 → 지표 계산 → 텔레그램 전송)
config.json                       종목·규칙·메모 설정 (직접 수정 가능)
requirements.txt                  필요 라이브러리
.github/workflows/daily-report.yml  평일 16:30(KST) 자동 실행 설정
```

## 설정 순서

### 1. 비공개(Private) 저장소 만들기
평단과 수량이 코드 근처에 있으므로 반드시 **Private** 저장소를 쓰세요. 위 파일을 그대로 올립니다.
(`.github` 폴더가 숨김 폴더라 업로드 시 빠지기 쉬우니, 올린 뒤 Actions 탭에서 워크플로가 보이는지 확인하세요.)

### 2. 텔레그램 봇 만들기
1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 이름 입력 → **봇 토큰** 받기
2. 만든 봇을 검색해 들어가 아무 메시지나 한 번 보내기
3. 브라우저에서 `https://api.telegram.org/bot<봇토큰>/getUpdates` 열기
4. 결과의 `"chat":{"id": 숫자}` 가 **채팅 ID**

토큰이 외부에 노출되면 BotFather에서 `/revoke`로 폐기하고 새로 발급하세요.

### 3. GitHub Secrets 등록
저장소 → Settings → Secrets and variables → Actions → New repository secret

| 이름 | 값 | 필수 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 봇 토큰 | ✅ |
| `TELEGRAM_CHAT_ID` | 채팅 ID | ✅ |
| `AVG_PRICE` | 평균단가 (예: 56215) | ✅ |
| `QUANTITY` | 보유 수량 | 권장 |
| `POSITION_LIMIT_KRW` | 이 ETF 평가금액 한도(원) | 선택 |
| `EXTRA_CASH_KRW` | 추가로 쓸 수 있는 현금(원) | 선택 |

### 4. 첫 실행 테스트
Actions 탭 → `daily-etf-report` → Run workflow → `force` 체크 후 실행.
텔레그램에 리포트가 오면 성공입니다. 실패하면 로그와 함께 "리포트 실패" 메시지가 텔레그램으로 옵니다.

### 5. 로컬에서 먼저 확인하고 싶다면
```
pip install -r requirements.txt
export AVG_PRICE=56215 QUANTITY=100
python report.py --dry-run
```
(Windows PowerShell은 `$env:AVG_PRICE="56215"` 형식)

## 규칙 바꾸기 (`config.json`)

| 항목 | 의미 |
|---|---|
| `rules.volume_ratio_max` | 거래량 진정 기준 (5일/60일 평균 ≤ 이 값) |
| `rules.big3_min_up` | 빅3 중 5일 상승해야 하는 최소 종목 수 |
| `rules.rsi_overbought / oversold` | RSI 과열/과매도 기준 |
| `scenarios_pct_of_value` | 물타기 시뮬레이션: 현재 평가금액의 몇 %를 더 살 때를 계산할지 |
| `big3` | 구성 상위 종목 (운용사 PDF로 확인해 수정) |
| `notes` | 매일 리포트 [6]에 표시할 직접 확인 메모 |

## 알아둘 점

- **휴장일/데이터 미갱신**: 최신 데이터 날짜가 오늘이 아니면 전송을 건너뜁니다. 수동 실행의 `force`로 강제 전송할 수 있습니다.
- **실행 시각**: GitHub 예약 실행은 몇 분~수십 분 늦을 수 있습니다. 데이터가 아직 갱신 전이면 그날은 건너뛰므로, 필요하면 `cron` 시각을 늦추세요.
- **60일 비활성**: 공개 저장소는 60일간 활동이 없으면 예약 실행이 꺼질 수 있습니다. 가끔 Actions 탭에서 상태를 확인하세요.
- **데이터 소스**: FinanceDataReader를 먼저 쓰고 실패하면 yfinance로 대체합니다. GitHub 서버(해외)에서 조회가 막히면 실패 알림이 오며, 이 경우 데이터 소스를 바꿔야 합니다.
- **종가 기준**: 배당(분배금) 반영 수정주가가 아니라 실제 종가 기준입니다.
- **수급/공시/뉴스는 자동 수집하지 않습니다**: [6]번 항목의 메모를 보고 직접 확인하세요.

## 다음 단계 아이디어

외국인·기관 수급 추가, DART 공시 알림, 과거 데이터로 규칙 백테스트, 증권사 모의투자 API 연동.
