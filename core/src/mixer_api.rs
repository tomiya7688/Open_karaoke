//! Core API for the karaoke mixer: `GET /mixer` and `PUT /mixer`.
//!
//! Settings are shared with the audio callback through [`crate::mixer_control::MixerControl`],
//! so a change takes effect on the next callback without the API touching audio state.
//! Finite out-of-range values are clamped and the applied values are returned; anything
//! non-finite is refused.

use axum::{Json, Router, extract::State, http::StatusCode, routing::get};
use serde::{Deserialize, Serialize};

use crate::{
    ApiError, AppState,
    audio_engine::SAMPLE_RATE,
    mixer::{MAX_ECHO_DELAY_FRAMES, MixerSettings},
    mixer_control::MonitorSettings,
};

const FRAMES_PER_MS: usize = SAMPLE_RATE as usize / 1000;
const MAX_ECHO_DELAY_MS: u32 = 1000;
const MAX_GAIN: f32 = 2.0;
const MAX_ECHO_FEEDBACK: f32 = 0.9;

/// {
///   責務: [`MixerSettingsBody`: `GET/PUT /mixer`の本文。全項目を指定して置き換える]
///   フィールド: [
///     `music_gain` / `mic_gain`: 0..=2,
///     `echo_level` / `reverb_level`: 0..=1,
///     `echo_delay_ms`: 1..=1000,
///     `echo_feedback`: 0..=0.9,
///     `effects_bypass`: エコー・リバーブを通さない,
///     `mic_monitor`: マイクをモニタへ流す(既定false)
///   ]
/// }
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MixerSettingsBody {
    pub music_gain: f32,
    pub mic_gain: f32,
    pub echo_level: f32,
    pub echo_delay_ms: u32,
    pub echo_feedback: f32,
    pub reverb_level: f32,
    pub effects_bypass: bool,
    pub mic_monitor: bool,
}

impl MixerSettingsBody {
    /// {
    ///   責務: [`from_settings`: 内部設定をAPIの本文へ変換する]
    ///   引数: [settings: 内部設定]
    ///   戻り値: [ミリ秒単位の遅延を持つ本文]
    /// }
    fn from_settings(settings: &MonitorSettings) -> Self {
        let mixer = settings.mixer;
        Self {
            music_gain: mixer.music_gain,
            mic_gain: mixer.mic_gain,
            echo_level: mixer.echo_level,
            echo_delay_ms: u32::try_from(mixer.echo_delay_frames / FRAMES_PER_MS)
                .unwrap_or(MAX_ECHO_DELAY_MS)
                .max(1),
            echo_feedback: mixer.echo_feedback,
            reverb_level: mixer.reverb_level,
            effects_bypass: mixer.effects_bypass,
            mic_monitor: settings.mic_monitor,
        }
    }

    /// {
    ///   責務: [`into_settings`: 本文を検証し、範囲外の有限値は範囲へ丸めて内部設定にする]
    ///   処理: [1: NaN/無限大を拒否する, 2: 音量・エコー・リバーブ・feedbackを範囲へclampする, 3: 遅延msを1..=1000にclampしてframeへ換算する]
    ///   引数: []
    ///   戻り値: [内部設定。非有限値なら`Err`の項目名]
    /// }
    fn into_settings(self) -> Result<MonitorSettings, &'static str> {
        for (name, value) in [
            ("music_gain", self.music_gain),
            ("mic_gain", self.mic_gain),
            ("echo_level", self.echo_level),
            ("echo_feedback", self.echo_feedback),
            ("reverb_level", self.reverb_level),
        ] {
            if !value.is_finite() {
                return Err(name);
            }
        }
        let delay_ms = self.echo_delay_ms.clamp(1, MAX_ECHO_DELAY_MS) as usize;
        Ok(MonitorSettings {
            mixer: MixerSettings {
                music_gain: self.music_gain.clamp(0.0, MAX_GAIN),
                mic_gain: self.mic_gain.clamp(0.0, MAX_GAIN),
                echo_level: self.echo_level.clamp(0.0, 1.0),
                echo_delay_frames: (delay_ms * FRAMES_PER_MS).min(MAX_ECHO_DELAY_FRAMES),
                echo_feedback: self.echo_feedback.clamp(0.0, MAX_ECHO_FEEDBACK),
                reverb_level: self.reverb_level.clamp(0.0, 1.0),
                effects_bypass: self.effects_bypass,
            },
            mic_monitor: self.mic_monitor,
        })
    }
}

