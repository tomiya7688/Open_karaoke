namespace OpenKaraoke.Client;

/// {
///   責務: [ClockQualityInfo: 提示済み位置の可聴時刻の品質(Core由来)。未測定の遅延を測定済みに見せない]
///   フィールド: [Kind: unknown | estimated | measured, UncertaintySamples: measuredのときの不確かさ(48kHz sample)]
/// }
public sealed record ClockQualityInfo(string Kind, ulong? UncertaintySamples = null);

/// {
///   責務: [ScoreEligibilityInfo: 採点の扱い(Core由来)。GUIは判定せず表示だけに使う]
///   フィールド: [Kind: practice | whole_song | downgraded, Reason: downgradedの理由(seek | interrupted)]
/// }
public sealed record ScoreEligibilityInfo(string Kind, string? Reason = null);

/// {
///   責務: [KaraokeSessionSnapshot: Coreの歌唱セッション状態の写し(GET karaoke-sessions/current と commands の応答)]
///   フィールド: [
///     SessionId / CoreInstanceId / SongId: 識別子,
///     State: preparing | ready | playing | paused | interrupted | finished | stopped | failed,
///     StateRevision: 状態遷移ごとに増える版,
///     TransportEpoch: 再生に影響する遷移ごとに増える世代,
///     PositionSongSample: 曲内の位置(48kHz整数sample),
///     PositionSource: none | committed | presented,
///     ClockQuality: 提示位置の時刻品質,
///     DurationSamples: 曲の長さ(sample),
///     ScoringMode / ScoreEligibility: 採点の扱い,
///     ErrorCode: 失敗理由, StaleRejections: Coreが除外した古い観測数
///   ]
///   補足: [GUIは再生時計も採点も所有しない。この値を描くだけに使う]
/// }
public sealed record KaraokeSessionSnapshot(
    Guid SessionId, Guid CoreInstanceId, Guid SongId, string State,
    ulong StateRevision, ulong TransportEpoch, ulong PositionSongSample, string PositionSource,
    ClockQualityInfo ClockQuality, ulong DurationSamples, string ScoringMode,
    ScoreEligibilityInfo ScoreEligibility, string? ErrorCode, ulong StaleRejections)
{
    /// <summary>これ以上遷移しない状態か。</summary>
    public bool IsTerminal => State is "finished" or "stopped" or "failed";

    /// {
    ///   責務: [Validate: 受け取った状態が契約の範囲か確かめる]
    ///   処理: [1: 識別子が空でないこと, 2: 状態・位置の由来・時刻品質・採点の列挙値が既知であること, 3: 位置が曲の長さ以内であること]
    ///   引数: []
    ///   戻り値: [検証済みの自分自身]
    ///   エラー: [InvalidDataException: 契約外の値]
    /// }
    public KaraokeSessionSnapshot Validate()
    {
        if (SessionId == Guid.Empty || CoreInstanceId == Guid.Empty || SongId == Guid.Empty ||
            State is not ("preparing" or "ready" or "playing" or "paused" or "interrupted" or "finished" or "stopped" or "failed") ||
            PositionSource is not ("none" or "committed" or "presented") ||
            ClockQuality?.Kind is not ("unknown" or "estimated" or "measured") ||
            ScoringMode is not ("practice" or "whole_song") ||
            ScoreEligibility?.Kind is not ("practice" or "whole_song" or "downgraded") ||
            PositionSongSample > DurationSamples)
            throw new InvalidDataException("Invalid Core karaoke session snapshot.");
        return this;
    }
}

/// {
///   責務: [KaraokeCommandBody: 送るコマンド1件の内容]
///   フィールド: [Type: play | pause | seek | stop, SongSample: seekの移動先(48kHz整数sample)。他は省略]
/// }
public sealed record KaraokeCommandBody(string Type, ulong? SongSample = null);

/// {
///   責務: [KaraokeCommandRequest: POST karaoke-sessions/{id}/commands の本文]
///   フィールド: [
///     CoreInstanceId: 宛先のCore世代, CommandId: 再送キー(同じIDの再送は前回結果が返る),
///     ExpectedRevision: 期待するstate_revision(stopは古くても受理される), Command: 内容
///   ]
/// }
public sealed record KaraokeCommandRequest(Guid CoreInstanceId, Guid CommandId, ulong ExpectedRevision, KaraokeCommandBody Command);

/// {
///   責務: [KaraokeCommandResult: コマンド成功の応答]
///   フィールド: [Disposition: applied | no_op | replayed, Snapshot: 適用後の状態]
/// }
public sealed record KaraokeCommandResult(string Disposition, KaraokeSessionSnapshot Snapshot);
