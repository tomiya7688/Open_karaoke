//! Device-free synthetic tests for Issue #107 slice 107.1.
//! Each test names the acceptance item (AC#) of docs/design/issue-107-karaoke-session.md.

#![allow(clippy::many_single_char_names)] // short test locals (s=session, i=instance)

use open_karaoke_core::karaoke_session::{
    COMMAND_HISTORY_LIMIT, CommandEnvelope, Disposition, DowngradeReason, EVENT_LOG_LIMIT,
    EventOrigin, EventsAfter, KaraokeCommand, KaraokeError, KaraokeSession, KaraokeSessionRegistry,
    KaraokeState, PositionSource, ScoreEligibility, ScoringMode, SessionSnapshot,
};
use uuid::Uuid;

const DURATION: u64 = 48_000 * 10;

fn ready_session(mode: ScoringMode) -> (KaraokeSession, Uuid) {
    let instance = Uuid::new_v4();
    let mut session =
        KaraokeSession::new(Uuid::new_v4(), instance, Uuid::new_v4(), DURATION, mode).unwrap();
    assert!(session.finish_preparing(Ok(())));
    (session, instance)
}

fn send(
    session: &mut KaraokeSession,
    instance: Uuid,
    command: KaraokeCommand,
) -> Result<(Disposition, SessionSnapshot), KaraokeError> {
    let snapshot = session.snapshot();
    session
        .execute(CommandEnvelope {
            core_instance_id: instance,
            session_id: snapshot.session_id,
            command_id: Uuid::new_v4(),
            expected_revision: snapshot.state_revision,
            command,
        })
        .map(|outcome| (outcome.disposition, outcome.snapshot))
}

fn code(result: Result<(Disposition, SessionSnapshot), KaraokeError>) -> &'static str {
    result.unwrap_err().code()
}

#[test]
fn ac1_play_report_stop_without_ui() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let (d, snap) = send(&mut s, i, KaraokeCommand::Play).unwrap();
    assert_eq!(d, Disposition::Applied);
    assert_eq!(snap.state, KaraokeState::Playing);
    let got = s.report_presented(snap.transport_epoch, 4_800).unwrap();
    assert_eq!(got.position_song_sample, 4_800);
    assert_eq!(got.position_source, PositionSource::Presented);
    let (_, stopped) = send(&mut s, i, KaraokeCommand::Stop).unwrap();
    assert_eq!(stopped.state, KaraokeState::Stopped);
}

