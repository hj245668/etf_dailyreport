#!/usr/bin/env python3
"""KODEX AI전력핵심설비(487240) 일일 퀀트 리포트 -> 텔레그램 전송.

사용법
  python report.py --dry-run   # 전송 없이 화면에만 출력 (로컬 테스트)
  python report.py             # 텔레그램으로 전송 (휴장일/데이터 미갱신이면 건너뜀)
  python report.py --force     # 데이터 날짜가 오늘이 아니어도 전송

환경변수 (GitHub Secrets 로 등록)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   텔레그램 전송용
  AVG_PRICE, QUANTITY                    내 평균단가, 보유 수량
  POSITION_LIMIT_KRW (선택)              이 ETF 평가금액 한도(원)
  EXTRA_CASH_KRW (선택)                  추가로 쓸 수 있는 현금(원)

이 리포트는 규칙 충족 여부를 점검하는 용도이며 매수/매도 추천이 아닙니다.
"""
import argparse
import json
import math
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
NAN = float("nan")


# ----------------------------------------------------------------- 설정/입력
def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def to_float(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_position():
    return {
        "avg": to_float(os.environ.get("AVG_PRICE")),
        "qty": to_float(os.environ.get("QUANTITY")),
        "limit": to_float(os.environ.get("POSITION_LIMIT_KRW")),
        "cash": to_float(os.environ.get("EXTRA_CASH_KRW")),
    }


# ----------------------------------------------------------------- 데이터 수집
def _normalize(df):
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: str(c).capitalize() for c in df.columns})
    if "Close" not in df.columns:
        raise ValueError("Close 컬럼이 없습니다")
    need = ["Open", "High", "Low", "Close", "Volume"]
    for c in need:
        if c not in df.columns:
            df[c] = np.nan
    df = df[need].copy()
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df.index = idx
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["Close"])


def fetch(fdr_symbol, yf_symbol, start):
    """FinanceDataReader -> yfinance 순서로 시도한다."""
    errs = []
    try:
        import FinanceDataReader as fdr

        df = fdr.DataReader(fdr_symbol, start)
        if df is not None and len(df) > 0:
            return _normalize(df)
        errs.append("FDR: 빈 결과")
    except Exception as e:  # noqa: BLE001
        errs.append(f"FDR: {e}")
    try:
        import yfinance as yf

        df = yf.download(yf_symbol, start=start, auto_adjust=False, progress=False)
        if df is not None and len(df) > 0:
            return _normalize(df)
        errs.append("yfinance: 빈 결과")
    except Exception as e:  # noqa: BLE001
        errs.append(f"yfinance: {e}")
    raise RuntimeError(f"{fdr_symbol} 조회 실패 -> " + " | ".join(errs))


# ----------------------------------------------------------------- 지표 계산
def indicators(df):
    c, v = df["Close"], df["Volume"]
    n = len(c)

    def ret(k):
        return c.iloc[-1] / c.iloc[-1 - k] - 1 if n > k else NAN

    out = {"date": df.index[-1].date(), "close": float(c.iloc[-1]), "n": n}
    out["chg"] = ret(1)
    out["ret5"], out["ret20"] = ret(5), ret(20)
    for k in (20, 60, 120):
        out[f"ma{k}"] = float(c.rolling(k).mean().iloc[-1]) if n >= k else NAN

    # RSI(14), Wilder 방식
    if n > 15:
        d = c.diff()
        gain = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        g, l = gain.iloc[-1], loss.iloc[-1]
        out["rsi"] = 100.0 if l == 0 else float(100 - 100 / (1 + g / l))
    else:
        out["rsi"] = NAN

    # 볼린저밴드(20, 2σ) 내 위치 %B
    if n >= 20:
        ma, sd = c.rolling(20).mean().iloc[-1], c.rolling(20).std(ddof=0).iloc[-1]
        up, lo = ma + 2 * sd, ma - 2 * sd
        out["pctb"] = float((c.iloc[-1] - lo) / (up - lo)) if up > lo else NAN
        out["vol20"] = float(c.pct_change().tail(20).std() * math.sqrt(252))
        out["low20"] = float(c.tail(20).min())
        out["new_low20"] = bool(c.iloc[-1] <= c.tail(20).min())
    else:
        out.update(pctb=NAN, vol20=NAN, low20=NAN, new_low20=False)

    # 고점 대비 낙폭(최근 252거래일 종가 기준)
    high = float(c.tail(252).max())
    out["high"] = high
    out["dd"] = float(c.iloc[-1] / high - 1)

    # 거래량: 최근 5일 평균 / 60일 평균
    if n >= 60 and v.tail(60).mean() > 0:
        out["volratio"] = float(v.tail(5).mean() / v.tail(60).mean())
    else:
        out["volratio"] = NAN
    out["volume"] = float(v.iloc[-1]) if not pd.isna(v.iloc[-1]) else NAN
    return out


