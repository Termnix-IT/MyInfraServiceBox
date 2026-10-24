# MyInfraServiceBox

自宅のUbuntuサーバで、宅内LAN向けのWebサービスを動かすための基盤です。入口になるまとめページ（ポータル）、共有PostgreSQL、デプロイと切り戻しの`infra`コマンド、サーバの初期設定スクリプトをこのrepoで管理します。各サービスのコードはそれぞれのrepoに置き、この基盤に載せて動かします。

要件と、構成を選んだ理由は[docs/要件定義.md](docs/要件定義.md)にまとめています。

## 構成

| パス | 内容 |
| --- | --- |
| `services.toml` | サービス台帳。ポータルと`infra`コマンドの両方が読む |
| `compose.yaml` | 基盤本体（PostgreSQL、ソケットプロキシ、ポータル） |
| `infrabox/` | `infra`コマンドの本体。標準ライブラリだけで動く |
| `portal/` | ポータル（FastAPI）。台帳の読み込みには`infrabox/`を使う |
| `setup/` | サーバ初期設定スクリプト、`infra.env`の例、systemdのユニット |
| `examples/sample-service/` | 基盤との取り決めを満たす見本のサービス |

サーバ上では次の場所を使います。repoの外にあるファイルはgitで管理しません。

| パス | 内容 |
| --- | --- |
| `/opt/myinfraservicebox` | このrepo |
| `/etc/myinfraservicebox/infra.env` | サーバ固有の設定（LANのアドレス帯など） |
| `/etc/myinfraservicebox/secrets/` | PostgreSQLの管理者パスワードと、サービスごとの`DATABASE_URL` |
| `/var/lib/myinfraservicebox/` | サービスrepoのclone（`services/`）、デプロイ履歴（`deploy-history.jsonl`）、ロック |
| `/var/backups/myinfraservicebox/postgres/` | PostgreSQLのバックアップ（7世代） |

## サーバの初期設定

1. repoを配置します。
   ```bash
   sudo install -d -o "$USER" -g "$USER" /opt/myinfraservicebox
   git clone https://github.com/<owner>/MyInfraServiceBox.git /opt/myinfraservicebox
   ```
2. 初期設定スクリプトを実行します。初回は、LAN側のインターフェース・IPアドレス・アドレス帯を検出して`/etc/myinfraservicebox/infra.env`を作り、そこで止まります。
   ```bash
   sudo /opt/myinfraservicebox/setup/setup-server.sh
   ```
3. 表示された`infra.env`の値が正しいかを確認し、違っていれば`sudo nano /etc/myinfraservicebox/infra.env`で直してから、もう一度スクリプトを実行します。2回目で、Dockerの導入、`daemon.json`、グループとディレクトリ、共通ネットワーク`infra`、UFW、バックアップのtimerまで設定されます。既存のUFWルール（SSHやリモートデスクトップなど）は変更しません。
4. `docker`と`infrabox`のグループを反映するため、一度ログアウトしてログインし直します。
5. 基盤本体を起動します。ポータルは`http://<サーバのIP>/`で開けます。
   ```bash
   infra core up
   ```

## サーバでの確認

初期設定の後、次の点を確認します。どれも、設定を間違えたときに気付きにくい項目です。

1. **DOCKER-USERのルールが1組だけ入っていること**：`sudo iptables -S DOCKER-USER`を実行し、`RELATED,ESTABLISHED`、LANのアドレス帯の`RETURN`、`DROP`、最後の`RETURN`の4行だけが表示されることを確認します。`sudo ufw reload`や`sudo systemctl restart docker`の後にも同じ表示になることを確認します。
2. **LAN外から公開ポートに届かないこと**：スマートフォンをWi-Fiから外し、モバイル回線からサーバのグローバルIPアドレスの80番などへ接続できないことを確認します（ルーターでポート転送をしていなければ、そもそも届かないのが正常です）。
3. **IPv6で公開されていないこと**：`sudo ss -tlnp`で、`[::]:80`などIPv6側で待ち受けているポートがある場合は、別の端末からIPv6アドレスで接続できないことを確認します。UFWはIPv6をLANのルールで許可していないので、拒否されるのが正常です。
4. **ポータルのメモリ表示が実際の値と合っていること**：ポータルのメモリ使用量と、`free -b`の`total`および`total - available`を比べます。大きくずれる場合は、`compose.yaml`のポータルにホストの`/proc`を読み取り専用でマウントし、`psutil.PROCFS_PATH`をそこへ向ける対応が必要です。
5. **ポータルからMinecraftの死活が見えること**：Minecraftを起動したときに、ポータルで稼働中と表示されることを確認します。停止中と表示され続ける場合は、`sudo ufw status`に`infra`ネットワークのアドレス帯から25565番への許可があるかを確認します。

## サービス台帳

`services.toml`の`[[services]]`に1サービスずつ書きます。編集したら`infra check`で検査します。ポータルは台帳を読み直すので、ポータルの再起動は要りません。