#[test]
fn ac2_transition_table_for_every_state_and_command() {
    // preparing
    let instance = Uuid::new_v4();
    let mut p = KaraokeSession::new(
        Uuid::new_v4(),
        instance,
        Uuid::new_v4(),
        DURATION,
        ScoringMode::Practice,
    )
    .unwrap();
    for c in [
        KaraokeCommand::Play,
        KaraokeCommand::Pause,
        KaraokeCommand::Seek { song_sample: 1 },
    ] {
        assert_eq!(code(send(&mut p, instance, c)), "not_ready");
    }
    assert_eq!(
        send(&mut p, instance, KaraokeCommand::Stop)
            .unwrap()
            .1
            .state,
        KaraokeState::Stopped
    );

    // ready
    let (mut r, i) = ready_session(ScoringMode::Practice);
    assert_eq!(
        send(&mut r, i, KaraokeCommand::Pause).unwrap().0,
        Disposition::NoOp
    );
    assert_eq!(
        send(&mut r, i, KaraokeCommand::Seek { song_sample: 100 })
            .unwrap()
            .1
            .position_song_sample,
        100
    );
    assert_eq!(
        send(&mut r, i, KaraokeCommand::Seek { song_sample: 100 })
            .unwrap()
            .0,
        Disposition::NoOp
    );
    assert_eq!(
        code(send(
            &mut r,
            i,
            KaraokeCommand::Seek {
                song_sample: DURATION
            }
        )),
        "out_of_range"
    );

    // playing -> paused -> playing, play twice is no-op
    assert_eq!(
        send(&mut r, i, KaraokeCommand::Play).unwrap().1.state,
        KaraokeState::Playing
    );
    assert_eq!(
        send(&mut r, i, KaraokeCommand::Play).unwrap().0,
        Disposition::NoOp
    );
    assert_eq!(
        send(&mut r, i, KaraokeCommand::Pause).unwrap().1.state,
        KaraokeState::Paused
    );
    assert_eq!(
        send(&mut r, i, KaraokeCommand::Pause).unwrap().0,
        Disposition::NoOp
    );
    assert_eq!(
        send(&mut r, i, KaraokeCommand::Play).unwrap().1.state,
        KaraokeState::Playing
    );

    // interrupted rejects commands except stop
    let epoch = r.snapshot().transport_epoch;
    r.interrupt(epoch, "device_removed").unwrap();
    for c in [
        KaraokeCommand::Play,
        KaraokeCommand::Pause,
        KaraokeCommand::Seek { song_sample: 1 },
    ] {
        assert_eq!(code(send(&mut r, i, c)), "device_unavailable");
    }
    assert_eq!(
        send(&mut r, i, KaraokeCommand::Stop).unwrap().1.state,
        KaraokeState::Stopped
    );

    // terminal: everything but stop is rejected; stop is a no-op returning the same snapshot
    for c in [
        KaraokeCommand::Play,
        KaraokeCommand::Pause,
        KaraokeCommand::Seek { song_sample: 1 },
    ] {
        assert_eq!(code(send(&mut r, i, c)), "terminal");
    }
    let before = r.snapshot();
    let (d, after) = send(&mut r, i, KaraokeCommand::Stop).unwrap();
    assert_eq!(d, Disposition::NoOp);
    assert_eq!(before, after);
}

#[test]
fn ac2_finished_when_presented_position_reaches_end() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let epoch = send(&mut s, i, KaraokeCommand::Play)
        .unwrap()
        .1
        .transport_epoch;
    let done = s.report_presented(epoch, DURATION).unwrap();
    assert_eq!(done.state, KaraokeState::Finished);
    assert_eq!(code(send(&mut s, i, KaraokeCommand::Play)), "terminal");
    // stop on finished keeps finished (not rewritten to stopped)
    assert_eq!(
        send(&mut s, i, KaraokeCommand::Stop).unwrap().1.state,
        KaraokeState::Finished
    );
}

#[test]
fn ac2_preparation_failure_and_zero_duration() {
    let mut s = KaraokeSession::new(
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        DURATION,
        ScoringMode::Practice,
    )
    .unwrap();
    assert!(s.finish_preparing(Err("asset_missing")));
    let snap = s.snapshot();
    assert_eq!(snap.state, KaraokeState::Failed);
    assert_eq!(snap.error_code.as_deref(), Some("asset_missing"));
    assert!(!s.finish_preparing(Ok(())));
    assert_eq!(
        KaraokeSession::new(
            Uuid::new_v4(),
            Uuid::new_v4(),
            Uuid::new_v4(),
            0,
            ScoringMode::Practice
        )
        .unwrap_err()
        .code(),
        "invalid_duration"
    );
}

#[test]
fn ac3_command_id_replay_returns_same_result_and_does_not_reapply() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let snap = s.snapshot();
    let envelope = CommandEnvelope {
        core_instance_id: i,
        session_id: snap.session_id,
        command_id: Uuid::new_v4(),
        expected_revision: snap.state_revision,
        command: KaraokeCommand::Play,
    };
    let first = s.execute(envelope).unwrap();
    let second = s.execute(envelope).unwrap();
    assert_eq!(first.disposition, Disposition::Applied);
    assert_eq!(second.disposition, Disposition::Replayed);
    assert_eq!(first.snapshot, second.snapshot);
    assert_eq!(s.snapshot().state_revision, first.snapshot.state_revision);
    // same id, different payload is refused
    let conflicting = CommandEnvelope {
        command: KaraokeCommand::Pause,
        ..envelope
    };
    assert_eq!(
        s.execute(conflicting).unwrap_err().code(),
        "command_id_conflict"
    );
}

