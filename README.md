# AION2 新着通知

AION2 公式サイト(日本語版)の「お知らせ」「アップデート」「イベント」に新しい記事が載ると、Gemini で3行に要約して Discord に送る。GitHub Actions が15分ごとに `notify.py` を実行する。

## ファイル

| ファイル | 役割 |
|---|---|
| `notify.py` | 3つの掲示板の新着を調べ、要約して Discord に送る |
| `state.json` | 通知済みの記事ID。Actions が実行のたびに更新してコミットする |
| `.github/workflows/notify.yml` | 15分ごとの定期実行と手動実行の設定 |

## 設定

リポジトリの Settings → Secrets and variables → Actions で登録する。

| 名前 | 種類 | 内容 |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Secret | 通知先チャンネルのウェブフック URL |
| `GEMINI_API_KEY` | Secret | Google AI Studio で発行した API キー |
| `GEMINI_MODEL` | Variable(任意) | 要約に使うモデル。省略時は `gemini-3.5-flash` |

## 動き方

- 初めて見る掲示板は、その時点の記事を既読として記録するだけで通知しない。
- 1回の実行で送るのは1掲示板あたり最大5件。`state.json` が消えて大量の記事が新着扱いになったときの連投を防ぐ。
- 要約に失敗したときは、本文の冒頭3文を載せて送る。埋め込みの下に「AI要約に失敗したため本文冒頭を表示」と出る。
- Discord への送信に失敗した記事は既読にせず、次の実行で送り直す。

## テスト送信

Actions タブ → 「AION2 新着通知」→ Run workflow で `latest` に `1` を入れると、各掲示板の最新1件を既読に関係なく送る。`state.json` は変わらない。

手元で送信せずに中身だけ見るときは次を実行する。`GEMINI_API_KEY` が環境変数にあれば要約も試す。

```bash
python notify.py --dry-run --latest 1
```

## 掲示板を増やす・変える

`notify.py` の `BOARDS` に1件足す。記事一覧を返す API は、公式サイトの掲示板ページをブラウザで開き、開発者ツールの通信一覧で `api-global-community.plaync.com/.../board/<名前>` を探すと分かる。
