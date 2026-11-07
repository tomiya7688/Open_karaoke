using System.ComponentModel;

namespace OpenKaraoke.Client;

/// {
///   責務: [PreviewController: 編集中の歌詞・ノートの区間を、Coreの歌唱セッション(#16/#107)でプレビュー再生する]
///   処理: [
///     1: 区間の開始へシークして再生を依頼する(再生・時計・採点はCoreが持つ。GUIは依頼と終端での一時停止だけ),
///     2: 表示位置が終端へ達したら一時停止を1回だけ依頼する,
///     3: 一時停止・停止・曲の入替・切断など再生が続かない状態になったら、終端での一時停止予約を捨てる
///   ]
///   フィールド: [
///     _follower: Coreの歌唱セッションへの追従と操作, _karaoke: 表示位置, _editor: 編集中の曲,
///     _stopAt: 終端(48kHz sample)。予約が無ければnull, _stopSession: 予約したセッションのID,
///     _pausing: 終端での一時停止を送信中か, _notice: 利用者向けの結果表示, _idle: 終端での一時停止の完了待ち
///   ]
///   補足: [未保存の編集範囲もそのまま再生できる(音声は編集の影響を受けない)。実機の音は未検証]
/// }
public sealed class PreviewController : INotifyPropertyChanged
{
    private readonly PlaybackFollower _follower;
    private readonly KaraokeViewModel _karaoke;
    private readonly SongEditorViewModel _editor;
    private ulong? _stopAt;
    private Guid _stopSession;
    private bool _pausing;
    private string _notice = "";
    private Task _idle = Task.CompletedTask;

