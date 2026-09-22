use std::{
    ffi::OsStr,
    fmt, fs, io,
    path::{Path, PathBuf},
    process::{Command, Output},
};

use serde_json::Value;

use crate::song_data::{INTERNAL_SAMPLE_RATE, SongManifest};

pub const INTERNAL_CHANNELS: u32 = 2;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedAudioMetadata {
    pub sample_rate: u32,
    pub channels: u32,
    pub duration_samples: u64,
}

#[derive(Debug)]
pub enum AudioImportError {
    Io(io::Error),
    UnsupportedInput(String),
    ToolFailure { tool: String, stderr: String },
    LicensePolicy(String),
    InvalidProbe(String),
}

impl fmt::Display for AudioImportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "I/O error: {error}"),
            Self::UnsupportedInput(extension) => {
                write!(formatter, "unsupported input extension: {extension}")
            }
            Self::ToolFailure { tool, stderr } => {
                write!(formatter, "{tool} failed: {}", stderr.trim())
            }
            Self::LicensePolicy(message) => write!(formatter, "FFmpeg license policy: {message}"),
            Self::InvalidProbe(message) => write!(formatter, "invalid ffprobe output: {message}"),
        }
    }
}

impl std::error::Error for AudioImportError {}

impl From<io::Error> for AudioImportError {
    fn from(value: io::Error) -> Self {
        Self::Io(value)
    }
}

/// Normalize supported input to the internal WAV format.
///
/// # Errors
/// Returns input, distribution-policy, process, probe or filesystem errors.
pub fn normalize_audio(
    ffmpeg: impl AsRef<OsStr>,
    ffprobe: impl AsRef<OsStr>,
    input: impl AsRef<Path>,
    output: impl AsRef<Path>,
) -> Result<NormalizedAudioMetadata, AudioImportError> {
    let input = input.as_ref();
    let output = output.as_ref();

    ensure_supported_input(input)?;
    verify_ffmpeg_distribution(ffmpeg.as_ref())?;

    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }

    let temporary = temporary_wav_path(output);
    let args = ffmpeg_arguments(input, &temporary);
    let result = Command::new(ffmpeg.as_ref()).args(&args).output()?;
    ensure_success("ffmpeg", &result)?;

    let metadata = probe_audio(ffprobe.as_ref(), &temporary)?;
    if metadata.sample_rate != INTERNAL_SAMPLE_RATE {
        return Err(AudioImportError::InvalidProbe(format!(
            "expected sample rate {INTERNAL_SAMPLE_RATE}, got {}",
            metadata.sample_rate
        )));
    }
    if metadata.channels != INTERNAL_CHANNELS {
        return Err(AudioImportError::InvalidProbe(format!(
            "expected {INTERNAL_CHANNELS} channels, got {}",
            metadata.channels
        )));
    }

    if output.exists() {
        fs::remove_file(output)?;
    }
    fs::rename(temporary, output)?;

    Ok(metadata)
}

pub fn apply_metadata(
    manifest: &mut SongManifest,
    relative_output_path: impl Into<String>,
    metadata: &NormalizedAudioMetadata,
) {
    manifest.sample_rate = metadata.sample_rate;
    manifest.duration_samples = metadata.duration_samples;
    manifest.files.original = relative_output_path.into();
}

