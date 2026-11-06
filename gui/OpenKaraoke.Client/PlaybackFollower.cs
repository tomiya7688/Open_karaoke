using System.ComponentModel;
using System.Net;

namespace OpenKaraoke.Client;

/// {
///   責務: [FollowerLink: 追従の接続状態。画面は「再生中」と見せてよいかをここで決める]
///   フィールド: [Detached: Core未接続, NoSession: 接続済みだが歌唱セッションが無い, Following: 追従中, CoreLost: Coreと通信できない(位置は不明)]
/// }
public enum FollowerLink { Detached, NoSession, Following, CoreLost }

/// {
///   責務: [ApplyResult: Coreの状態を受け取った結果。Applied系だけが画面へ反映される]
///   フィールド: [
///     Applied: 反映した, EpochAdvanced: 新しいepochを受理し歌唱線等の表示状態を破棄した, NewSession: 別のセッションへ乗り換えた,
///     StaleRevision / StaleEpoch / Regressed / WrongInstance / WrongSession: 捨てた(理由別)
///   ]
/// }
public enum ApplyResult { Applied, EpochAdvanced, NewSession, StaleRevision, StaleEpoch, Regressed, WrongInstance, WrongSession }

/// {
///   責務: [PlaybackFollower: Coreの歌唱セッション状態に表示位置を追従させ、再生/一時停止/停止/シークの操作をCore APIへ送る]
///   フィールド: [
///     _karaoke: 位置を反映する表示, _api / _instanceId / _token: 接続中のCoreとその寿命,
///     _generation: 接続の世代(古い応答を捨てるため),
///     _session: 最後に受理した状態(無ければnull), _gate: 操作を1件ずつ送る,
///     Discarded: 捨てた更新の件数, EpochResets: 新epochを受理した回数
///   ]
///   補足: [
///     GUIは再生時計も採点も所有しない。位置はCoreが提示済みと報告した48kHz整数sampleだけを使い、補間しない(補間は未実装)。
///     状態は定期的に取得する(上限付きの一定間隔)。SSEは107.5の実測後に検討する。
///     ViewModelの更新はUIの同期コンテキストで行う(awaitの継続で戻る)。描画はaudio callbackと無関係(Coreは別プロセス)。
///     Core再接続後は新しいAttachで始まり、旧セッションを自動で再開しない
///   ]
/// }
public sealed class PlaybackFollower : INotifyPropertyChanged
{
    private readonly KaraokeViewModel _karaoke;
    private readonly Func<Guid> _newCommandId;
    private readonly bool _autoPoll;
    /// {
    ///   責務: [_gate: 操作を1件ずつ送るための排他]
    ///   処理: [1: 同時に1件だけ通す]
    ///   引数: [なし]
    ///   戻り値: [なし(フィールド)]
    /// }
    private readonly SemaphoreSlim _gate = new(1, 1);
    private ICoreApi? _api;
    private Guid _instanceId;
    private CancellationToken _token;
    private long _generation;
    private KaraokeSessionSnapshot? _session;
    private FollowerLink _link = FollowerLink.Detached;
    private string _lostReason = "";
    private string _notice = "";
    private double _seekTarget;
    private Task _loop = Task.CompletedTask;

    /// {
    ///   責務: [PollInterval: 状態を取得する間隔。Coreへの負荷を抑える一定間隔]
    ///   処理: [1: 取得ループが待機に使う]
    ///   引数: [なし]
    ///   戻り値: [間隔(既定200ms)]
    /// }
    public TimeSpan PollInterval { get; set; } = TimeSpan.FromMilliseconds(200);

    public event PropertyChangedEventHandler? PropertyChanged;

    /// <summary>新しいepochを受理した。歌唱線・暫定採点の表示状態はここで破棄する(16.3以降)。</summary>
    public event EventHandler? EpochChanged;

