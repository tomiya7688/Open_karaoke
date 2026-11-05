//! Karaoke session controller (Issue #107 slice 107.3, composition only).
//!
//! The controller selects a song, reads it through a `SongSource`, leases the audio devices
//! through a `DeviceLeaseProvider` and drives `preparing -> ready` on the `KaraokeSessionHost`.
//! Real devices / CPAL workers are NOT wired here: both collaborators are traits so the
//! composition is tested with fakes. Wiring the real library and devices is a later slice.

#![allow(clippy::doc_markdown)] // JSON-style declaration comments name snake_case fields

use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use uuid::Uuid;

use crate::{
    karaoke_session::{EventOrigin, KaraokeError, KaraokeState, ScoringMode, SessionSnapshot},
    karaoke_session_api::KaraokeSessionHost,
};

/// Stable code recorded when the song cannot be read while preparing.
pub const SONG_UNAVAILABLE_CODE: &str = "song_unavailable";
/// Stable code recorded when the device lease cannot be taken.
pub const DEVICE_UNAVAILABLE_CODE: &str = "device_unavailable";

/// {
///   責務: [`PreparedSong`: SongSourceが検証して返した曲の情報]
///   フィールド: [duration_samples: 48kHz整数sampleの曲長(1以上)]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PreparedSong {
    pub duration_samples: u64,
}

/// {
///   責務: [`SongSource`: 保存曲を検証して読み取る抽象(実体はSongLibrary。本スライスはfakeで合成する)]
///   補足: [実装はブロックしてよいが、Controllerはregistryのlockを保持したまま呼ばない]
/// }
pub trait SongSource: Send + Sync {
    /// {
    ///   責務: [`open`: song_idの保存曲を検証し曲長を返す]
    ///   引数: [song_id]
    ///   戻り値: [`Result<PreparedSong, String>`: Errは診断メッセージ(秘密を含めない)]
    /// }
    ///
    /// # Errors
    /// Returns a diagnostic when the song is missing or invalid.
    fn open(&self, song_id: Uuid) -> Result<PreparedSong, String>;
}

/// {
///   責務: [`DeviceLease`: 音声デバイスの排他貸出。dropで返却される]
/// }
pub trait DeviceLease: Send {}

/// {
///   責務: [`DeviceLeaseProvider`: 入出力デバイスを1セッションへ貸し出す抽象(実体はCPAL配線の後続スライス)]
/// }
pub trait DeviceLeaseProvider: Send + Sync {
    /// {
    ///   責務: [`acquire`: セッションのためにデバイスを借りる]
    ///   引数: [session_id]
    ///   戻り値: [`Result<Box<dyn DeviceLease>, String>`: 借りられなければErr(診断)]
    /// }
    ///
    /// # Errors
    /// Returns a diagnostic when the devices are busy or missing.
    fn acquire(&self, session_id: Uuid) -> Result<Box<dyn DeviceLease>, String>;
}

/// {
///   責務: [`ControllerError`: Controller操作の失敗。失敗は状態を壊さない]
///   フィールド: [Session: 状態機械の拒否, SongUnavailable: 曲を読めず、セッションは作られない]
/// }
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ControllerError {
    Session(KaraokeError),
    SongUnavailable(String),
}

impl ControllerError {
    /// {
    ///   責務: [`code`: transportへ載せる安定コード]
    ///   戻り値: [`&'static str`]
    /// }
    #[must_use]
    pub const fn code(&self) -> &'static str {
        match self {
            Self::Session(error) => error.code(),
            Self::SongUnavailable(_) => SONG_UNAVAILABLE_CODE,
        }
    }
}

impl From<KaraokeError> for ControllerError {
    fn from(error: KaraokeError) -> Self {
        Self::Session(error)
    }
}

/// {
///   責務: [`HeldLease`: 現在保持しているデバイス貸出とその持ち主セッション]
/// }
struct HeldLease {
    session_id: Uuid,
    _lease: Box<dyn DeviceLease>,
}

/// {
///   責務: [`KaraokeSessionController`: 選曲・読取・準備・デバイス貸出を1世代のhostへ配線する]
///   フィールド: [host: 状態の正本, songs: 曲の読取, leases: デバイス貸出, held: 保持中の貸出(高々1つ)]
///   補足: [継承を使わず保持・委譲する。再起動すると新しいhost(新instance)と空のheldで始まり、旧Sessionは復活しない]
/// }
pub struct KaraokeSessionController {
    host: KaraokeSessionHost,
    songs: Arc<dyn SongSource>,
    leases: Arc<dyn DeviceLeaseProvider>,
    held: Mutex<Option<HeldLease>>,
}

impl KaraokeSessionController {
    /// {
    ///   責務: [`new`: hostと2つの抽象からControllerを作る]
    ///   引数: [host, songs, leases]
    ///   戻り値: [`Self`]
    /// }
    #[must_use]
    pub fn new(
        host: KaraokeSessionHost,
        songs: Arc<dyn SongSource>,
        leases: Arc<dyn DeviceLeaseProvider>,
    ) -> Self {
        Self {
            host,
            songs,
            leases,
            held: Mutex::new(None),
        }
    }

