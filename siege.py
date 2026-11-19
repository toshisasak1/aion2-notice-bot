"""AION2 アーティファクト戦の結果を DaevaOS から読み、組み合わせごとの一覧を Discord に送る。

組み合わせは2週間ほどで変わるので、送る前に毎回、公式のお知らせ「サーバーマッチングのご案内」の
最新版を読み、そのアビスの表の順に全組を並べる。DaevaOS に記録が無い組は「記録なし」と出す。

DaevaOS の運営者の許可を得て、開始から2時間半後(21:00 の戦なら 23:30)にページを1回だけ読む。
どの組み合わせもまだ記録されていないときだけ、次の起動で読み直す(1回の戦につき最大3回)。

DaevaOS に記録の無い組は、まとめサイト(vega-gaming-pc.com の「AF戦の組み合わせ・結果まとめ」)の
表で補う。それでも記録なしの組が残ったら、翌日 12:00 に DaevaOS とまとめサイトをもう1回読み、
その組が埋まっていれば、送った投稿を書き換える。
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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notify import BOARDS, fetch_body, http_json, parse_time, webhook_url

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
FIRST_READ = timedelta(hours=2, minutes=30)  # 戦は最長30分。まとめサイトに載るのも待って読む
RECHECK_AT = timedelta(hours=15)  # 記録なしの組を確かめ直す時刻(21:00 の戦なら翌日 12:00)
LAST_READ = timedelta(hours=24)  # これより遅れた回は送らない
SIEGE_WINDOW = timedelta(minutes=60)  # 開始からこの間に終わった記録を、その回の結果とみなす
EMPTY_RETRIES = 3  # どの組み合わせも記録が無いとき、この回数まで読み直す
READ_CAP = 5  # 送信の失敗も含めて、1回の戦でページを読む上限
# DaevaOS に記録の無い組を補う、プレイヤーの集計をまとめた記事(WordPress の API で探す)
SUPPLEMENT_API = "https://vega-gaming-pc.com/wp-json/wp/v2/posts"
SUPPLEMENT_SEARCH = "AF戦の組み合わせ"
WEEKDAYS_JA = "月火水木金土日"
ENDED_STATE = 3  # DaevaOS のデータで、戦が終わった回の state
SQUARE = {"L": "🟦", "R": "🟥", None: "⬜"}  # 左のサーバー、右のサーバー、どちらも持っていない

NOTICE_BOARD = next(b for b in BOARDS if b["key"] == "notice")
MATCHING_TITLE = "サーバーマッチング"
MATCHING_PAGES = 3  # お知らせを20件ずつ、この回数まで遡って探す
# 公式のお知らせのサーバー名(日本語)と、DaevaOS のサーバー番号。サーバーが増えたらここに足す。
# 番号は DaevaOS のページのスクリプトにあるサーバー一覧(GLOBAL_ASIA)から取った
SERVER_NAMES = {
    "シエル": 1501, "ネザカン": 1502, "バイゼル": 1503, "カイジネル": 1504, "ユスティエル": 1505,
    "アリエル": 1506, "フレギオン": 1507, "ミスランテイダ": 1508, "ヒタニエ": 1509, "ナニア": 1510,
    "イズラフェル": 2501, "ジケル": 2502, "トリニエル": 2503, "ルミエル": 2504, "マルクタン": 2505,
    "アスフェル": 2506, "エレスギガル": 2507, "ベリトラ": 2508, "ネモン": 2509, "ハダル": 2510,
}
SERVER_IDS = {v: k for k, v in SERVER_NAMES.items()}


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


def parse_supplement(content, matching_date, event_date):
    """まとめ記事の表から、event_date の回の {(西部, 東部): (西部の数, 東部の数)} を読む。

    表は組み合わせの発表日(data-af-matching)ごとに分かれ、セルに回の日付と両サーバー名が付いている。
    """
    found = {}
    for t in re.finditer(r'<table[^>]*data-af-matching="([\d-]+)"[^>]*>(.*?)</table>', content, re.S):
        if t.group(1) != matching_date:
            continue
        for td in re.finditer(r"<td([^>]*)>(.*?)</td>", t.group(2), re.S):
            attrs = dict(re.findall(r'data-([\w-]+)="([^"]*)"', td.group(1)))
            if attrs.get("event-date") != event_date or not attrs.get("west") or not attrs.get("east"):
                continue
            w = re.search(r'af2-side-west">\s*(\d)\s*<', td.group(2))
            e = re.search(r'af2-side-east">\s*(\d)\s*<', td.group(2))
            if w and e:
                key = (html.unescape(attrs["west"]).strip(), html.unescape(attrs["east"]).strip())
                found[key] = (int(w.group(1)), int(e.group(1)))
    return found


def fetch_supplement(region, matching, start):
    """今の組み合わせのまとめ記事を探し、この回の結果を返す。載っていなければ空の dict。"""
    url = (f"{SUPPLEMENT_API}?search={urllib.parse.quote(SUPPLEMENT_SEARCH)}"
           "&per_page=5&_fields=link,content")
    posts = http_json(url, headers={"User-Agent": UA}) or []
    matching_date = matching["posted"].astimezone(region["tz"]).strftime("%Y-%m-%d")
    event_date = start.astimezone(region["tz"]).strftime("%Y-%m-%d")
    for post in posts:
        found = parse_supplement((post.get("content") or {}).get("rendered", ""), matching_date, event_date)
        if found:
            return found
    return {}


def fill_from_supplement(rows, supplement):
    """DaevaOS に今回の記録が無い組を、まとめの結果で埋める。埋めた組の数を返す。

    両方にある組は DaevaOS を使い、食い違っていればログに出す。まとめには持っていた数しか無いので、
    ■は数から作る。
    """
    filled = 0
    for row in rows:
        if (row["left"], row["right"]) in supplement:
            a, b = supplement[(row["left"], row["right"])]
        elif (row["right"], row["left"]) in supplement:
            b, a = supplement[(row["right"], row["left"])]
        else:
            continue
        if a + b > 3:
            print(f"  まとめの数がおかしいので使わない: {row['left']} {a}-{b} {row['right']}")
            continue
        rec = row["rec"]
        if rec and rec["current"]:
            if tuple(rec["score"]) != (a, b):
                print(f"  DaevaOS とまとめが食い違う(DaevaOS を使う): {row['left']} - {row['right']}"
                      f" DaevaOS {rec['score'][0]}-{rec['score'][1]}、まとめ {a}-{b}")
            continue
        row["rec"] = {"ended": None, "current": True, "score": (a, b),
                      "holders": ["L"] * a + [None] * (3 - a - b) + ["R"] * b}
        filled += 1
    return filled


def send_discord(message):
    """投稿して、あとで書き換えられるようにメッセージの ID を返す。"""
    res = http_json(webhook_url() + "?wait=true", message, retries=2)
    time.sleep(1.2)  # ウェブフックの送信間隔の制限に合わせる
    return (res or {}).get("id")


def edit_discord(message_id, message):
    # 書き換えで変えられるのは本文と埋め込みだけ(送信者名は送ったときのまま)
    http_json(f"{webhook_url()}/messages/{message_id}", {"embeds": message["embeds"]},
              retries=2, method="PATCH")
    time.sleep(1.2)


def missing_pairs(rows):
    return [[row["left"], row["right"]] for row in rows if not (row["rec"] and row["rec"]["current"])]


def from_ms(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc) if ms else None


def parse_rounds(page, key):
    """地域の戦の記録(過去の回も含む)を新しい順に返す。読めなければ None。

    画面の HTML は終了時刻をブラウザで埋めるようになった(2026-11-19 に気づいた)ので、
    ページに埋め込まれた Next.js のデータ(self.__next_f)を読む。holders の race 1・2 は、
    その回の elyos・asmodian の欄のサーバーを指す(同じ陣営どうしの組もこの欄で分かれる)。
    """
    chunks = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', page)
    data = "".join(json.loads(c) for c in chunks)
    m = re.search(r'\{"region":"%s","rounds":' % re.escape(key), data)
    if not m:
        return None
    try:
        group, _ = json.JSONDecoder().raw_decode(data, m.start())
    except ValueError:
        return None
    rounds = []
    for r in group.get("rounds") or []:
        try:
            sides = {1: r["elyos"], 2: r["asmodian"]}
            ended = from_ms(r["rewardBaseMs"]) if r["state"] == ENDED_STATE else None
            rounds.append({
                "servers": (r["elyos"], r["asmodian"]),
                "ended": ended,
                "last_report": from_ms(r.get("lastReportMs")),
                "winner": sides.get(r.get("winner")),
                # アーティファクトの順に、持っていたサーバーの番号(どちらも持っていなければ None)
                "holders": [sides.get(h["race"]) for h in sorted(r["holders"], key=lambda h: h["artifact"])],
            })
        except (KeyError, TypeError):
            continue
    if not any(r["ended"] for r in rounds):
        return None  # 終わった回が1つも読めないのは、データの形が変わったとき
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(rounds, key=lambda r: r["ended"] or oldest, reverse=True)


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


def is_current(rnd, start):
    return rnd["ended"] is not None and start <= rnd["ended"] <= start + SIEGE_WINDOW


def date_label(dt, region, with_time=False):
    local = dt.astimezone(region["tz"])
    label = f"{local.month}/{local.day}({WEEKDAYS_JA[local.weekday()]})"
    return label + local.strftime(" %H:%M") if with_time else label


def bar(holders):
    holders = holders or [None] * 3
    order = {"L": 0, None: 1, "R": 2}
    return "".join(SQUARE[h] for h in sorted(holders, key=lambda h: order[h]))


def align(matching, rounds, start):
    """公式の組ごとに、DaevaOS の今回か前回の記録を公式の表の左・右に合わせて並べる。

    DaevaOS は同じ陣営どうしの組で片方を反対の陣営として扱うので、陣営ではなく
    サーバー番号で左右を決める。公式の組に無い DaevaOS の今回の記録は、2つ目の戻り値で返す。
    """
    rows, known = [], set()
    for left, right in matching["pairs"]:
        ids = (SERVER_NAMES.get(left), SERVER_NAMES.get(right))
        for name in (left, right):
            if name not in SERVER_NAMES:
                print(f"  SERVER_NAMES に無いサーバー: {name}(この組は記録なしになる)")
        known.add(frozenset(ids))
        rec = None
        for r in rounds:  # 新しい順なので、最初に当たった回を使う
            if None in ids or set(r["servers"]) != set(ids) or not r["ended"] or r["ended"] > start + SIEGE_WINDOW:
                continue
            current = is_current(r, start)
            holders = ["L" if h == ids[0] else "R" if h == ids[1] else None for h in r["holders"]]
            a, b = holders.count("L"), holders.count("R")
            # DaevaOS では、前の回の保持数が次の戦の最中の読み取りで上書きされることがある(勝者の欄は残る)。
            # 今回の戦が始まった後に読まれた前の回と、保持数と勝者が食い違う回は使わない
            if not current and r["last_report"] and r["last_report"] > start:
                break
            if a != b and r["winner"] is not None and r["winner"] != ids[0 if a > b else 1]:
                print(f"  DaevaOS の保持数と勝者が食い違うので使わない: {left} {a}-{b} {right}")
                break
            rec = {
                "ended": r["ended"],
                "current": current,
                "score": (a, b),
                "holders": holders,
            }
            break
        rows.append({"left": left, "right": right, "rec": rec})
    stray = [tuple(SERVER_IDS.get(s, str(s)) for s in r["servers"]) for r in rounds
             if is_current(r, start) and frozenset(r["servers"]) not in known]
    return rows, stray


def build_message(region, start, matching, rows):
    side_l, side_r = matching["left"], matching["right"]
    lines, missing, wins, n_current = [], [], {"L": 0, "R": 0, None: 0}, 0
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
                prev = f"(前回 {date_label(rec['ended'], region)} {rec['score'][0]}-{rec['score'][1]})"
            missing.append(f"{row['left']}－{row['right']}{prev}")
    if missing:
        # 記録の無い組は1行にまとめ、記録のある組の後に置く
        lines.append(("\n" if lines else "") + "記録なし: " + "、".join(missing))

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
        recheck = None  # 送った投稿を書き換えるかを確かめ直すときの、送ったときの記録
        if not args.test:
            if rs.get("posted") == sid:
                recheck = rs.get("recheck")
                if not recheck or recheck.get("sid") != sid:
                    print(f"{name} 送信済み")
                    continue
                if now < start + RECHECK_AT and not recheck.get("redo"):
                    print(f"{name} 送信済み。記録なしの組は"
                          f" {date_label(start + RECHECK_AT, region, True)} 以降に確かめ直す")
                    continue
            elif now < start + FIRST_READ:
                print(f"{name} 戦の終了待ち({date_label(start + FIRST_READ, region, True)} 以降に読む)")
                continue
            if now > start + LAST_READ:
                print(f"{name} 開始から {LAST_READ.total_seconds() / 3600:.0f} 時間を過ぎたので送らない")
                if recheck and keep_state:
                    rs.pop("recheck", None)
                    save_state(state)
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

        # DaevaOS に記録の無い組はまとめサイトで補う
        try:
            supplement = fetch_supplement(region, matching, start)
        except Exception as e:  # noqa: BLE001 まとめが読めなくても DaevaOS だけで送れる
            print(f"{name} まとめサイトを読めません: {e}")
            supplement = {}
        print(f"{name} まとめサイトにこの回の {len(supplement)} 組")

        redo = bool(recheck and recheck.get("redo"))
        if redo:
            # 読み取りの不具合で投稿が誤っていたときに、手で siege_state.json に redo を足して読み直させる
            print(f"{name} 送った投稿を DaevaOS から読み直して書き換える")
        elif recheck:
            # DaevaOS の記録は、後からゲームに入った Companion の利用者の分が足されることがある
            print(f"{name} 記録なしだった {len(recheck['missing'])} 組を DaevaOS とまとめで確かめ直す")

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

        rounds = parse_rounds(page, key)
        if not rounds:
            # 「記録なし」として送らずに止める(Actions の実行が失敗になり、次の起動で読み直す)
            print(f"{name} ページから {key} の記録を読めません。ページの作りが変わった可能性があります")
            failed = True
            continue
        rows, stray = align(matching, rounds, start)
        for a, b in stray:
            print(f"{name} 公式の組み合わせに無い組の記録(投稿しない): {a} - {b}")
        filled = fill_from_supplement(rows, supplement)
        if filled:
            print(f"{name} DaevaOS に記録の無い {filled} 組をまとめサイトで補った")
        message, n_current = build_message(region, start, matching, rows)
        missing = missing_pairs(rows)
        if n_current == 0 and not recheck and not args.test and reads + 1 < EMPTY_RETRIES:
            print(f"{name} どの組み合わせもまだ記録なし。次の起動で読み直す({reads + 1}/{EMPTY_RETRIES} 回目)")
            continue
        if recheck and not redo:
            filled_now = [p for p in recheck["missing"] if p not in missing]
            if n_current < recheck.get("n", 0):
                filled_now = []  # 送ったときより記録が減るなら、何かがおかしいので書き換えない
            if not filled_now:
                print(f"{name} 記録なしだった {len(recheck['missing'])} 組は、まだ埋まっていない。書き換えない")
                if keep_state:
                    rs.pop("recheck", None)
                    save_state(state)
                continue
            print(f"{name} 記録なしだった組のうち {len(filled_now)} 組が埋まった。投稿を書き換える")

        if args.dry_run:
            print(f"{name} (dry-run{'、書き換え' if recheck else ''}) 記録 {n_current}/{len(rows)} 組")
            print(json.dumps(message, ensure_ascii=False, indent=2))
            continue

        if recheck:
            try:
                edit_discord(recheck["message_id"], message)
            except Exception as e:  # noqa: BLE001 次の起動で書き換え直す
                print(f"{name} Discord の投稿の書き換えに失敗: {e}")
                failed = True
                continue
            print(f"{name} 投稿を書き換えました(記録 {recheck.get('n', '?')} → {n_current}/{len(rows)} 組)")
            if keep_state:
                rs.pop("recheck", None)
                rs["reads"] = {}
                if missing and redo and now < start + RECHECK_AT:
                    # 読み直して直した投稿も、記録なしの組が残れば予定どおり翌日に確かめ直す
                    rs["recheck"] = {"sid": sid, "message_id": recheck["message_id"], "missing": missing,
                                     "n": n_current}
                save_state(state)
            continue

        try:
            message_id = send_discord(message)
        except Exception as e:  # noqa: BLE001 次の起動で送り直す
            print(f"{name} Discord への送信に失敗: {e}")
            failed = True
            continue
        print(f"{name} 通知しました(記録 {n_current}/{len(rows)} 組)")
        if keep_state:
            rs["posted"] = sid
            rs["reads"] = {}
            rs.pop("recheck", None)
            if missing and message_id:
                # 送った投稿を翌日に書き換えられるよう、ID と記録なしの組(結果ではなく組の名前)だけ残す
                rs["recheck"] = {"sid": sid, "message_id": message_id, "missing": missing, "n": n_current}
                print(f"{name} 記録なしの {len(missing)} 組は {date_label(start + RECHECK_AT, region, True)}"
                      " 以降に確かめ直す")
            save_state(state)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
