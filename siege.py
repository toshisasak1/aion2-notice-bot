"""AION2 アーティファクト戦の結果を DaevaOS から読み、組み合わせごとの一覧を Discord に送る。

DaevaOS の運営者の許可を得て、戦が終わったあとにページを1回だけ読む。どの組み合わせも
まだ記録されていないときだけ、次の起動(15分後)で読み直す(1回の戦につき最大3回)。
GitHub Actions で notify.py と同じ15分ごとの起動から呼ばれ、戦の後でなければ通信せずに終わる。

環境変数:
  DISCORD_WEBHOOK_URL  送り先のウェブフック URL(--dry-run では不要)
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notify import post_discord, webhook_url

PAGE = "https://daevaos.com/statistics/artifact-siege"
UA = "Mozilla/5.0 (compatible; aion2-siege-bot/1.0; +https://github.com/toshisasak1/aion2-notice-bot)"
STATE_PATH = Path(__file__).with_name("siege_state.json")

# 地域ごとの戦の日程(サーバー時刻)。DaevaOS のページの data-region の値をキーにする
REGIONS = {
    "GLOBAL_ASIA": {
        "label": "アジア",
        "tz": timezone(timedelta(hours=9)),
        "weekdays": (0, 3, 5),  # 月・木・土
        "start": (21, 0),
    },
}
FIRST_READ = timedelta(minutes=40)  # 戦は最長30分。終わってから読む
LAST_READ = timedelta(hours=24)  # これより遅れた回は送らない
SIEGE_WINDOW = timedelta(minutes=60)  # 開始からこの間に終わった記録を、その回の結果とみなす
EMPTY_RETRIES = 3  # どの組み合わせも記録が無いとき、この回数まで読み直す
READ_CAP = 5  # 送信の失敗も含めて、1回の戦でページを読む上限
WEEKDAYS_JA = "月火水木金土日"
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
FACTION = {"elyos": "天族", "asmodian": "魔族"}
SQUARE = {"elyos": "🟦", "asmodian": "🟥", None: "⬜"}


def fetch_page(retries=2):
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(PAGE, headers={"User-Agent": UA, "Accept": "text/html"})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"HTTP {e.code} {PAGE}")
            if e.code != 429 and e.code < 500:
                raise last from None
        except (urllib.error.URLError, TimeoutError) as e:
            last = RuntimeError(f"通信エラー {PAGE}: {e}")
        time.sleep(5 * (attempt + 1))
    raise last


def parse_ended(text):
    """'Nov 5, 2026, 12:14'(UTC で表示される)を datetime にする。"""
    m = re.fullmatch(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2})", text.strip())
    if not m or m.group(1) not in MONTHS:
        return None
    mon, day, year, hour, minute = MONTHS[m.group(1)], *map(int, m.group(2, 3, 4, 5))
    return datetime(year, mon, day, hour, minute, tzinfo=timezone.utc)


def parse_region(page, key):
    """地域の組み合わせを、ページに並んでいる順に返す。"""
    m = re.search(rf'<div[^>]*data-testid="siege-region-group"[^>]*data-region="{key}"', page)
    if not m:
        return []
    end = page.find('data-testid="siege-region-group"', m.end())
    seg = page[m.end(): end if end >= 0 else len(page)]
    pairs = []
    for sec in re.findall(r'<section[^>]*data-testid="siege-pair".*?</section>', seg, re.S):
        sides = re.findall(r'races/(elyos|asmodian)\.webp"[^>]*>\s*<span[^>]*title="([^"]*)"', sec)
        score = re.search(r"<span[^>]*>(\d+)</span><span[^>]*>vs</span><span[^>]*>(\d+)</span>", sec)
        if len(sides) != 2 or {f for f, _ in sides} != {"elyos", "asmodian"}:
            continue
        holders = []
        for _, who in re.findall(r">Artifact (\d)</p><p[^>]*>([^<]*)</p>", sec):
            who = who.strip().lower()
            holders.append(who if who in FACTION else None)
        status = re.search(r'data-status="([A-Z]+)"', sec)
        ended = re.search(r">Ended ([^<]+)<", sec)
        pair = {
            "status": status.group(1) if status else "",
            "ended": parse_ended(ended.group(1)) if ended else None,
            "holders": holders,
            "score": {},
        }
        for i, (faction, name) in enumerate(sides):
            pair[faction] = html.unescape(name)
            if score:
                pair["score"][faction] = int(score.group(i + 1))
        pairs.append(pair)
    return pairs


def latest_start(region, now):
    """now 以前で最も新しい戦の開始時刻(UTC)。"""
    local = now.astimezone(region["tz"])
    hour, minute = region["start"]
    for back in range(8):
        day = (local - timedelta(days=back)).date()
        if day.weekday() not in region["weekdays"]:
            continue
        start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=region["tz"])
        if start <= local:
            return start.astimezone(timezone.utc)
    raise RuntimeError("戦の日程が見つかりません")


def is_current(pair, start):
    return (
        pair["status"] == "ENDED"
        and pair["ended"] is not None
        and start <= pair["ended"] <= start + SIEGE_WINDOW
        and len(pair["score"]) == 2
    )


def date_label(dt, region, with_time=False):
    local = dt.astimezone(region["tz"])
    label = f"{local.month}/{local.day}({WEEKDAYS_JA[local.weekday()]})"
    return label + local.strftime(" %H:%M") if with_time else label


def score_text(pair):
    return f"{pair['score']['elyos']}-{pair['score']['asmodian']}"


def bar(pair):
    holders = pair["holders"] or [None] * 3
    order = {"elyos": 0, None: 1, "asmodian": 2}
    return "".join(SQUARE[h] for h in sorted(holders, key=lambda h: order[h]))


def winner(pair):
    e, a = pair["score"]["elyos"], pair["score"]["asmodian"]
    return "elyos" if e > a else "asmodian" if a > e else None


def build_message(region, start, pairs):
    lines, wins = [], {"elyos": 0, "asmodian": 0, None: 0}
    current = [p for p in pairs if is_current(p, start)]
    for p in pairs:
        if p in current:
            w = winner(p)
            wins[w] += 1
            left = f"**{p['elyos']}**" if w == "elyos" else p["elyos"]
            right = f"**{p['asmodian']}**" if w == "asmodian" else p["asmodian"]
            lines.append(f"{bar(p)}　{left}　{score_text(p)}　{right}")
        else:
            prev = ""
            if p["ended"] and len(p["score"]) == 2:
                prev = f"、前回 {date_label(p['ended'], region)} {score_text(p)}"
            lines.append(f"➖➖➖　{p['elyos']} － {p['asmodian']}(記録なし{prev})")

    head = "左が天族、右が魔族。数字と■は、戦の終わりにアーティファクトを持っていた数(🟦天族、🟥魔族、⬜なし)。"
    if not current:
        head += "\n今回はどの組み合わせも記録されていません。前回までの結果を載せています。"
    tally = f"天族 {wins['elyos']}勝 ・ 魔族 {wins['asmodian']}勝"
    if wins[None]:
        tally += f" ・ 引き分け {wins[None]}"
    tally += f"(記録 {len(current)}/{len(pairs)}組)"
    embed = {
        "title": f"アーティファクト戦 {region['label']} {date_label(start, region, with_time=True)} の結果",
        "url": PAGE,
        "description": (head + "\n\n" + "\n".join(lines))[:4000],
        "color": 0x8B5CF6,
        "fields": [{"name": "今回の勝敗", "value": tally}],
        "footer": {"text": "出典: DaevaOS(Companion 利用者がゲーム内で読んだ記録。運営者の許可を得て掲載)"},
        "timestamp": start.isoformat().replace("+00:00", "Z"),
    }
    return {"username": "アーティファクト戦", "embeds": [embed]}, len(current)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_now(text):
    if not text:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="Discord に送らず、状態も保存しない")
    ap.add_argument("--test", action="store_true", help="日程と送信済みの記録を無視して、直近の回を送る(状態は変えない)")
    ap.add_argument("--file", help="ページを読む代わりに、保存した HTML を使う")
    ap.add_argument("--now", help="現在時刻の代わりに使う時刻(例 2026-11-05T13:00Z)")
    args = ap.parse_args()

    now = parse_now(args.now)
    keep_state = not (args.dry_run or args.test)
    state = load_state()
    failed = False

    for key, region in REGIONS.items():
        start = latest_start(region, now)
        sid = start.strftime("%Y-%m-%dT%H:%MZ")
        rs = state.setdefault(key, {})
        name = f"[{region['label']} {date_label(start, region, with_time=True)}]"
        reads = rs.get("reads", {}).get(sid, 0)
        if not args.test:
            if rs.get("posted") == sid:
                print(f"{name} 送信済み")
                continue
            if now < start + FIRST_READ:
                print(f"{name} 戦の終了待ち({date_label(start + FIRST_READ, region, True)} 以降に読む)")
                continue
            if now > start + LAST_READ:
                print(f"{name} 開始から {LAST_READ.total_seconds() / 3600:.0f} 時間を過ぎたので送らない")
                continue
            if reads >= READ_CAP:
                print(f"{name} ページを {READ_CAP} 回読んだので、この回はあきらめる")
                continue
            if not args.dry_run:
                try:
                    webhook_url()  # 送れないならページを読む前に止める
                except RuntimeError as e:
                    print(f"{name} 送れません: {e}")
                    failed = True
                    continue

        try:
            page = Path(args.file).read_text(encoding="utf-8") if args.file else fetch_page()
        except Exception as e:  # noqa: BLE001 次の起動で読み直す
            print(f"{name} ページの取得に失敗: {e}")
            failed = True
            page = None
        if keep_state and not args.file:
            rs["reads"] = {sid: reads + 1}
            save_state(state)
        if page is None:
            continue

        pairs = parse_region(page, key)
        if not pairs:
            print(f"{name} ページに {key} の組み合わせが見つかりません。ページの作りが変わった可能性があります")
            failed = True
            continue
        message, n_current = build_message(region, start, pairs)
        if n_current == 0 and not args.test and reads + 1 < EMPTY_RETRIES:
            print(f"{name} どの組み合わせもまだ記録なし。次の起動で読み直す({reads + 1}/{EMPTY_RETRIES} 回目)")
            continue

        if args.dry_run:
            print(f"{name} (dry-run) 記録 {n_current}/{len(pairs)} 組")
            print(json.dumps(message, ensure_ascii=False, indent=2))
            continue
        try:
            post_discord(message)
        except Exception as e:  # noqa: BLE001 次の起動で送り直す
            print(f"{name} Discord への送信に失敗: {e}")
            failed = True
            continue
        print(f"{name} 通知しました(記録 {n_current}/{len(pairs)} 組)")
        if keep_state:
            rs["posted"] = sid
            rs["reads"] = {}
            save_state(state)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