#[test]
fn ac3_history_is_bounded_and_evicted_state_change_cannot_double_apply() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let snap = s.snapshot();
    let first = CommandEnvelope {
        core_instance_id: i,
        session_id: snap.session_id,
        command_id: Uuid::new_v4(),
        expected_revision: snap.state_revision,
        command: KaraokeCommand::Play,
    };
    s.execute(first).unwrap();
    for _ in 0..=COMMAND_HISTORY_LIMIT {
        send(&mut s, i, KaraokeCommand::Pause).unwrap();
        send(&mut s, i, KaraokeCommand::Play).unwrap();
    }
    // The old id left the window: the replay is judged by revision CAS instead.
    assert_eq!(s.execute(first).unwrap_err().code(), "stale_revision");
}

#[test]
fn ac4_stale_revision_is_rejected_without_change_but_stop_is_exempt() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let snap = s.snapshot();
    let stale = CommandEnvelope {
        core_instance_id: i,
        session_id: snap.session_id,
        command_id: Uuid::new_v4(),
        expected_revision: snap.state_revision + 5,
        command: KaraokeCommand::Play,
    };
    let err = s.execute(stale).unwrap_err();
    assert_eq!(err.code(), "stale_revision");
    assert_eq!(s.snapshot(), snap);
    let stop = CommandEnvelope {
        command: KaraokeCommand::Stop,
        command_id: Uuid::new_v4(),
        ..stale
    };
    assert_eq!(
        s.execute(stop).unwrap().snapshot.state,
        KaraokeState::Stopped
    );
}

#[test]
fn ac5_stale_epoch_observations_are_excluded_and_counted() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let old = send(&mut s, i, KaraokeCommand::Play)
        .unwrap()
        .1
        .transport_epoch;
    send(&mut s, i, KaraokeCommand::Seek { song_sample: 480 }).unwrap();
    let current = s.snapshot();
    assert!(current.transport_epoch > old);
    assert_eq!(
        s.report_presented(old, 9_999).unwrap_err().code(),
        "stale_epoch"
    );
    assert_eq!(s.credit(old, 0, 100).unwrap_err().code(), "stale_epoch");
    assert_eq!(s.interrupt(old, "late").unwrap_err().code(), "stale_epoch");
    let after = s.snapshot();
    assert_eq!(after.state, KaraokeState::Playing);
    assert_eq!(after.position_song_sample, 480);
    assert_eq!(after.stale_rejections, 3);
    assert_eq!(after.state_revision, current.state_revision);
}

#[test]
fn ac6_position_is_presented_only_and_never_regresses() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    assert_eq!(s.snapshot().position_source, PositionSource::None);
    let epoch = send(&mut s, i, KaraokeCommand::Play)
        .unwrap()
        .1
        .transport_epoch;
    s.report_presented(epoch, 1_000).unwrap();
    assert_eq!(
        s.report_presented(epoch, 999).unwrap_err().code(),
        "position_regressed"
    );
    assert_eq!(
        s.report_presented(epoch, DURATION + 1).unwrap_err().code(),
        "out_of_range"
    );
    // pause: reports are refused (position does not advance)
    let paused = send(&mut s, i, KaraokeCommand::Pause).unwrap().1;
    assert_eq!(
        s.report_presented(paused.transport_epoch, 2_000)
            .unwrap_err()
            .code(),
        "not_playing"
    );
    assert_eq!(s.snapshot().position_song_sample, 1_000);
    // a seek commits a target position but is not presented audio
    let sought = send(&mut s, i, KaraokeCommand::Seek { song_sample: 7_000 })
        .unwrap()
        .1;
    assert_eq!(sought.position_source, PositionSource::Committed);
}