# ----------------------------------------------------------------- 포맷 도우미
def isnan(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def pct(x, d=2, sign=True):
    if isnan(x):
        return "확인 불가"
    return f"{x * 100:+.{d}f}%" if sign else f"{x * 100:.{d}f}%"


def won(x):
    return "확인 불가" if isnan(x) else f"{x:,.0f}원"


def num(x, d=1):
    return "확인 불가" if isnan(x) else f"{x:,.{d}f}"


def trend_label(i):
    c, a, b, d = i["close"], i["ma20"], i["ma60"], i["ma120"]
    if any(isnan(x) for x in (a, b, d)):
        return "이동평균 계산에 필요한 데이터 부족"
    if a > b > d:
        return "정배열(20>60>120일선, 상승 추세)"
    if a < b < d:
        return "역배열(20<60<120일선, 하락 추세)"
    return "혼조(이동평균선이 얽혀 있음)"


def rsi_label(r, cfg):
    if isnan(r):
        return "확인 불가"
    if r >= cfg["rsi_overbought"]:
        return "과열"
    if r <= cfg["rsi_oversold"]:
        return "과매도"
    return "중립"


def pos_vs(close, ma):
    if isnan(ma):
        return "확인 불가"
    return f"{ma:,.0f}원 ({pct(close / ma - 1)}, {'위' if close >= ma else '아래'})"


# ----------------------------------------------------------------- 리포트
def build_report(cfg, pos, etf, bench, big3):
    """etf/bench: DataFrame, big3: {이름: DataFrame}. 문자열 리포트를 반환."""
    rules = cfg["rules"]
    i = indicators(etf)
    close = i["close"]
    name, code = cfg["ticker"]["name"], cfg["ticker"]["code"]
    avg, qty, limit, cash = pos["avg"], pos["qty"], pos["limit"], pos["cash"]

    # 빅3
    b3 = []
    for nm, df in big3.items():
        bi = indicators(df)
        b3.append((nm, bi))
    up_cnt = sum(1 for _, bi in b3 if not isnan(bi["ret5"]) and bi["ret5"] > 0)

    # 벤치마크 대비
    bench_i = indicators(bench) if bench is not None else None
    rel20 = (
        i["ret20"] - bench_i["ret20"]
        if bench_i and not isnan(i["ret20"]) and not isnan(bench_i["ret20"])
        else NAN
    )

    # 손익
    pnl = None
    if avg:
        pnl = {
            "pct": close / avg - 1,
            "need": avg / close - 1,
            "krw": (close - avg) * qty if qty else None,
            "value": close * qty if qty else None,
        }

    # 규칙 체크 (True/False/None=판단 불가)
    checks = [
        (i["close"] > i["ma60"] if not isnan(i["ma60"]) else None,
         f"종가가 60일선 위 ({pos_vs(close, i['ma60'])})"),
        (i["ret5"] >= 0 if not isnan(i["ret5"]) else None,
         f"최근 5일 수익률 0% 이상 ({pct(i['ret5'])})"),
        (i["volratio"] <= rules["volume_ratio_max"] if not isnan(i["volratio"]) else None,
         f"거래량 진정: 5일/60일 평균 ≤ {rules['volume_ratio_max']:.1f} ({num(i['volratio'], 2)}배)"),
        (up_cnt >= rules["big3_min_up"] if b3 else None,
         f"빅3 중 {rules['big3_min_up']}개 이상 5일 상승 ({up_cnt}/{len(b3)})" if b3 else "빅3 5일 상승 개수"),
        ((pnl["value"] <= limit) if (pnl and pnl["value"] and limit) else None,
         "평가금액이 내가 정한 한도 이내" + (f" ({won(pnl['value'])} / {won(limit)})" if pnl and pnl["value"] and limit else "")),
    ]
    # numpy.bool_ 을 파이썬 bool 로 통일 (is True / is False 비교가 맞게 동작하도록)
    checks = [(None if r is None else bool(r), t) for r, t in checks]
    ok = sum(1 for r, _ in checks if r is True)
    evaluable = sum(1 for r, _ in checks if r is not None)

    warnings = []
    if not isnan(i["rsi"]) and i["rsi"] >= rules["rsi_overbought"]:
        warnings.append(f"RSI {i['rsi']:.1f}: 과열 구간")
    if i["new_low20"]:
        warnings.append("20일 신저가 구간: 하락이 진행 중")
    if pnl and pnl["value"] and limit and pnl["value"] > limit:
        warnings.append("평가금액이 내가 정한 한도를 초과")
    if not warnings:
        warnings.append("특이 경고 없음")

    L = []
    L.append(f"📊 {name}({code}) 일일 리포트")
    L.append(f"기준일 {i['date']} (장마감 데이터 {i['n']}거래일 사용)")
    L.append("")

    # 1. 요약
    L.append("[1] 요약")
    line = f"• 종가 {won(close)} ({pct(i['chg'])})"
    if pnl:
        line += f" · 평단 대비 {pct(pnl['pct'], 1)}"
    line += f" · 고점 대비 {pct(i['dd'], 1)}"
    L.append(line)
    L.append(f"• 추세 {trend_label(i)} · RSI {num(i['rsi'])}({rsi_label(i['rsi'], rules)})")
    if b3:
        L.append(f"• 빅3 5일 기준 상승 {up_cnt}/{len(b3)} · 코스피 대비 20일 {('확인 불가' if isnan(rel20) else f'{rel20 * 100:+.1f}%p')}")
    L.append(f"• 규칙 충족 {ok}/{evaluable}" + (f" (판단 불가 {len(checks) - evaluable}개)" if evaluable < len(checks) else ""))
    L.append("")

    # 2. 가격·손익
    L.append("[2] 가격·손익")
    L.append(f"• 시세: 종가 {won(close)}, 거래량 {num(i['volume'], 0)}주")
    L.append(f"• 기간 수익률: 5일 {pct(i['ret5'])}, 20일 {pct(i['ret20'])}")
    L.append(f"• 52주(최대 252거래일) 고점 {won(i['high'])} 대비 {pct(i['dd'])}")
    if pnl:
        L.append(f"• 평단 {won(avg)} 대비 {pct(pnl['pct'])}")
        L.append(f"• 본전까지 필요한 상승률 {pct(pnl['need'])}  (= {avg:,.0f} ÷ {close:,.0f} − 1)")
        if pnl["krw"] is not None:
            L.append(f"• 평가손익 {pnl['krw']:+,.0f}원, 평가금액 {won(pnl['value'])}")
        for p in cfg.get("scenarios_pct_of_value", []):
            r = p / 100.0
            new_avg = (avg + r * close) / (1 + r)
            need = new_avg / close - 1
            s = f"• 가정: 현재 평가금액의 {p}% 추가 매수 → 평단 {won(new_avg)}, 본전 필요 {pct(need)}"
            if qty:
                s += f" (추가금액 약 {qty * close * r:,.0f}원)"
            L.append(s)
        if cash and qty:
            add = cash / close
            new_avg = (qty * avg + cash) / (qty + add)
            total = (qty + add) * close
            s = f"• 가정: 가용 현금 {won(cash)} 전액 투입 → 평단 {won(new_avg)}, 본전 필요 {pct(new_avg / close - 1)}, 평가금액 {won(total)}"
            if limit:
                s += " (한도 초과)" if total > limit else " (한도 이내)"
            L.append(s)
    else:
        L.append("• 평단 미설정: AVG_PRICE 를 등록하면 손익을 계산합니다")
    L.append("")

    # 3. 추세·기술지표
    L.append("[3] 추세·기술지표")
    L.append(f"• 20일선 {pos_vs(close, i['ma20'])}")
    L.append(f"• 60일선 {pos_vs(close, i['ma60'])}")
    L.append(f"• 120일선 {pos_vs(close, i['ma120'])}")
    L.append(f"• RSI(14) {num(i['rsi'])} → {rsi_label(i['rsi'], rules)}")
    L.append(f"• 볼린저밴드 %B {num(i['pctb'], 2)} (0 이하=하단 이탈, 1 이상=상단 이탈)")
    L.append(f"• 20일 변동성(연환산) {pct(i['vol20'], 1, sign=False)}")
    L.append(f"• 거래량 5일/60일 평균 {num(i['volratio'], 2)}배")
    L.append("")

    # 4. 구성종목 (빅3)
    L.append("[4] 구성 상위 종목(빅3)")
    if b3:
        for nm, bi in b3:
            L.append(f"• {nm} {won(bi['close'])} ({pct(bi['chg'])}) · 5일 {pct(bi['ret5'])} · 20일 {pct(bi['ret20'])}")
        L.append("※ 종목 구성과 비중은 바뀔 수 있으니 운용사 PDF 로 확인하세요.")
    else:
        L.append("• 빅3 데이터를 가져오지 못했습니다")
    L.append("")

    # 5. 시장 대비
    L.append("[5] 시장 대비")
    if bench_i:
        L.append(f"• {cfg['benchmark']['name']} {num(bench_i['close'], 2)} ({pct(bench_i['chg'])}) · 20일 {pct(bench_i['ret20'])}")
        L.append(f"• 이 ETF 20일 {pct(i['ret20'])} → 상대 {('확인 불가' if isnan(rel20) else f'{rel20 * 100:+.1f}%p')}")
    else:
        L.append("• 벤치마크 데이터를 가져오지 못했습니다")
    L.append("")

    # 6. 논거 점검 (자동 수집이 어려운 항목은 직접 확인)
    L.append("[6] 논거 점검(직접 확인)")
    for note in cfg.get("notes", []):
        L.append(f"• {note}")
    L.append("• 자동 수집하지 않는 항목: 수주·실적 공시(DART), 외국인·기관 수급, 뉴스")
    L.append("")

    # 7. 규칙 체크리스트
    L.append("[7] 규칙 체크리스트 (조건 충족 현황이며 매매 지시가 아닙니다)")
    for r, text in checks:
        mark = "✅" if r is True else ("❌" if r is False else "➖")
        L.append(f"{mark} {text}")
    L.append("")

    # 8. 주의
    L.append("[8] 오늘 조심할 점")
    for w in warnings:
        L.append(f"• {w}")
    L.append("• 내일 확인: 60일선 회복 여부, 거래량 변화, 빅3 동반 흐름")
    L.append("")
    L.append("※ 규칙 점검용 분석이며 투자 권유가 아닙니다. 데이터는 지연·오류가 있을 수 있습니다.")
    return "\n".join(L)


def make_report(cfg, pos, fetch_fn=fetch):
    start = (datetime.now(KST) - timedelta(days=420)).strftime("%Y-%m-%d")
    code = cfg["ticker"]["code"]
    etf = fetch_fn(code, f"{code}.KS", start)

    bench = None
    try:
        b = cfg["benchmark"]
        bench = fetch_fn(b["fdr"], b["yf"], start)
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 벤치마크 조회 실패: {e}", file=sys.stderr)

    big3 = {}
    for s in cfg.get("big3", []):
        try:
            big3[s["name"]] = fetch_fn(s["code"], f"{s['code']}.KS", start)
        except Exception as e:  # noqa: BLE001
            print(f"[경고] {s['name']} 조회 실패: {e}", file=sys.stderr)

    return build_report(cfg, pos, etf, bench, big3), etf.index[-1].date()


# ----------------------------------------------------------------- 텔레그램
def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다")
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for ch in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": ch, "disable_web_page_preview": True},
            timeout=20,
        )
        r.raise_for_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="전송하지 않고 출력만")
    ap.add_argument("--force", action="store_true", help="데이터 날짜가 오늘이 아니어도 전송")
    args = ap.parse_args()

    try:
        cfg, pos = load_config(), load_position()
        text, last_date = make_report(cfg, pos)
        today = datetime.now(KST).date()
        if args.dry_run:
            print(text)
            return 0
        if last_date != today and not args.force:
            print(f"최신 데이터가 {last_date} 입니다(오늘 {today}). 휴장이거나 아직 갱신 전이라 전송을 건너뜁니다.")
            return 0
        send_telegram(text)
        print("전송 완료")
        return 0
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        if not args.dry_run:
            try:
                send_telegram(f"⚠️ 일일 리포트 실패\n{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
