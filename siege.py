"""AION2 アーティファクト戦の結果を DaevaOS から読み、組み合わせごとの一覧を Discord に送る。

組み合わせは2週間ほどで変わるので、送る前に毎回、公式のお知らせ「サーバーマッチングのご案内」の
最新版を読み、そのアビスの表の順に全組を並べる。DaevaOS に記録が無い組は「記録なし」と出す。

DaevaOS の運営者の許可を得て、戦が終わったあとにページを1回だけ読む。どの組み合わせも
まだ記録されていないときだけ、次の起動で読み直す(1回の戦につき最大3回)。
GitHub Actions で notify.py と同じ起動(cron-job.org から30分ごと)から呼ばれ、戦の後でなければ通信せずに終わる。

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

from notify import BOARDS, fetch_body, http_json, parse_time, post_discord, webhook_url

PAGE = "https://daevaos.com/statistics/artifact-siege"
UA = "Mozilla/5.0 (compatible; aion2-siege-bot/1.0; +https://github.com/toshisasak1/aion2-notice-bot)"
STATE_PATH = Path(__file__).with_name("siege_state.json")

# 地域ごとの戦の日程(サーバー時刻)。DaevaOS のページの data-region の値をキーにする
REGIONS = {
    "GLOBAL_ASIA": {
        "label": "アジア",
        "tz": timezone(timedelta(hours=9)),
        "weekdays": (3, 5),  # 木・土(2026-11-04 のアップデートで月曜はなくなった)
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
SQUARE = {"L": "🟦", "R": "🟥", None: "⬜"}  # 左のサーバー、右のサーバー、どちらも持っていない

NOTICE_BOARD = next(b for b in BOARDS if b["key"] == "notice")
MATCHING_TITLE = "サーバーマッチング"
MATCHING_PAGES = 3  # お知らせを20件ずつ、この回数まで遡って探す
# 公式のお知らせのサーバー名(日本語)と、DaevaOS の表示名。サーバーが増えたらここに足す
SERVER_NAMES = {
    "シエル": "Siel", "アリエル": "Ariel", "イズラフェル": "Israphel", "ルミエル": "Lumiel",
    "ネザカン": "Nezekan", "ネモン": "Nemon", "ジケル": "Zikel", "マルクタン": "Marchutan",
    "バイゼル": "Vaizel", "エレスギガル": "Ereshkigal", "カイジネル": "Kaisinel", "ヒタニエ": "Hithanya",
    "ユスティエル": "Yustiel", "トリニエル": "Triniel", "フレギオン": "Fregion", "ナニア": "Nania",
    "ミスランテイダ": "Meslamtaeda", "アスフェル": "Azphel", "ベリトラ": "Beritra", "ハダル": "Hadala",
}


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


def parse_matching(body):
    """「サーバーマッチングのご案内」の本文から、アビスの表の列名と組み合わせを読む。"""
    m = re.search(r"■\s*アビス(.*?)(?=\n■|\Z)", body, re.S)
    if not m:
        raise RuntimeError("本文に「■ アビス」の表がありません")
    cells = [c.strip() for c in m.group(1).splitlines() if c.strip() and c.strip() != "|"]
    first = next((i for i, c in enumerate(cells) if c.isdigit()), None)
    if first is None or first < 2:
        raise RuntimeError("アビスの表の行が読めません")
    left, right = cells[first - 2], cells[first - 1]  # 見出しの「西部」「東部」
    pairs, rows = [], cells[first:]
    for i in range(0, len(rows) - 2, 3):
        num, a, b = rows[i:i + 3]
        if not num.isdigit():
            break
        pairs.append((a, b))
    names = [n for p in pairs for n in p]
    if not pairs or len(set(names)) != len(names):
        raise RuntimeError("アビスの表の組み合わせが読めません")
    return left, right, pairs


def fetch_matching(before):
    """before より前に出た、最新の「サーバーマッチングのご案内」を公式のお知らせから読む。"""
    found, prev = [], 0
    for _ in range(MATCHING_PAGES):
        items = http_json(
            f"{NOTICE_BOARD['api']}/article/search/moreArticle"
            f"?isVote=true&moreSize=20&moreDirection=BEFORE&previousArticleId={prev}"
        )["contentList"]
        found += [it for it in items if MATCHING_TITLE in it["title"]
                  and parse_time(it["timestamps"]["postedAt"]) <= before]
        if found or not items:
            break
        prev = items[-1]["id"]
    if not found:
        raise RuntimeError(f"お知らせに「{MATCHING_TITLE}」が見つかりません")
    item = max(found, key=lambda it: it["timestamps"]["postedAt"])
    left, right, pairs = parse_matching(fetch_body(NOTICE_BOARD, item["id"]))
    return {
        "title": item["title"],
        "posted": parse_time(item["timestamps"]["postedAt"]),
        "left": left,
        "right": right,
        "pairs": pairs,
    }


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


def bar(holders):
    holders = holders or [None] * 3
    order = {"L": 0, None: 1, "R": 2}
    return "".join(SQUARE[h] for h in sorted(holders, key=lambda h: order[h]))


def align(matching, pairs, start):
    """公式の組ごとに、DaevaOS の記録を公式の表の左・右に合わせて並べる。

    DaevaOS は同じ陣営どうしの組で片方を反対の陣営として表示するので、陣営ではなく
    サーバー名で左右を決める。公式の組に無い DaevaOS の今回の記録は、2つ目の戻り値で返す。
    """
    rows, used = [], set()
    for left, right in matching["pairs"]:
        en = {SERVER_NAMES.get(left), SERVER_NAMES.get(right)}
        for name in (left, right):
            if name not in SERVER_NAMES:
                print(f"  SERVER_NAMES に無いサーバー: {name}(この組は記録なしになる)")
        rec = None
        for i, p in enumerate(pairs):
            if None in en or {p.get("elyos"), p.get("asmodian")} != en:
                continue
            used.add(i)
            side_l = "elyos" if p["elyos"] == SERVER_NAMES[left] else "asmodian"
            side_r = "asmodian" if side_l == "elyos" else "elyos"
            rec = {
                "ended": p["ended"],
                "current": is_current(p, start),
                "score": (p["score"][side_l], p["score"][side_r]) if len(p["score"]) == 2 else None,
                "holders": ["L" if h == side_l else "R" if h == side_r else None for h in p["holders"]],
            }
            break
        rows.append({"left": left, "right": right, "rec": rec})
    stray = [(p.get("elyos"), p.get("asmodian")) for i, p in enumerate(pairs)
             if i not in used and is_current(p, start)]
    return rows, stray


def build_message(region, start, matching, rows):
    side_l, side_r = matching["left"], matching["right"]
    lines, wins, n_current = [], {"L": 0, "R": 0, None: 0}, 0
    for row in rows:
        rec = row["rec"]
        if rec and rec["current"]:
            n_current += 1
            a, b = rec["score"]
            w = "L" if a > b else "R" if b > a else None
            wins[w] += 1
            left = f"**{row['left']}**" if w == "L" else row["left"]
            right = f"**{row['right']}**" if w == "R" else row["right"]
            lines.append(f"{bar(rec['holders'])}　{left}　{a}-{b}　{right}")
        else:
            prev = ""
            # 前回の結果は、今の組み合わせになってからの記録だけ載せる
            if rec and rec["score"] and rec["ended"] and rec["ended"] >= matching["posted"]:
                prev = f"、前回 {date_label(rec['ended'], region)} {rec['score'][0]}-{rec['score'][1]}"
            lines.append(f"➖➖➖　{row['left']} － {row['right']}(記録なし{prev})")

    head = (f"左が{side_l}、右が{side_r}のサーバー。"
            "数字と■は、戦の終わりにアーティファクトを持っていた数(🟦左、🟥右、⬜なし)。")
    if not n_current:
        head += "\n今回はどの組み合わせも記録されていません。"
    tally = f"{side_l} {wins['L']}勝 ・ {side_r} {wins['R']}勝"
    if wins[None]:
        tally += f" ・ 引き分け {wins[None]}"
    tally += f"(記録 {n_current}/{len(rows)}組)"
    embed = {
        "title": f"アーティファクト戦 {region['label']} {date_label(start, region, with_time=True)} の結果",
        "url": PAGE,
        "description": (head + "\n\n" + "\n".join(lines))[:4000],
        "color": 0x8B5CF6,
        "fields": [{"name": "今回の勝敗", "value": tally}],
    }
    return {"username": "アーティファクト戦", "embeds": [embed]}, n_current


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

        # 組み合わせは毎回変わりうるので、DaevaOS を読む前に公式のお知らせで確かめる
        try:
            matching = fetch_matching(start)
        except Exception as e:  # noqa: BLE001 次の起動で読み直す
            print(f"{name} 公式の組み合わせを読めません: {e}")
            failed = True
            continue
        print(f"{name} 組み合わせ: {matching['title']}({len(matching['pairs'])}組)")

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
        rows, stray = align(matching, pairs, start)
        for a, b in stray:
            print(f"{name} 公式の組み合わせに無い組の記録(投稿しない): {a} - {b}")
        message, n_current = build_message(region, start, matching, rows)
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
