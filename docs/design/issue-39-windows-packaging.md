# #39 Windows配布物（同梱ランタイムとモデル導入） 設計

> 状態: 設計のみ。現状の配布候補はCore/GUI zipとPython wheelのみで、**インストーラー・Python同梱・FFmpeg同梱・モデル配布は無い**
> （[Development §Distribution provenance](../development.md#distribution-provenance-and-verification-67)）。コード署名は#67（証明書待ち）。

## 目的
Rust・.NET SDK・Python・FFmpegを手動導入しなくても、一般ユーザーがインストール→起動→解析→終了→再起動できるWindows配布物を作る。

## 受入条件（[Issue #39](https://github.com/tomiya7688/Open_karaoke/issues/39) から転記）
- 開発ツール未導入のWindowsでアプリを起動できる
- モデルセットアップがアプリ内で完結し、必要な同意/情報のみ示す
- 公式配布物のコード/重み/ランタイムの商用・再配布条件を記録する
- スタンドアロン起動、解析・終了・再起動を確認できる
- テスト: クリーンWindows VM smoke、SBOM/NOTICE/FFmpeg buildconf/モデルライセンスチェック、オフライン/初回導入/モデル破損

## 非目標
- コード署名・Attestationの導入そのもの（#67）、Python証明書対応（#66）
- 1.0.0の精度・実機評価（#19）、モデルMOD/ユーザー導入モデルのライセンス審査（禁止しない方針）
- macOS/Linux/コンテナ配布

## 設計
**責務**
- 配布構成: `app/`（Core exe, GUI publish）、`runtime/python/`（埋込Python+固定した依存）、`tools/ffmpeg/`（ffmpeg/ffprobe+`buildconf`記録）、`models/`（利用者領域。公式既定モデルは初回に導入）。レイアウトは案、未決。
- 起動: GUIがCoreを子プロセスで起動し、Coreがworkerを監視する既存方式（[Desktop session](../desktop_session.md)）を維持。パスは同梱位置から解決し、利用者にパス入力を求めない（現GUIは設定タブでパス指定）。
- モデル導入: 既存の`models/<id>/manifest.json`検出（`model_packages.py`、[Model Plugins](../model_plugins.md)）へ載せる。ダウンロード元・サイズ・SHA-256・ライセンスをmanifest/台帳に持ち、検証失敗は導入しない。manifestを読んだだけで未知コードを実行しない。
- 台帳: 同梱物ごとにライセンス・版・hash・再配布条件を`THIRD_PARTY_NOTICES.md`と機械可読inventory（`scripts/dependency-inventory.ps1`）で追跡。FFmpegは`ffmpeg -buildconf`が`--enable-gpl`/`--enable-nonfree`を含まないことをCoreが検査（`audio_import.rs` `verify_ffmpeg_distribution`）、配布時にbuildconf全文を成果物へ同梱。

**データ契約（案）**: `runtime-manifest.json { app_version, components[{name,version,sha256,license,source_url}], models[{id,role,sha256,size,license,commercial_use,redistribution}] }`。

**エラー**: モデル欠落/破損/オフラインは「導入が必要」「再取得」の明示エラーでサービス全体は落とさない（#17の不正manifestの扱いと同じ）。更新・アンインストールは利用者データ（曲・設定）を既定で残す。
**スレッド/リアルタイム**: 導入・ダウンロード・hash検証はバックグラウンドJob。音声再生中は開始を遅延（#43）。

## 実装スライス
| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 39.1 | 同梱レイアウトと`runtime-manifest.json`、パス解決（同梱優先） | #15 | 開発ツール無しの環境を模した起動テスト（CI、Linux可の範囲）、manifest schema検証 |
| 39.2 | 埋込Python+依存のlock/hash固定とビルドスクリプト | 39.1, #66 | 再現ビルド（同一入力で同一hash）、`check_python_trust`通過 |
| 39.3 | FFmpegの適法な同梱（LGPL構成）+buildconf記録+NOTICE | 39.1 | buildconf検査テスト、NOTICE/inventoryが成果物と一致 |
| 39.4 | アプリ内モデル導入UI（同意・容量・進捗・取消・検証） | #17, #110 | 破損/オフライン/容量不足のContract test、GUI headless smoke |
| 39.5 | インストーラー+アンインストール+更新（形式は未決） | 39.1-39.3 | クリーンWindows VM smoke（手順書化、実行はUnverifiedとして別報告） |
| 39.6 | 配布物のライセンス監査と`distribution.yml`への接続 | 39.2-39.5, #67 | 成果物hashに紐づくSBOM/NOTICE、署名はこのスライスの外 |

## 触るファイル
`scripts/build.ps1`、`.github/workflows/distribution.yml`、`scripts/dependency-inventory.ps1`、`THIRD_PARTY_NOTICES.md`、`docs/licensing.md`・`docs/development.md`、`core/src/main.rs`・`analysis_service.rs`（パス解決）、`core/src/audio_import.rs`（ffmpeg解決）、`python/src/open_karaoke_analysis/model_packages.py`、`gui/OpenKaraoke.Gui/`（導入UI）。**署名鍵・秘密をコミットしない**。

## 守る規則
- [Licensing](../licensing.md): 商用・再配布条件が確認できない依存は公式配布物に含めない。ユーザーMODのローダー側にライセンス硬制限を作らない
- コードと重みのライセンスは別々に確認（[AI models](../licensing.md#ai-models)）。モデルはAdapter越し
- [宣言コメント必須](../coding_rules.md#5-ソースコードコメント)。秘密・証明書はCI secretのみ
- CI生成物はレビュー証跡であり配布物ではない（[development](../development.md#distribution-provenance-and-verification-67)）。クリーンVM未実施なら`Unverified`

## 未決事項
- インストーラー技術（MSIX/WiX/Inno/zip自己展開）と更新方式
- 埋込Pythonの配布形式（embeddable zip vs venv同梱）とGPU（CUDA）ランタイムの扱い
- 公式既定モデルの選定・ダウンロード元・同意文面（法務確認が必要）
- CPUのみの最低動作プロファイルの定義（#43の実測後）
