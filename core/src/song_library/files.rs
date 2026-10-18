use std::{
    fs::{self, File, Metadata},
    io::{Read, Seek, SeekFrom, Write},
    path::{Component, Path, PathBuf},
};

use serde::{Serialize, de::DeserializeOwned};
use sha2::{Digest, Sha256};

use super::{Asset, ImportControl, ImportError, MAX_JSON};
use crate::song_data::{LyricsDocument, NotesDocument, SongBundle};

pub(super) fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn no_link(metadata: &Metadata) -> Result<(), ImportError> {
    if metadata.file_type().is_symlink() {
        return Err(ImportError::Invalid("symlink paths are forbidden"));
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if metadata.file_attributes() & 0x400 != 0 {
            return Err(ImportError::Invalid("Windows reparse points are forbidden"));
        }
    }
    Ok(())
}

pub(super) fn make_root(path: &Path) -> Result<PathBuf, ImportError> {
    let path = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    if path.components().any(|part| matches!(part, Component::ParentDir)) {
        return Err(ImportError::Invalid("root paths must not contain parent traversal"));
    }
    check_ancestors(&path)?;
    fs::create_dir_all(&path)?;
    check_directory(&path)?;
    Ok(path.canonicalize()?)
}

fn check_ancestors(path: &Path) -> Result<(), ImportError> {
    for part in path.ancestors() {
        match fs::symlink_metadata(part) {
            Ok(metadata) => no_link(&metadata)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

pub(super) fn check_directory(path: &Path) -> Result<(), ImportError> {
    check_ancestors(path)?;
    if !fs::symlink_metadata(path)?.is_dir() {
        return Err(ImportError::Invalid("expected a managed directory"));
    }
    Ok(())
}

pub(super) fn regular_file(path: &Path) -> Result<Metadata, ImportError> {
    let metadata = fs::symlink_metadata(path)?;
    no_link(&metadata)?;
    if !metadata.is_file() {
        return Err(ImportError::Invalid("only regular files are allowed"));
    }
    Ok(metadata)
}

pub(super) fn relative(value: &str) -> Result<(), ImportError> {
    if value.is_empty() || value.len() > 512 {
        return Err(ImportError::Invalid("invalid managed relative path"));
    }
    for part in value.split('/') {
        if part.is_empty() || part.len() > 128 || part == "." || part == ".."
            || part.ends_with('.')
            || !part.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
        {
            return Err(ImportError::Invalid("noncanonical managed path"));
        }
        let stem = part.split('.').next().unwrap_or_default().to_ascii_uppercase();
        if ["CON", "PRN", "AUX", "NUL"].contains(&stem.as_str())
            || (stem.len() == 4 && (stem.starts_with("COM") || stem.starts_with("LPT"))
                && matches!(stem.as_bytes()[3], b'1'..=b'9'))
        {
            return Err(ImportError::Invalid("reserved Windows path"));
        }
    }
    Ok(())
}

pub(super) fn under(root: &Path, value: &str) -> Result<PathBuf, ImportError> {
    relative(value)?;
    check_directory(root)?;
    let mut path = root.to_owned();
    for part in value.split('/') {
        path.push(part);
        match fs::symlink_metadata(&path) {
            Ok(metadata) => no_link(&metadata)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(path)
}

pub(super) fn bounded_bytes(path: &Path, max: u64) -> Result<Vec<u8>, ImportError> {
    if regular_file(path)?.len() > max { return Err(ImportError::TooLarge); }
    let mut bytes = Vec::new();
    File::open(path)?.take(max + 1).read_to_end(&mut bytes)?;
    if u64::try_from(bytes.len()).unwrap_or(u64::MAX) > max { return Err(ImportError::TooLarge); }
    Ok(bytes)
}

pub(super) fn bounded_json<T: DeserializeOwned>(path: &Path, max: u64) -> Result<T, ImportError> {
    Ok(serde_json::from_slice(&bounded_bytes(path, max)?)?)
}

pub(super) fn write_new_json(path: &Path, value: &impl Serialize) -> Result<(), ImportError> {
    let mut file = File::create_new(path)?;
    serde_json::to_writer(&mut file, value)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    Ok(())
}

pub(super) fn copy_verified(
    input: &Path, output: &Path, asset: &Asset, control: &ImportControl,
    report: impl FnMut(u64),
) -> Result<(), ImportError> {
    control.checkpoint()?;
    if let Some(parent) = output.parent() { fs::create_dir_all(parent)?; }
    let mut file = File::create_new(output)?;
    stream_checked(input, &mut file, asset, control, report)?;
    file.sync_all()?;
    Ok(())
}

pub(super) fn check_hash(
    input: &Path, asset: &Asset, control: &ImportControl, report: impl FnMut(u64),
) -> Result<(), ImportError> {
    stream_checked(input, &mut std::io::sink(), asset, control, report)
}

fn stream_checked(
    input: &Path, output: &mut impl Write, asset: &Asset, control: &ImportControl,
    mut report: impl FnMut(u64),
) -> Result<(), ImportError> {
    control.checkpoint()?;
    if regular_file(input)?.len() != asset.bytes {
        return Err(ImportError::Invalid("asset byte count mismatch"));
    }
    let mut reader = File::open(input)?.take(asset.bytes + 1);
    let mut hash = Sha256::new();
    let mut buffer = vec![0_u8; 65_536];
    let mut count = 0_u64;
    loop {
        control.checkpoint()?;
        let size = reader.read(&mut buffer)?;
        if size == 0 { break; }
        let amount = u64::try_from(size).map_err(|_| ImportError::TooLarge)?;
        count += amount;
        if count > asset.bytes { return Err(ImportError::Invalid("asset grew while copying")); }
        hash.update(&buffer[..size]);
        output.write_all(&buffer[..size])?;
        report(amount);
    }
    control.checkpoint()?;
    if count != asset.bytes || format!("{:x}", hash.finalize()) != asset.sha256 {
        return Err(ImportError::Invalid("asset checksum mismatch"));
    }
    Ok(())
}

pub(super) fn verify_content(
    root: &Path, bundle: &SongBundle, control: &ImportControl,
) -> Result<(), ImportError> {
    for name in [&bundle.song.files.original, &bundle.song.files.vocals, &bundle.song.files.accompaniment] {
        verify_wave(&under(root, name)?, bundle.song.duration_samples, control)?;
    }
    let lyrics: LyricsDocument = bounded_json(&under(root, "provenance/stages/alignment/lyrics.json")?, MAX_JSON)?;
    let notes: NotesDocument = bounded_json(&under(root, "provenance/stages/notes/notes.json")?, MAX_JSON)?;
    if lyrics != bundle.lyrics || notes != bundle.notes {
        return Err(ImportError::Invalid("canonical documents differ from their verified stage outputs"));
    }
    let reference = under(root, "provenance/source/reference.txt")?;
    if reference.try_exists()? {
        let bytes = bounded_bytes(&reference, 65_536)?;
        std::str::from_utf8(&bytes).map_err(|_| ImportError::Invalid("reference lyrics are not UTF-8"))?;
    }
    control.checkpoint()
}

fn verify_wave(path: &Path, expected: u64, control: &ImportControl) -> Result<(), ImportError> {
    regular_file(path)?;
    let mut file = File::open(path)?;
    let metadata = crate::audio_wav::parse_wav(&mut file)
        .map_err(|_| ImportError::Invalid("expected 48 kHz stereo float WAV"))?;
    if metadata.duration_samples != expected {
        return Err(ImportError::Invalid("WAV sample count differs from song timeline"));
    }
    file.seek(SeekFrom::Start(metadata.data_offset))?;
    let mut remaining = expected * 8;
    let mut buffer = vec![0_u8; 65_536];
    while remaining > 0 {
        control.checkpoint()?;
        let count = usize::try_from(remaining.min(65_536)).map_err(|_| ImportError::TooLarge)?;
        file.read_exact(&mut buffer[..count])?;
        for bytes in buffer[..count].as_chunks::<4>().0 {
            if !f32::from_le_bytes(*bytes).is_finite() {
                return Err(ImportError::Invalid("WAV contains NaN or infinity"));
            }
        }
        remaining -= u64::try_from(count).map_err(|_| ImportError::TooLarge)?;
    }
    Ok(())
}

// All filesystem helpers are blocking and must stay outside audio callbacks.
