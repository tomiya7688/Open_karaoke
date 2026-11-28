//! Core `audio.import` Job: normalize one user-chosen source file (WAV/AAC/MP3/MP4) into the
//! shared artifact root so a later `analysis.song` Job can use it as `input_artifact`.
//! The blocking `FFmpeg` work runs on a worker thread; cancellation kills the running tool and
//! has one atomic ordering point with publication, like `song.import`.

use std::{
    fs,
    path::{Path, PathBuf},
    sync::Arc,
};

use axum::http::StatusCode;
use serde::Deserialize;
use serde_json::json;
use uuid::Uuid;

use crate::{
    ApiError, AppState, JobStatus,
    audio_import::{self, AudioImportError, NormalizedAudioMetadata},
    song_library::ImportControl,
};

const IMPORTS_DIR: &str = "imports";
const AUDIO_FILE: &str = "original_48k.wav";
const SUMMARY_FILE: &str = "normalized.json";
/// 入力ファイルの上限。30分の高音質WAV(96kHz/24bit/stereo≒1GiB)を余裕で通し、巨大ファイルを拒否する。
const MAX_SOURCE_BYTES: u64 = 4 * 1024 * 1024 * 1024;

/// {
///   責務: [`AudioImportRequest`: `audio.import` Jobの入力(ローカル音声ファイル)]
///   フィールド: [`source_path`: 認証済みデスクトップクライアントが選んだ入力ファイルの絶対パス]
/// }
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AudioImportRequest {
    /// Absolute path of the local source file chosen by the authenticated desktop client.
    pub source_path: PathBuf,
}

/// `(input, output, cancelled)` -> normalized metadata. Production uses `FFmpeg`.
pub type Normalizer = Arc<
    dyn Fn(&Path, &Path, &dyn Fn() -> bool) -> Result<NormalizedAudioMetadata, AudioImportError>
        + Send
        + Sync,
>;

/// {
///   責務: [`AudioImportConfig`: 音声取り込みの成果物保存先と正規化処理の設定]
///   フィールド: [`artifact_root`: 成果物の保存ルート, `normalizer`: 入力から正規化WAVを作る関数]
/// }
#[derive(Clone)]
pub struct AudioImportConfig {
    artifact_root: PathBuf,
    normalizer: Normalizer,
}

impl AudioImportConfig {
    /// {
    ///   責務: [`AudioImportConfig::ffmpeg`: 外部`ffmpeg`/`ffprobe`で正規化する設定を作る(ライセンス方針を強制する)]
    ///   処理: [1: ffmpeg/ffprobeを呼ぶ正規化クロージャを作る, 2: `with_normalizer`へ渡す]
    ///   引数: [`artifact_root`: 成果物ルート, `ffmpeg`/`ffprobe`: 実行ファイル]
    ///   戻り値: [設定`AudioImportConfig`]
    /// }
    #[must_use]
    pub fn ffmpeg(artifact_root: PathBuf, ffmpeg: PathBuf, ffprobe: PathBuf) -> Self {
        Self::with_normalizer(
            artifact_root,
            Arc::new(move |input, output, cancelled| {
                audio_import::normalize_audio_cancellable(
                    &ffmpeg, &ffprobe, input, output, cancelled,
                )
            }),
        )
    }

    /// {
    ///   責務: [`AudioImportConfig::with_normalizer`: 任意の正規化関数を使う設定を作る(テストで差し替える)]
    ///   処理: [1: 保存ルートと正規化関数を保持する]
    ///   引数: [`artifact_root`: 成果物ルート, `normalizer`: 正規化関数]
    ///   戻り値: [設定`AudioImportConfig`]
    /// }
    #[must_use]
    pub fn with_normalizer(artifact_root: PathBuf, normalizer: Normalizer) -> Self {
        Self {
            artifact_root,
            normalizer,
        }
    }
}

/// {
///   責務: [`check_source_size`: 入力ファイルの大きさが上限内か確かめる]
///   処理: [1: バイト数を`MAX_SOURCE_BYTES`と比べる, 2: 超過なら413を返す]
///   引数: [`bytes`: 入力ファイルのバイト数]
///   戻り値: [上限内なら`Ok`、超過なら`audio_source_too_large`の`ApiError`]
/// }
fn check_source_size(bytes: u64) -> Result<(), ApiError> {
    if bytes > MAX_SOURCE_BYTES {
        return Err(ApiError(
            StatusCode::PAYLOAD_TOO_LARGE,
            "audio_source_too_large: source file exceeds the 4 GiB limit".to_owned(),
        ));
    }
    Ok(())
}

