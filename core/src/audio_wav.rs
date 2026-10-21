//! Sequential disk feeder for canonical stereo f32 WAV accompaniment.
//!
//! Parsing and file I/O happen on a dedicated non-real-time thread, never in CPAL callbacks.

use std::{
    fs::File,
    io::{self, Read, Seek, SeekFrom},
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    thread::{self, JoinHandle},
    time::Duration,
};

use crate::audio_engine::{AudioIo, SAMPLE_RATE};

const FLOAT_SUBFORMAT: [u8; 16] = [
    3, 0, 0, 0, 0, 0, 0x10, 0, 0x80, 0, 0, 0xaa, 0, 0x38, 0x9b, 0x71,
];
const READ_FRAMES: usize = 1024;

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct WavMetadata {
    pub duration_samples: u64,
    pub(crate) data_offset: u64,
}

pub(crate) fn parse_wav(file: &mut File) -> io::Result<WavMetadata> {
    let mut header = [0_u8; 12];
    file.read_exact(&mut header)?;
    if &header[..4] != b"RIFF" || &header[8..] != b"WAVE" {
        return Err(invalid("Expected RIFF/WAVE normalized audio"));
    }
    let file_size = file.metadata()?.len();
    let declared_end = 8 + u64::from(u32::from_le_bytes(header[4..8].try_into().unwrap()));
    if declared_end > file_size {
        return Err(invalid("Truncated RIFF container"));
    }
    let mut normalized_format = false;
    let mut data = None;

    while file.stream_position()? + 8 <= declared_end {
        let mut tag = [0_u8; 8];
        file.read_exact(&mut tag)?;
        let chunk_size = u64::from(u32::from_le_bytes(tag[4..8].try_into().unwrap()));
        let start = file.stream_position()?;
        let end = start
            .checked_add(chunk_size)
            .ok_or_else(|| invalid("Invalid WAV chunk length"))?;
        if end > declared_end {
            return Err(invalid("WAV chunk extends past declared RIFF length"));
        }
        if &tag[..4] == b"fmt " {
            if !(16..=4096).contains(&chunk_size) {
                return Err(invalid("Invalid WAV fmt chunk"));
            }
            let mut bytes = vec![0; usize::try_from(chunk_size).unwrap_or(4096)];
            file.read_exact(&mut bytes)?;
            let format = u16::from_le_bytes([bytes[0], bytes[1]]);
            let channels = u16::from_le_bytes([bytes[2], bytes[3]]);
            let sample_rate = u32::from_le_bytes(bytes[4..8].try_into().unwrap());
            let align = u16::from_le_bytes([bytes[12], bytes[13]]);
            let bits = u16::from_le_bytes([bytes[14], bytes[15]]);
            let ieee_float = format == 3
                || (format == 0xfffe && bytes.len() >= 40 && bytes[24..40] == FLOAT_SUBFORMAT);
            normalized_format = ieee_float
                && channels == 2
                && sample_rate == SAMPLE_RATE
                && align == 8
                && bits == 32;
        } else if &tag[..4] == b"data" && data.replace((start, chunk_size)).is_some() {
            return Err(invalid("Multiple WAV data chunks are unsupported"));
        }
        let padded_end = end + (chunk_size % 2);
        if padded_end > declared_end {
            return Err(invalid("Invalid WAV chunk padding"));
        }
        file.seek(SeekFrom::Start(padded_end))?;
    }
    if !normalized_format {
        return Err(invalid("Expected 48 kHz stereo float32 WAV"));
    }
    let (data_offset, data_bytes) = data.ok_or_else(|| invalid("Missing WAV data chunk"))?;
    if data_bytes == 0 || data_bytes % 8 != 0 {
        return Err(invalid("Invalid float32 stereo sample count"));
    }
    Ok(WavMetadata {
        duration_samples: data_bytes / 8,
        data_offset,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WavFeedStatus {
    pub duration_samples: u64,
    pub enqueued_samples: u64,
    pub finished: bool,
    pub failed: bool,
}

pub struct WavFeeder {
    stop: Arc<AtomicBool>,
    sent: Arc<AtomicU64>,
    finished: Arc<AtomicBool>,
    failed: Arc<AtomicBool>,
    duration_samples: u64,
    thread: Option<JoinHandle<()>>,
}

impl WavFeeder {
    /// Open a canonical WAV and enqueue it off the audio thread with bounded memory.
    ///
    /// # Errors
    /// Returns an I/O or invalid-format error before spawning a feeder.
    pub fn start(io: AudioIo, path: impl AsRef<Path>) -> io::Result<Self> {
        let mut file = File::open(path)?;
        let metadata = parse_wav(&mut file)?;
        file.seek(SeekFrom::Start(metadata.data_offset))?;

        let stop = Arc::new(AtomicBool::new(false));
        let sent = Arc::new(AtomicU64::new(0));
        let finished = Arc::new(AtomicBool::new(false));
        let failed = Arc::new(AtomicBool::new(false));
        let duration_samples = metadata.duration_samples;
        let stopping = Arc::clone(&stop);
        let sending = Arc::clone(&sent);
        let done = Arc::clone(&finished);
        let failure = Arc::clone(&failed);

        let handle = thread::spawn(move || {
            let mut bytes = [0_u8; READ_FRAMES * 8];
            let mut samples = [[0.0_f32; 2]; READ_FRAMES];
            let mut remaining = metadata.duration_samples;
            while remaining > 0 && !stopping.load(Ordering::Acquire) {
                let count = remaining.min(READ_FRAMES as u64);
                let count = usize::try_from(count).unwrap_or(READ_FRAMES);
                if file.read_exact(&mut bytes[..count * 8]).is_err() {
                    failure.store(true, Ordering::Release);
                    break;
                }
                for (index, frame) in samples.iter_mut().enumerate().take(count) {
                    let left = index * 8;
                    let right = left + 4;
                    *frame = [
                        f32::from_le_bytes([
                            bytes[left],
                            bytes[left + 1],
                            bytes[left + 2],
                            bytes[left + 3],
                        ]),
                        f32::from_le_bytes([
                            bytes[right],
                            bytes[right + 1],
                            bytes[right + 2],
                            bytes[right + 3],
                        ]),
                    ];
                }
                if samples[..count]
                    .iter()
                    .flatten()
                    .any(|sample| !sample.is_finite())
                {
                    failure.store(true, Ordering::Release);
                    break;
                }
                let mut written = 0;
                while written < count && !stopping.load(Ordering::Acquire) {
                    match io.enqueue_stereo(&samples[written..count]) {
                        Ok(0) => thread::sleep(Duration::from_millis(2)),
                        Ok(accepted) => {
                            written += accepted;
                            sending
                                .fetch_add(u64::try_from(accepted).unwrap_or(0), Ordering::Release);
                        }
                        Err(_) => {
                            failure.store(true, Ordering::Release);
                            break;
                        }
                    }
                }
                if written < count {
                    break;
                }
                remaining -= u64::try_from(count).unwrap_or(0);
            }
            done.store(true, Ordering::Release);
        });
        Ok(Self {
            stop,
            sent,
            finished,
            failed,
            duration_samples,
            thread: Some(handle),
        })
    }

    #[must_use]
    pub fn status(&self) -> WavFeedStatus {
        WavFeedStatus {
            duration_samples: self.duration_samples,
            enqueued_samples: self.sent.load(Ordering::Acquire),
            finished: self.finished.load(Ordering::Acquire),
            failed: self.failed.load(Ordering::Acquire),
        }
    }

    /// Stop and join the feeder from a non-realtime control thread.
    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(handle) = self.thread.take() {
            let _ = handle.join();
        }
    }
}

impl Drop for WavFeeder {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::{WavFeeder, invalid};
    use crate::audio_engine::AudioIo;

    fn fixture(frames: &[[f32; 2]]) -> Vec<u8> {
        let data_size = u32::try_from(frames.len() * 8).unwrap();
        let mut bytes = Vec::new();
        bytes.extend_from_slice(b"RIFF");
        bytes.extend_from_slice(&(data_size + 36).to_le_bytes());
        bytes.extend_from_slice(b"WAVEfmt ");
        bytes.extend_from_slice(&16_u32.to_le_bytes());
        bytes.extend_from_slice(&3_u16.to_le_bytes());
        bytes.extend_from_slice(&2_u16.to_le_bytes());
        bytes.extend_from_slice(&48_000_u32.to_le_bytes());
        bytes.extend_from_slice(&384_000_u32.to_le_bytes());
        bytes.extend_from_slice(&8_u16.to_le_bytes());
        bytes.extend_from_slice(&32_u16.to_le_bytes());
        bytes.extend_from_slice(b"data");
        bytes.extend_from_slice(&data_size.to_le_bytes());
        for frame in frames {
            for sample in frame {
                bytes.extend_from_slice(&sample.to_le_bytes());
            }
        }
        bytes
    }

    /// {
    ///   責務: [`feeder_decodes_48k_stereo_float_without_callback_file_io`: WAV の非同期読込と PCM を検査する]
    ///   処理: [1: queue 容量内の fixture を作る, 2: worker 完了を待つ, 3: 再生値と時刻を確認する]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[test]
    fn feeder_decodes_48k_stereo_float_without_callback_file_io() {
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("accompaniment.wav");
        fs::write(&path, fixture(&[[0.25, -0.25], [0.5, -0.5]])).unwrap();
        let io = AudioIo::new(32).unwrap();
        let mut feeder = WavFeeder::start(io.clone(), &path).unwrap();
        // Both fixture frames fit in the ring, so completion needs no consumer.
        feeder.thread.take().unwrap().join().unwrap();
        assert!(feeder.status().finished);
        assert!(!feeder.status().failed);
        assert_eq!(feeder.status().enqueued_samples, 2);
        io.set_playing(true);
        let mut output = [0.0; 4];
        io.render_mock(&mut output, 2);
        // These binary fractions must survive PCM decoding exactly.
        assert_eq!(
            output.map(f32::to_bits),
            [0.25_f32, -0.25, 0.5, -0.5].map(f32::to_bits)
        );
        assert_eq!(io.counters().playback_sample_position, 2);
    }

    #[test]
    fn invalid_wav_is_rejected_before_starting() {
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("bad.wav");
        fs::write(&path, b"invalid data").unwrap();
        let io = AudioIo::default();
        assert!(WavFeeder::start(io, &path).is_err());
        assert_eq!(invalid("bad").kind(), std::io::ErrorKind::InvalidData);
    }
}