    /// {
    ///   責務: [PlaybackFollower: 位置を反映する表示を受け取る]
    ///   処理: [1: 反映先を保持する, 2: コマンドID生成と取得ループの有無を決める]
    ///   引数: [karaoke: 位置の反映先, newCommandId: コマンドIDの生成(テスト用。省略時はGuid.NewGuid), autoPoll: Attachで取得ループを起動するか(テストは手動取得のためfalse)]
    ///   戻り値: []
    /// }
    public PlaybackFollower(KaraokeViewModel karaoke, Func<Guid>? newCommandId = null, bool autoPoll = true)
    {
        _autoPoll = autoPoll;
        _karaoke = karaoke ?? throw new ArgumentNullException(nameof(karaoke));
        _newCommandId = newCommandId ?? Guid.NewGuid;
    }

    public FollowerLink Link => _link;
    public long Discarded { get; private set; }
    public long EpochResets { get; private set; }
    public KaraokeSessionSnapshot? Session => _session;
    public string? State => _link == FollowerLink.Following ? _session?.State : null;
    public ulong Revision => _session?.StateRevision ?? 0;
    public ulong Epoch => _session?.TransportEpoch ?? 0;

    /// <summary>「再生中」と見せてよいか。切断・通信失敗・中断では必ずfalse。</summary>
    public bool IsPlaying => _link == FollowerLink.Following && _session?.State == "playing";
    public bool CanPlay => Operable && _session?.State is "ready" or "paused";
    public bool CanPause => Operable && _session?.State == "playing";
    public bool CanSeek => Operable && _session?.State is "ready" or "playing" or "paused";
    public bool CanStop => _link == FollowerLink.Following && _api is not null && _session is { IsTerminal: false };
    private bool Operable => _link == FollowerLink.Following && _api is not null && _session is { IsTerminal: false, State: not "interrupted" };

    /// {
    ///   責務: [DurationSeconds: 受理中の曲の長さを秒で返す(シーク先スライダーの上限用)]
    ///   処理: [1: 状態が無ければ0にする]
    ///   引数: [なし]
    ///   戻り値: [秒]
    /// }
    public double DurationSeconds => SongTimeline.ToSeconds(_session?.DurationSamples ?? 0);

    /// <summary>シーク先(秒)。状態の取得では上書きされない。</summary>
    public double SeekTargetSeconds
    {
        get => _seekTarget;
        set
        {
            var next = double.IsFinite(value) ? Math.Clamp(value, 0, DurationSeconds) : 0;
            if (next == _seekTarget) return;
            _seekTarget = next;
            Changed();
        }
    }

    public string ClockQualityText => _link != FollowerLink.Following || _session is null ? "—" : _session.ClockQuality.Kind switch
    {
        "measured" => $"測定済み(±{_session.ClockQuality.UncertaintySamples} sample)",
        "estimated" => "推定",
        _ => "未測定",
    };

    /// <summary>操作の結果や拒否理由。次の更新成功で消える。</summary>
    public string Notice => _notice;
    public bool HasNotice => _notice.Length > 0;

    public string StatusText => _link switch
    {
        FollowerLink.Detached => "Coreに接続すると操作できます",
        FollowerLink.NoSession => "歌唱セッションはありません",
        FollowerLink.CoreLost => $"Coreと通信できません。再生位置は不明です({_lostReason})",
        _ => StateText(_session!) + (SongMismatch ? "(表示中の曲とは別の曲です)" : ""),
    };

    private bool SongMismatch => _session is not null && _karaoke.SongId is { } shown && shown != _session.SongId;

    /// <summary>接続（取得ループ）の完了待ち。テストと切断処理用。</summary>
    public Task Idle => _loop;