impl AudioImportConfig {
    /// {
    ///   責務: [`AudioImportConfig::sweep_stale_imports`: 古い取り込み成果物と中断で残った作業領域を起動時に掃除する]
    ///   処理: [1: `imports`直下の子を列挙する, 2: `imports`自体がリンクなら中止し、子のリンクと正規形でない名前を飛ばす, 3: 最終更新が`max_age`より古い`<uuid>`と`.staging-<uuid>`を削除する, 4: 列挙・削除の失敗は警告に出して続行する]
    ///   引数: [`max_age`: 残す期間]
    ///   戻り値: [削除した件数]
    ///   副作用: [`imports`配下の古いディレクトリを削除する。`song.import`は成果物をコピーするため、取込済みの曲には影響しない]
    /// }
    #[must_use]
    pub fn sweep_stale_imports(&self, max_age: std::time::Duration) -> usize {
        let imports = self.artifact_root.join(IMPORTS_DIR);
        // `imports`自体がリンクなら、外の領域を列挙・削除しかねないので何もしない。
        match fs::symlink_metadata(&imports) {
            Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
            Ok(_) => {
                eprintln!("warning: audio import sweep skipped: imports is not a plain directory");
                return 0;
            }
            Err(_) => return 0,
        }
        let entries = match fs::read_dir(&imports) {
            Ok(entries) => entries,
            Err(error) => {
                eprintln!("warning: audio import sweep cannot read imports: {error}");
                return 0;
            }
        };
        let mut removed = 0;
        for entry in entries {
            let entry = match entry {
                Ok(entry) => entry,
                Err(error) => {
                    eprintln!("warning: audio import sweep cannot read an entry: {error}");
                    continue;
                }
            };
            let name = entry.file_name().to_string_lossy().into_owned();
            let id = name.strip_prefix(".staging-").unwrap_or(&name);
            // Coreが作る正規形(小文字・ハイフン付き)だけを対象にし、別表記の名前は触らない。
            if !Uuid::parse_str(id).is_ok_and(|uuid| uuid.hyphenated().to_string() == id) {
                continue;
            }
            // リンクを辿って外を消さないよう、リンク自体のメタデータで判定する。
            let Ok(metadata) = fs::symlink_metadata(entry.path()) else {
                continue;
            };
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                continue;
            }
            let stale = metadata
                .modified()
                .ok()
                .and_then(|time| time.elapsed().ok())
                .is_some_and(|age| age > max_age);
            if !stale {
                continue;
            }
            match fs::remove_dir_all(entry.path()) {
                Ok(()) => removed += 1,
                Err(error) => eprintln!("warning: audio import sweep cannot remove {name}: {error}"),
            }
        }
        removed
    }
}

/// {
///   責務: [`AudioWork`: 検証済みの音声取り込み1件分の作業(Job作成後に実行する)]
///   フィールド: [`config`: 取り込み設定, `source`: 入力ファイル, `control`: 中断・確定の制御]
/// }
pub(crate) struct AudioWork {
    config: AudioImportConfig,
    source: PathBuf,
    pub control: Arc<ImportControl>,
}

/// {
///   責務: [`prepare`: `Job`作成前に要求を`Core`境界で検証する]
///   処理: [1: 設定の有無を確認する, 2: 絶対パスと対応拡張子を検証する, 3: 通常ファイルとして読めることと`4GiB`以下であることを確認する, 4: `AudioWork`を作る]
///   引数: [`state`: アプリ状態, `request`: 取り込み要求]
///   戻り値: [`AudioWork`、不正は400・上限超過は413・未存在は404・未設定は利用不可の`ApiError`]
/// }
pub(crate) fn prepare(
    state: &AppState,
    request: &AudioImportRequest,
) -> Result<AudioWork, ApiError> {
    let config = state
        .audio_import
        .clone()
        .ok_or_else(|| ApiError::unavailable("Audio import is not configured".to_owned()))?;
    let source = &request.source_path;
    let invalid = |message: &str| {
        ApiError(
            StatusCode::BAD_REQUEST,
            format!("audio_source_invalid: {message}"),
        )
    };
    if !source.is_absolute() {
        return Err(invalid("source_path must be absolute"));
    }
    audio_import::ensure_supported_input(source).map_err(|error| {
        ApiError(
            StatusCode::BAD_REQUEST,
            format!("{}: {error}", error.code()),
        )
    })?;
    match fs::metadata(source) {
        Ok(metadata) if metadata.is_file() => {
            check_source_size(metadata.len())?;
        }
        Ok(_) => return Err(invalid("source_path is not a regular file")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(ApiError(
                StatusCode::NOT_FOUND,
                "audio_source_not_found: source file does not exist".to_owned(),
            ));
        }
        Err(_) => return Err(invalid("source_path cannot be read")),
    }
    // 種別は取れても読取権限が無い場合があるため、Job作成前に実際に開いて確かめる。
    if fs::File::open(source).is_err() {
        return Err(invalid("source_path cannot be opened for reading"));
    }
    Ok(AudioWork {
        config,
        source: source.clone(),
        control: Arc::new(ImportControl::default()),
    })
}

