//! Synthetic CPU/memory load evaluator for the mock audio path (Issue #43, slice 43.1).
//! No hardware, WASAPI or real inference is measured; results are only a relative
//! baseline-vs-loaded comparison on this machine and must stay `Unverified` for real devices.

use std::{
    env,
    error::Error,
    fs, io,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

use open_karaoke_core::audio_engine::AudioIo;

const CALLBACK_FRAMES: usize = 480;
const CALLBACK_PERIOD: Duration = Duration::from_millis(10);

/// {
///   責務: [`PhaseReport`: 1区間の mock callback 所要時間と underrun を集計する]
///   フィールド: [`callbacks`: 実行回数, `underrun_frames`: 増えた無音補完数, `p50_us`/`p95_us`/`p99_us`/`max_us`: callback処理時間, `late_wakeups`: 周期を2倍以上超えた回数]
/// }
struct PhaseReport {
    callbacks: usize,
    underrun_frames: u64,
    p50_us: u64,
    p95_us: u64,
    p99_us: u64,
    max_us: u64,
    late_wakeups: usize,
}

/// {
///   責務: [percentile: 昇順済みの値から百分位を返す]
///   引数: [sorted: 昇順の値, pct: 0..=100]
///   戻り値: [u64: 該当値。空なら0]
/// }
fn percentile(sorted: &[u64], pct: usize) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let index = (sorted.len() * pct).div_ceil(100).clamp(1, sorted.len()) - 1;
    sorted[index]
}

/// {
///   責務: [`run_phase`: 固定周期でmock出力callbackを回しcallback処理時間とunderrunを測る]
///   処理: [1: queueを先に満たす, 2: 周期ごとにproducerが補充しcallbackを時間測定する, 3: 集計する]
///   引数: [io: 音声経路, duration: 測定時間]
///   戻り値: [`PhaseReport`: 区間集計]
/// }
fn run_phase(io: &AudioIo, duration: Duration) -> Result<PhaseReport, Box<dyn Error>> {
    let silence = vec![[0.1_f32, -0.1_f32]; CALLBACK_FRAMES * 4];
    let mut buffer = vec![0.0_f32; CALLBACK_FRAMES * 2];
    let before = io.counters().output_underrun_frames;
    let mut times = Vec::new();
    let mut late = 0_usize;
    let start = Instant::now();
    let mut next = start;
    while start.elapsed() < duration {
        // Producer (control side) tops the queue up; the callback below must not depend on it.
        io.enqueue_stereo(&silence)?;
        let began = Instant::now();
        io.render_mock(&mut buffer, 2);
        times.push(u64::try_from(began.elapsed().as_micros()).unwrap_or(u64::MAX));
        next += CALLBACK_PERIOD;
        let now = Instant::now();
        if now > next + CALLBACK_PERIOD {
            late += 1;
            next = now;
        } else if next > now {
            thread::sleep(next - now);
        }
    }
    times.sort_unstable();
    Ok(PhaseReport {
        callbacks: times.len(),
        underrun_frames: io.counters().output_underrun_frames - before,
        p50_us: percentile(&times, 50),
        p95_us: percentile(&times, 95),
        p99_us: percentile(&times, 99),
        max_us: times.last().copied().unwrap_or(0),
        late_wakeups: late,
    })
}

/// {
///   責務: [`phase_json`: 区間集計をJSONへ変換する]
///   引数: [report: 区間集計]
///   戻り値: [Value: JSON]
/// }
fn phase_json(report: &PhaseReport) -> serde_json::Value {
    serde_json::json!({
        "callbacks": report.callbacks,
        "underrun_frames": report.underrun_frames,
        "callback_p50_us": report.p50_us,
        "callback_p95_us": report.p95_us,
        "callback_p99_us": report.p99_us,
        "callback_max_us": report.max_us,
        "late_wakeups": report.late_wakeups
    })
}

fn main() -> Result<(), Box<dyn Error>> {
    let args: Vec<String> = env::args().collect();
    let output = PathBuf::from(
        args.get(1)
            .cloned()
            .unwrap_or_else(|| "artifacts/inference-load.json".to_owned()),
    );
    let seconds: u64 = args.get(2).and_then(|v| v.parse().ok()).unwrap_or(3);
    let load_threads: usize = args
        .get(3)
        .and_then(|v| v.parse().ok())
        .unwrap_or_else(|| thread::available_parallelism().map_or(2, std::num::NonZero::get));
    let ballast_mib: usize = args.get(4).and_then(|v| v.parse().ok()).unwrap_or(64);
    if !(1..=600).contains(&seconds) || load_threads > 256 || ballast_mib > 4096 {
        return Err(io::Error::other(
            "seconds must be 1..=600, load_threads <= 256 and ballast_mib <= 4096",
        )
        .into());
    }

    let io = AudioIo::new(24_000)?;
    io.set_playing(true);
    let duration = Duration::from_secs(seconds);
    let baseline = run_phase(&io, duration)?;

    // Synthetic "inference": busy float loops plus a touched memory ballast.
    let stop = Arc::new(AtomicBool::new(false));
    let mut ballast = vec![0_u8; ballast_mib * 1024 * 1024];
    for page in ballast.chunks_mut(4096) {
        page[0] = 1;
    }
    let workers: Vec<_> = (0..load_threads)
        .map(|_| {
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                let mut x = 1.0_f64;
                while !stop.load(Ordering::Relaxed) {
                    for _ in 0..10_000 {
                        x = (x * 1.000_001 + 0.5).sin().abs() + 1.0;
                    }
                }
                x
            })
        })
        .collect();
    let loaded = run_phase(&io, duration);
    stop.store(true, Ordering::Relaxed);
    let mut worker_panics = 0_usize;
    for worker in workers {
        if worker.join().is_err() {
            worker_panics += 1;
        }
    }
    let loaded = loaded?;
    if worker_panics > 0 {
        return Err(io::Error::other("synthetic load worker panicked; result invalid").into());
    }
    drop(ballast);

    let report = serde_json::json!({
        "evaluator": "inference-load-synthetic-v1",
        "mock_audio_path_only": true,
        "hardware_verified": false,
        "real_inference_measured": false,
        "conditions": {
            "phase_seconds": seconds,
            "load_threads": load_threads,
            "ballast_mib": ballast_mib,
            "callback_frames": CALLBACK_FRAMES,
            "percentile_method": "nearest-rank over render_mock processing time only",
            "late_wakeup_definition": "scheduler woke more than 2 periods (20 ms) after the planned time; separate from output_underrun_frames",
            "os": env::consts::OS
        },
        "baseline": phase_json(&baseline),
        "loaded": phase_json(&loaded),
        "passed": null,
        "note": "Relative comparison only. No quality gate; Windows real-device results remain Unverified."
    });
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(&output, serde_json::to_vec_pretty(&report)?)?;
    println!("{}", serde_json::to_string(&report)?);
    Ok(())
}
