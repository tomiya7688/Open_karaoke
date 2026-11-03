//! Deterministic checks for the karaoke mixer (#13): impulse/latency, DSP behaviour,
//! saturation, bypass and isolation from the raw microphone block.
//!
//! Exact float comparison is intentional: these tests assert bit-for-bit determinism
//! and exact silence, and the small casts build test signals.
#![allow(clippy::float_cmp, clippy::cast_precision_loss)]

use open_karaoke_core::mixer::{
    ADDED_MONITOR_LATENCY_FRAMES, MAX_ECHO_DELAY_FRAMES, Mixer, MixerError, MixerSettings,
};

fn dry() -> MixerSettings {
    MixerSettings {
        music_gain: 1.0,
        mic_gain: 1.0,
        echo_level: 0.0,
        echo_delay_frames: 100,
        echo_feedback: 0.0,
        reverb_level: 0.0,
        effects_bypass: false,
    }
}

fn run(mixer: &mut Mixer, music: &[[f32; 2]], mic: &[f32]) -> Vec<[f32; 2]> {
    let mut output = vec![[0.0; 2]; music.len()];
    mixer.process(music, mic, &mut output).unwrap();
    output
}

fn impulse(length: usize, at: usize, value: f32) -> Vec<f32> {
    let mut block = vec![0.0; length];
    block[at] = value;
    block
}

fn silence(length: usize) -> Vec<[f32; 2]> {
    vec![[0.0; 2]; length]
}

#[test]
fn dry_microphone_impulse_has_no_added_latency() {
    let mut mixer = Mixer::new(dry()).unwrap();
    let mic = impulse(512, 37, 0.5);
    let output = run(&mut mixer, &silence(512), &mic);
    assert_eq!(ADDED_MONITOR_LATENCY_FRAMES, 0);
    for (index, frame) in output.iter().enumerate() {
        let expected = if index == 37 { 0.5 } else { 0.0 };
        assert_eq!(frame, &[expected, expected], "frame {index}");
    }
}

#[test]
fn gains_are_independent_and_exact_at_steady_state() {
    let settings = MixerSettings {
        music_gain: 0.5,
        mic_gain: 0.25,
        ..dry()
    };
    let mut mixer = Mixer::new(settings).unwrap();
    let music = vec![[0.8, -0.4]; 64];
    let mic = vec![0.4; 64];
    for frame in run(&mut mixer, &music, &mic) {
        assert!((frame[0] - (0.8 * 0.5 + 0.4 * 0.25)).abs() < 1e-6);
        assert!((frame[1] - (-0.4 * 0.5 + 0.4 * 0.25)).abs() < 1e-6);
    }
}

#[test]
fn echo_repeats_at_the_configured_delay_with_feedback() {
    let settings = MixerSettings {
        echo_level: 1.0,
        echo_delay_frames: 100,
        echo_feedback: 0.5,
        ..dry()
    };
    let mut mixer = Mixer::new(settings).unwrap();
    let output = run(&mut mixer, &silence(450), &impulse(450, 10, 0.5));
    assert!((output[10][0] - 0.5).abs() < 1e-6, "dry impulse");
    assert!((output[110][0] - 0.5).abs() < 1e-6, "first echo");
    assert!((output[210][0] - 0.25).abs() < 1e-6, "second echo");
    assert!((output[310][0] - 0.125).abs() < 1e-6, "third echo");
    assert!(output[60][0].abs() < 1e-9, "silence between repeats");
}

#[test]
fn reverb_is_silent_before_the_shortest_comb_and_decays_afterwards() {
    let settings = MixerSettings {
        reverb_level: 1.0,
        ..dry()
    };
    let mut mixer = Mixer::new(settings).unwrap();
    let length = 48_000 * 4;
    let output = run(&mut mixer, &silence(length), &impulse(length, 0, 0.5));
    assert!(
        output[1..1215].iter().all(|f| f[0].abs() < 1e-9),
        "pre-delay"
    );
    assert!(
        output[1215..4000].iter().any(|f| f[0].abs() > 1e-5),
        "tail starts"
    );
    assert!(
        output[length - 4000..].iter().all(|f| f[0].abs() < 1e-3),
        "tail ends"
    );
    assert!(output.iter().all(|f| f[0].is_finite() && f[0].abs() <= 1.0));
}

