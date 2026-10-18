use std::collections::{BTreeMap, HashSet};

use serde::Deserialize;
use uuid::Uuid;

use crate::{Job, JobStatus, song_data::{ArtifactKind, SongBundle, SongFiles}};
use super::{Asset, ImportError, MAX_ASSET, MAX_ASSETS, MAX_TOTAL, files::relative};

const ROLES: [&str; 6] = ["stems", "lyrics", "alignment", "pitch", "events", "notes"];
const REQUIRED: [&str; 13] = [
    "source/original_48k.wav", "stages/stems/vocals.wav", "stages/stems/accompaniment.wav",
    "stages/stems/stems.json", "stages/lyrics/lyrics.json", "stages/lyrics/asr_raw.json",
    "stages/alignment/lyrics.json", "stages/alignment/alignment.json",
    "stages/alignment/alignment_evidence.json", "stages/pitch/pitch.json",
    "stages/events/vocal_events.json", "stages/notes/notes.json", "stages/notes/note_evidence.json",
];
const AUDIO: [(&str, &str); 3] = [
    ("source/original_48k.wav", "audio/original_48k.wav"),
    ("stages/stems/vocals.wav", "audio/vocals.wav"),
    ("stages/stems/accompaniment.wav", "audio/accompaniment.wav"),
];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Export {
    pub format_version: u32,
    pub bundle: SongBundle,
    pub assets: Vec<Asset>,
}

pub(super) fn source_result(source: &Job) -> Result<String, ImportError> {
    if source.kind != "analysis.song" || source.status != JobStatus::Completed || source.id.is_nil() {
        return Err(ImportError::Invalid("a completed analysis.song Job is required"));
    }
    if source.artifacts.len() > MAX_ASSETS || source.artifacts.iter().collect::<HashSet<_>>().len() != source.artifacts.len() {
        return Err(ImportError::Invalid("invalid published artifact set"));
    }
    let candidates = source.artifacts.iter().filter(|name| name.ends_with("/song-result.json")).collect::<Vec<_>>();
    if candidates.len() != 1 { return Err(ImportError::Invalid("exactly one song result is required")); }
    relative(candidates[0])?;
    let parts = candidates[0].split('/').collect::<Vec<_>>();
    if parts.len() != 4 || parts[0] != "jobs" || parts[3] != "song-result.json" {
        return Err(ImportError::Invalid("song result is not Job-owned"));
    }
    canonical_uuid(parts[1])?;
    canonical_uuid(parts[2])?;
    Ok(candidates[0].clone())
}

fn canonical_uuid(value: &str) -> Result<Uuid, ImportError> {
    let id = Uuid::parse_str(value).map_err(|_| ImportError::Invalid("invalid stable song identity"))?;
    if id.is_nil() || id.to_string() != value {
        return Err(ImportError::Invalid("noncanonical song identity"));
    }
    Ok(id)
}

impl Export {
    pub(super) fn validate(&self, source: &Job, result: &str) -> Result<Uuid, ImportError> {
        if self.format_version != 1 || self.assets.is_empty() || self.assets.len() > MAX_ASSETS {
            return Err(ImportError::Invalid("unsupported or oversized song export"));
        }
        let prefix = result.strip_suffix("song-result.json").ok_or(ImportError::Invalid("invalid result"))?;
        let id = canonical_uuid(&self.bundle.song.song_id)?;
        if !prefix.ends_with(&format!("/{id}/")) {
            return Err(ImportError::Invalid("song identity differs from owning worker Job"));
        }
        validate_bundle(&self.bundle, id)?;
        self.validate_assets(source, result, prefix)?;
        self.validate_provenance(prefix)?;
        Ok(id)
    }

    fn validate_assets(&self, source: &Job, result: &str, prefix: &str) -> Result<(), ImportError> {
        let allowed = source.artifacts.iter().map(String::as_str).collect::<HashSet<_>>();
        let mut destinations = HashSet::new();
        let mut provenance = HashSet::new();
        let mut descriptors = BTreeMap::new();
        let mut total = 0_u64;
        for asset in &self.assets {
            relative(&asset.source)?;
            relative(&asset.destination)?;
            if !allowed.contains(asset.source.as_str()) || asset.source == result {
                return Err(ImportError::Invalid("unpublished asset reference"));
            }
            let suffix = asset.source.strip_prefix(prefix).ok_or(ImportError::Invalid("asset belongs to another Job"))?;
            valid_suffix(suffix)?;
            if !destinations.insert(asset.destination.to_ascii_lowercase()) {
                return Err(ImportError::Invalid("duplicate/case-colliding asset destination"));
            }
            if asset.destination == format!("provenance/{suffix}") {
                provenance.insert(asset.source.as_str());
            } else if !AUDIO.iter().any(|(input, output)| suffix == *input && asset.destination == *output) {
                return Err(ImportError::Invalid("asset destination may overwrite metadata"));
            }
            if !is_digest(&asset.sha256) || asset.bytes > MAX_ASSET {
                return Err(ImportError::Invalid("invalid checksum or asset size"));
            }
            total = total.checked_add(asset.bytes).ok_or(ImportError::TooLarge)?;
            if total > MAX_TOTAL { return Err(ImportError::TooLarge); }
            if let Some(previous) = descriptors.insert(asset.source.as_str(), (&asset.sha256, asset.bytes))
                && previous != (&asset.sha256, asset.bytes)
            { return Err(ImportError::Invalid("one source has inconsistent descriptors")); }
        }
        if provenance.len() + 1 != allowed.len()
            || REQUIRED.iter().any(|suffix| !provenance.contains(format!("{prefix}{suffix}").as_str()))
            || AUDIO.iter().any(|(_, output)| !destinations.contains(*output))
        { return Err(ImportError::Invalid("missing audio or stage provenance")); }
        Ok(())
    }

