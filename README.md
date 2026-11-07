# AION2 新着通知

AION2 公式サイト(日本語版)の「お知らせ」「アップデート」「イベント」に新しい記事が載ると、Gemini で箇条書きに要約して Discord に送る。行数は記事の中身に合わせて1〜10行で変わる(上限は `notify.py` の `MAX_LINES`)。cron-job.org が15分ごとに GitHub Actions を起動し、Actions が `notify.py` を実行する。

同じ起動で `siege.py` も動き、アーティファクト戦(アジア、月・木・土の21:00)が終わると、組み合わせごとの結果(3-0、1-2 など)の一覧を送る。結果は DaevaOS から、運営者の許可を得て読んでいる(詳しくは下の「アーティファクト戦の結果」)。

## ファイル

| ファイル | 役割 |
|---|---|
| `notify.py` | 3つの掲示板の新着を調べ、要約して Discord に送る |
| `state.json` | 通知済みの記事ID。Actions が実行のたびに更新してコミットする |
| `siege.py` | アーティファクト戦の後に DaevaOS を読み、組み合わせごとの結果を Discord に送る |
| `siege_state.json` | アーティファクト戦の送信済みの回と、その回にページを読んだ回数。結果そのものは保存しない |
| `.github/workflows/notify.yml` | 外部・手動からの起動と、予備の定期実行(毎時7・22・37・52分)の設定 |

## 設定

リポジトリの Settings → Secrets and variables → Actions で登録する。

| 名前 | 種類 | 内容 |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Secret | 通知先チャンネルのウェブフック URL |
| `GEMINI_API_KEY` | Secret | Google AI Studio で発行した API キー |
| `GEMINI_MODEL` | Variable(任意) | 要約に使うモデル。省略時は `gemini-3.5-flash` |
| `SIEGE_WEBHOOK_URL` | Secret(任意) | アーティファクト戦の結果の送り先。省略時は `DISCORD_WEBHOOK_URL` と同じチャンネル |

## 外部からの起動(cron-job.org)

GitHub の定期実行(`schedule`)は混雑時に実行を捨てるため、2026-10-23 の登録から約7時間で1回しか動かなかった。そこで cron-job.org から15分ごとに、次の API で Actions を起動している。`schedule` は予備として残してあり、両方から起動されても `concurrency` と `ref: main` によって二重通知にはならない。

| 項目 | 値 |
|---|---|
| URL | `https://api.github.com/repos/toshisasak1/aion2-notice-bot/actions/workflows/notify.yml/dispatches` |
| メソッド | `POST` |
| ヘッダー | `Accept: application/vnd.github+json`<br>`Authorization: Bearer <トークン>`<br>`X-GitHub-Api-Version: 2026-03-10`<br>`Content-Type: application/json` |
| 本文 | `{"ref":"main"}` |
| 成功時の応答 | `200`(本文に起動した実行の ID が入る) |

トークンは fine-grained personal access token で、対象はこのリポジトリだけ、権限は Actions の Read and write だけ。期限が切れると cron-job.org の実行が失敗し、失敗通知のメールが届く。そのときは同じ設定でトークンを作り直し、cron-job.org のジョブの `Authorization` ヘッダーを差し替える。

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

## アーティファクト戦の結果

NCSOFT は結果を公開していない。DaevaOS(`https://daevaos.com/statistics/artifact-siege`)が、DaevaOS Companion の利用者がゲーム内で読んだ保持数を組み合わせごとに載せているので、それを読む。DaevaOS の規約では、スクリプトでの取得には運営者の書面の許可が要る。2026-11 に、このリポジトリの持ち主が DaevaOS の Discord で許可を得た。許可の範囲を超えないよう、次のように動かす。

- 読むのは戦が終わったあと(開始から40分後以降)に1回だけ。どの組み合わせもまだ記録されていないときだけ、次の起動で読み直す(1回の戦で最大3回。送信の失敗を含めても5回まで)。
- 開始から24時間を過ぎた回は送らない。
- 投稿のタイトルを DaevaOS のページへのリンクにする。
- 結果をリポジトリに残さない(このリポジトリは公開なので)。`siege_state.json` にあるのは送信済みの回と読んだ回数だけ。

投稿は、DaevaOS の並び順(天族側のサーバーの番号順)で全組み合わせを1行ずつ出す。その回の記録がある組は「🟦🟦🟥 **Fregion** 2-1 Azphel」のように、保持数と勝った側の太字を出す。記録が無い組は、前回の結果と日付を添えて「記録なし」と出す。DaevaOS に載るのは Companion の利用者がその場にいた組だけなので、毎回いくつかの組は記録なしになる。

DaevaOS のサーバー一覧では Nania は天族、Marchutan は魔族だが、組み合わせの表示では Hithanya - Nania、Marchutan - Ereshkigal となっている(2026-11-07 時点)。どちらが正しいかは確かめていない。投稿は DaevaOS の組み合わせの表示に合わせている。

地域や日程を変えるときは `siege.py` の `REGIONS` を書き換える。キーは DaevaOS のページの `data-region` の値(`GLOBAL_ASIA` など)。

テスト送信は、Actions タブ → 「AION2 新着通知」→ Run workflow で `siege_test` に印を付ける。直近の回を、日程と送信済みの記録に関係なく送る(`siege_state.json` は変わらない)。手元で送信せずに中身だけ見るときは次を実行する。

```bash
python siege.py --dry-run --test
```

保存した HTML と時刻を指定すれば、DaevaOS を読まずに試せる(例: `python siege.py --dry-run --file page.html --now 2026-11-05T13:00Z`)。

## 掲示板を増やす・変える

`notify.py` の `BOARDS` に1件足す。記事一覧を返す API は、公式サイトの掲示板ページをブラウザで開き、開発者ツールの通信一覧で `api-global-community.plaync.com/.../board/<名前>` を探すと分かる。
