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

#[derive(Clone)]
pub struct AudioImportConfig {
    artifact_root: PathBuf,
    normalizer: Normalizer,
}

impl AudioImportConfig {
    /// Normalize with external `ffmpeg`/`ffprobe` executables (license policy is enforced).
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

    #[must_use]
    pub fn with_normalizer(artifact_root: PathBuf, normalizer: Normalizer) -> Self {
        Self {
            artifact_root,
            normalizer,
        }
    }
}

pub(crate) struct AudioWork {
    config: AudioImportConfig,
    source: PathBuf,
    pub control: Arc<ImportControl>,
}

/// Validate the request at the Core boundary before a Job is created.
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
        Ok(metadata) if metadata.is_file() => {}
        Ok(_) => return Err(invalid("source_path is not a regular file")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(ApiError(
                StatusCode::NOT_FOUND,
                "audio_source_not_found: source file does not exist".to_owned(),
            ));
        }
        Err(_) => return Err(invalid("source_path cannot be read")),
    }
    Ok(AudioWork {
        config,
        source: source.clone(),
        control: Arc::new(ImportControl::default()),
    })
}

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
    /// Normalize into private staging, then publish the Job directory with one rename.
    /// Failure or cancellation removes staging, so no partial input is ever advertised.
    fn execute(self, id: Uuid) -> Result<Vec<String>, AudioImportError> {
        let imports = self.config.artifact_root.join(IMPORTS_DIR);
        fs::create_dir_all(&imports)?;
        let staging = imports.join(format!(".staging-{id}"));
        let result = self.publish(&imports, &staging, id);
        if result.is_err() {
            let _ = fs::remove_dir_all(&staging);
        }
        result
    }

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
        fs::rename(staging, imports.join(id.to_string()))?;
        Ok(vec![
            format!("{IMPORTS_DIR}/{id}/{AUDIO_FILE}"),
            format!("{IMPORTS_DIR}/{id}/{SUMMARY_FILE}"),
        ])
    }
}

#[cfg(test)]
mod tests;