#[test]
fn bypass_removes_effects_and_does_not_replay_old_tails() {
    let effects = MixerSettings {
        echo_level: 1.0,
        echo_delay_frames: 100,
        echo_feedback: 0.5,
        reverb_level: 1.0,
        ..dry()
    };
    let mut mixer = Mixer::new(effects).unwrap();
    run(&mut mixer, &silence(64), &impulse(64, 0, 0.5));
    mixer
        .set_settings(MixerSettings {
            effects_bypass: true,
            ..effects
        })
        .unwrap();
    let bypassed = run(&mut mixer, &silence(6000), &impulse(6000, 5, 0.5));
    assert_eq!(bypassed[5], [0.5, 0.5]);
    assert!(
        bypassed
            .iter()
            .enumerate()
            .all(|(i, f)| i == 5 || f[0] == 0.0)
    );
    mixer.set_settings(effects).unwrap();
    let resumed = run(&mut mixer, &silence(6000), &vec![0.0; 6000]);
    assert!(
        resumed.iter().all(|f| f[0].abs() < 1e-9),
        "old tail returned"
    );
}

#[test]
fn raw_microphone_block_is_never_modified_and_scoring_signal_is_effect_free() {
    let effects = MixerSettings {
        echo_level: 1.0,
        echo_delay_frames: 50,
        echo_feedback: 0.8,
        reverb_level: 1.0,
        mic_gain: 0.1,
        ..dry()
    };
    let mic: Vec<f32> = (0..4096).map(|i| ((i % 64) as f32 / 64.0) - 0.5).collect();
    let original = mic.clone();
    let mut mixer = Mixer::new(effects).unwrap();
    let wet = run(&mut mixer, &silence(4096), &mic);
    assert_eq!(mic, original, "mixer wrote to the raw microphone block");
    let mut plain = Mixer::new(MixerSettings {
        effects_bypass: true,
        ..effects
    })
    .unwrap();
    assert_ne!(
        wet,
        run(&mut plain, &silence(4096), &mic),
        "effects had no effect"
    );
}

#[test]
fn output_saturates_at_one_and_counts_clipped_frames() {
    let settings = MixerSettings {
        music_gain: 2.0,
        ..dry()
    };
    let mut mixer = Mixer::new(settings).unwrap();
    let mut music = vec![[0.1, 0.1]; 100];
    music[10] = [0.9, -0.9];
    music[11] = [0.7, 0.0];
    let output = run(&mut mixer, &music, &vec![0.0; 100]);
    assert_eq!(output[10], [1.0, -1.0]);
    assert_eq!(output[11][0], 1.0);
    assert!(output.iter().all(|f| f.iter().all(|s| s.abs() <= 1.0)));
    let counters = mixer.counters();
    assert_eq!((counters.frames, counters.clipped_frames), (100, 2));
}

#[test]
fn invalid_input_samples_become_silence_and_are_counted() {
    let mut mixer = Mixer::new(dry()).unwrap();
    let mut mic = vec![0.0; 8];
    mic[2] = f32::NAN;
    let mut music = silence(8);
    music[3] = [f32::INFINITY, 0.25];
    let output = run(&mut mixer, &music, &mic);
    assert_eq!(output[2], [0.0, 0.0]);
    assert_eq!(output[3], [0.0, 0.25]);
    assert_eq!(mixer.counters().rejected_samples, 2);
}