| 項目 | 必須 | 内容 |
| --- | --- | --- |
| `name` | ○ | 英小文字で始まる英小文字・数字・`_`の2〜31文字。composeのプロジェクト名とDB名にも使う |
| `port` | ○ | LANに公開するポート。基盤のサービスは`settings.port_range`（8100〜8199）の中で、予約ポート以外 |
| `repo` | 基盤のサービスのみ | サービスrepoのURL（public repoをそのままclone） |
| `description` / `category` | | ポータルに表示する説明と分類 |
| `health` | | `{ type = "http", path = "/health" }`（既定）または`{ type = "tcp" }` |
| `branch` | | `--ref`を省略したときにデプロイするブランチ（既定は`main`） |
| `database` | | `true`にすると共有PostgreSQLの`DATABASE_URL`を渡す |
| `memory` | | composeのメモリ上限の合計の上限（既定`256m`） |
| `memory_reason` | | `memory`を`256m`より大きくする場合の理由（必須） |
| `external` | | `true`にすると基盤の外で動くサービスとして扱い、死活の確認だけを行う（Minecraftなど） |

メモリは、Minecraftの起動中（最大4GB）を前提に、基盤のコンテナ全体で約3GBに収まるよう配分します。内訳はPostgreSQLに512MB、ポータルに256MB、残り約2GBを各サービスです。

## サービスの追加とデプロイ

基盤に載せるサービスのrepoは、次の取り決めを守ります。`infra deploy`は起動前に`docker compose config`で検査し、守られていなければ起動しません。見本は[examples/sample-service](examples/sample-service)にあります。

- repoのルートに`Dockerfile`と`compose.yaml`を置く。
- 公開ポートは`${SERVICE_PORT}`で受け取り、台帳の`port`以外は公開しない。
- `infra`ネットワーク（`external: true`、`name: ${INFRA_NETWORK:-infra}`）に参加する。
- HTTPで`/health`を提供し、正常なときに200を返す。
- 共有PostgreSQLを使う場合は、接続先を`${DATABASE_URL}`で受け取る。
- すべてのサービスに`mem_limit`を指定する。
- パスワードなどの秘密情報をrepoに含めない。

追加とデプロイの手順は次のとおりです。

1. サービスrepoをGitHubなどにpushし、`services.toml`に登録して`infra check`で検査します。台帳の変更はこのrepoにcommitし、サーバで`git pull`します。
2. DBを使う場合は、DBとユーザーを作ります。接続先は`/etc/myinfraservicebox/secrets/<name>.env`に保存されます。
   ```bash
   infra db create <name>
   ```
3. デプロイします。`--ref`にはブランチ・タグ・commitを指定でき、省略すると台帳の`branch`をデプロイします。
   ```bash
   infra deploy <name> --ref <branch|tag|commit>
   ```
   ヘルスチェックが60秒以内に成功しなければ、直前に成功した版へ自動で戻します。結果は`infra history`とポータルで確認できます。
4. 動作確認の後、前の版に戻したい場合は切り戻します。いま動いている版の1つ前に動いていた版をデプロイします。
   ```bash
   infra rollback <name>
   ```

`infra list`で、各サービスがいまどの版で動いているかを一覧できます。

## バックアップと復元

毎日4時30分ごろに、systemd timerが`infra backup`を実行し、全DBを`/var/backups/myinfraservicebox/postgres/<YYYYmmdd-HHMMSS>/`へ保存します。各DBは`<DB名>.dump`（`pg_dump`のcustom形式）、ユーザーと権限は`globals.sql`に入ります。新しい順に7世代を残し、古いものは消します。手動でバックアップを取る場合は`infra backup`を実行します。

1つのDBを復元するときは、次のように実行します。対象のDBは上書きされるので、先に`infra backup`で現在の状態を残しておきます。

```bash
infra restore <YYYYmmdd-HHMMSS> <DB名>
```

PostgreSQLのボリュームごと失った場合は、`infra core up`で空のPostgreSQLを起動し、先にユーザーを戻してから各DBを復元します。

```bash
docker exec -i infrabox-postgres psql -U postgres -d postgres < /var/backups/myinfraservicebox/postgres/<YYYYmmdd-HHMMSS>/globals.sql
docker exec infrabox-postgres createdb -U postgres -O <DB名> <DB名>
infra restore <YYYYmmdd-HHMMSS> <DB名>
```

## 開発

テストは、Dockerやgitを呼ぶ部分を差し替えて、台帳の検査・デプロイの流れ・ポータルの表示を確認します。`fcntl`を使うロックと、実際のDockerの操作はサーバ上で確認します。

```bash
python -m venv .venv
.venv/bin/pip install -r portal/requirements.txt pytest
.venv/bin/python -m pytest
```

Windowsでは`.venv\Scripts\python -m pytest`のように読み替えます。
