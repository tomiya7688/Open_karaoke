//! Explicit, opt-in WASAPI hardware smoke test. No automatic device access in CI.

use std::{error::Error, io, thread, time::Duration};

use open_karaoke_core::{
    audio_clock::ClockSession,
    audio_device::{AudioDeviceStreams, WasapiMode, enumerate_devices},
    audio_engine::DEFAULT_BUFFER_FRAMES,
    audio_wav::WavFeeder,
};

/// {
///   責務: [main: 明示起動した WASAPI の再生と raw capture を診断する]
///   処理: [1: デバイスと WAV を準備する, 2: 制御側で clock の不連続を観測する, 3: stream を停止する]
///   引数: [CLI: 正規化済み WAV のパス]
///   戻り値: [`Result`: setup または clock 処理の失敗]
/// }
fn main() -> Result<(), Box<dyn Error>> {
    let path = std::env::args_os()
        .nth(1)
        .ok_or("Usage: cargo run --example wasapi_smoke -- <normalized-48k-stereo-f32.wav>")?;
    let devices = enumerate_devices().map_err(io::Error::other)?;
    for device in devices {
        println!(
            "{}: {} (default={}, 48k={}, host={})",
            match device.direction {
                open_karaoke_core::audio_device::DeviceDirection::Output => "output",
                open_karaoke_core::audio_device::DeviceDirection::Input => "input",
            },
            device.name,
            device.is_default,
            device.supports_48khz,
            device.host
        );
    }
    let device = AudioDeviceStreams::open(None, None, WasapiMode::Shared, DEFAULT_BUFFER_FRAMES)
        .map_err(io::Error::other)?;
    let io = device.audio();
    let mut clock = ClockSession::new();
    clock.observe_audio(io.counters())?;
    let mut feeder = WavFeeder::start(io.clone(), path)?;

    // Prebuffer on the disk thread before enabling the physical stream.
    while feeder.status().enqueued_samples < 4096 && !feeder.status().finished {
        if feeder.status().failed {
            return Err("Could not load normalized accompaniment".into());
        }
        thread::sleep(Duration::from_millis(5));
    }
    if feeder.status().failed {
        return Err("Could not read normalized accompaniment".into());
    }
    if device.has_microphone() {
        device.start_microphone().map_err(io::Error::other)?;
    } else {
        eprintln!("No default microphone: testing playback only");
    }
    device.play().map_err(io::Error::other)?;
    clock.observe_audio(io.counters())?;
    let mut last_report = 0_u64;
    loop {
        thread::sleep(Duration::from_millis(10));
        let status = feeder.status();
        let counters = io.counters();
        if let Some(change) = clock.observe_audio(counters)? {
            eprintln!(
                "Clock invalidated: epoch={}, reason={:?}; fresh anchors required",
                change.epoch.sequence(),
                change.reason
            );
        }
        // This example drains raw PCM without performing any scoring or effects.
        let mut microphone = [0.0_f32; 960];
        let _ = io.read_microphone(&mut microphone);
        if counters.output_device_error || counters.input_device_error || status.failed {
            eprintln!("Detected audio-device or disk-feeder error");
            break;
        }
        if counters.playback_sample_position >= last_report + 48_000 {
            println!(
                "epoch={}, sample={} / {}, queued={}, mic_received={}, out_underrun={}, in_overrun={}",
                clock.epoch().sequence(),
                counters.playback_sample_position,
                status.duration_samples,
                counters.output_queued_frames,
                counters.microphone_frames_received,
                counters.output_underrun_frames,
                counters.input_overrun_frames
            );
            last_report = counters.playback_sample_position;
        }
        if status.finished && counters.playback_sample_position >= status.duration_samples {
            break;
        }
    }
    device.pause().map_err(io::Error::other)?;
    clock.observe_audio(io.counters())?;
    if device.has_microphone() {
        device.stop_microphone().map_err(io::Error::other)?;
    }
    feeder.stop();
    println!("Final counters: {:?}", io.counters());
    Ok(())
}