    /// {
    ///   責務: [StateText: 状態を利用者向けの文にする。中断・失敗を再生中と見せない]
    ///   処理: [1: 状態名ごとの文を返す, 2: 失敗にはエラーコードを添える]
    ///   引数: [snapshot]
    ///   戻り値: [表示文]
    /// }
    private static string StateText(KaraokeSessionSnapshot snapshot) => snapshot.State switch
    {
        "preparing" => "準備中です",
        "ready" => "再生できます",
        "playing" => "再生中",
        "paused" => "一時停止中",
        "interrupted" => "中断されました(自動では再開しません)",
        "finished" => "曲が終わりました",
        "stopped" => "停止しました",
        _ => "失敗しました" + (snapshot.ErrorCode is { Length: > 0 } code ? $"({code})" : ""),
    };

    /// {
    ///   責務: [Attach: 接続したCoreの歌唱セッションへ追従を始める]
    ///   処理: [1: 世代を進めて前の状態を捨てる, 2: 取得ループを起動する(旧セッションは再開しない)]
    ///   引数: [api: 接続済みのCore API, instanceId: そのCoreの世代, token: 接続の寿命]
    ///   戻り値: []
    /// }
    public void Attach(ICoreApi api, Guid instanceId, CancellationToken token)
    {
        var generation = ++_generation;
        _api = api ?? throw new ArgumentNullException(nameof(api));
        _instanceId = instanceId;
        _token = token;
        _session = null;
        _link = FollowerLink.NoSession;
        _lostReason = _notice = "";
        Changed();
        if (_autoPoll) _loop = LoopAsync(generation);
    }

    /// {
    ///   責務: [Detach: 切断時に追従と未完了の応答を捨てる]
    ///   処理: [1: 世代を進める, 2: 状態を空にして「未接続」にする]
    ///   引数: []
    ///   戻り値: []
    /// }
    public void Detach()
    {
        _generation++;
        _api = null;
        _session = null;
        _link = FollowerLink.Detached;
        _lostReason = _notice = "";
        Changed();
    }

    /// {
    ///   責務: [LoopAsync: 一定間隔で状態を取得し続ける]
    ///   処理: [1: 世代が同じ間、取得して待つことを繰り返す, 2: 中断されたら静かに終わる]
    ///   引数: [generation: 開始時の世代]
    ///   戻り値: [世代が変わるか中断されたら完了]
    /// }
    private async Task LoopAsync(long generation)
    {
        try
        {
            while (generation == _generation && !_token.IsCancellationRequested)
            {
                await PollOnceAsync();
                await Task.Delay(PollInterval, _token);
            }
        }
        catch (OperationCanceledException) when (_token.IsCancellationRequested) { }
    }

    /// {
    ///   責務: [PollOnceAsync: 現行セッションを1回取得して反映する]
    ///   処理: [
    ///     1: GET currentを送る,
    ///     2: 世代が変わっていたら結果を捨てる,
    ///     3: nullならセッション無し(旧セッションは再開しない), 状態ならApplyへ渡す,
    ///     4: 失敗は「Core切断」として表示する(旧位置で再生中に見せない)
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal async Task PollOnceAsync()
    {
        var generation = _generation;
        if (_api is not { } api) return;
        try
        {
            var snapshot = await api.GetCurrentKaraokeSessionAsync(_token);
            if (generation != _generation) return;
            if (snapshot is null) ClearSession();
            else Apply(snapshot, allowNewSession: true);
        }
        catch (OperationCanceledException) when (_token.IsCancellationRequested) { }
        catch (Exception error)
        {
            if (generation == _generation) SetLost(error);
        }
    }