/// {
///   責務: [`routes`: `/mixer`のGET/PUTを返す]
///   戻り値: [`AppState`を使うRouter。認証は呼び出し側の層が担う]
/// }
pub(crate) fn routes() -> Router<AppState> {
    Router::new().route("/mixer", get(read).put(write))
}

/// {
///   責務: [`read`: 現在のミキサー設定を返す]
///   引数: [state: Core状態]
///   戻り値: [現在の設定]
/// }
async fn read(State(state): State<AppState>) -> Json<MixerSettingsBody> {
    Json(MixerSettingsBody::from_settings(&state.mixer.get()))
}

/// {
///   責務: [`write`: 設定を検証・clampして保存し、実際に適用した値を返す]
///   処理: [1: 停止中なら拒否する, 2: 非有限値を422で拒否する, 3: 共有設定を更新する, 4: 適用値を返す]
///   引数: [state: Core状態, body: 新しい設定]
///   戻り値: [適用された設定。拒否時は`ApiError`]
/// }
async fn write(
    State(state): State<AppState>,
    Json(body): Json<MixerSettingsBody>,
) -> Result<Json<MixerSettingsBody>, ApiError> {
    state.ensure_running()?;
    let settings = body.into_settings().map_err(|name| {
        ApiError(
            StatusCode::UNPROCESSABLE_ENTITY,
            format!("invalid_request: {name} must be a finite number"),
        )
    })?;
    state
        .mixer
        .set(settings)
        .map_err(|error| ApiError(StatusCode::UNPROCESSABLE_ENTITY, error.to_string()))?;
    Ok(Json(MixerSettingsBody::from_settings(&state.mixer.get())))
}

#[cfg(test)]
#[allow(clippy::float_cmp)]
mod tests {
    use super::*;

    /// {
    ///   責務: [`body`: テスト用の有効な本文を作る]
    ///   戻り値: [既定値に近い本文]
    /// }
    fn body() -> MixerSettingsBody {
        MixerSettingsBody {
            music_gain: 1.0,
            mic_gain: 1.0,
            echo_level: 0.25,
            echo_delay_ms: 200,
            echo_feedback: 0.35,
            reverb_level: 0.2,
            effects_bypass: false,
            mic_monitor: false,
        }
    }

    #[test]
    fn default_settings_round_trip_through_the_body() {
        let defaults = MonitorSettings::default();
        let back = MixerSettingsBody::from_settings(&defaults)
            .into_settings()
            .unwrap();
        assert_eq!(back, defaults);
    }

    #[test]
    fn finite_out_of_range_values_are_clamped_and_non_finite_refused() {
        let wild = MixerSettingsBody {
            music_gain: 9.0,
            mic_gain: -1.0,
            echo_level: 5.0,
            echo_delay_ms: 0,
            echo_feedback: 2.0,
            reverb_level: -3.0,
            ..body()
        };
        let clamped = wild.into_settings().unwrap().mixer;
        assert_eq!(clamped.music_gain, 2.0);
        assert_eq!(clamped.mic_gain, 0.0);
        assert_eq!(clamped.echo_level, 1.0);
        assert_eq!(clamped.echo_delay_frames, FRAMES_PER_MS);
        assert_eq!(clamped.echo_feedback, 0.9);
        assert_eq!(clamped.reverb_level, 0.0);
        let late = MixerSettingsBody {
            echo_delay_ms: u32::MAX,
            ..body()
        };
        assert_eq!(
            late.into_settings().unwrap().mixer.echo_delay_frames,
            MAX_ECHO_DELAY_FRAMES
        );
        let nan = MixerSettingsBody {
            mic_gain: f32::NAN,
            ..body()
        };
        assert_eq!(nan.into_settings().unwrap_err(), "mic_gain");
    }
}