/// {
///   責務: [`run`: 音声取り込みをブロッキングスレッドで実行し、Jobを終端状態へ遷移させる]
///   処理: [1: stageを`audio_import.normalizing`にする, 2: `execute`を`spawn_blocking`で実行する, 3: 成功ならprogress=1.0・成果物を設定して完了、中断ならcancelled、失敗ならコード付きfailedにする]
///   引数: [`state`: アプリ状態, `id`: Job ID, `work`: 検証済み作業]
///   戻り値: [[]]
/// }
pub(crate) async fn run(state: &AppState, id: Uuid, work: AudioWork) {
    state
        .update(id, |job| {
            "audio_import.normalizing".clone_into(&mut job.stage);
        })
        .await;
    match tokio::task::spawn_blocking(move || work.execute(id)).await {
        Ok(Ok(artifacts)) => {
            state
                .update(id, |job| {
                    job.progress = 1.0;
                    job.artifacts = artifacts;
                })
                .await;
            state.finish(id, JobStatus::Completed, None).await;
        }
        Ok(Err(AudioImportError::Cancelled)) => state.finish(id, JobStatus::Cancelled, None).await,
        Ok(Err(error)) => {
            let message = format!("{}: {error}", error.code());
            state.finish(id, JobStatus::Failed, Some(message)).await;
        }
        Err(_) => {
            let message = "audio_import_worker_failed".to_owned();
            state.finish(id, JobStatus::Failed, Some(message)).await;
        }
    }
}

impl AudioWork {
    /// {
    ///   責務: [`AudioWork::execute`: 非公開のstagingへ正規化してからJobディレクトリを1回のrenameで公開する。失敗・中断時はstagingを消し、部分入力を公開しない]
    ///   処理: [1: importsディレクトリを作る, 2: `.staging-{id}`で`publish`を実行する, 3: 失敗ならstagingを削除する]
    ///   引数: [`self`: 作業(消費する), `id`: Job ID]
    ///   戻り値: [公開した成果物の相対パス一覧、失敗は`AudioImportError`]
    /// }
    fn execute(self, id: Uuid) -> Result<Vec<String>, AudioImportError> {
        let imports = self.config.artifact_root.join(IMPORTS_DIR);
        fs::create_dir_all(&imports)?;
        let staging = imports.join(format!(".staging-{id}"));
        let result = self.publish(&imports, &staging, id);
        if result.is_err() {
            // Jobの結果は元のエラーのまま保ち、残骸の原因を追えるよう削除失敗だけを記録する。
            if let Err(error) = fs::remove_dir_all(&staging)
                && error.kind() != std::io::ErrorKind::NotFound
            {
                eprintln!(
                    "warning: audio import staging {} was not removed: {error}",
                    staging.display()
                );
            }
        }
        result
    }

    /// {
    ///   責務: [`AudioWork::publish`: staging内で正規化し、要約JSONを書いて確定し、Jobディレクトリへrenameする]
    ///   処理: [1: stagingを作る, 2: 中断判定付きで正規化する, 3: 長さ0なら`InvalidProbe`, 4: `normalized.json`を書く, 5: `finalize`で中断を締め切る, 6: renameして成果物パスを返す]
    ///   引数: [`imports`: importsルート, `staging`: 作業用ディレクトリ, `id`: Job ID]
    ///   戻り値: [音声とサマリの成果物パス、失敗・中断は`AudioImportError`]
    /// }
    fn publish(
        &self,
        imports: &Path,
        staging: &Path,
        id: Uuid,
    ) -> Result<Vec<String>, AudioImportError> {
        fs::create_dir(staging)?;
        let cancelled = || self.control.checkpoint().is_err();
        let metadata =
            (self.config.normalizer)(&self.source, &staging.join(AUDIO_FILE), &cancelled)?;
        if metadata.duration_samples == 0 {
            return Err(AudioImportError::InvalidProbe(
                "normalized audio has no samples".to_owned(),
            ));
        }
        let summary = json!({
            "format_version": 1,
            "sample_rate": metadata.sample_rate,
            "channels": metadata.channels,
            "duration_samples": metadata.duration_samples,
            "audio_artifact": format!("{IMPORTS_DIR}/{id}/{AUDIO_FILE}"),
        });
        fs::write(staging.join(SUMMARY_FILE), format!("{summary:#}\n"))?;
        self.control
            .finalize()
            .map_err(|_| AudioImportError::Cancelled)?;
        let published = imports.join(id.to_string());
        // 既存の成果物を置き換えない(同一idの再実行・衝突でも公開済みを壊さない)。
        if published.exists() {
            return Err(AudioImportError::Io(std::io::Error::new(
                std::io::ErrorKind::AlreadyExists,
                "import directory already exists",
            )));
        }
        fs::rename(staging, published)?;
        Ok(vec![
            format!("{IMPORTS_DIR}/{id}/{AUDIO_FILE}"),
            format!("{IMPORTS_DIR}/{id}/{SUMMARY_FILE}"),
        ])
    }
}

#[cfg(test)]
mod tests;