#[test]
fn results_are_deterministic_and_independent_of_block_size() {
    let settings = MixerSettings {
        echo_level: 0.5,
        echo_delay_frames: 333,
        echo_feedback: 0.4,
        reverb_level: 0.5,
        ..dry()
    };
    let music: Vec<[f32; 2]> = (0..3000)
        .map(|i| [((i * 7) % 100) as f32 / 400.0, 0.0])
        .collect();
    let mic: Vec<f32> = (0..3000)
        .map(|i| ((i * 13) % 100) as f32 / 500.0 - 0.1)
        .collect();
    let mut whole = Mixer::new(settings).unwrap();
    let expected = run(&mut whole, &music, &mic);
    let mut again = Mixer::new(settings).unwrap();
    assert_eq!(expected, run(&mut again, &music, &mic), "not deterministic");
    let mut pieces = Mixer::new(settings).unwrap();
    let mut joined = Vec::new();
    for (m, c) in music.chunks(477).zip(mic.chunks(477)) {
        joined.extend(run(&mut pieces, m, c));
    }
    assert_eq!(expected, joined, "block size changed the result");
}

#[test]
fn setting_changes_ramp_within_one_block_without_a_step() {
    let mut mixer = Mixer::new(dry()).unwrap();
    mixer
        .set_settings(MixerSettings {
            mic_gain: 0.0,
            ..dry()
        })
        .unwrap();
    let output = run(&mut mixer, &silence(1000), &vec![1.0; 1000]);
    assert!(output[0][0] > 0.99, "starts near the old gain");
    assert!(output[999][0] < 0.01, "ends at the new gain");
    assert!(
        output.windows(2).all(|w| w[1][0] <= w[0][0]),
        "monotonic fade"
    );
    let settled = run(&mut mixer, &silence(10), &[1.0; 10]);
    assert!(settled.iter().all(|f| f[0] == 0.0));
}

#[test]
fn invalid_settings_and_lengths_are_rejected_without_changing_state() {
    for bad in [
        MixerSettings {
            music_gain: -0.1,
            ..dry()
        },
        MixerSettings {
            music_gain: 2.5,
            ..dry()
        },
        MixerSettings {
            mic_gain: f32::NAN,
            ..dry()
        },
        MixerSettings {
            echo_level: 1.5,
            ..dry()
        },
        MixerSettings {
            echo_delay_frames: 0,
            ..dry()
        },
        MixerSettings {
            echo_delay_frames: MAX_ECHO_DELAY_FRAMES + 1,
            ..dry()
        },
        MixerSettings {
            echo_feedback: 0.95,
            ..dry()
        },
        MixerSettings {
            reverb_level: f32::INFINITY,
            ..dry()
        },
    ] {
        assert!(matches!(
            Mixer::new(bad),
            Err(MixerError::InvalidSetting(_))
        ));
    }
    let mut mixer = Mixer::new(dry()).unwrap();
    assert!(
        mixer
            .set_settings(MixerSettings {
                music_gain: 9.0,
                ..dry()
            })
            .is_err()
    );
    assert_eq!(mixer.settings(), dry());
    let mut out = [[0.5; 2]; 4];
    assert_eq!(
        mixer.process(&silence(4), &[0.0; 3], &mut out),
        Err(MixerError::LengthMismatch)
    );
    assert_eq!(out, [[0.5; 2]; 4], "output must stay untouched");
    mixer.process(&[], &[], &mut []).unwrap();
}

#[test]
fn huge_finite_input_cannot_poison_the_effect_state() {
    let settings = MixerSettings {
        mic_gain: 2.0,
        echo_level: 1.0,
        echo_delay_frames: 10,
        echo_feedback: 0.9,
        reverb_level: 1.0,
        ..dry()
    };
    let mut mixer = Mixer::new(settings).unwrap();
    let mut mic = vec![0.0; 40_000];
    mic[0] = 3.0e38;
    let output = run(&mut mixer, &silence(40_000), &mic);
    assert!(output.iter().all(|f| f[0].is_finite() && f[0].abs() <= 1.0));
    assert!(
        output[20_000..].iter().all(|f| f[0].abs() < 1e-3),
        "state stayed poisoned"
    );
    assert!(mixer.counters().rejected_samples >= 1);
}
