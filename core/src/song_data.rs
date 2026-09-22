use std::{
    collections::BTreeMap,
    fmt, fs, io,
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const CURRENT_FORMAT_VERSION: u32 = 1;
pub const INTERNAL_SAMPLE_RATE: u32 = 48_000;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct SongBundle {
    pub song: SongManifest,
    pub lyrics: LyricsDocument,
    pub notes: NotesDocument,
    pub evidence: EvidenceDocument,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct SongManifest {
    pub format_version: u32,
    pub song_id: String,
    pub title: String,
    pub artist: Option<String>,
    pub sample_rate: u32,
    pub duration_samples: u64,
    pub analysis_version: u32,
    pub files: SongFiles,
    #[serde(default)]
    pub artifacts: BTreeMap<ArtifactKind, ArtifactMetadata>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct SongFiles {
    pub original: String,
    pub vocals: String,
    pub accompaniment: String,
    pub lyrics: String,
    pub notes: String,
    pub evidence: String,
}

impl Default for SongFiles {
    fn default() -> Self {
        Self {
            original: "audio/original_48k.wav".to_owned(),
            vocals: "audio/vocals.wav".to_owned(),
            accompaniment: "audio/accompaniment.wav".to_owned(),
            lyrics: "analysis/lyrics.json".to_owned(),
            notes: "analysis/notes.json".to_owned(),
            evidence: "analysis/evidence.json".to_owned(),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Ord, PartialOrd, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ArtifactKind {
    Original,
    Vocals,
    Accompaniment,
    Lyrics,
    Notes,
    Evidence,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ArtifactMetadata {
    pub generation: u64,
    pub valid: bool,
    #[serde(default)]
    pub depends_on: Vec<ArtifactKind>,
    pub analysis_version: u32,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct LyricsDocument {
    pub format_version: u32,
    pub analysis_version: u32,
    pub segments: Vec<LyricSegment>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct LyricSegment {
    pub id: String,
    pub text: String,
    pub start_sample: u64,
    pub end_sample: u64,
    pub text_confidence: Option<f32>,
    pub timing_confidence: Option<f32>,
    pub source: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct NotesDocument {
    pub format_version: u32,
    pub analysis_version: u32,
    pub notes: Vec<Note>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Note {
    pub id: String,
    pub start_sample: u64,
    pub end_sample: u64,
    pub midi_note: u8,
    pub pitch_confidence: Option<f32>,
    pub lyric_segment_id: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct EvidenceDocument {
    pub format_version: u32,
    pub analysis_version: u32,
    pub items: Vec<EvidenceItem>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct EvidenceItem {
    pub target_id: String,
    pub evidence: BTreeMap<String, Value>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DocumentKind {
    Song,
    Lyrics,
    Notes,
    Evidence,
}

#[derive(Debug)]
pub enum SongDataError {
    Io(io::Error),
    Json(serde_json::Error),
    Validation(Vec<String>),
    UnsupportedVersion {
        document: DocumentKind,
        version: u64,
    },
}

impl fmt::Display for SongDataError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "I/O error: {error}"),
            Self::Json(error) => write!(formatter, "JSON error: {error}"),
            Self::Validation(errors) => {
                write!(formatter, "validation failed: {}", errors.join("; "))
            }
            Self::UnsupportedVersion { document, version } => {
                write!(
                    formatter,
                    "unsupported {document:?} format version {version}"
                )
            }
        }
    }
}

impl std::error::Error for SongDataError {}

impl From<io::Error> for SongDataError {
    fn from(value: io::Error) -> Self {
        Self::Io(value)
    }
}

impl From<serde_json::Error> for SongDataError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json(value)
    }
}

impl SongBundle {
    /// Validate the bundle.
    ///
    /// # Errors
    /// Returns collected schema, timeline, path and reference violations.
    pub fn validate(&self) -> Result<(), SongDataError> {
        let mut errors = Vec::new();

        validate_version(self.song.format_version, "song", &mut errors);
        validate_version(self.lyrics.format_version, "lyrics", &mut errors);
        validate_version(self.notes.format_version, "notes", &mut errors);
        validate_version(self.evidence.format_version, "evidence", &mut errors);

        if self.song.sample_rate != INTERNAL_SAMPLE_RATE {
            errors.push(format!(
                "song.sample_rate must be {INTERNAL_SAMPLE_RATE}, got {}",
                self.song.sample_rate
            ));
        }
        if self.song.song_id.trim().is_empty() {
            errors.push("song.song_id must not be empty".to_owned());
        }
        if self.song.title.trim().is_empty() {
            errors.push("song.title must not be empty".to_owned());
        }

        validate_paths(&self.song.files, &mut errors);

        for segment in &self.lyrics.segments {
            validate_span(
                "lyric",
                &segment.id,
                segment.start_sample,
                segment.end_sample,
                self.song.duration_samples,
                &mut errors,
            );
            validate_confidence(
                segment.text_confidence,
                "text_confidence",
                &segment.id,
                &mut errors,
            );
            validate_confidence(
                segment.timing_confidence,
                "timing_confidence",
                &segment.id,
                &mut errors,
            );
        }

        for note in &self.notes.notes {
            validate_span(
                "note",
                &note.id,
                note.start_sample,
                note.end_sample,
                self.song.duration_samples,
                &mut errors,
            );
            if note.midi_note > 127 {
                errors.push(format!(
                    "note {} has invalid MIDI note {}",
                    note.id, note.midi_note
                ));
            }
            validate_confidence(
                note.pitch_confidence,
                "pitch_confidence",
                &note.id,
                &mut errors,
            );

            if let Some(segment_id) = &note.lyric_segment_id
                && !self
                    .lyrics
                    .segments
                    .iter()
                    .any(|segment| &segment.id == segment_id)
            {
                errors.push(format!(
                    "note {} references missing lyric segment {segment_id}",
                    note.id
                ));
            }
        }

        for (kind, metadata) in &self.song.artifacts {
            if metadata.analysis_version > self.song.analysis_version {
                errors.push(format!(
                    "artifact {kind:?} analysis_version {} exceeds song analysis_version {}",
                    metadata.analysis_version, self.song.analysis_version
                ));
            }
        }

        if errors.is_empty() {
            Ok(())
        } else {
            Err(SongDataError::Validation(errors))
        }
    }

    pub fn invalidate(&mut self, kind: ArtifactKind) {
        if let Some(metadata) = self.song.artifacts.get_mut(&kind) {
            metadata.valid = false;
        }

        let dependent = self
            .song
            .artifacts
            .iter()
            .filter_map(|(candidate, metadata)| {
                metadata.depends_on.contains(&kind).then_some(*candidate)
            })
            .collect::<Vec<_>>();

        for candidate in dependent {
            self.invalidate(candidate);
        }
    }

    pub fn mark_regenerated(&mut self, kind: ArtifactKind, analysis_version: u32) {
        let metadata = self.song.artifacts.entry(kind).or_insert(ArtifactMetadata {
            generation: 0,
            valid: true,
            depends_on: Vec::new(),
            analysis_version,
        });
        metadata.generation = metadata.generation.saturating_add(1);
        metadata.valid = true;
        metadata.analysis_version = analysis_version;
        self.song.analysis_version = self.song.analysis_version.max(analysis_version);
    }
}

pub struct SongStore;

impl SongStore {
    /// Persist a validated bundle.
    ///
    /// # Errors
    /// Returns validation, serialization or filesystem errors.
    pub fn save(root: impl AsRef<Path>, bundle: &SongBundle) -> Result<(), SongDataError> {
        bundle.validate()?;
        let root = root.as_ref();
        fs::create_dir_all(root)?;
        fs::create_dir_all(root.join("analysis"))?;

        write_json(root.join("song.json"), &bundle.song)?;
        write_json(root.join(&bundle.song.files.lyrics), &bundle.lyrics)?;
        write_json(root.join(&bundle.song.files.notes), &bundle.notes)?;
        write_json(root.join(&bundle.song.files.evidence), &bundle.evidence)?;
        Ok(())
    }

    /// Load and validate all song documents.
    ///
    /// # Errors
    /// Returns filesystem, JSON, migration or validation errors.
    pub fn load(root: impl AsRef<Path>) -> Result<SongBundle, SongDataError> {
        let root = root.as_ref();
        let song: SongManifest = read_migrated(root.join("song.json"), DocumentKind::Song)?;
        let lyrics: LyricsDocument =
            read_migrated(root.join(&song.files.lyrics), DocumentKind::Lyrics)?;
        let notes: NotesDocument =
            read_migrated(root.join(&song.files.notes), DocumentKind::Notes)?;
        let evidence: EvidenceDocument =
            read_migrated(root.join(&song.files.evidence), DocumentKind::Evidence)?;

        let bundle = SongBundle {
            song,
            lyrics,
            notes,
            evidence,
        };
        bundle.validate()?;
        Ok(bundle)
    }
}

fn read_migrated<T>(path: PathBuf, kind: DocumentKind) -> Result<T, SongDataError>
where
    T: for<'de> Deserialize<'de>,
{
    let bytes = fs::read(path)?;
    let value: Value = serde_json::from_slice(&bytes)?;
    let migrated = migrate_document(kind, value)?;
    Ok(serde_json::from_value(migrated)?)
}

/// Migrate legacy document fields.
///
/// # Errors
/// Returns an error for non-object legacy documents or unsupported versions.
pub fn migrate_document(kind: DocumentKind, mut value: Value) -> Result<Value, SongDataError> {
    let version = value
        .get("format_version")
        .and_then(Value::as_u64)
        .unwrap_or(0);

    match version {
        1 => Ok(value),
        0 => {
            let object = value.as_object_mut().ok_or_else(|| {
                SongDataError::Validation(vec!["document root must be a JSON object".to_owned()])
            })?;
            object.insert(
                "format_version".to_owned(),
                Value::from(CURRENT_FORMAT_VERSION),
            );

            if matches!(kind, DocumentKind::Song) {
                object
                    .entry("analysis_version".to_owned())
                    .or_insert(Value::from(1));
                object
                    .entry("artifacts".to_owned())
                    .or_insert_with(|| Value::Object(Default::default()));

                if let Some(files) = object.get_mut("files").and_then(Value::as_object_mut) {
                    files
                        .entry("evidence".to_owned())
                        .or_insert(Value::from("analysis/evidence.json"));
                }
            } else {
                object
                    .entry("analysis_version".to_owned())
                    .or_insert(Value::from(1));
            }

            if matches!(kind, DocumentKind::Evidence) {
                object
                    .entry("items".to_owned())
                    .or_insert_with(|| Value::Array(Vec::new()));
            }

            Ok(value)
        }
        other => Err(SongDataError::UnsupportedVersion {
            document: kind,
            version: other,
        }),
    }
}

fn write_json(path: PathBuf, value: &impl Serialize) -> Result<(), SongDataError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut bytes = serde_json::to_vec_pretty(value)?;
    bytes.push(b'\n');
    let temporary = path.with_extension("tmp");
    fs::write(&temporary, bytes)?;
    if path.exists() {
        fs::remove_file(&path)?;
    }
    fs::rename(temporary, path)?;
    Ok(())
}

fn validate_version(version: u32, name: &str, errors: &mut Vec<String>) {
    if version != CURRENT_FORMAT_VERSION {
        errors.push(format!(
            "{name}.format_version must be {CURRENT_FORMAT_VERSION}, got {version}"
        ));
    }
}

fn validate_span(
    kind: &str,
    id: &str,
    start: u64,
    end: u64,
    duration: u64,
    errors: &mut Vec<String>,
) {
    if id.trim().is_empty() {
        errors.push(format!("{kind} id must not be empty"));
    }
    if start >= end {
        errors.push(format!("{kind} {id} must have start_sample < end_sample"));
    }
    if end > duration {
        errors.push(format!(
            "{kind} {id} ends at {end}, beyond song duration {duration}"
        ));
    }
}

fn validate_confidence(value: Option<f32>, field: &str, id: &str, errors: &mut Vec<String>) {
    if value.is_some_and(|confidence| !(0.0..=1.0).contains(&confidence)) {
        errors.push(format!("{field} for {id} must be between 0 and 1"));
    }
}

fn validate_paths(files: &SongFiles, errors: &mut Vec<String>) {
    for (name, path) in [
        ("original", &files.original),
        ("vocals", &files.vocals),
        ("accompaniment", &files.accompaniment),
        ("lyrics", &files.lyrics),
        ("notes", &files.notes),
        ("evidence", &files.evidence),
    ] {
        let path = Path::new(path);
        if path.is_absolute()
            || path
                .components()
                .any(|component| matches!(component, std::path::Component::ParentDir))
        {
            errors.push(format!(
                "files.{name} must be a relative path without parent traversal"
            ));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn fixture() -> SongBundle {
        let mut artifacts = BTreeMap::new();
        artifacts.insert(
            ArtifactKind::Lyrics,
            ArtifactMetadata {
                generation: 1,
                valid: true,
                depends_on: vec![ArtifactKind::Vocals],
                analysis_version: 3,
            },
        );
        artifacts.insert(
            ArtifactKind::Notes,
            ArtifactMetadata {
                generation: 1,
                valid: true,
                depends_on: vec![ArtifactKind::Lyrics],
                analysis_version: 3,
            },
        );

        SongBundle {
            song: SongManifest {
                format_version: 1,
                song_id: "fixture-song".to_owned(),
                title: "Fixture Song".to_owned(),
                artist: Some("Fixture Artist".to_owned()),
                sample_rate: 48_000,
                duration_samples: 192_000,
                analysis_version: 3,
                files: SongFiles::default(),
                artifacts,
            },
            lyrics: LyricsDocument {
                format_version: 1,
                analysis_version: 3,
                segments: vec![LyricSegment {
                    id: "lyric-0001".to_owned(),
                    text: "hello".to_owned(),
                    start_sample: 48_000,
                    end_sample: 96_000,
                    text_confidence: Some(0.95),
                    timing_confidence: Some(0.9),
                    source: Some("fixture".to_owned()),
                }],
            },
            notes: NotesDocument {
                format_version: 1,
                analysis_version: 3,
                notes: vec![Note {
                    id: "note-0001".to_owned(),
                    start_sample: 48_000,
                    end_sample: 72_000,
                    midi_note: 64,
                    pitch_confidence: Some(0.92),
                    lyric_segment_id: Some("lyric-0001".to_owned()),
                }],
            },
            evidence: EvidenceDocument {
                format_version: 1,
                analysis_version: 3,
                items: Vec::new(),
            },
        }
    }

    #[test]
    fn round_trip_serialization_and_persistence() {
        let directory = tempdir().unwrap();
        let expected = fixture();
        SongStore::save(directory.path(), &expected).unwrap();
        let actual = SongStore::load(directory.path()).unwrap();
        assert_eq!(actual, expected);
    }

    #[test]
    fn validation_rejects_non_48khz_and_invalid_spans() {
        let mut bundle = fixture();
        bundle.song.sample_rate = 44_100;
        bundle.notes.notes[0].end_sample = bundle.notes.notes[0].start_sample;
        let error = bundle.validate().unwrap_err().to_string();
        assert!(error.contains("48000"));
        assert!(error.contains("start_sample < end_sample"));
    }

    #[test]
    fn migration_adds_v1_fields() {
        let migrated = migrate_document(
            DocumentKind::Lyrics,
            serde_json::json!({
                "segments": []
            }),
        )
        .unwrap();
        assert_eq!(migrated["format_version"], 1);
        assert_eq!(migrated["analysis_version"], 1);
    }

    #[test]
    fn unsupported_version_is_rejected() {
        let error = migrate_document(
            DocumentKind::Notes,
            serde_json::json!({
                "format_version": 99,
                "analysis_version": 1,
                "notes": []
            }),
        )
        .unwrap_err();
        assert!(matches!(
            error,
            SongDataError::UnsupportedVersion { version: 99, .. }
        ));
    }

    #[test]
    fn invalidation_propagates_to_dependents_only() {
        let mut bundle = fixture();
        bundle.invalidate(ArtifactKind::Lyrics);
        assert!(!bundle.song.artifacts[&ArtifactKind::Lyrics].valid);
        assert!(!bundle.song.artifacts[&ArtifactKind::Notes].valid);
    }

    #[test]
    fn regeneration_increments_generation() {
        let mut bundle = fixture();
        bundle.mark_regenerated(ArtifactKind::Lyrics, 4);
        let metadata = &bundle.song.artifacts[&ArtifactKind::Lyrics];
        assert_eq!(metadata.generation, 2);
        assert!(metadata.valid);
        assert_eq!(metadata.analysis_version, 4);
        assert_eq!(bundle.song.analysis_version, 4);
    }

    #[test]
    fn fixture_json_is_deterministic() {
        let song = serde_json::to_string_pretty(&fixture().song).unwrap();
        assert_eq!(song, include_str!("../tests/fixtures/song.json").trim_end());
    }
}
