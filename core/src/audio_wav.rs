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

/// {
///   責務: [`WavFeeder`: 正規化`WAV`を指定epochとsong位置から非audio threadで送る]
///   フィールド: [`stop`: 停止要求, `sent`: 送信数, `finished`: 完了状態, `failed`: 失敗状態, `epoch`: `PCM`世代, `duration_samples`: 曲長, `thread`: worker]
/// }
pub struct WavFeeder {
    stop: Arc<AtomicBool>,
    sent: Arc<AtomicU64>,
    finished: Arc<AtomicBool>,
    failed: Arc<AtomicBool>,
    epoch: crate::audio_clock::ClockEpoch,
    duration_samples: u64,
    thread: Option<JoinHandle<()>>,
}

impl WavFeeder {
    /// {
    ///   責務: [`start`: 従来の`WAV`開始`API`で曲頭から現在epochへ送る]
    ///   処理: [1: `AudioIo`の現epochを取得する, 2: 曲頭から`start_at`を呼ぶ]
    ///   引数: [io: audio queue, path: 正規化WAV]
    ///   戻り値: [`WavFeeder`: workerの制御と状態]
    ///   エラー: [WAVを開けない、headerまたはformatが不正]
    /// }
    ///
    /// # Errors
    /// Returns an I/O or invalid-format error before spawning a feeder.
    pub fn start(io: AudioIo, path: impl AsRef<Path>) -> io::Result<Self> {
        let epoch = io.clock_epoch();
        Self::start_at(io, epoch, 0, path)
    }

    /// {
    ///   責務: [`start_at`: `正規化WAVを指定epochとsong位置から非audio` threadでqueueへ送る]
    ///   処理: [1: `WAV形式と開始song位置を検証する`, 2: `位置へseekする`, 3: bounded workerでepoch付きPCMを送る]
    ///   引数: [io: audio queue, epoch: feederを所有するclock世代, `song_sample`: 最初のsong位置, path: 正規化WAV]
    ///   戻り値: [`WavFeeder`: workerの制御と状態を持つowner]
    ///   エラー: [WAVを開けない、headerまたはformatが不正]
    /// }
    ///
    /// # Errors
    /// Returns an I/O or invalid-format error before spawning a feeder.
    pub fn start_at(
        io: AudioIo,
        epoch: crate::audio_clock::ClockEpoch,
        song_sample: u64,
        path: impl AsRef<Path>,
    ) -> io::Result<Self> {
        let mut file = File::open(path)?;
        let metadata = parse_wav(&mut file)?;
        if song_sample > metadata.duration_samples {
            return Err(invalid("WAV start sample exceeds its duration"));
        }
        let byte_offset = song_sample
            .checked_mul(8)
            .and_then(|offset| metadata.data_offset.checked_add(offset))
            .ok_or_else(|| invalid("WAV start sample position overflowed"))?;
        file.seek(SeekFrom::Start(byte_offset))?;

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
            let mut remaining = metadata.duration_samples - song_sample;
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
                    let Some(first_song_sample) =
                        song_sample.checked_add(sending.load(Ordering::Acquire))
                    else {
                        failure.store(true, Ordering::Release);
                        break;
                    };
                    match io.enqueue_stereo_at(epoch, first_song_sample, &samples[written..count]) {
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
            epoch,
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

    /// {
    ///   責務: [`epoch`: `このfeederがPCMを生成するtransport世代を読む`]
    ///   処理: [1: ownerが保持するepoch値を返す]
    ///   引数: []
    ///   戻り値: [`ClockEpoch`: enqueue時に指定される世代]
    /// }
    #[must_use]
    pub const fn epoch(&self) -> crate::audio_clock::ClockEpoch {
        self.epoch
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
        assert_eq!(
            io.playback_timestamp(),
            Some(crate::audio_engine::AudioPlaybackTimestamp {
                epoch: feeder.epoch(),
                output_sample: 1,
                song_sample: 1,
            })
        );
        assert_eq!(io.counters().playback_sample_position, 2);
    }

    #[test]
    fn invalid_wav_is_rejected_before_starting() {
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("bad.wav");
        fs::write(&path, b"invalid data").unwrap();
        let io = AudioIo::default();
        assert!(WavFeeder::start(io.clone(), &path).is_err());
        assert_eq!(invalid("bad").kind(), std::io::ErrorKind::InvalidData);
    }

    /// {
    ///   責務: [`feeder_starts_at_the_requested_song_sample`: seek後のPCMとsong timestampが同じ位置を示すことを検査する]
    ///   処理: [1: 複数frameのfixtureを作る, 2: 指定sampleから送る, 3: 音声値と再生timestampを照合する]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[test]
    fn feeder_starts_at_the_requested_song_sample() {
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("seek.wav");
        fs::write(&path, fixture(&[[0.1, -0.1], [0.5, -0.5]])).unwrap();
        let io = AudioIo::new(32).unwrap();
        let epoch = io.clock_epoch();
        let mut feeder = WavFeeder::start_at(io.clone(), epoch, 1, &path).unwrap();
        feeder.thread.take().unwrap().join().unwrap();
        assert_eq!(feeder.status().enqueued_samples, 1);
        io.set_playing(true);
        let mut output = [0.0; 2];
        io.render_mock(&mut output, 2);
        assert!((output[0] - 0.5).abs() < 1e-6);
        assert!((output[1] + 0.5).abs() < 1e-6);
        assert_eq!(
            io.playback_timestamp(),
            Some(crate::audio_engine::AudioPlaybackTimestamp {
                epoch,
                output_sample: 0,
                song_sample: 1,
            })
        );
    }
}
