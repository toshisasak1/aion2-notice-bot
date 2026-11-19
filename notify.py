"""AION2 公式サイトの新着記事を Gemini で箇条書きに要約し、Discord に通知する。

GitHub Actions から定期実行する前提。標準ライブラリだけで動く。

本文にクーポンコードがある記事は、コードと使用期限を読み取ってクーポン用のチャンネルに送り、
期限の前日 21:00(日本時間)にもう一度知らせる。

環境変数:
  DISCORD_WEBHOOK_URL  Discord のウェブフック URL(必須。--dry-run では不要)
  COUPON_WEBHOOK_URL   クーポン用のチャンネルのウェブフック URL(無いと DISCORD_WEBHOOK_URL に送る)
  GEMINI_API_KEY       Gemini API キー(無いと要約せず本文冒頭を載せる)
  GEMINI_MODEL         使うモデル(省略時 gemini-3.5-flash。失敗したら gemini-flash-lite-latest)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

API = "https://api-global-community.plaync.com"
SITE = "https://aion2.plaync.com/ja-jp"
UA = "Mozilla/5.0 (compatible; aion2-notice-bot/1.0)"

BOARDS = [
    {
        "key": "notice",
        "label": "お知らせ",
        "api": f"{API}/aion2_global/board/notice_ja",
        "view": f"{SITE}/board/notice/view?articleId={{cid}}",
        "color": 0x3B82F6,
    },
    {
        "key": "update",
        "label": "アップデート",
        "api": f"{API}/aion2_global/board/update_ja",
        "view": f"{SITE}/board/update/view?articleId={{cid}}",
        "color": 0x22C55E,
    },
    {
        "key": "event",
        "label": "イベント",
        "api": f"{API}/mks_aion2_global/board/event_notice-jp",
        "view": f"{SITE}/eventon/board/event_notice/view?articleId={{cid}}",
        "color": 0xF59E0B,
    },
]

STATE_PATH = Path(__file__).with_name("state.json")
LIST_SIZE = 20  # 1回に見る件数
MAX_PER_RUN = 5  # 1掲示板あたり1回に送る上限。状態ファイルが消えたときの大量送信を防ぐ
KEEP_IDS = 300  # 掲示板ごとに覚えておく既読IDの数
BODY_LIMIT = 15000  # 要約に渡す本文の最大文字数
MAX_LINES = 10  # 要約の最大行数。行数は記事の中身に合わせて Gemini が決める
# gemini-flash-latest(3.8)は 2026-10 時点の無料枠で応答が返らなかったため使わない
DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-flash-lite-latest"]

COUPON_TITLE = "クーポン"  # タイトルにこれを含む記事はクーポン用のチャンネルだけに送る。他は両方に送る
REMIND_HOUR = 21  # 使用期限の前日のこの時刻(日本時間)にリマインドする
COUPON_COLOR = 0xEC4899
REMIND_COLOR = 0xEF4444
JST = timezone(timedelta(hours=9))
WEEKDAYS_JA = "月火水木金土日"
CODE_RE = re.compile(r"(?<![A-Za-z0-9-])[A-Z0-9][A-Z0-9-]{3,39}(?![A-Za-z0-9-])")
DATE_RE = r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日(?:\s*[(（][^)）]*[)）])?\s*(?:(\d{1,2})[:：](\d{2}))?"


def http_json(url, data=None, headers=None, timeout=30, retries=2, method=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = RuntimeError(f"HTTP {e.code} {url.split('?')[0]}: {detail}")
            if e.code == 429 or e.code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            raise last from None
        except (urllib.error.URLError, TimeoutError) as e:
            last = RuntimeError(f"通信エラー {url.split('?')[0]}: {e}")
            time.sleep(3 * (attempt + 1))
    raise last


class _TextExtractor(HTMLParser):
    BLOCK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html):
    p = _TextExtractor()
    p.feed(html or "")
    text = "".join(p.parts).replace("​", "").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_list(board):
    url = (
        f"{board['api']}/article/search/moreArticle"
        f"?isVote=true&moreSize={LIST_SIZE}&moreDirection=BEFORE&previousArticleId=0"
    )
    return http_json(url)["contentList"]


def fetch_body(board, article_id):
    data = http_json(f"{board['api']}/article/{article_id}")
    html = data["article"]["content"]["content"]
    return html_to_text(html)


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def summarize(board, title, body):
    """要約の行のリストを返す。失敗したら (None, 理由) を返す。"""
    key = env("GEMINI_API_KEY")
    if not key:
        return None, "GEMINI_API_KEY が未設定"
    prompt = (
        f"以下はオンラインゲーム『AION2』の公式サイトに載った「{board['label']}」の記事です。"
        "プレイヤーが知るべき要点を日本語で、1行に1項目の箇条書きに要約してください。\n"
        f"- 行数は記事の中身に合わせて1〜{MAX_LINES}行で決める。"
        "要点が少ない記事は短く、アップデートノートのように項目が多い記事は"
        f"重要なものから順に最大{MAX_LINES}行。行数を埋めるための水増しはしない。\n"
        "- 1行は60文字以内。\n"
        "- 日時・期間・対象・報酬・変更点など具体的な情報を優先する。\n"
        "- 行頭に記号や番号を付けない。前置きや締めの文は書かない。\n\n"
        f"タイトル: {title}\n\n本文:\n{body[:BODY_LIMIT]}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        # 思考を浅くすると応答が数秒で返る(既定の深さだと数十秒〜タイムアウト)
        "generationConfig": {"temperature": 0.2, "thinkingConfig": {"thinkingLevel": "low"}},
    }
    models = [env("GEMINI_MODEL") or DEFAULT_MODELS[0]]
    models += [m for m in DEFAULT_MODELS if m not in models]
    errors = []
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            res = http_json(url, payload, {"x-goog-api-key": key}, timeout=60, retries=0)
            parts = res["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except Exception as e:  # noqa: BLE001 次のモデルを試す
            errors.append(f"{model}: {e}")
            continue
        lines = [re.sub(r"^\s*(?:[-*・•●]|\d+[.)、])\s*", "", l).strip() for l in text.splitlines()]
        lines = [l for l in lines if l]
        if lines:
            return lines[:MAX_LINES], None
        errors.append(f"{model}: 空の応答")
    return None, " / ".join(errors)


def fallback_lines(item, body):
    text = (body or item.get("summary") or "").replace("​", "")
    text = re.sub(r"ふたつの空、ひとつの栄光\s*", "", text)
    text = re.sub(r"ディーヴァの皆さま、AION2からのお知らせです。\s*", "", text)
    sentences = [s.strip() for s in re.split(r"(?<=[。!！?？])\s*|\n", text) if s.strip()]
    return [s[:80] for s in sentences[:3]] or ["(本文を取得できませんでした)"]


def build_message(board, item, lines, ai_ok):
    cid = item["snow"]["contentId"]
    footer = board["label"] if ai_ok else f"{board['label']} ・ AI要約に失敗したため本文冒頭を表示"
    embed = {
        "title": item["title"][:256],
        "url": board["view"].format(cid=cid),
        "description": "\n".join(f"・{l}" for l in lines)[:4000],
        "color": board["color"],
        "footer": {"text": footer},
        "timestamp": item["timestamps"]["postedAt"],
    }
    if item.get("thumbnailUrl"):
        embed["thumbnail"] = {"url": item["thumbnailUrl"]}
    return {"username": "AION2 公式新着", "embeds": [embed]}


def env(name):
    # Windows から登録した値に付きやすい BOM と前後の空白・改行を落とす
    return os.environ.get(name, "").replace("﻿", "").strip()


def webhook_url():
    url = env("DISCORD_WEBHOOK_URL")
    if not re.match(r"^https://(?:discord|discordapp)\.com/api/webhooks/", url):
        raise RuntimeError("DISCORD_WEBHOOK_URL が未設定か、Discord のウェブフック URL ではありません")
    return url


def coupon_webhook_url():
    url = env("COUPON_WEBHOOK_URL")
    if re.match(r"^https://(?:discord|discordapp)\.com/api/webhooks/", url):
        return url
    return webhook_url()  # クーポン用のチャンネルが無ければ、新着と同じチャンネルに送る


def post_discord(message, url=None):
    http_json(url or webhook_url(), message, retries=2)
    time.sleep(1.2)  # ウェブフックの送信間隔の制限に合わせる


def section(body, heading):
    """本文の「■ 見出し」の行(heading に合うもの)から、次の「■」の前までを返す。無ければ None。"""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("■") and re.search(heading, line):
            rest = []
            for following in lines[i + 1:]:
                if following.lstrip().startswith("■"):
                    break
                rest.append(following)
            return "\n".join([line] + rest)
    return None


def parse_coupon(body):
    """本文からクーポンコード・使用期限・使い方を読む。コードが無ければ None。"""
    sec = section(body, "クーポンコード")
    codes = CODE_RE.findall(sec) if sec else []
    if not codes:
        codes = re.findall(r"クーポンコード\s*[:：]\s*([A-Z0-9][A-Z0-9-]{3,39})", body)
    codes = list(dict.fromkeys(c for c in codes if not c.isdigit()))
    if not codes:
        return None
    m = None
    for heading in (r"クーポン.*(期間|期限)", r"(使用|利用|入力|有効)(期間|期限)"):
        period = section(body, heading)
        found = list(re.finditer(DATE_RE, period)) if period else []
        if found:
            m = found[-1]  # 「開始 ～ 終了」なら後ろの日付が終わり
            break
    if not m:
        m = re.search(r"(?:終了日|期限)\s*[:：]?\s*" + DATE_RE, body)
    ends = None
    if m:
        y, mo, d, hh, mm = m.groups()
        ends = datetime(int(y), int(mo), int(d), int(hh or 23), int(mm or 59), tzinfo=JST)
    usage = section(body, "利用方法|使用方法|入力方法")
    if usage:
        steps = [l.strip().lstrip("・").strip() for l in usage.splitlines()[1:] if l.strip()]
        usage = steps[0][:300] if steps else None
    return {"codes": codes, "ends": ends, "usage": usage}


def jst_label(dt):
    t = dt.astimezone(JST)
    return f"{t.year}/{t.month}/{t.day}({WEEKDAYS_JA[t.weekday()]}) {t:%H:%M}"


def remind_at(ends):
    day = ends.astimezone(JST).date() - timedelta(days=1)
    return datetime(day.year, day.month, day.day, REMIND_HOUR, 0, tzinfo=JST)


def build_coupon_message(board, item, coupon, lines, ai_ok):
    """コードは本文(content)に1行ずつ置く。スマホで長押しすれば、そのままコピーできる。"""
    message = build_message(board, item, lines, ai_ok)
    embed = message["embeds"][0]
    embed["color"] = COUPON_COLOR
    fields = []
    if coupon["ends"]:
        fields.append({"name": "使用期限", "value": jst_label(coupon["ends"])})
    if coupon["usage"]:
        fields.append({"name": "使い方", "value": coupon["usage"][:1024]})
    if fields:
        embed["fields"] = fields
    return {"username": "AION2 クーポン", "content": "\n".join(coupon["codes"]), "embeds": [embed]}


def build_reminder(c):
    ends = datetime.fromisoformat(c["ends"])
    return {
        "username": "AION2 クーポン",
        "content": "\n".join(c["codes"]),
        "embeds": [{
            "title": f"明日で期限切れ: {c['title']}"[:256],
            "url": c["url"],
            "description": f"{jst_label(ends)} で使えなくなります。",
            "color": REMIND_COLOR,
        }],
    }


def register_coupon(state, board, item, coupon, now):
    """期限のあるクーポンを、リマインド用に覚える(コードも期限も公開されている情報)。"""
    if not coupon["ends"] or coupon["ends"] <= now:
        return
    state.setdefault("coupons", {})[item["id"]] = {
        "title": item["title"],
        "url": board["view"].format(cid=item["snow"]["contentId"]),
        "codes": coupon["codes"],
        "ends": coupon["ends"].isoformat(),
        "reminded": remind_at(coupon["ends"]) <= now,  # 前日21時を過ぎてから出たものは知らせ直さない
    }


def mark_checked(state, item_id):
    checked = state.setdefault("coupon_checked", [])
    if item_id not in checked:
        checked.append(item_id)
        del checked[:-KEEP_IDS]


def send_coupon_posts(board, item, body, coupon, dry_run):
    """記事を送り先ごとのメッセージにして送る。全部送れたら True。"""
    title = item["title"]
    lines, err = summarize(board, title, body)
    if lines is None:
        print(f"[{board['key']}] 要約に失敗: {err}")
        lines = fallback_lines(item, body)
    sends = []
    if coupon:
        sends.append(("クーポン", coupon_webhook_url, build_coupon_message(board, item, coupon, lines, err is None)))
    if not coupon or COUPON_TITLE not in title:
        sends.append(("新着", webhook_url, build_message(board, item, lines, err is None)))
    ok = True
    for label, url, message in sends:
        if dry_run:
            print(f"[{board['key']}] (dry-run・{label}) {title}")
            print(json.dumps(message, ensure_ascii=False, indent=2))
            continue
        try:
            post_discord(message, url())
        except Exception as e:  # noqa: BLE001 既読にせず次回また送る
            print(f"[{board['key']}] Discord({label})への送信に失敗: {title}: {e}")
            ok = False
            continue
        print(f"[{board['key']}] 通知しました({label}): {title}")
    return ok


def scan_coupons(board, items, state, now, skip_ids, dry_run):
    """機能を入れる前に出ていた、まだ期限内のクーポンの記事を、クーポン用のチャンネルに送る。"""
    ok = True
    checked = set(state.get("coupon_checked", []))
    for item in items:
        if COUPON_TITLE not in item["title"] or item["id"] in checked or item["id"] in skip_ids:
            continue
        try:
            body = fetch_body(board, item["id"])
        except Exception as e:  # noqa: BLE001 次回また調べる
            print(f"[{board['key']}] クーポンの記事の本文を読めません: {e}")
            continue
        coupon = parse_coupon(body)
        if not coupon or not coupon["ends"] or coupon["ends"] <= now:
            if not dry_run:
                mark_checked(state, item["id"])
            continue
        # 新着としてはもう送ってあるので、クーポン用のチャンネルにだけ送る
        print(f"[{board['key']}] 期限内のクーポンの記事をクーポン用のチャンネルに送る: {item['title']}")
        lines, err = summarize(board, item["title"], body)
        if lines is None:
            lines = fallback_lines(item, body)
        message = build_coupon_message(board, item, coupon, lines, err is None)
        if dry_run:
            print(json.dumps(message, ensure_ascii=False, indent=2))
            continue
        try:
            post_discord(message, coupon_webhook_url())
        except Exception as e:  # noqa: BLE001 次回また送る
            print(f"[{board['key']}] Discord(クーポン)への送信に失敗: {e}")
            ok = False
            continue
        mark_checked(state, item["id"])
        register_coupon(state, board, item, coupon, now)
    return ok


def send_reminders(state, now, dry_run):
    """使用期限の前日 21:00 を過ぎたクーポンを知らせ直す。期限が過ぎたものは記録から外す。"""
    ok = True
    coupons = state.get("coupons", {})
    for aid, c in list(coupons.items()):
        ends = datetime.fromisoformat(c["ends"])
        if now >= ends:
            print(f"[coupon] 期限が過ぎたので記録から外す: {c['title']}")
            if not dry_run:
                del coupons[aid]
            continue
        if c.get("reminded") or now < remind_at(ends):
            continue
        message = build_reminder(c)
        if dry_run:
            print(f"[coupon] (dry-run) リマインド: {c['title']}")
            print(json.dumps(message, ensure_ascii=False, indent=2))
            continue
        try:
            post_discord(message, coupon_webhook_url())
        except Exception as e:  # noqa: BLE001 次回また送る
            print(f"[coupon] リマインドの送信に失敗: {c['title']}: {e}")
            ok = False
            continue
        c["reminded"] = True
        print(f"[coupon] リマインドしました: {c['title']}")
    return ok


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="Discord に送らず、状態も保存しない")
    ap.add_argument("--latest", type=int, default=0, help="テスト用: 各掲示板の最新N件を既読に関係なく送る(状態は変えない)")
    ap.add_argument("--board", default="", help="対象を絞る(例: notice,update)")
    ap.add_argument("--now", help="現在時刻の代わりに使う時刻(リマインドの試し用。例 2027-03-02T12:00Z)")
    args = ap.parse_args()

    only = {b for b in args.board.split(",") if b}
    boards = [b for b in BOARDS if not only or b["key"] in only]
    state = load_state()
    now = parse_now(args.now)
    failed = False
    test_mode = args.latest > 0
    fetched, sent_ids = [], set()

    for board in boards:
        try:
            items = fetch_list(board)
        except Exception as e:  # noqa: BLE001 他の掲示板は続ける
            print(f"[{board['key']}] 一覧の取得に失敗: {e}")
            failed = True
            continue
        items = [i for i in items if parse_time(i["timestamps"]["postedAt"]) <= now]
        items.sort(key=lambda i: i["timestamps"]["postedAt"])
        fetched.append((board, items))

        if test_mode:
            targets = items[-args.latest:]
        elif board["key"] not in state:
            state[board["key"]] = [i["id"] for i in items][-KEEP_IDS:]
            print(f"[{board['key']}] 初回のため既存の {len(items)} 件を既読として記録(通知なし)")
            continue
        else:
            seen = set(state[board["key"]])
            targets = [i for i in items if i["id"] not in seen]
            if len(targets) > MAX_PER_RUN:
                skipped = targets[:-MAX_PER_RUN]
                print(f"[{board['key']}] 新着 {len(targets)} 件。古い {len(skipped)} 件は通知せず既読にする")
                state[board["key"]].extend(i["id"] for i in skipped)
                targets = targets[-MAX_PER_RUN:]

        if not targets:
            print(f"[{board['key']}] 新着なし")
            continue
        if not args.dry_run:
            try:
                webhook_url()  # 送れないなら要約(API の無料枠)を使う前に止める
            except RuntimeError as e:
                print(f"[{board['key']}] 新着 {len(targets)} 件を送れません: {e}")
                failed = True
                continue
        for item in targets:
            sent_ids.add(item["id"])
            try:
                body = fetch_body(board, item["id"])
            except Exception as e:  # noqa: BLE001 一覧の抜粋で代用する
                print(f"[{board['key']}] 本文の取得に失敗、一覧の抜粋で代用: {e}")
                body = item.get("summary") or ""
            coupon = parse_coupon(body)
            if not send_coupon_posts(board, item, body, coupon, args.dry_run):
                failed = True
                continue
            if not test_mode and not args.dry_run:
                state[board["key"]].append(item["id"])
                state[board["key"]] = state[board["key"]][-KEEP_IDS:]
                mark_checked(state, item["id"])
                if coupon:
                    register_coupon(state, board, item, coupon, now)
                save_state(state)

    # テスト送信のときは、クーポンの掘り起こしとリマインドをしない(記録を変えないため)
    if not test_mode:
        try:
            if not args.dry_run:
                coupon_webhook_url()
            for board, items in fetched:
                if not scan_coupons(board, items, state, now, sent_ids, args.dry_run):
                    failed = True
            if not send_reminders(state, now, args.dry_run):
                failed = True
        except RuntimeError as e:
            print(f"[coupon] 送れません: {e}")
            failed = True

    if not args.dry_run and not test_mode:
        save_state(state)
    return 1 if failed else 0


def parse_now(text):
    if not text:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    sys.exit(main())