#[test]
fn ac7_interruption_is_explicit_and_recovery_never_autoplays() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let epoch = send(&mut s, i, KaraokeCommand::Play)
        .unwrap()
        .1
        .transport_epoch;
    s.report_presented(epoch, 2_000).unwrap();
    let interrupted = s.interrupt(epoch, "device_removed").unwrap();
    assert_eq!(interrupted.state, KaraokeState::Interrupted);
    assert_eq!(interrupted.error_code.as_deref(), Some("device_removed"));
    // repeated report of the same fault is idempotent success once the epoch matches
    let again = s
        .interrupt(interrupted.transport_epoch, "device_removed")
        .unwrap();
    assert_eq!(again.state_revision, interrupted.state_revision);
    // old-epoch playback progress cannot revive it
    assert!(s.report_presented(epoch, 3_000).is_err());
    let recovered = s.recover().unwrap();
    assert_eq!(recovered.state, KaraokeState::Paused);
    assert_eq!(recovered.position_song_sample, 2_000);
    assert_eq!(recovered.error_code, None);
    assert_eq!(s.recover().unwrap_err().code(), "not_ready");
}

#[test]
fn ac7_interrupted_from_ready_recovers_to_ready_and_prepare_fault_fails() {
    let (mut s, _) = ready_session(ScoringMode::Practice);
    let epoch = s.snapshot().transport_epoch;
    s.interrupt(epoch, "stream_error").unwrap();
    assert_eq!(s.recover().unwrap().state, KaraokeState::Ready);

    let mut p = KaraokeSession::new(
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        DURATION,
        ScoringMode::Practice,
    )
    .unwrap();
    let epoch = p.snapshot().transport_epoch;
    assert_eq!(
        p.interrupt(epoch, "no_device").unwrap().state,
        KaraokeState::Failed
    );
    assert_eq!(p.recover().unwrap_err().code(), "terminal");
}

#[test]
fn ac8_fail_is_terminal_and_core_restart_does_not_revive_sessions() {
    let old_instance = Uuid::new_v4();
    let mut old = KaraokeSessionRegistry::new(old_instance);
    let created = old
        .create(Uuid::new_v4(), DURATION, ScoringMode::Practice)
        .unwrap();
    old.session_mut(created.session_id)
        .unwrap()
        .finish_preparing(Ok(()));

    // "restart": a fresh registry with a new instance id knows nothing of the old session.
    let mut fresh = KaraokeSessionRegistry::new(Uuid::new_v4());
    let envelope = CommandEnvelope {
        core_instance_id: old_instance,
        session_id: created.session_id,
        command_id: Uuid::new_v4(),
        expected_revision: 1,
        command: KaraokeCommand::Play,
    };
    assert_eq!(
        fresh.execute(envelope).unwrap_err().code(),
        "stale_instance"
    );
    // an old-instance observation is rejected by origin
    let origin = EventOrigin {
        core_instance_id: old_instance,
        session_id: created.session_id,
        epoch: 1,
    };
    assert_eq!(
        fresh.report_presented(origin, 1).unwrap_err().code(),
        "stale_instance"
    );
    assert_eq!(
        fresh.credit(origin, 0, 1).unwrap_err().code(),
        "stale_instance"
    );
    assert_eq!(
        fresh.interrupt(origin, "x").unwrap_err().code(),
        "stale_instance"
    );

    // fail() is terminal and idempotent
    let (mut s, _) = ready_session(ScoringMode::Practice);
    let failed = s.fail("decode_error");
    assert_eq!(failed.state, KaraokeState::Failed);
    assert_eq!(s.fail("other").error_code.as_deref(), Some("decode_error"));
}

#[test]
fn ac9_second_session_is_refused_until_the_first_is_terminal() {
    let instance = Uuid::new_v4();
    let mut registry = KaraokeSessionRegistry::new(instance);
    let first = registry
        .create(Uuid::new_v4(), DURATION, ScoringMode::Practice)
        .unwrap();
    assert_eq!(
        registry
            .create(Uuid::new_v4(), DURATION, ScoringMode::Practice)
            .unwrap_err()
            .code(),
        "session_busy"
    );
    registry
        .session_mut(first.session_id)
        .unwrap()
        .finish_preparing(Ok(()));
    let stop = CommandEnvelope {
        core_instance_id: instance,
        session_id: first.session_id,
        command_id: Uuid::new_v4(),
        expected_revision: 0,
        command: KaraokeCommand::Stop,
    };
    assert_eq!(
        registry.execute(stop).unwrap().snapshot.state,
        KaraokeState::Stopped
    );
    let second = registry
        .create(Uuid::new_v4(), DURATION, ScoringMode::WholeSong)
        .unwrap();
    assert_ne!(first.session_id, second.session_id);
    // the replaced session is gone
    let stale = CommandEnvelope {
        command_id: Uuid::new_v4(),
        ..stop
    };
    assert_eq!(
        registry.execute(stale).unwrap_err().code(),
        "session_not_found"
    );
}

