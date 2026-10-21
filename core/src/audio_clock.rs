//! Clock-domain to song-timeline mapping. This module performs integer-only,
//! constant-time transformations; hardware measurement belongs to the audio engine.
//!
//! A mapping is valid for exactly one transport epoch. Seek, device changes, and
//! discontinuities must invalidate mappings before more samples are scored.

use std::fmt;

use crate::audio_engine::AudioCounters;

pub const SONG_SAMPLE_RATE: u32 = 48_000;
const PPM_BASE: i128 = 1_000_000;
const MAX_ABS_DRIFT_PPM: i32 = 2_000;

/// Changes whenever a seek, pause/resume, device reset, or discontinuity occurs.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ClockEpoch(u64);

impl ClockEpoch {
    #[must_use]
    pub const fn sequence(self) -> u64 {
        self.0
    }
}

/// Distinct devices have distinct sample counters and must not share anchors.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ClockDomain {
    Output,
    Capture,
}

/// A zero compensation with Unknown means *unmeasured*, not zero latency.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LatencyCalibration {
    Unknown,
    Estimated {
        /// Positive moves the stream sample forward on the song timeline.
        compensation_samples: i64,
    },
    Measured {
        compensation_samples: i64,
        uncertainty_samples: u64,
    },
}

impl LatencyCalibration {
    const fn compensation(self) -> i64 {
        match self {
            Self::Unknown => 0,
            Self::Estimated {
                compensation_samples,
            }
            | Self::Measured {
                compensation_samples,
                ..
            } => compensation_samples,
        }
    }
}

/// A pair of simultaneous logical stream/song samples for a single epoch.
///
/// Stream samples must already be expressed on a logical 48 kHz grid, even if
/// the physical device uses a different sample rate. Drift is an explicit
/// estimate in parts per million, never inferred from the sample numbers here.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ClockAnchor {
    pub epoch: ClockEpoch,
    pub domain: ClockDomain,
    pub stream_sample: u64,
    pub song_sample: u64,
    /// Positive means the song clock advances faster than this stream clock.
    pub drift_ppm: i32,
    pub latency: LatencyCalibration,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MappedSongSample {
    pub epoch: ClockEpoch,
    pub song_sample: u64,
    /// Preserved for consumers; do not turn Unknown/Estimated into Measured.
    pub latency: LatencyCalibration,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ClockError {
    StaleEpoch,
    UnanchoredDomain,
    DomainAlreadyAnchored,
    DriftOutOfRange,
    BeforeSongStart,
    ArithmeticOverflow,
}

impl fmt::Display for ClockError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            Self::StaleEpoch => "sample belongs to a stale transport epoch",
            Self::UnanchoredDomain => "clock domain has no anchor for this epoch",
            Self::DomainAlreadyAnchored => "clock domain is already anchored",
            Self::DriftOutOfRange => "drift exceeds the supported ppm range",
            Self::BeforeSongStart => "compensated sample precedes song start",
            Self::ArithmeticOverflow => "clock calculation exceeded its range",
        };
        f.write_str(message)
    }
}

impl std::error::Error for ClockError {}

/// {
///   責務: [
///     `ClockDiscontinuity`: 観測できた時刻不連続の原因を表す
///   ]
///   フィールド: []
///   補足: [同一snapshotの複数原因はdevice error、counter reset、capture gap、playbackの順に代表する]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ClockDiscontinuity {
    DeviceError,
    CounterReset,
    CaptureGap,
    PlaybackTransition,
}

/// {
///   責務: [`ClockInvalidation`: anchor無効化後のepochと観測理由を保持する]
///   フィールド: [
///     epoch: 次のanchorを構築するepoch
///     reason: 無効化を必要とした代表原因
///   ]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ClockInvalidation {
    pub epoch: ClockEpoch,
    pub reason: ClockDiscontinuity,
}

/// {
///   責務: [`ClockSession`: transport epoch内の独立したoutput/capture mappingを所有する]
///   フィールド: [
///     epoch: 現在のtransport世代
///     output: output domainのanchor
///     capture: capture domainのanchor
///     `last_audio`: 不連続検出に使う直前のlogical counter snapshot
///   ]
/// }
#[derive(Clone, Debug)]
pub struct ClockSession {
    epoch: ClockEpoch,
    output: Option<ClockAnchor>,
    capture: Option<ClockAnchor>,
    last_audio: Option<AudioCounters>,
}

impl ClockSession {
    /// {
    ///   責務: [new: anchor未設定のclock sessionを作る]
    ///   処理: [1: epochを1で開始し両domainとcounter baselineを未設定にする]
    ///   引数: []
    ///   戻り値: [`ClockSession`: 新しいclock owner]
    /// }
    #[must_use]
    pub const fn new() -> Self {
        Self {
            epoch: ClockEpoch(1),
            output: None,
            capture: None,
            last_audio: None,
        }
    }

    #[must_use]
    pub const fn epoch(&self) -> ClockEpoch {
        self.epoch
    }

    /// {
    ///   責務: [`begin_new_epoch`: 不連続のcontrol境界で両anchorとcounter baselineを破棄する]
    ///   処理: [
    ///     1: epochをoverflow検査付きで進める
    ///     2: 両domainのanchorと直前のcounter snapshotを消す
    ///   ]
    ///   引数: []
    ///   戻り値: [`ClockEpoch`: 新しいtransport世代]
    ///   エラー: [`ArithmeticOverflow`: epoch counterを使い切った]
    /// }
    ///
    /// # Errors
    /// Returns `ClockError::ArithmeticOverflow` if the epoch counter is exhausted.
    pub fn begin_new_epoch(&mut self) -> Result<ClockEpoch, ClockError> {
        self.epoch = ClockEpoch(
            self.epoch
                .0
                .checked_add(1)
                .ok_or(ClockError::ArithmeticOverflow)?,
        );
        self.output = None;
        self.capture = None;
        self.last_audio = None;
        Ok(self.epoch)
    }

