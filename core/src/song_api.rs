//! Core Job/API wiring for verified song imports. Clients supply a completed
//! Core Job ID, never an arbitrary filesystem path or replacement `SongBundle`.

#![allow(clippy::doc_markdown)] // JSON風宣言コメントがsnake_caseのフィールド名を含む

use std::sync::Arc;

use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::StatusCode,
    routing::get,
};
use serde::Deserialize;
use tokio::sync::{OwnedSemaphorePermit, watch};
use uuid::Uuid;

use crate::song_edit::{EditCommand, EditOp};
use crate::song_library::{
    EditedSong, ImportControl, ImportError, SongEditSaveError, SongLibrary, SongPage,
};
use crate::{ApiError, AppState, Job, JobStatus, song_data::SongBundle};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SongImportRequest {
    pub analysis_job_id: Uuid,
}

pub(crate) struct ImportWork {
    library: SongLibrary,
    source: Job,
    pub control: Arc<ImportControl>,
    permit: OwnedSemaphorePermit,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/songs", get(list))
        .route("/songs/{id}", get(load).patch(edit))
}

fn library(state: &AppState) -> Result<SongLibrary, ApiError> {
    state.ensure_running()?;
    state
        .song_library
        .clone()
        .ok_or_else(|| ApiError::unavailable("Song library is not configured".to_owned()))
}

fn permit(library: &SongLibrary) -> Result<OwnedSemaphorePermit, ApiError> {
    Arc::clone(&library.budget)
        .try_acquire_owned()
        .map_err(|_| {
            ApiError(
                StatusCode::TOO_MANY_REQUESTS,
                "Song library I/O budget is busy".to_owned(),
            )
        })
}

pub(crate) async fn prepare(
    state: &AppState,
    request: &SongImportRequest,
) -> Result<ImportWork, ApiError> {
    let library = library(state)?;
    let source = state
        .snapshot(request.analysis_job_id)
        .await
        .ok_or_else(ApiError::not_found)?;
    if source.kind != "analysis.song" || source.status != JobStatus::Completed {
        return Err(ApiError(
            StatusCode::CONFLICT,
            "Only a completed analysis.song Job can be imported".to_owned(),
        ));
    }
    let permit = permit(&library)?;
    Ok(ImportWork {
        library,
        source,
        control: Arc::new(ImportControl::default()),
        permit,
    })
}

pub(crate) async fn run(state: &AppState, id: Uuid, work: ImportWork) {
    let (progress, mut updates) = watch::channel((0_u16, "song_import.starting"));
    let mut task = tokio::task::spawn_blocking(move || {
        let _permit = work.permit;
        work.library
            .import(&work.source, &work.control, |value, label| {
                progress.send_replace((value, label));
            })
    });
    let mut progress_open = true;
    let result = loop {
        tokio::select! {
            result = &mut task => break result,
            changed = updates.changed(), if progress_open => {
                if changed.is_err() { progress_open = false; continue; }
                let (progress, label) = *updates.borrow_and_update();
                state.update(id, |job| {
                    job.progress = f32::from(progress) / 10_000.0;
                    label.clone_into(&mut job.stage);
                }).await;
            }
        }
    };
    match result {
        Ok(Ok(imported)) => {
            state
                .update(id, |job| {
                    job.progress = 1.0;
                    job.imported_song = Some(imported);
                })
                .await;
            state.finish(id, JobStatus::Completed, None).await;
        }
        Ok(Err(ImportError::Cancelled)) => state.finish(id, JobStatus::Cancelled, None).await,
        Ok(Err(error)) => {
            state
                .finish(id, JobStatus::Failed, Some(error.to_string()))
                .await;
        }
        Err(_) => {
            state
                .finish(
                    id,
                    JobStatus::Failed,
                    Some("song_import_worker_failed".to_owned()),
                )
                .await;
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ListQuery {
    after: Option<Uuid>,
    limit: Option<usize>,
}

async fn list(
    State(state): State<AppState>,
    Query(query): Query<ListQuery>,
) -> Result<Json<SongPage>, ApiError> {
    let library = library(&state)?;
    let limit = query.limit.unwrap_or(50);
    if !(1..=100).contains(&limit) {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "limit must be 1..100".to_owned(),
        ));
    }
    let permit = permit(&library)?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        library.list(query.after, limit)
    })
    .await
    .map_err(|_| worker_error())?
    .map(Json)
    .map_err(|error| api_error(&error))
}