    /// {
    ///   責務: [`lock_held`: 貸出保持のlockを得る(内部)]
    /// }
    fn lock_held(&self) -> MutexGuard<'_, Option<HeldLease>> {
        self.held.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// {
    ///   責務: [`select_song`: 曲を読み取り、preparingのセッションを作る]
    ///   処理: [1: SongSourceで検証し曲長を得る 2: 失敗ならセッションを作らない 3: hostにpreparingを作る]
    ///   引数: [song_id, scoring_mode]
    ///   戻り値: [`Result<SessionSnapshot, ControllerError>`: preparingのsnapshot]
    ///   エラー: [`SongUnavailable` / `Session(SessionBusy | InvalidDuration)`]
    /// }
    ///
    /// # Errors
    /// Returns `SongUnavailable` when the song cannot be read, or the session rejection.
    pub fn select_song(
        &self,
        song_id: Uuid,
        scoring_mode: ScoringMode,
    ) -> Result<SessionSnapshot, ControllerError> {
        self.reconcile();
        let song = self
            .songs
            .open(song_id)
            .map_err(ControllerError::SongUnavailable)?;
        Ok(self
            .host
            .create(song_id, song.duration_samples, scoring_mode)?)
    }

    /// {
    ///   責務: [`prepare`: デバイスを借りてpreparingをreadyへ進める。借りられなければfailed]
    ///   処理: [1: 貸出を要求 2: 成功はready 3: 失敗はfailed(device_unavailable) 4: 取消済み(preparingでない)なら貸出を返す]
    ///   引数: [session_id]
    ///   戻り値: [`Result<SessionSnapshot, ControllerError>`: 結果のsnapshot(failedもOk)]
    ///   エラー: [`Session(SessionNotFound)`]
    /// }
    ///
    /// # Errors
    /// Returns `SessionNotFound` when the id is not the current session.
    pub fn prepare(&self, session_id: Uuid) -> Result<SessionSnapshot, ControllerError> {
        self.host.snapshot(session_id)?;
        match self.leases.acquire(session_id) {
            Ok(lease) => {
                let moved = self.host.finish_preparing(session_id, Ok(()))?;
                if moved {
                    *self.lock_held() = Some(HeldLease {
                        session_id,
                        _lease: lease,
                    });
                }
            }
            Err(_) => {
                self.host
                    .finish_preparing(session_id, Err(DEVICE_UNAVAILABLE_CODE))?;
            }
        }
        Ok(self.host.snapshot(session_id)?)
    }

    /// {
    ///   責務: [`start`: select_songとprepareを続けて行う]
    ///   引数: [song_id, scoring_mode]
    ///   戻り値: [`Result<SessionSnapshot, ControllerError>`]
    /// }
    ///
    /// # Errors
    /// Same as `select_song` and `prepare`.
    pub fn start(
        &self,
        song_id: Uuid,
        scoring_mode: ScoringMode,
    ) -> Result<SessionSnapshot, ControllerError> {
        let created = self.select_song(song_id, scoring_mode)?;
        self.prepare(created.session_id)
    }

    /// {
    ///   責務: [`stream_error`: 機器抜去・stream errorを明示的なinterruptとして報告し貸出を返す]
    ///   処理: [1: hostへinterrupt 2: 貸出を返す(自動復旧・自動再生はしない)]
    ///   引数: [origin, code]
    ///   戻り値: [`Result<SessionSnapshot, ControllerError>`]
    /// }
    ///
    /// # Errors
    /// Propagates the session rejection (stale instance / epoch).
    pub fn stream_error(
        &self,
        origin: EventOrigin,
        code: &str,
    ) -> Result<SessionSnapshot, ControllerError> {
        let snapshot = self.host.interrupt(origin, code)?;
        self.reconcile();
        Ok(snapshot)
    }

    /// {
    ///   責務: [`recover`: 中断したセッションの貸出を取り直して復旧する。自動再生しない]
    ///   処理: [1: interruptedか確認 2: 貸出を再取得(失敗はdevice_unavailableで中断のまま) 3: hostのrecover]
    ///   引数: [session_id]
    ///   戻り値: [`Result<SessionSnapshot, ControllerError>`]
    ///   エラー: [`Session(DeviceUnavailable | NotReady | Terminal | SessionNotFound)`]
    /// }
    ///
    /// # Errors
    /// Returns `DeviceUnavailable` when the devices cannot be leased again.
    pub fn recover(&self, session_id: Uuid) -> Result<SessionSnapshot, ControllerError> {
        let snapshot = self.host.snapshot(session_id)?;
        match snapshot.state {
            KaraokeState::Interrupted => {}
            state if state.is_terminal() => return Err(KaraokeError::Terminal.into()),
            _ => return Err(KaraokeError::NotReady.into()),
        }
        let lease = self
            .leases
            .acquire(session_id)
            .map_err(|_| KaraokeError::DeviceUnavailable)?;
        let recovered = self.host.recover(session_id)?;
        *self.lock_held() = Some(HeldLease {
            session_id,
            _lease: lease,
        });
        Ok(recovered)
    }

    /// {
    ///   責務: [`reconcile`: 貸出が現行セッションの稼働中(ready/playing/paused)でなければ返却する]
    ///   処理: [1: 保持中の持ち主を確認 2: 終端・中断・不在なら破棄(dropで返却)]
    ///   戻り値: [`bool`: 返却したか]
    ///   補足: [クライアントのstopなどhost経由の遷移後にも呼べる。select_song/stream_errorは内部で呼ぶ]
    /// }
    pub fn reconcile(&self) -> bool {
        let mut held = self.lock_held();
        let alive = held.as_ref().is_none_or(|entry| {
            self.host.snapshot(entry.session_id).is_ok_and(|snapshot| {
                matches!(
                    snapshot.state,
                    KaraokeState::Ready | KaraokeState::Playing | KaraokeState::Paused
                )
            })
        });
        if alive {
            return false;
        }
        *held = None;
        true
    }

    /// {
    ///   責務: [`holds_lease`: 貸出を保持中か(テスト・診断用)]
    ///   戻り値: [`bool`]
    /// }
    #[must_use]
    pub fn holds_lease(&self) -> bool {
        self.lock_held().is_some()
    }
}