/// Inspect the configured `FFmpeg` executable.
///
/// # Errors
/// Returns process failures or violations of the selected distribution policy.
pub fn verify_ffmpeg_distribution(ffmpeg: &OsStr) -> Result<(), AudioImportError> {
    let result = Command::new(ffmpeg).arg("-buildconf").output()?;
    ensure_success("ffmpeg -buildconf", &result)?;
    let text = format!(
        "{}\n{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
    validate_ffmpeg_build_configuration(&text)
}

/// Validate the selected `FFmpeg` build policy.
///
/// # Errors
/// Rejects GPL-enabled and nonfree-enabled configurations under this policy.
pub fn validate_ffmpeg_build_configuration(configuration: &str) -> Result<(), AudioImportError> {
    let lowered = configuration.to_ascii_lowercase();

    if lowered.contains("--enable-nonfree") {
        return Err(AudioImportError::LicensePolicy(
            "--enable-nonfree builds are not redistributable".to_owned(),
        ));
    }
    if lowered.contains("--enable-gpl") {
        return Err(AudioImportError::LicensePolicy(
            "--enable-gpl builds are outside the selected LGPL distribution policy".to_owned(),
        ));
    }

    Ok(())
}

#[must_use]
pub fn ffmpeg_arguments(input: &Path, output: &Path) -> Vec<String> {
    vec![
        "-nostdin".to_owned(),
        "-hide_banner".to_owned(),
        "-loglevel".to_owned(),
        "error".to_owned(),
        "-y".to_owned(),
        "-i".to_owned(),
        input.to_string_lossy().into_owned(),
        "-map".to_owned(),
        "0:a:0".to_owned(),
        "-vn".to_owned(),
        "-ar".to_owned(),
        INTERNAL_SAMPLE_RATE.to_string(),
        "-ac".to_owned(),
        INTERNAL_CHANNELS.to_string(),
        "-c:a".to_owned(),
        "pcm_f32le".to_owned(),
        "-map_metadata".to_owned(),
        "-1".to_owned(),
        "-fflags".to_owned(),
        "+bitexact".to_owned(),
        "-flags:a".to_owned(),
        "+bitexact".to_owned(),
        output.to_string_lossy().into_owned(),
    ]
}

/// Probe audio stream metadata.
///
/// # Errors
/// Returns process failures or invalid probe output.
pub fn probe_audio(
    ffprobe: &OsStr,
    input: &Path,
) -> Result<NormalizedAudioMetadata, AudioImportError> {
    let result = Command::new(ffprobe)
        .args([
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels,duration_ts",
            "-of",
            "json",
        ])
        .arg(input)
        .output()?;

    ensure_success("ffprobe", &result)?;
    parse_probe_json(&result.stdout)
}

/// Decode required integer stream metadata.
///
/// # Errors
/// Rejects malformed JSON, missing fields and out-of-range integer values.
pub fn parse_probe_json(bytes: &[u8]) -> Result<NormalizedAudioMetadata, AudioImportError> {
    let value: Value = serde_json::from_slice(bytes)
        .map_err(|error| AudioImportError::InvalidProbe(error.to_string()))?;
    let stream = value
        .get("streams")
        .and_then(Value::as_array)
        .and_then(|streams| streams.first())
        .ok_or_else(|| AudioImportError::InvalidProbe("missing audio stream".to_owned()))?;

    let sample_rate = parse_u64_field(stream, "sample_rate")?;
    let channels = parse_u64_field(stream, "channels")?;
    let duration_samples = parse_u64_field(stream, "duration_ts")?;

    let sample_rate = u32::try_from(sample_rate)
        .map_err(|_| AudioImportError::InvalidProbe("sample_rate does not fit u32".to_owned()))?;
    let channels = u32::try_from(channels)
        .map_err(|_| AudioImportError::InvalidProbe("channels does not fit u32".to_owned()))?;

    Ok(NormalizedAudioMetadata {
        sample_rate,
        channels,
        duration_samples,
    })
}

fn parse_u64_field(value: &Value, field: &str) -> Result<u64, AudioImportError> {
    let field_value = value
        .get(field)
        .ok_or_else(|| AudioImportError::InvalidProbe(format!("missing {field}")))?;

    if let Some(number) = field_value.as_u64() {
        return Ok(number);
    }

    if let Some(text) = field_value.as_str() {
        return text
            .parse::<u64>()
            .map_err(|_| AudioImportError::InvalidProbe(format!("invalid {field}: {text}")));
    }

    Err(AudioImportError::InvalidProbe(format!(
        "{field} must be an integer or integer string"
    )))
}

fn ensure_supported_input(path: &Path) -> Result<(), AudioImportError> {
    let extension = path
        .extension()
        .and_then(OsStr::to_str)
        .unwrap_or_default()
        .to_ascii_lowercase();

    if matches!(extension.as_str(), "wav" | "aac" | "mp3" | "mp4") {
        Ok(())
    } else {
        Err(AudioImportError::UnsupportedInput(extension))
    }
}

fn ensure_success(tool: &str, output: &Output) -> Result<(), AudioImportError> {
    if output.status.success() {
        Ok(())
    } else {
        Err(AudioImportError::ToolFailure {
            tool: tool.to_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        })
    }
}

fn temporary_wav_path(output: &Path) -> PathBuf {
    let file_name = output
        .file_name()
        .and_then(OsStr::to_str)
        .unwrap_or("normalized.wav");
    output.with_file_name(format!("{file_name}.tmp.wav"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::song_data::{CURRENT_FORMAT_VERSION, SongFiles};

    #[test]
    fn command_normalizes_to_internal_pcm_format() {
        let args = ffmpeg_arguments(Path::new("input.mp4"), Path::new("output.wav"));
        assert!(args.windows(2).any(|pair| pair == ["-ar", "48000"]));
        assert!(args.windows(2).any(|pair| pair == ["-ac", "2"]));
        assert!(args.windows(2).any(|pair| pair == ["-c:a", "pcm_f32le"]));
        assert!(args.windows(2).any(|pair| pair == ["-map", "0:a:0"]));
        assert!(args.windows(2).any(|pair| pair == ["-map_metadata", "-1"]));
    }

    #[test]
    fn supported_format_matrix_matches_requirements() {
        for name in ["fixture.wav", "fixture.aac", "fixture.mp3", "fixture.mp4"] {
            assert!(ensure_supported_input(Path::new(name)).is_ok(), "{name}");
        }
        assert!(ensure_supported_input(Path::new("fixture.flac")).is_err());
    }

    #[test]
    fn probe_parser_uses_exact_duration_samples() {
        let metadata = parse_probe_json(
            br#"{
                "streams": [{
                    "sample_rate": "48000",
                    "channels": 2,
                    "duration_ts": 96001
                }]
            }"#,
        )
        .unwrap();

        assert_eq!(
            metadata,
            NormalizedAudioMetadata {
                sample_rate: 48_000,
                channels: 2,
                duration_samples: 96_001,
            }
        );
    }

    #[test]
    fn malformed_probe_output_is_rejected() {
        assert!(parse_probe_json(br#"{"streams":[]}"#).is_err());
        assert!(parse_probe_json(b"not-json").is_err());
    }

    #[test]
    fn gpl_and_nonfree_builds_are_rejected() {
        assert!(validate_ffmpeg_build_configuration("configuration: --enable-gpl").is_err());
        assert!(validate_ffmpeg_build_configuration("configuration: --enable-nonfree").is_err());
        assert!(
            validate_ffmpeg_build_configuration("configuration: --disable-gpl --disable-nonfree")
                .is_ok()
        );
    }

    #[test]
    fn metadata_updates_song_duration_and_original_path() {
        let mut manifest = SongManifest {
            format_version: CURRENT_FORMAT_VERSION,
            song_id: "song".to_owned(),
            title: "Song".to_owned(),
            artist: None,
            sample_rate: 48_000,
            duration_samples: 0,
            analysis_version: 1,
            files: SongFiles::default(),
            artifacts: std::collections::BTreeMap::new(),
        };
        let metadata = NormalizedAudioMetadata {
            sample_rate: 48_000,
            channels: 2,
            duration_samples: 123_456,
        };

        apply_metadata(&mut manifest, "audio/original_48k.wav", &metadata);

        assert_eq!(manifest.duration_samples, 123_456);
        assert_eq!(manifest.files.original, "audio/original_48k.wav");
    }
}
