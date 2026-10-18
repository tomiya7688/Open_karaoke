//! Core Job/API wiring for verified song imports. Clients supply a completed
//! Core Job ID, never an arbitrary filesystem path or replacement SongBundle.

use std::sync::Arc;

use axum::{Json, Router, extract::{Path, Query, State}, http::StatusCode, routing::get};
use serde::Deserialize;
use tokio::sync::{OwnedSemaphorePermit, watch};
use uuid::Uuid;

use crate::{AppState, ApiError, Job, JobStatus, song_data::SongBundle};
use crate::song_library::{ImportControl, ImportError, SongLibrary, SongPage};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SongImportRequest { pub analysis_job_id: Uuid }

pub(crate) struct ImportWork {
    library: SongLibrary,
    source: Job,
    pub control: Arc<ImportControl>,
    permit: OwnedSemaphorePermit,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new().route("/songs", get(list)).route("/songs/{id}", get(load))
}

fn library(state: &AppState) -> Result<SongLibrary, ApiError> {
    state.song_library.clone().ok_or_else(|| ApiError::unavailable("Song library is not configured".to_owned()))
}

fn permit(library: &SongLibrary) -> Result<OwnedSemaphorePermit, ApiError> {
    Arc::clone(&library.budget).try_acquire_owned().map_err(|_| ApiError(
        StatusCode::TOO_MANY_REQUESTS, "Song library I/O budget is busy".to_owned(),
    ))
}

pub(crate) async fn prepare(state: &AppState, request: &SongImportRequest) -> Result<ImportWork, ApiError> {
    let library = library(state)?;
    let source = state.snapshot(request.analysis_job_id).await.ok_or_else(ApiError::not_found)?;
    if source.kind != "analysis.song" || source.status != JobStatus::Completed {
        return Err(ApiError(StatusCode::CONFLICT, "Only a completed analysis.song Job can be imported".to_owned()));
    }
    let permit = permit(&library)?;
    Ok(ImportWork { library, source, control: Arc::new(ImportControl::default()), permit })
}

pub(crate) async fn run(state: &AppState, id: Uuid, work: ImportWork) {
    let (progress, mut updates) = watch::channel((0_u16, "song_import.starting"));
    let mut task = tokio::task::spawn_blocking(move || {
        let _permit = work.permit;
        work.library.import(&work.source, &work.control, |value, stage| {
            progress.send_replace((value, stage));
        })
    });
    let mut progress_open = true;
    let result = loop {
        tokio::select! {
            result = &mut task => break result,
            changed = updates.changed(), if progress_open => {
                if changed.is_err() { progress_open = false; continue; }
                let (progress, stage) = *updates.borrow_and_update();
                state.update(id, |job| {
                    job.progress = f32::from(progress) / 10_000.0;
                    stage.clone_into(&mut job.stage);
                }).await;
            }
        }
    };
    match result {
        Ok(Ok(imported)) => {
            state.update(id, |job| {
                job.progress = 1.0;
                job.imported_song = Some(imported);
            }).await;
            state.finish(id, JobStatus::Completed, None).await;
        },
        Ok(Err(ImportError::Cancelled)) => state.finish(id, JobStatus::Cancelled, None).await,
        Ok(Err(error)) => state.finish(id, JobStatus::Failed, Some(error.to_string())).await,
        Err(_) => state.finish(id, JobStatus::Failed, Some("song_import_worker_failed".to_owned())).await,
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ListQuery { after: Option<Uuid>, limit: Option<usize> }

async fn list(State(state): State<AppState>, Query(query): Query<ListQuery>) -> Result<Json<SongPage>, ApiError> {
    let library = library(&state)?;
    let limit = query.limit.unwrap_or(50);
    if !(1..=100).contains(&limit) {
        return Err(ApiError(StatusCode::BAD_REQUEST, "limit must be 1..100".to_owned()));
    }
    let permit = permit(&library)?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        library.list(query.after, limit)
    }).await.map_err(|_| worker_error())?.map(Json).map_err(api_error)
}

async fn load(State(state): State<AppState>, Path(id): Path<Uuid>) -> Result<Json<SongBundle>, ApiError> {
    let library = library(&state)?;
    let permit = permit(&library)?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        library.load(id)
    }).await.map_err(|_| worker_error())?.map(Json).map_err(api_error)
}

fn worker_error() -> ApiError {
    ApiError(StatusCode::INTERNAL_SERVER_ERROR, "Song library worker failed".to_owned())
}

fn api_error(error: ImportError) -> ApiError {
    let status = match &error {
        ImportError::NotFound => StatusCode::NOT_FOUND,
        ImportError::Busy => StatusCode::TOO_MANY_REQUESTS,
        ImportError::Conflict => StatusCode::CONFLICT,
        _ => StatusCode::UNPROCESSABLE_ENTITY,
    };
    ApiError(status, error.to_string())
}