    /// {
    ///   責務: [PreviewController: 追従・表示位置・編集状態を受け取り、再生状態の変化を監視する]
    ///   処理: [1: 位置と再生状態の変更通知を購読する]
    ///   引数: [follower: 歌唱セッションの追従, karaoke: 表示位置, editor: 編集中の曲]
    ///   戻り値: []
    /// }
    public PreviewController(PlaybackFollower follower, KaraokeViewModel karaoke, SongEditorViewModel editor)
    {
        _follower = follower ?? throw new ArgumentNullException(nameof(follower));
        _karaoke = karaoke ?? throw new ArgumentNullException(nameof(karaoke));
        _editor = editor ?? throw new ArgumentNullException(nameof(editor));
        _karaoke.PropertyChanged += (_, _) => OnStateChanged();
        _follower.PropertyChanged += (_, _) => OnStateChanged();
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    /// <summary>区間の終端で止める予約があるか(プレビュー中)。</summary>
    public bool IsPreviewing => _stopAt.HasValue;
    public string Notice => _notice;
    public bool HasNotice => _notice.Length > 0;
    /// <summary>終端での一時停止の送信が終わるまで完了しないTask(テスト用)。</summary>
    public Task Idle => _idle;

    /// {
    ///   責務: [CanPreview: 今プレビューを始められるかを返す]
    ///   処理: [1: 編集中の曲があり、追従中のセッションが同じ曲で、シークできる状態か調べる]
    ///   引数: []
    ///   戻り値: [始められるならtrue]
    /// }
    public bool CanPreview => _editor.HasSong && _follower.CanSeek && _follower.Session?.SongId == _editor.SongId;

    /// {
    ///   責務: [PlayRangeAsync: 指定区間を再生し、終端で一時停止する]
    ///   処理: [
    ///     1: 区間が空・曲が違う・セッションが操作できない場合は理由を表示して何も送らない,
    ///     2: 終端を曲の長さに収め、前のプレビューの予約を捨てる,
    ///     3: 開始位置へシークし、反映された位置が区間内でなければ中止する,
    ///     4: 再生中でなければ再生を依頼し、再生中になったことを確かめる,
    ///     5: 終端での一時停止を予約する(既に終端以降なら直ちに一時停止する)
    ///   ]
    ///   引数: [startSample: 開始(48kHz sample), endSample: 終了(48kHz sample、含まない)]
    ///   戻り値: [完了するTask]
    ///   副作用: [Coreの歌唱セッションへseek/play/pauseコマンドを送る]
    /// }
    public async Task PlayRangeAsync(ulong startSample, ulong endSample)
    {
        Cancel();
        if (!_editor.HasSong) { SetNotice("編集する曲がありません"); return; }
        if (_follower.Session is not { } session || session.SongId != _editor.SongId)
        {
            SetNotice("再生セッションの曲が編集中の曲と一致しないためプレビューできません");
            return;
        }
        if (!_follower.CanSeek) { SetNotice("今は再生を操作できません"); return; }
        var end = Math.Min(endSample, session.DurationSamples);
        if (startSample >= end) { SetNotice("再生する区間が空です"); return; }

        await _follower.SeekAsync(startSample);
        var position = _follower.Session?.PositionSongSample ?? 0;
        if (_follower.Session?.SessionId != session.SessionId || position < startSample || position >= end)
        {
            SetNotice("開始位置へ移動できませんでした");
            return;
        }
        if (!_follower.IsPlaying)
        {
            await _follower.PlayAsync();
            if (!_follower.IsPlaying || _follower.Session?.SessionId != session.SessionId)
            {
                SetNotice("再生を始められませんでした");
                return;
            }
        }
        _stopSession = session.SessionId;
        _stopAt = end;
        SetNotice("");
        OnStateChanged();
    }

    /// {
    ///   責務: [StopAsync: プレビューを止める]
    ///   処理: [1: 終端の予約を捨てる, 2: プレビュー中だったなら一時停止を依頼する]
    ///   引数: []
    ///   戻り値: [完了するTask]
    ///   副作用: [プレビュー中ならpauseコマンドを送る]
    /// }
    public async Task StopAsync()
    {
        var wasPreviewing = _stopAt.HasValue;
        Cancel();
        if (wasPreviewing && _follower.CanPause) await _follower.PauseAsync();
    }

    /// {
    ///   責務: [Cancel: 終端での一時停止の予約だけを捨てる(再生状態は変えない)]
    ///   処理: [1: 予約を消して変更を通知する]
    ///   引数: []
    ///   戻り値: []
    /// }
    public void Cancel()
    {
        if (_stopAt is null) return;
        _stopAt = null;
        Notify();
    }

    /// {
    ///   責務: [OnStateChanged: 位置・再生状態の変化に応じて予約を保つか、終端で一時停止するか決める]
    ///   処理: [
    ///     1: 予約が無ければ何もしない,
    ///     2: 再生中でない・別セッション・別の曲になったら予約を捨てる,
    ///     3: 位置が終端以降で、送信中でなければ予約を捨てて一時停止を1回だけ依頼する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    private void OnStateChanged()
    {
        if (_stopAt is not { } stop) return;
        if (!_follower.IsPlaying || _follower.Session?.SessionId != _stopSession || _follower.Session.SongId != _editor.SongId)
        {
            Cancel();
            return;
        }
        if (_karaoke.PositionSamples < stop || _pausing) return;
        _stopAt = null;
        _pausing = true;
        _idle = PauseAtEndAsync();
        Notify();
    }

    /// {
    ///   責務: [PauseAtEndAsync: 終端到達で一時停止を依頼する]
    ///   処理: [1: pauseを送る(失敗はPlaybackFollowerが表示する), 2: 送信中の印を外す]
    ///   引数: []
    ///   戻り値: [完了するTask]
    /// }
    private async Task PauseAtEndAsync()
    {
        try
        {
            if (_follower.CanPause) await _follower.PauseAsync();
        }
        finally
        {
            _pausing = false;
        }
    }

    /// {
    ///   責務: [SetNotice: 結果の文を保存して通知する]
    ///   処理: [1: 文を保存する, 2: 変更を通知する]
    ///   引数: [text: 表示する文(空なら消す)]
    ///   戻り値: []
    /// }
    private void SetNotice(string text)
    {
        _notice = text;
        Notify();
    }

    /// {
    ///   責務: [Notify: 全プロパティの変更を通知する]
    ///   処理: [1: 空の名前で通知する]
    ///   引数: []
    ///   戻り値: []
    /// }
    private void Notify() => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));
}
