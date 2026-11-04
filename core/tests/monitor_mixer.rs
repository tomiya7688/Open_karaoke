//! Engine-level checks for the monitored output path (#13): the mixer is fed from the
//! output callback, raw microphone capture stays untouched for scoring, and settings
//! reach the callback through the shared control.
//!
//! Exact float comparison is intentional: dry paths must be bit-exact.
#![allow(
    clippy::float_cmp,
    clippy::cast_precision_loss,
    clippy::chunks_exact_to_as_chunks
)]

use open_karaoke_core::{
    audio_engine::AudioIo,
    mixer::MixerSettings,
    mixer_control::{MixerControl, MonitorMixer, MonitorSettings},
};

fn dry_settings(mic_monitor: bool) -> MonitorSettings {
    MonitorSettings {
        mixer: MixerSettings {
            music_gain: 1.0,
            mic_gain: 1.0,
            echo_level: 0.0,
            echo_delay_frames: 100,
            echo_feedback: 0.0,
            reverb_level: 0.0,
            effects_bypass: true,
        },
        mic_monitor,
    }
}

fn engine(settings: MonitorSettings) -> (AudioIo, MonitorMixer, MixerControl) {
    let control = MixerControl::new(settings).unwrap();
    let io = AudioIo::with_mixer_control(4096, control.clone()).unwrap();
    io.set_playing(true);
    let monitor = MonitorMixer::new(control.clone()).unwrap();
    (io, monitor, control)
}

fn queue_music(io: &AudioIo, frames: &[[f32; 2]]) {
    assert_eq!(io.enqueue_stereo(frames).unwrap(), frames.len());
}

#[test]
fn monitor_off_passes_music_through_and_never_taps_the_microphone() {
    let (io, mut monitor, _) = engine(dry_settings(false));
    queue_music(&io, &[[0.25, -0.25]; 8]);
    io.capture_mock(&[0.5; 8], 1);
    let mut out = [0.0_f32; 16];
    io.render_monitored_mock(&mut out, 2, &mut monitor);
    for frame in out.chunks_exact(2) {
        assert_eq!(frame, [0.25, -0.25]);
    }
    assert_eq!(io.counters().playback_sample_position, 8);
}

#[test]
fn microphone_monitor_is_sample_aligned_with_zero_added_latency() {
    let (io, mut monitor, _) = engine(dry_settings(true));
    queue_music(&io, &[[0.0, 0.0]; 16]);
    let mut mic = [0.0_f32; 16];
    mic[3] = 0.5;
    io.capture_mock(&mic, 1);
    let mut out = [0.0_f32; 32];
    io.render_monitored_mock(&mut out, 2, &mut monitor);
    for (index, frame) in out.chunks_exact(2).enumerate() {
        let expected = if index == 3 { 0.5 } else { 0.0 };
        assert_eq!(frame, [expected, expected], "frame {index}");
    }
}

#[test]
fn scoring_still_reads_the_raw_microphone_whatever_the_mixer_does() {
    let mut settings = dry_settings(true);
    settings.mixer.effects_bypass = false;
    settings.mixer.echo_level = 1.0;
    settings.mixer.reverb_level = 1.0;
    settings.mixer.mic_gain = 0.0;
    let (io, mut monitor, _) = engine(settings);
    queue_music(&io, &[[0.1, 0.1]; 64]);
    let raw: Vec<f32> = (0..64).map(|n| n as f32 / 100.0).collect();
    io.capture_mock(&raw, 1);
    let mut out = [0.0_f32; 128];
    io.render_monitored_mock(&mut out, 2, &mut monitor);
    let mut scored = [0.0_f32; 64];
    assert_eq!(io.read_microphone(&mut scored), 64);
    assert_eq!(scored.to_vec(), raw, "raw capture is bit-exact for scoring");
}

#[test]
fn settings_written_by_the_control_side_reach_the_next_callback() {
    let (io, mut monitor, control) = engine(dry_settings(false));
    queue_music(&io, &[[0.8, 0.8]; 4]);
    let mut out = [0.0_f32; 8];
    io.render_monitored_mock(&mut out, 2, &mut monitor);
    assert_eq!(out[0], 0.8);
    let mut quieter = dry_settings(false);
    quieter.mixer.music_gain = 0.5;
    control.set(quieter).unwrap();
    // The first block after a change ramps; the second is steady.
    queue_music(&io, &[[0.8, 0.8]; 2048]);
    let mut ramp = vec![0.0_f32; 4096];
    io.render_monitored_mock(&mut ramp, 2, &mut monitor);
    queue_music(&io, &[[0.8, 0.8]; 4]);
    io.render_monitored_mock(&mut out, 2, &mut monitor);
    assert!((out[0] - 0.4).abs() < 1e-6, "got {}", out[0]);
    let mut bad = quieter;
    bad.mixer.music_gain = f32::NAN;
    assert!(control.set(bad).is_err());
    assert_eq!(control.get(), quieter, "a refused write changes nothing");
}

#[test]
fn stale_microphone_backlog_is_dropped_instead_of_adding_latency() {
    let (io, mut monitor, _) = engine(dry_settings(true));
    let raw: Vec<f32> = (0..1000).map(|n| n as f32 / 10_000.0).collect();
    io.capture_mock(&raw, 1);
    queue_music(&io, &[[0.0, 0.0]; 64]);
    let mut out = [0.0_f32; 128];
    io.render_monitored_mock(&mut out, 2, &mut monitor);
    // Two callback blocks (128 frames) are kept, so playback starts 128 frames from the end.
    assert!((out[0] - 872.0 / 10_000.0).abs() < 1e-6, "got {}", out[0]);
}

#[test]
fn mono_and_paused_outputs_keep_engine_behaviour() {
    let (io, mut monitor, _) = engine(dry_settings(false));
    queue_music(&io, &[[0.2, 0.4]; 2]);
    let mut mono = [0.0_f32; 3];
    io.render_monitored_mock(&mut mono, 1, &mut monitor);
    assert!((mono[0] - 0.3).abs() < 1e-6);
    assert_eq!(mono[2], 0.0, "underrun is silence");
    io.set_playing(false);
    let mut paused = [1.0_f32; 4];
    io.render_monitored_mock(&mut paused, 2, &mut monitor);
    assert_eq!(paused, [0.0; 4]);
}