    /// {
    ///   責務: [`observe_audio`: logical audio counterの不連続で両domainのanchorを無効化する]
    ///   処理: [
    ///     1: error flagの立上りとcounter巻き戻りを確認する
    ///     2: capture欠落増加とplayback状態遷移を確認する
    ///     3: 原因があれば一度だけepochを進め両anchorを消す
    ///     4: snapshotを次回のbaselineとして保存する
    ///   ]
    ///   引数: [counters: control threadが読み取った`AudioIo`の観測値]
    ///   戻り値: [`Option<ClockInvalidation>`: anchorを無効化した場合のepochと理由]
    ///   エラー: [`ArithmeticOverflow`: epoch counterを使い切った]
    ///   補足: [
    ///     snapshotは同時刻のhardware timestampではなくanchorを自動生成しない
    ///     制御側は各play/pause境界で観測しseekや同値counterのdevice交換は明示的にepochを進める
    ///     output underrunの無音frameはlogical clockを維持するため単独ではepochを変えない
    ///   ]
    /// }
    ///
    /// # Errors
    /// Returns `ClockError::ArithmeticOverflow` if the epoch counter is exhausted.
    pub fn observe_audio(
        &mut self,
        counters: AudioCounters,
    ) -> Result<Option<ClockInvalidation>, ClockError> {
        let previous = self.last_audio;
        let new_device_error = (counters.output_device_error
            && !previous.is_some_and(|old| old.output_device_error))
            || (counters.input_device_error && !previous.is_some_and(|old| old.input_device_error));
        let counter_reset = previous.is_some_and(|old| {
            counters.playback_sample_position < old.playback_sample_position
                || counters.microphone_frames_received < old.microphone_frames_received
                || counters.output_underrun_frames < old.output_underrun_frames
                || counters.input_overrun_frames < old.input_overrun_frames
        });
        let capture_gap = previous.map_or_else(
            || {
                counters.input_overrun_frames > 0
                    && (self.output.is_some() || self.capture.is_some())
            },
            |old| counters.input_overrun_frames > old.input_overrun_frames,
        );
        let playback_transition = previous.is_some_and(|old| old.playing != counters.playing);
        let reason = if new_device_error {
            Some(ClockDiscontinuity::DeviceError)
        } else if counter_reset {
            Some(ClockDiscontinuity::CounterReset)
        } else if capture_gap {
            Some(ClockDiscontinuity::CaptureGap)
        } else if playback_transition {
            Some(ClockDiscontinuity::PlaybackTransition)
        } else {
            None
        };
        let invalidation = if let Some(reason) = reason {
            Some(ClockInvalidation {
                epoch: self.begin_new_epoch()?,
                reason,
            })
        } else {
            None
        };
        self.last_audio = Some(counters);
        Ok(invalidation)
    }

    /// Install a domain anchor once per epoch.
    ///
    /// # Errors
    /// Rejects stale epochs, unsupported drift, or duplicate domain anchors.
    pub fn bind(&mut self, anchor: ClockAnchor) -> Result<(), ClockError> {
        if anchor.epoch != self.epoch {
            return Err(ClockError::StaleEpoch);
        }
        if !(-MAX_ABS_DRIFT_PPM..=MAX_ABS_DRIFT_PPM).contains(&anchor.drift_ppm) {
            return Err(ClockError::DriftOutOfRange);
        }

        let slot = match anchor.domain {
            ClockDomain::Output => &mut self.output,
            ClockDomain::Capture => &mut self.capture,
        };
        if slot.is_some() {
            return Err(ClockError::DomainAlreadyAnchored);
        }
        *slot = Some(anchor);
        Ok(())
    }

    /// Map a logical stream sample to the song's 48 kHz integer timeline.
    ///
    /// Does not silently clamp negative positions to zero, since that would
    /// mis-score pre-roll. A mapping's latency quality remains observable.
    ///
    /// # Errors
    /// Returns `StaleEpoch`, `UnanchoredDomain`, `BeforeSongStart`, or `ArithmeticOverflow`.
    pub fn resolve(
        &self,
        domain: ClockDomain,
        epoch: ClockEpoch,
        stream_sample: u64,
    ) -> Result<MappedSongSample, ClockError> {
        if epoch != self.epoch {
            return Err(ClockError::StaleEpoch);
        }
        let anchor = match domain {
            ClockDomain::Output => self.output,
            ClockDomain::Capture => self.capture,
        }
        .ok_or(ClockError::UnanchoredDomain)?;

        let delta = i128::from(stream_sample) - i128::from(anchor.stream_sample);
        let scale = PPM_BASE + i128::from(anchor.drift_ppm);
        let numerator = delta * scale;
        // Integer round-to-nearest, ties away from zero. Rust division
        // truncates toward zero, including for negative values.
        let rounded = if numerator >= 0 {
            (numerator + PPM_BASE / 2) / PPM_BASE
        } else {
            (numerator - PPM_BASE / 2) / PPM_BASE
        };
        let mapped =
            i128::from(anchor.song_sample) + rounded + i128::from(anchor.latency.compensation());
        if mapped < 0 {
            return Err(ClockError::BeforeSongStart);
        }
        let song_sample = u64::try_from(mapped).map_err(|_| ClockError::ArithmeticOverflow)?;
        Ok(MappedSongSample {
            epoch,
            song_sample,
            latency: anchor.latency,
        })
    }
}

impl Default for ClockSession {
    fn default() -> Self {
        Self::new()
    }
}