async fn load(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Json<SongBundle>, ApiError> {
    let library = library(&state)?;
    let permit = permit(&library)?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        library.load(id)
    })
    .await
    .map_err(|_| worker_error())?
    .map(Json)
    .map_err(|error| api_error(&error))
}

/// {
///   責務: [`SongEditRequest`: `PATCH /songs/{id}` の本文。曲IDはパスから取り、本文では受け付けない]
///   フィールド: [base_generation(Evidence artifactのgeneration。GET /songs/{id}のsong.artifacts.evidence.generation), ops(編集op配列。sampleは48kHz整数)]
///   制約: [未知フィールドは422。本文は全ルート共通の64KiBまで(超過413)]
/// }
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SongEditRequest {
    base_generation: u64,
    ops: Vec<EditOp>,
}

/// {
///   責務: [`edit`: `PATCH /songs/{id}` 公開済み曲へ手編集を原子的に保存する(#38 38.3)]
///   処理: [ライブラリ取得(未設定/停止中は503) → I/O budget取得(満杯は429) → blockingでSongLibrary::edit_songを実行 → 成否をstatusへ写像]
///   引数: [state, id(パスの曲UUID), request(本文)]
///   戻り値: [200 {song_id, generation, pending} / 404 曲なし / 409 base_generation不一致 / 413 文書サイズ超過 / 422 編集違反 / 429 書込中・budget満杯 / 503]
/// }
async fn edit(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(request): Json<SongEditRequest>,
) -> Result<Json<EditedSong>, ApiError> {
    let library = library(&state)?;
    let permit = permit(&library)?;
    let command = EditCommand {
        song_id: id.to_string(),
        base_generation: request.base_generation,
        ops: request.ops,
    };
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        library.edit_song(id, &command)
    })
    .await
    .map_err(|_| worker_error())?
    .map(Json)
    .map_err(|error| edit_error(&error))
}

/// {
///   責務: [`edit_error`: 編集保存の失敗をHTTP statusとエラー本文へ写像する]
///   処理: [GenerationConflict→409 / Edit(違反)→422 / Library(TooLarge)→413 / Library(その他)→`api_error`と同じ写像]
///   引数: [error]
///   戻り値: [ApiError(status, "song_edit_...: 説明")。内部パス等は含めない]
/// }
fn edit_error(error: &SongEditSaveError) -> ApiError {
    match error {
        SongEditSaveError::GenerationConflict { .. } => {
            ApiError(StatusCode::CONFLICT, error.to_string())
        }
        SongEditSaveError::Edit(_) => ApiError(StatusCode::UNPROCESSABLE_ENTITY, error.to_string()),
        SongEditSaveError::Library(ImportError::TooLarge) => {
            ApiError(StatusCode::PAYLOAD_TOO_LARGE, error.to_string())
        }
        SongEditSaveError::Library(inner) => api_error(inner),
    }
}

fn worker_error() -> ApiError {
    ApiError(
        StatusCode::INTERNAL_SERVER_ERROR,
        "Song library worker failed".to_owned(),
    )
}

fn api_error(error: &ImportError) -> ApiError {
    let status = match error {
        ImportError::NotFound => StatusCode::NOT_FOUND,
        ImportError::Busy => StatusCode::TOO_MANY_REQUESTS,
        ImportError::Conflict => StatusCode::CONFLICT,
        _ => StatusCode::UNPROCESSABLE_ENTITY,
    };
    ApiError(status, error.to_string())
}

#[cfg(test)]
mod tests;
