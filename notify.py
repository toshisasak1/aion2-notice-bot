"""AION2 公式サイトの新着記事を Gemini で3行に要約し、Discord に通知する。

GitHub Actions から定期実行する前提。標準ライブラリだけで動く。

環境変数:
  DISCORD_WEBHOOK_URL  Discord のウェブフック URL(必須。--dry-run では不要)
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
from datetime import datetime, timezone
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
# gemini-flash-latest(3.8)は 2026-10 時点の無料枠で応答が返らなかったため使わない
DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-flash-lite-latest"]


def http_json(url, data=None, headers=None, timeout=30, retries=2):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs)
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
    """3行の要約を返す。失敗したら (None, 理由) を返す。"""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None, "GEMINI_API_KEY が未設定"
    prompt = (
        f"以下はオンラインゲーム『AION2』の公式サイトに載った「{board['label']}」の記事です。"
        "プレイヤーが知るべき要点を日本語で3行に要約してください。\n"
        "- ちょうど3行。1行は60文字以内。\n"
        "- 日時・期間・対象・報酬・変更点など具体的な情報を優先する。\n"
        "- 行頭に記号や番号を付けない。前置きや締めの文は書かない。\n\n"
        f"タイトル: {title}\n\n本文:\n{body[:BODY_LIMIT]}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        # 思考を浅くすると応答が数秒で返る(既定の深さだと数十秒〜タイムアウト)
        "generationConfig": {"temperature": 0.2, "thinkingConfig": {"thinkingLevel": "low"}},
    }
    models = [os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODELS[0]]
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
            return lines[:3], None
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
    footer = f"{board['label']} ・ " + ("Gemini で要約" if ai_ok else "AI要約に失敗したため本文冒頭を表示")
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


def webhook_url():
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not re.match(r"^https://(?:discord|discordapp)\.com/api/webhooks/", url):
        raise RuntimeError("DISCORD_WEBHOOK_URL が未設定か、Discord のウェブフック URL ではありません")
    return url


def post_discord(message):
    http_json(webhook_url(), message, retries=2)
    time.sleep(1.2)  # ウェブフックの送信間隔の制限に合わせる


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
    args = ap.parse_args()

    only = {b for b in args.board.split(",") if b}
    boards = [b for b in BOARDS if not only or b["key"] in only]
    state = load_state()
    now = datetime.now(timezone.utc)
    failed = False

    for board in boards:
        try:
            items = fetch_list(board)
        except Exception as e:  # noqa: BLE001 他の掲示板は続ける
            print(f"[{board['key']}] 一覧の取得に失敗: {e}")
            failed = True
            continue
        items = [i for i in items if parse_time(i["timestamps"]["postedAt"]) <= now]
        items.sort(key=lambda i: i["timestamps"]["postedAt"])

        test_mode = args.latest > 0
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
            title = item["title"]
            try:
                body = fetch_body(board, item["id"])
            except Exception as e:  # noqa: BLE001 一覧の抜粋で代用する
                print(f"[{board['key']}] 本文の取得に失敗、一覧の抜粋で代用: {e}")
                body = item.get("summary") or ""
            lines, err = summarize(board, title, body)
            if lines is None:
                print(f"[{board['key']}] 要約に失敗: {err}")
                lines = fallback_lines(item, body)
            message = build_message(board, item, lines, err is None)

            if args.dry_run:
                print(f"[{board['key']}] (dry-run) {title}")
                print(json.dumps(message, ensure_ascii=False, indent=2))
                continue
            try:
                post_discord(message)
            except Exception as e:  # noqa: BLE001 既読にせず次回また送る
                print(f"[{board['key']}] Discord への送信に失敗: {title}: {e}")
                failed = True
                continue
            print(f"[{board['key']}] 通知しました: {title}")
            if not test_mode:
                state[board["key"]].append(item["id"])
                state[board["key"]] = state[board["key"]][-KEEP_IDS:]
                save_state(state)

    if not args.dry_run and args.latest == 0:
        save_state(state)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
