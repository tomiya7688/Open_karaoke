//! Device-free composition tests for Issue #107 slice 107.3 (`KaraokeSessionController`).
//! Each test names the acceptance item (T#) of docs/design/issue-107-karaoke-session.md.
//! Songs and device leases are fakes; no audio device is opened.

#![allow(clippy::doc_markdown)] // JSON-style declaration comments name snake_case fields

use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};

use open_karaoke_core::{
    karaoke_controller::{
        ControllerError, DeviceLease, DeviceLeaseProvider, KaraokeSessionController, PreparedSong,
        SongSource,
    },
    karaoke_session::{
        CommandEnvelope, EventOrigin, KaraokeCommand, KaraokeError, KaraokeState, ScoringMode,
    },
    karaoke_session_api::KaraokeSessionHost,
};
use uuid::Uuid;

const DURATION: u64 = 48_000 * 10;

/// {
///   責務: [`FakeSongs`: 既知の1曲だけを返すSongSource]
///   フィールド: [known: 読める曲のID]
/// }
struct FakeSongs {
    known: Uuid,
}

impl SongSource for FakeSongs {
    fn open(&self, song_id: Uuid) -> Result<PreparedSong, String> {
        if song_id == self.known {
            Ok(PreparedSong {
                duration_samples: DURATION,
            })
        } else {
            Err("missing".to_string())
        }
    }
}

/// {
///   責務: [`FakeLeases`: 全controllerで共有する排他のデバイス貸出(同時に1つ)]
///   フィールド: [busy: 貸出中か, acquired: 取得成功回数]
/// }
#[derive(Default)]
struct FakeLeases {
    busy: Arc<AtomicBool>,
    acquired: AtomicUsize,
}

/// {
///   責務: [`FakeLease`: dropでbusyを下ろす貸出の実体]
/// }
struct FakeLease(Arc<AtomicBool>);

impl DeviceLease for FakeLease {}