#[test]
fn ac10_whole_song_is_downgraded_by_seek_or_interrupt_and_never_restored() {
    // contiguous: stays whole_song through pause/resume
    let (mut s, i) = ready_session(ScoringMode::WholeSong);
    send(&mut s, i, KaraokeCommand::Play).unwrap();
    send(&mut s, i, KaraokeCommand::Pause).unwrap();
    let snap = send(&mut s, i, KaraokeCommand::Play).unwrap().1;
    assert_eq!(snap.score_eligibility, ScoreEligibility::WholeSong);
    // seeking after play started downgrades, seeking back to 0 does not restore
    send(&mut s, i, KaraokeCommand::Seek { song_sample: 100 }).unwrap();
    let back = send(&mut s, i, KaraokeCommand::Seek { song_sample: 0 })
        .unwrap()
        .1;
    assert_eq!(
        back.score_eligibility,
        ScoreEligibility::Downgraded(DowngradeReason::Seek)
    );

    // seek before first play to a non-zero start also downgrades; to 0 does not
    let (mut a, ia) = ready_session(ScoringMode::WholeSong);
    let still = send(&mut a, ia, KaraokeCommand::Seek { song_sample: 0 });
    assert_eq!(still.unwrap().0, Disposition::NoOp);
    assert_eq!(a.snapshot().score_eligibility, ScoreEligibility::WholeSong);
    let moved = send(&mut a, ia, KaraokeCommand::Seek { song_sample: 5 })
        .unwrap()
        .1;
    assert_eq!(
        moved.score_eligibility,
        ScoreEligibility::Downgraded(DowngradeReason::Seek)
    );

    // interruption mid-song downgrades
    let (mut b, ib) = ready_session(ScoringMode::WholeSong);
    let epoch = send(&mut b, ib, KaraokeCommand::Play)
        .unwrap()
        .1
        .transport_epoch;
    let broken = b.interrupt(epoch, "device_removed").unwrap();
    assert_eq!(
        broken.score_eligibility,
        ScoreEligibility::Downgraded(DowngradeReason::Interrupted)
    );
    assert_eq!(
        b.recover().unwrap().score_eligibility,
        ScoreEligibility::Downgraded(DowngradeReason::Interrupted)
    );

    // practice is reported as practice
    let (p, _) = ready_session(ScoringMode::Practice);
    assert_eq!(p.snapshot().score_eligibility, ScoreEligibility::Practice);
}

#[test]
fn ac10_credit_ledger_prevents_double_credit_after_seek_back() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let mut epoch = send(&mut s, i, KaraokeCommand::Play)
        .unwrap()
        .1
        .transport_epoch;
    assert_eq!(s.credit(epoch, 100, 200).unwrap(), vec![(100, 200)]);
    assert_eq!(s.credit(epoch, 300, 400).unwrap(), vec![(300, 400)]);
    // overlap spanning both: only the gaps are new
    assert_eq!(s.credit(epoch, 150, 350).unwrap(), vec![(200, 300)]);
    // seek back and sing the same region again: nothing new
    epoch = send(&mut s, i, KaraokeCommand::Seek { song_sample: 0 })
        .unwrap()
        .1
        .transport_epoch;
    assert_eq!(s.credit(epoch, 100, 400).unwrap(), Vec::<(u64, u64)>::new());
    // extending past the covered region credits only the extension
    assert_eq!(s.credit(epoch, 350, 500).unwrap(), vec![(400, 500)]);
    // invalid input and non-playing states are rejected without change
    assert_eq!(s.credit(epoch, 5, 5).unwrap_err().code(), "invalid_range");
    assert_eq!(
        s.credit(epoch, 0, DURATION + 1).unwrap_err().code(),
        "invalid_range"
    );
    let paused = send(&mut s, i, KaraokeCommand::Pause).unwrap().1;
    assert_eq!(
        s.credit(paused.transport_epoch, 600, 700)
            .unwrap_err()
            .code(),
        "not_playing"
    );
}