    fn validate_provenance(&self, prefix: &str) -> Result<(), ImportError> {
        let song_evidence = self.bundle.evidence.items.iter().find(|item| item.target_id == "song")
            .ok_or(ImportError::Invalid("song provenance missing"))?;
        for (key, suffix) in [("source_sha256", AUDIO[0].0), ("vocals_sha256", AUDIO[1].0)] {
            let source = format!("{prefix}{suffix}");
            let asset = self.assets.iter().find(|asset| asset.source == source)
                .ok_or(ImportError::Invalid("audio descriptor missing"))?;
            if song_evidence.evidence.get(key).and_then(serde_json::Value::as_str) != Some(&asset.sha256) {
                return Err(ImportError::Invalid("provenance audio hash mismatch"));
            }
        }
        let stages = song_evidence.evidence.get("stages").and_then(serde_json::Value::as_array)
            .ok_or(ImportError::Invalid("stage provenance missing"))?;
        if stages.len() != ROLES.len() { return Err(ImportError::Invalid("stage provenance incomplete")); }
        for (stage, role) in stages.iter().zip(ROLES) {
            if stage.get("role").and_then(serde_json::Value::as_str) != Some(role)
                || stage["model"]["role"].as_str() != Some(role)
                || stage["model"]["id"].as_str().is_none_or(str::is_empty)
                || stage["model"]["version"].as_str().is_none_or(str::is_empty)
            { return Err(ImportError::Invalid("stage model provenance inconsistent")); }
        }
        Ok(())
    }
}

fn valid_suffix(suffix: &str) -> Result<(), ImportError> {
    if matches!(suffix, "source/original_48k.wav" | "source/reference.txt") { return Ok(()); }
    let parts = suffix.split('/').collect::<Vec<_>>();
    if parts.len() != 3 || parts[0] != "stages" || !ROLES.contains(&parts[1])
        || !(parts[2].ends_with(".json") || parts[2].ends_with(".wav"))
    { return Err(ImportError::Invalid("unexpected stage asset path")); }
    Ok(())
}

fn is_digest(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(super) fn validate_bundle(bundle: &SongBundle, id: Uuid) -> Result<(), ImportError> {
    if bundle.song.song_id != id.to_string() || bundle.song.files != SongFiles::default()
        || bundle.song.duration_samples == 0 || bundle.song.duration_samples > 30 * 60 * 48_000
        || bundle.song.analysis_version == 0 || bundle.song.title.chars().count() > 1024
        || bundle.song.artist.as_ref().is_some_and(|name| name.chars().count() > 1024)
        || [bundle.lyrics.analysis_version, bundle.notes.analysis_version, bundle.evidence.analysis_version]
            .iter().any(|version| *version != bundle.song.analysis_version)
    { return Err(ImportError::Invalid("noncanonical bundle identity, files or versions")); }
    let mut ids = HashSet::from(["song"]);
    for id in bundle.lyrics.segments.iter().map(|row| row.id.as_str())
        .chain(bundle.notes.notes.iter().map(|row| row.id.as_str()))
    {
        if id.trim().is_empty() || !ids.insert(id) {
            return Err(ImportError::Invalid("empty or duplicate canonical ID"));
        }
    }
    if bundle.evidence.items.iter().any(|item| !ids.contains(item.target_id.as_str())) {
        return Err(ImportError::Invalid("evidence refers to a missing target"));
    }
    use ArtifactKind::{Accompaniment, Evidence, Lyrics, Notes, Original, Vocals};
    let dependencies: [(ArtifactKind, &[ArtifactKind]); 6] = [
        (Original, &[]), (Vocals, &[Original]), (Accompaniment, &[Original]),
        (Lyrics, &[Vocals]), (Notes, &[Vocals, Lyrics]), (Evidence, &[Lyrics, Notes]),
    ];
    if bundle.song.artifacts.len() != dependencies.len() {
        return Err(ImportError::Invalid("artifact dependency graph is incomplete"));
    }
    for (kind, depends_on) in dependencies {
        let metadata = bundle.song.artifacts.get(&kind).ok_or(ImportError::Invalid("missing artifact state"))?;
        if !metadata.valid || metadata.generation == 0 || metadata.analysis_version != bundle.song.analysis_version
            || metadata.depends_on.as_slice() != depends_on
        { return Err(ImportError::Invalid("stale or inconsistent artifact dependency graph")); }
    }
    bundle.validate().map_err(|_| ImportError::Invalid("SongBundle validation failed"))
}