    /// {
    ///   責務: [Apply: Coreの状態を受け取り、古い更新を捨てて表示へ反映する]
    ///   処理: [
    ///     1: 別のCore世代の状態は捨てる,
    ///     2: 別セッションは、取得経路(allowNewSession)だけ乗り換えを許し、コマンド応答では捨てる,
    ///     3: revisionが古い、またはepochが現行より古い更新は捨てる,
    ///     4: 同じ版・同じepochで再生位置が後退する更新は捨てる,
    ///     5: 受理したら保存し、新しいepochなら通知する(歌唱線等の表示状態の破棄用),
    ///     6: 位置が意味を持つ状態(ready/playing/paused/finished)だけ、同じ曲を表示中のときに位置を反映する
    ///   ]
    ///   引数: [snapshot: 受け取った状態, allowNewSession: 別セッションへの乗り換えを許すか]
    ///   戻り値: [反映結果]
    /// }
    internal ApplyResult Apply(KaraokeSessionSnapshot snapshot, bool allowNewSession)
    {
        if (snapshot.CoreInstanceId != _instanceId) return Discard(ApplyResult.WrongInstance);
        var previous = _session;
        var isNew = previous is null || previous.SessionId != snapshot.SessionId;
        if (previous is not null && isNew && !allowNewSession) return Discard(ApplyResult.WrongSession);
        if (!isNew)
        {
            if (snapshot.StateRevision < previous!.StateRevision) return Discard(ApplyResult.StaleRevision);
            if (snapshot.TransportEpoch < previous.TransportEpoch) return Discard(ApplyResult.StaleEpoch);
            if (snapshot.StateRevision == previous.StateRevision && snapshot.TransportEpoch == previous.TransportEpoch &&
                snapshot.State == "playing" && snapshot.PositionSongSample < previous.PositionSongSample)
                return Discard(ApplyResult.Regressed);
        }
        var epochAdvanced = !isNew && snapshot.TransportEpoch > previous!.TransportEpoch;
        _session = snapshot;
        _link = FollowerLink.Following;
        _lostReason = _notice = "";
        if (isNew || epochAdvanced) EpochResets++;
        if (isNew) _seekTarget = 0;
        if (snapshot.State is ("ready" or "playing" or "paused" or "finished") && !SongMismatch)
            _karaoke.Seek(snapshot.PositionSongSample);
        Changed();
        if (isNew || epochAdvanced) EpochChanged?.Invoke(this, EventArgs.Empty);
        return isNew ? ApplyResult.NewSession : epochAdvanced ? ApplyResult.EpochAdvanced : ApplyResult.Applied;
    }

    /// {
    ///   責務: [Discard: 捨てた更新を数え、理由を返す]
    ///   処理: [1: 捨てた件数を増やす]
    ///   引数: [reason: 捨てた理由]
    ///   戻り値: [reason]
    /// }
    private ApplyResult Discard(ApplyResult reason)
    {
        Discarded++;
        return reason;
    }

    /// {
    ///   責務: [ClearSession: セッションが無くなったことを表示する]
    ///   処理: [1: 保持していた状態を捨てる(同じIDが戻っても新規扱い), 2: 新規の歌唱線等の破棄を通知する]
    ///   引数: []
    ///   戻り値: []
    /// }
    private void ClearSession()
    {
        var had = _session is not null;
        _session = null;
        _link = FollowerLink.NoSession;
        _lostReason = _notice = "";
        Changed();
        if (had) EpochChanged?.Invoke(this, EventArgs.Empty);
    }

    /// {
    ///   責務: [SetLost: Coreと通信できないことを表示する]
    ///   処理: [1: 理由を記録して接続状態をCoreLostにする(再生中・操作可の表示をすべて止める)]
    ///   引数: [error: 失敗]
    ///   戻り値: []
    /// }
    private void SetLost(Exception error)
    {
        _link = FollowerLink.CoreLost;
        _lostReason = error is CoreApiException api ? $"HTTP {(int)api.StatusCode}" : error.GetType().Name;
        Changed();
    }

    /// {
    ///   責務: [PlayAsync: 再生をCoreへ依頼する]
    ///   処理: [1: playコマンドを送る]
    ///   引数: [なし]
    ///   戻り値: [完了するTask]
    /// }
    public Task PlayAsync() => CommandAsync("play", null);
    /// {
    ///   責務: [PauseAsync: 一時停止をCoreへ依頼する]
    ///   処理: [1: pauseコマンドを送る]
    ///   引数: [なし]
    ///   戻り値: [完了するTask]
    /// }
    public Task PauseAsync() => CommandAsync("pause", null);
    /// {
    ///   責務: [StopAsync: 停止をCoreへ依頼する]
    ///   処理: [1: stopコマンドを送る]
    ///   引数: [なし]
    ///   戻り値: [完了するTask]
    /// }
    public Task StopAsync() => CommandAsync("stop", null);