#[test]
fn ac10_credit_ledger_is_bounded() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let epoch = send(&mut s, i, KaraokeCommand::Play)
        .unwrap()
        .1
        .transport_epoch;
    let mut last = Ok(Vec::new());
    for n in 0..300_u64 {
        let start = n * 10;
        last = s.credit(epoch, start, start + 5);
        if last.is_err() {
            break;
        }
    }
    assert_eq!(last.unwrap_err().code(), "ledger_full");
    // existing credit is intact after the refusal
    assert_eq!(s.credit(epoch, 0, 5).unwrap(), Vec::<(u64, u64)>::new());
}

#[test]
fn ac11_event_log_is_bounded_with_gap_detection_and_monotonic_seq() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let EventsAfter::Events(initial) = s.events_after(0) else {
        panic!("expected events");
    };
    assert_eq!(initial.len(), 2); // preparing, ready
    assert!(
        initial
            .windows(2)
            .all(|w| w[1].event_seq == w[0].event_seq + 1)
    );
    let last_seen = initial.last().unwrap().event_seq;
    for _ in 0..EVENT_LOG_LIMIT {
        send(&mut s, i, KaraokeCommand::Play).unwrap();
        send(&mut s, i, KaraokeCommand::Pause).unwrap();
    }
    // slow subscriber that last saw `last_seen` has missed events: gap, recover via snapshot
    match s.events_after(last_seen) {
        EventsAfter::Gap { oldest_event_seq } => assert!(oldest_event_seq > last_seen + 1),
        EventsAfter::Events(_) => panic!("expected a gap"),
    }
    // a subscriber resuming from just before the oldest retained event is contiguous
    let EventsAfter::Gap { oldest_event_seq } = s.events_after(0) else {
        panic!("expected a gap from seq 0");
    };
    let EventsAfter::Events(tail) = s.events_after(oldest_event_seq - 1) else {
        panic!("expected events");
    };
    assert_eq!(tail.len(), EVENT_LOG_LIMIT);
    assert!(
        tail.windows(2)
            .all(|w| w[1].event_seq == w[0].event_seq + 1)
    );
}

#[test]
fn ac11_events_carry_instance_session_revision_and_epoch() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    send(&mut s, i, KaraokeCommand::Play).unwrap();
    let EventsAfter::Events(events) = s.events_after(0) else {
        panic!("expected events");
    };
    let last = events.last().unwrap();
    assert_eq!(last.core_instance_id, i);
    assert_eq!(last.session_id, s.snapshot().session_id);
    assert_eq!(last.state_revision, s.snapshot().state_revision);
    assert_eq!(last.transport_epoch, s.snapshot().transport_epoch);
    assert_eq!(last.snapshot.state, KaraokeState::Playing);
}

#[test]
fn ac8_wrong_session_id_is_not_found_and_wrong_instance_is_stale() {
    let (mut s, i) = ready_session(ScoringMode::Practice);
    let snap = s.snapshot();
    let base = CommandEnvelope {
        core_instance_id: i,
        session_id: snap.session_id,
        command_id: Uuid::new_v4(),
        expected_revision: snap.state_revision,
        command: KaraokeCommand::Play,
    };
    let wrong_session = CommandEnvelope {
        session_id: Uuid::new_v4(),
        ..base
    };
    assert_eq!(
        s.execute(wrong_session).unwrap_err().code(),
        "session_not_found"
    );
    let wrong_instance = CommandEnvelope {
        core_instance_id: Uuid::new_v4(),
        ..base
    };
    assert_eq!(
        s.execute(wrong_instance).unwrap_err().code(),
        "stale_instance"
    );
    assert_eq!(s.snapshot(), snap);
}