impl Drop for FakeLease {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

impl DeviceLeaseProvider for FakeLeases {
    fn acquire(&self, _session_id: Uuid) -> Result<Box<dyn DeviceLease>, String> {
        if self.busy.swap(true, Ordering::SeqCst) {
            return Err("busy".to_string());
        }
        self.acquired.fetch_add(1, Ordering::SeqCst);
        Ok(Box::new(FakeLease(Arc::clone(&self.busy))))
    }
}

/// {
///   責務: [`Env`: controller・host・fakeをまとめる]
///   フィールド: [controller, host, instance: 世代ID, song: 読める曲, leases]
/// }
struct Env {
    controller: KaraokeSessionController,
    host: KaraokeSessionHost,
    instance: Uuid,
    song: Uuid,
    leases: Arc<FakeLeases>,
}

/// {
///   責務: [`env_with`: 貸出を共有できる環境を作る]
///   引数: [leases, song]
///   戻り値: [`Env`]
/// }
fn env_with(leases: Arc<FakeLeases>, song: Uuid) -> Env {
    let instance = Uuid::new_v4();
    let host = KaraokeSessionHost::new(instance);
    let controller = KaraokeSessionController::new(
        host.clone(),
        Arc::new(FakeSongs { known: song }),
        leases.clone(),
    );
    Env {
        controller,
        host,
        instance,
        song,
        leases,
    }
}

/// {
///   責務: [`env`: 独立した環境を作る]
///   戻り値: [`Env`]
/// }
fn env() -> Env {
    env_with(Arc::new(FakeLeases::default()), Uuid::new_v4())
}

/// {
///   責務: [`command`: 環境の世代宛のコマンド封筒を作る]
///   引数: [env, session, revision, command]
///   戻り値: [`CommandEnvelope`]
/// }
fn command(env: &Env, session: Uuid, revision: u64, command: KaraokeCommand) -> CommandEnvelope {
    CommandEnvelope {
        core_instance_id: env.instance,
        session_id: session,
        command_id: Uuid::new_v4(),
        expected_revision: revision,
        command,
    }
}

/// {
///   責務: [`origin`: 環境の世代の観測出所を作る]
///   引数: [env, session, epoch]
///   戻り値: [`EventOrigin`]
/// }
fn origin(env: &Env, session: Uuid, epoch: u64) -> EventOrigin {
    EventOrigin {
        core_instance_id: env.instance,
        session_id: session,
        epoch,
    }
}

/// T1 (AC1/AC2): 選曲でpreparing、準備でreadyになり、貸出を保持してplayまで進める。
#[test]
fn t1_select_prepare_reaches_ready_and_can_play() {
    let env = env();
    let created = env
        .controller
        .select_song(env.song, ScoringMode::WholeSong)
        .unwrap();
    assert_eq!(created.state, KaraokeState::Preparing);
    assert_eq!(created.duration_samples, DURATION);
    assert!(!env.controller.holds_lease());

    let ready = env.controller.prepare(created.session_id).unwrap();
    assert_eq!(ready.state, KaraokeState::Ready);
    assert!(env.controller.holds_lease());

    let played = env
        .host
        .execute(command(
            &env,
            ready.session_id,
            ready.state_revision,
            KaraokeCommand::Play,
        ))
        .unwrap();
    assert_eq!(played.snapshot.state, KaraokeState::Playing);
}

/// T2: 読めない曲ではセッションを作らず、貸出も取らない。
#[test]
fn t2_unreadable_song_creates_no_session() {
    let env = env();
    let error = env
        .controller
        .select_song(Uuid::new_v4(), ScoringMode::Practice)
        .unwrap_err();
    assert_eq!(error.code(), "song_unavailable");
    assert!(matches!(error, ControllerError::SongUnavailable(_)));
    assert!(env.host.current_snapshot().is_none());
    assert_eq!(env.leases.acquired.load(Ordering::SeqCst), 0);
}

/// T3 (AC9): デバイスが貸出中なら2つ目のセッションはfailed(device_unavailable)になる。
#[test]
fn t3_second_session_fails_when_the_device_is_leased() {
    let leases = Arc::new(FakeLeases::default());
    let song = Uuid::new_v4();
    let first = env_with(leases.clone(), song);
    let second = env_with(leases, song);
    assert_eq!(
        first
            .controller
            .start(song, ScoringMode::WholeSong)
            .unwrap()
            .state,
        KaraokeState::Ready
    );
    let failed = second
        .controller
        .start(song, ScoringMode::WholeSong)
        .unwrap();
    assert_eq!(failed.state, KaraokeState::Failed);
    assert_eq!(failed.error_code.as_deref(), Some("device_unavailable"));
    assert!(!second.controller.holds_lease());
}

/// T4 (AC9): 同一host内の2つ目の選曲は、1つ目が非終端の間は session_busy で拒否される。
#[test]
fn t4_second_selection_is_busy_until_the_first_ends() {
    let env = env();
    let first = env
        .controller
        .start(env.song, ScoringMode::WholeSong)
        .unwrap();
    let busy = env
        .controller
        .select_song(env.song, ScoringMode::WholeSong)
        .unwrap_err();
    assert_eq!(busy, ControllerError::Session(KaraokeError::SessionBusy));

    env.host
        .execute(command(&env, first.session_id, 0, KaraokeCommand::Stop))
        .unwrap();
    // 終端後は貸出が返却され、次の選曲で新しいセッションを作れる。
    let next = env
        .controller
        .start(env.song, ScoringMode::WholeSong)
        .unwrap();
    assert_eq!(next.state, KaraokeState::Ready);
    assert_ne!(next.session_id, first.session_id);
}

/// T5: preparing中の取消(stop)後のprepareは貸出を保持しない。
#[test]
fn t5_cancelled_preparation_does_not_keep_the_lease() {
    let env = env();
    let created = env
        .controller
        .select_song(env.song, ScoringMode::WholeSong)
        .unwrap();
    env.host
        .execute(command(&env, created.session_id, 0, KaraokeCommand::Stop))
        .unwrap();
    let after = env.controller.prepare(created.session_id).unwrap();
    assert_eq!(after.state, KaraokeState::Stopped);
    assert!(!env.controller.holds_lease());
    assert!(!env.leases.busy.load(Ordering::SeqCst));
}

/// T6 (AC7): stream errorはinterruptedになり貸出を返す。自動復旧・自動再生はしない。
#[test]
fn t6_stream_error_interrupts_and_releases_without_auto_recovery() {
    let env = env();
    let ready = env
        .controller
        .start(env.song, ScoringMode::WholeSong)
        .unwrap();
    let playing = env
        .host
        .execute(command(
            &env,
            ready.session_id,
            ready.state_revision,
            KaraokeCommand::Play,
        ))
        .unwrap()
        .snapshot;
    let interrupted = env
        .controller
        .stream_error(
            origin(&env, playing.session_id, playing.transport_epoch),
            "stream_error",
        )
        .unwrap();
    assert_eq!(interrupted.state, KaraokeState::Interrupted);
    assert!(!env.controller.holds_lease());
    assert!(!env.leases.busy.load(Ordering::SeqCst));
    // 中断中のplayはdevice_unavailable。
    let refused = env
        .host
        .execute(command(
            &env,
            playing.session_id,
            interrupted.state_revision,
            KaraokeCommand::Play,
        ))
        .unwrap_err();
    assert_eq!(refused, KaraokeError::DeviceUnavailable);
}

/// T7 (AC7): 復旧は貸出を取り直せた時だけ。再生中だったものはpausedに戻り、自動再生しない。
#[test]
fn t7_recover_requires_the_lease_and_never_autoplays() {
    let env = env();
    let ready = env
        .controller
        .start(env.song, ScoringMode::WholeSong)
        .unwrap();
    let playing = env
        .host
        .execute(command(
            &env,
            ready.session_id,
            ready.state_revision,
            KaraokeCommand::Play,
        ))
        .unwrap()
        .snapshot;
    env.controller
        .stream_error(
            origin(&env, playing.session_id, playing.transport_epoch),
            "device_removed",
        )
        .unwrap();

    // 他者がデバイスを奪っている間は復旧できず、中断のまま。
    env.leases.busy.store(true, Ordering::SeqCst);
    let error = env.controller.recover(playing.session_id).unwrap_err();
    assert_eq!(
        error,
        ControllerError::Session(KaraokeError::DeviceUnavailable)
    );
    assert_eq!(
        env.host.snapshot(playing.session_id).unwrap().state,
        KaraokeState::Interrupted
    );
    env.leases.busy.store(false, Ordering::SeqCst);

    let recovered = env.controller.recover(playing.session_id).unwrap();
    assert_eq!(recovered.state, KaraokeState::Paused);
    assert!(env.controller.holds_lease());
}

/// T8: 中断中でないセッションの復旧は拒否される。
#[test]
fn t8_recover_is_refused_unless_interrupted() {
    let env = env();
    let ready = env
        .controller
        .start(env.song, ScoringMode::WholeSong)
        .unwrap();
    assert_eq!(
        env.controller.recover(ready.session_id).unwrap_err(),
        ControllerError::Session(KaraokeError::NotReady)
    );
}

/// T9 (AC8): 再起動した新しい世代のhost/controllerは旧Sessionを復活させず、旧instance宛を拒否する。
#[test]
fn t9_restart_does_not_resurrect_the_old_session() {
    let leases = Arc::new(FakeLeases::default());
    let song = Uuid::new_v4();
    let old = env_with(leases.clone(), song);
    let ready = old.controller.start(song, ScoringMode::WholeSong).unwrap();
    let old_command = command(
        &old,
        ready.session_id,
        ready.state_revision,
        KaraokeCommand::Play,
    );
    // 旧Coreが終了すると貸出も返却される。
    drop(old);
    assert!(!leases.busy.load(Ordering::SeqCst));

    let new = env_with(leases, song);
    assert!(new.host.current_snapshot().is_none());
    assert_eq!(
        new.host.execute(old_command).unwrap_err(),
        KaraokeError::StaleInstance
    );
    assert_eq!(
        new.controller.recover(ready.session_id).unwrap_err(),
        ControllerError::Session(KaraokeError::SessionNotFound)
    );
    let fresh = new.controller.start(song, ScoringMode::WholeSong).unwrap();
    assert_eq!(fresh.state, KaraokeState::Ready);
    assert_ne!(fresh.session_id, ready.session_id);
}

/// T10: クライアント経由のstopでも reconcile で貸出が返る。
#[test]
fn t10_reconcile_releases_the_lease_after_a_client_stop() {
    let env = env();
    let ready = env
        .controller
        .start(env.song, ScoringMode::WholeSong)
        .unwrap();
    assert!(!env.controller.reconcile());
    env.host
        .execute(command(&env, ready.session_id, 0, KaraokeCommand::Stop))
        .unwrap();
    assert!(env.controller.reconcile());
    assert!(!env.controller.holds_lease());
    assert!(!env.controller.reconcile());
}

/// Issue #133: 復旧と同時に再anchorしたclock世代を束縛し、旧世代の提示値を拒否する。
#[test]
fn t11_recover_with_clock_binds_the_new_clock_epoch() {
    use open_karaoke_core::{
        audio_clock::{ClockAnchor, ClockDomain, ClockSession, LatencyCalibration},
        karaoke_session::PresentedStreamSample,
    };
    let env = env();
    let ready = env
        .controller
        .start(env.song, ScoringMode::WholeSong)
        .unwrap();
    let playing = env
        .host
        .execute(command(
            &env,
            ready.session_id,
            ready.state_revision,
            KaraokeCommand::Play,
        ))
        .unwrap()
        .snapshot;
    let mut clock = ClockSession::new();
    let old_epoch = clock.epoch();
    env.controller
        .bind_clock_epoch(playing.session_id, old_epoch)
        .unwrap();
    env.controller
        .stream_error(
            origin(&env, playing.session_id, playing.transport_epoch),
            "device_removed",
        )
        .unwrap();

    // 復旧に失敗した間は旧世代のまま(束縛を進めない)。
    env.leases.busy.store(true, Ordering::SeqCst);
    let new_epoch = clock.begin_new_epoch().unwrap();
    assert!(
        env.controller
            .recover_with_clock(playing.session_id, new_epoch)
            .is_err()
    );
    env.leases.busy.store(false, Ordering::SeqCst);

    let recovered = env
        .controller
        .recover_with_clock(playing.session_id, new_epoch)
        .unwrap();
    assert_eq!(recovered.state, KaraokeState::Paused);
    clock
        .bind(ClockAnchor {
            epoch: new_epoch,
            domain: ClockDomain::Output,
            stream_sample: 0,
            song_sample: 0,
            drift_ppm: 0,
            latency: LatencyCalibration::Unknown,
        })
        .unwrap();
    // 旧世代の提示値は、再生状態に関わらず世代不一致(clock_stale_epoch)で拒否される。
    let err = env
        .host
        .report_presented_from_clock(
            origin(&env, recovered.session_id, recovered.transport_epoch),
            &clock,
            old_epoch,
            PresentedStreamSample(1),
        )
        .unwrap_err();
    assert_eq!(err.code(), "clock_stale_epoch");
}
