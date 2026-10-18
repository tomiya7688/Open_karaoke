//! Verified, audio-inclusive SongStore publication. All methods perform filesystem
//! work and belong on blocking workers, never on the realtime audio thread.

mod files;
mod validation;

use std::{
    fs::{self, File, TryLockError},
    io,
    path::{Path, PathBuf},
    sync::{Arc, atomic::{AtomicU8, Ordering}},
};

use serde::{Deserialize, Serialize};
use tokio::sync::Semaphore;
use uuid::Uuid;

use crate::{Job, song_data::{SongBundle, SongStore}};
use files::{bounded_json, check_directory, digest, under};
use validation::{Export, source_result, validate_bundle};

const MAX_JSON: u64 = 16 * 1024 * 1024;
const MAX_ASSET: u64 = 30 * 60 * 48_000 * 8 + 1_048_576;
const MAX_TOTAL: u64 = 8 * 1024 * 1024 * 1024;
const MAX_ASSETS: usize = 256;
const DOCUMENTS: [&str; 4] = [
    "song.json", "analysis/lyrics.json", "analysis/notes.json", "analysis/evidence.json",
];

#[derive(Debug)]
pub enum ImportError {
    Invalid(&'static str),
    TooLarge,
    NotFound,
    Conflict,
    Busy,
    Cancelled,
    Io(io::Error),
    Json(serde_json::Error),
}

impl std::fmt::Display for ImportError {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid(reason) => write!(out, "song_import_invalid: {reason}"),
            Self::TooLarge => out.write_str("song_import_limit: size or catalog limit exceeded"),
            Self::NotFound => out.write_str("song_not_found: published song is absent"),
            Self::Conflict => out.write_str("song_import_conflict: existing song differs; never overwritten"),
            Self::Busy => out.write_str("song_library_busy: another operation holds the library budget"),
            Self::Cancelled => out.write_str("song_import_cancelled"),
            Self::Io(_) => out.write_str("song_import_io: filesystem operation failed"),
            Self::Json(_) => out.write_str("song_import_json: invalid or unsupported document"),
        }
    }
}
impl std::error::Error for ImportError {}
impl From<io::Error> for ImportError {
    fn from(error: io::Error) -> Self { Self::Io(error) }
}
impl From<serde_json::Error> for ImportError {
    fn from(error: serde_json::Error) -> Self { Self::Json(error) }
}