    /// {
    ///   責務: [SeekAsync: 曲内の位置(48kHz整数sample)へのシークをCoreへ依頼する]
    ///   処理: [1: 曲の終端より前へ収める(Coreはduration未満のみ受理), 2: コマンドを送る]
    ///   引数: [sample: 移動先]
    ///   戻り値: []
    /// }
    public Task SeekAsync(ulong sample) =>
        CommandAsync("seek", _session is { DurationSamples: > 0 } s ? Math.Min(sample, s.DurationSamples - 1) : sample);

    /// {
    ///   責務: [SeekToTargetAsync: 画面のシーク先(秒)へシークする]
    ///   処理: [1: 秒を48kHz整数sampleへ変換する, 2: SeekAsyncへ渡す]
    ///   引数: [なし]
    ///   戻り値: [完了するTask]
    /// }
    public Task SeekToTargetAsync() => SeekAsync(SongTimeline.FromSeconds(_seekTarget));

    /// {
    ///   責務: [CommandAsync: コマンドを1件ずつCoreへ送り、結果を同じ受理規則で反映する]
    ///   処理: [
    ///     1: 操作できない状態なら理由を表示して送らない,
    ///     2: 直前の操作が終わるまで待ち、その時点の最新revisionで送る,
    ///     3: 成功したら結果の状態をApply(コマンド応答)へ渡す,
    ///     4: 409(競合)は自動で再送せず、状態を取得し直して「もう一度」と知らせる(二重適用を避ける),
    ///     5: 422は範囲外、それ以外の通信失敗はCore切断として表示する
    ///   ]
    ///   引数: [type: play | pause | seek | stop, sample: seekの移動先]
    ///   戻り値: []
    /// }
    private async Task CommandAsync(string type, ulong? sample)
    {
        if (!Operable)
        {
            SetNotice("今は操作できません");
            return;
        }
        var generation = _generation;
        var api = _api!;
        try
        {
            await _gate.WaitAsync(_token);
        }
        catch (OperationCanceledException) when (_token.IsCancellationRequested) { return; }
        try
        {
            if (generation != _generation || !Operable) return;
            var session = _session!;
            var request = new KaraokeCommandRequest(
                session.CoreInstanceId, _newCommandId(), session.StateRevision, new KaraokeCommandBody(type, sample));
            var result = await api.SendKaraokeCommandAsync(session.SessionId, request, _token);
            if (generation != _generation) return;
            Apply(result.Snapshot, allowNewSession: false);
        }
        catch (OperationCanceledException) when (_token.IsCancellationRequested) { }
        catch (CoreApiException error) when (generation == _generation && error.StatusCode == HttpStatusCode.Conflict)
        {
            await PollOnceAsync();
            if (generation == _generation) SetNotice("状態が更新されました。もう一度操作してください");
        }
        catch (CoreApiException error) when (generation == _generation && error.StatusCode == HttpStatusCode.UnprocessableEntity)
        {
            SetNotice("その位置にはシークできません");
        }
        catch (Exception error) when (generation == _generation)
        {
            SetLost(error);
        }
        finally
        {
            _gate.Release();
        }
    }

    /// {
    ///   責務: [SetNotice: 操作の結果や拒否理由を表示する]
    ///   処理: [1: 文を保存して変更を通知する]
    ///   引数: [text: 表示文]
    ///   戻り値: [なし]
    /// }
    private void SetNotice(string text)
    {
        _notice = text;
        Changed();
    }

    /// {
    ///   責務: [Changed: 派生値を含む全プロパティの変更を通知する]
    ///   処理: [1: 空の名前で通知する]
    ///   引数: [なし]
    ///   戻り値: [なし]
    /// }
    private void Changed() => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));
}