/// Cancellation and publication have one atomic ordering point. Once finalization
/// is claimed, cancellation is no longer accepted; the Job then completes or fails.
#[derive(Default)]
pub struct ImportControl(AtomicU8);
impl ImportControl {
    #[must_use]
    pub fn cancel(&self) -> bool {
        matches!(self.0.compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire), Ok(_) | Err(1))
    }
    /// # Errors
    /// Returns Cancelled if cancellation won before finalization.
    pub fn checkpoint(&self) -> Result<(), ImportError> {
        if self.0.load(Ordering::Acquire) == 1 { Err(ImportError::Cancelled) } else { Ok(()) }
    }
    fn finalize(&self) -> Result<(), ImportError> {
        self.0.compare_exchange(0, 2, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| ())
            .map_err(|_| ImportError::Cancelled)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ImportedSong {
    pub song_id: Uuid,
    pub reused: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(super) struct Asset {
    pub source: String,
    pub destination: String,
    pub sha256: String,
    pub bytes: u64,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    format_version: u32,
    song_id: Uuid,
    analysis_job_id: Uuid,
    result_artifact: String,
    result_sha256: String,
    bundle_sha256: String,
    assets: Vec<Asset>,
}

#[derive(Serialize)]
pub struct SongListItem {
    pub song_id: Uuid,
    pub title: Option<String>,
    pub artist: Option<String>,
    pub duration_samples: Option<u64>,
    pub error: Option<String>,
}

#[derive(Serialize)]
pub struct SongPage {
    pub songs: Vec<SongListItem>,
    pub next_after: Option<Uuid>,
}

#[derive(Clone)]
pub struct SongLibrary {
    artifact_root: Arc<PathBuf>,
    root: Arc<PathBuf>,
    pub(crate) budget: Arc<Semaphore>,
}

impl SongLibrary {
    /// Open disjoint, administrator-configured artifact and song directories.
    /// # Errors
    /// Rejects symlinks/reparse points, overlapping roots and filesystem failures.
    pub fn open(artifact_root: impl AsRef<Path>, root: impl AsRef<Path>) -> Result<Self, ImportError> {
        let artifact_root = files::make_root(artifact_root.as_ref())?;
        let root = files::make_root(root.as_ref())?;
        if artifact_root.starts_with(&root) || root.starts_with(&artifact_root) {
            return Err(ImportError::Invalid("library and artifact roots must be disjoint"));
        }
        Ok(Self {
            artifact_root: Arc::new(artifact_root),
            root: Arc::new(root),
            budget: Arc::new(Semaphore::new(4)),
        })
    }

    /// Validate/copy all owned assets into private staging, then publish by one rename.
    /// # Errors
    /// Rejects incomplete/unowned exports, corrupt audio, hashes, paths, metadata,
    /// cancellation, concurrent writers and conflicting existing registrations.
    pub fn import(
        &self,
        source: &Job,
        control: &ImportControl,
        mut report: impl FnMut(u16, &'static str),
    ) -> Result<ImportedSong, ImportError> {
        control.checkpoint()?;
        check_directory(&self.artifact_root)?;
        check_directory(&self.root)?;
        let _lock = self.writer_lock()?;
        let result_artifact = source_result(source)?;
        let bytes = files::bounded_bytes(&under(&self.artifact_root, &result_artifact)?, MAX_JSON)?;
        let result_sha256 = digest(&bytes);
        let export: Export = serde_json::from_slice(&bytes)?;
        let song_id = export.validate(source, &result_artifact)?;
        let final_path = under(&self.root, &song_id.to_string())?;
        if final_path.try_exists()? {
            self.verify_reuse(source, &export, &result_sha256, song_id, control, &mut report)?;
            control.finalize()?;
            return Ok(ImportedSong { song_id, reused: true });
        }
        report(0, "song_import.staging");
        let staging = Staging::new(&self.root)?;
        let total = export.assets.iter().map(|asset| asset.bytes).sum::<u64>();
        let mut copied = 0;
        for asset in &export.assets {
            let input = under(&self.artifact_root, &asset.source)?;
            let output = under(&staging.path, &asset.destination)?;
            files::copy_verified(&input, &output, asset, control, |count| {
                copied += count;
                report(progress(copied, total), "song_import.copying");
            })?;
        }
        report(9200, "song_import.validating");
        files::verify_content(&staging.path, &export.bundle, control)?;
        control.checkpoint()?;
        SongStore::save(&staging.path, &export.bundle)
            .map_err(|_| ImportError::Invalid("SongStore serialization failed"))?;
        for document in DOCUMENTS {
            File::open(under(&staging.path, document)?)?.sync_all()?;
        }
        let receipt = Receipt {
            format_version: 1,
            song_id,
            analysis_job_id: source.id,
            result_artifact,
            result_sha256,
            bundle_sha256: digest(&serde_json::to_vec(&export.bundle)?),
            assets: export.assets,
        };
        files::write_new_json(&staging.path.join("import-receipt.json"), &receipt)?;
        if self.load_at(&staging.path, song_id)?.0 != export.bundle {
            return Err(ImportError::Invalid("stored metadata did not round-trip"));
        }
        report(9900, "song_import.publishing");
        control.finalize()?;
        // The OS file lock serializes trusted Core processes. The destination is
        // immutable: retries never replace it, even if it is damaged or empty.
        if final_path.try_exists()? { return Err(ImportError::Conflict); }
        fs::rename(&staging.path, final_path)?;
        Ok(ImportedSong { song_id, reused: false })
    }

    /// Read a published bundle, not a private staging directory.
    /// Metadata is checked on every read; full audio checksums are checked at import/retry.
    /// # Errors
    /// Rejects absent, malformed or incomplete published entries.
    pub fn load(&self, id: Uuid) -> Result<SongBundle, ImportError> {
        check_directory(&self.root)?;
        let path = under(&self.root, &id.to_string())?;
        if !path.try_exists()? { return Err(ImportError::NotFound); }
        Ok(self.load_at(&path, id)?.0)
    }

    /// Sorted cursor pagination. Damaged entries are reported, never silently hidden.
    /// # Errors
    /// Rejects invalid limits, excessive catalog size and filesystem errors.
    pub fn list(&self, after: Option<Uuid>, limit: usize) -> Result<SongPage, ImportError> {
        if !(1..=100).contains(&limit) { return Err(ImportError::Invalid("limit must be 1..100")); }
        check_directory(&self.root)?;
        let mut ids = Vec::new();
        for (count, entry) in fs::read_dir(&*self.root)?.enumerate() {
            if count >= 10_000 { return Err(ImportError::TooLarge); }
            let name = entry?.file_name();
            let Some(name) = name.to_str() else { continue; };
            if let Ok(id) = Uuid::parse_str(name)
                && id.to_string() == name && after.is_none_or(|after| id > after)
            { ids.push(id); }
        }
        ids.sort_unstable();
        let next_after = (ids.len() > limit).then(|| ids[limit - 1]);
        let songs = ids.into_iter().take(limit).map(|song_id| {
            match self.load(song_id) {
                Ok(bundle) => SongListItem {
                    song_id, title: Some(bundle.song.title), artist: bundle.song.artist,
                    duration_samples: Some(bundle.song.duration_samples), error: None,
                },
                Err(error) => SongListItem {
                    song_id, title: None, artist: None, duration_samples: None,
                    error: Some(error.to_string()),
                },
            }
        }).collect();
        Ok(SongPage { songs, next_after })
    }

    fn writer_lock(&self) -> Result<File, ImportError> {
        let path = under(&self.root, ".writer.lock")?;
        if path.try_exists()? { files::regular_file(&path)?; }
        let file = File::options().read(true).write(true).create(true).truncate(false).open(path)?;
        match file.try_lock() {
            Ok(()) => Ok(file),
            Err(TryLockError::WouldBlock) => Err(ImportError::Busy),
            Err(TryLockError::Error(error)) => Err(error.into()),
        }
    }

    fn load_at(&self, path: &Path, id: Uuid) -> Result<(SongBundle, Receipt), ImportError> {
        check_directory(path)?;
        let receipt: Receipt = bounded_json(&under(path, "import-receipt.json")?, MAX_JSON)?;
        let bundle = SongBundle {
            song: bounded_json(&under(path, DOCUMENTS[0])?, MAX_JSON)?,
            lyrics: bounded_json(&under(path, DOCUMENTS[1])?, MAX_JSON)?,
            notes: bounded_json(&under(path, DOCUMENTS[2])?, MAX_JSON)?,
            evidence: bounded_json(&under(path, DOCUMENTS[3])?, MAX_JSON)?,
        };
        validate_bundle(&bundle, id)?;
        if receipt.format_version != 1 || receipt.song_id != id
            || receipt.assets.len() > MAX_ASSETS
            || digest(&serde_json::to_vec(&bundle)?) != receipt.bundle_sha256
        { return Err(ImportError::Invalid("published receipt/metadata mismatch")); }
        for name in [&bundle.song.files.original, &bundle.song.files.vocals, &bundle.song.files.accompaniment] {
            let asset = receipt.assets.iter().find(|asset| &asset.destination == name)
                .ok_or(ImportError::Invalid("published audio receipt missing"))?;
            if files::regular_file(&under(path, name)?)?.len() != asset.bytes {
                return Err(ImportError::Invalid("published audio missing or truncated"));
            }
        }
        Ok((bundle, receipt))
    }

    fn verify_reuse(
        &self, source: &Job, export: &Export, hash: &str, id: Uuid,
        control: &ImportControl, report: &mut impl FnMut(u16, &'static str),
    ) -> Result<(), ImportError> {
        let path = under(&self.root, &id.to_string())?;
        let (bundle, receipt) = self.load_at(&path, id).map_err(|_| ImportError::Conflict)?;
        if receipt.analysis_job_id != source.id || receipt.result_sha256 != hash
            || receipt.assets != export.assets || bundle != export.bundle
        { return Err(ImportError::Conflict); }
        report(0, "song_import.verifying_existing");
        let total = export.assets.iter().map(|asset| asset.bytes).sum::<u64>();
        let mut read = 0;
        for asset in &export.assets {
            files::check_hash(&under(&path, &asset.destination)?, asset, control, |count| {
                read += count;
                report(progress(read, total), "song_import.verifying_existing");
            })?;
        }
        files::verify_content(&path, &bundle, control)
    }
}

fn progress(done: u64, total: u64) -> u16 {
    u16::try_from(done.saturating_mul(9000) / total.max(1)).unwrap_or(9000).min(9000)
}

struct Staging { path: PathBuf }
impl Staging {
    fn new(root: &Path) -> Result<Self, ImportError> {
        let path = root.join(format!(".staging-{}", Uuid::new_v4()));
        fs::create_dir(&path)?;
        Ok(Self { path })
    }
}
impl Drop for Staging {
    fn drop(&mut self) {
        // Only the unpredictable directory this transaction created. A successful
        // rename makes it absent. Crash leftovers stay hidden for explicit GC.
        if let Err(error) = fs::remove_dir_all(&self.path)
            && error.kind() != io::ErrorKind::NotFound
        { eprintln!("song_import_cleanup_failed: private staging may remain"); }
    }
}
