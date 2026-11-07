using System.Net;
using static OpenKaraoke.Client.Tests.PlaybackFollowerTests;
using static OpenKaraoke.Client.Tests.Program;

namespace OpenKaraoke.Client.Tests;

/// {
///   責務: [PreviewTests: 編集中の区間のプレビュー再生(#38 38.5)を、FakeのCoreセッションで検証する]
///   フィールド: [Second: 1秒のsample数]
///   補足: [実機の音は検証しない(Unverified)。コマンドの順序・終端での一時停止・予約の破棄を確かめる]
/// }
internal static class PreviewTests
{
    private const ulong Second = SongTimeline.SampleRate;

    /// {
    ///   責務: [Rig: テスト用の編集・追従・プレビューの組を作る]
    ///   処理: [1: 曲を読み込んだ編集と、指定状態のセッションに追従する追従を作る, 2: コマンドは seek/play/pause を状態へ写すFakeにする]
    ///   引数: [state: 初期のセッション状態, songId: 編集中の曲のID]
    ///   戻り値: [Api, Follower, Editor, Preview, 現在状態を返す関数]
    /// }
    private static async Task<(FakeSessionApi Api, PlaybackFollower Follower, PreviewController Preview, Func<KaraokeSessionSnapshot> Current)>
        Rig(string state, Guid? songId = null)
    {
        var karaoke = new KaraokeViewModel();
        karaoke.Load(new SongTimeline(SongTests.Song(10 * Second, [], [])), SongId);
        var editor = new SongEditorViewModel();
        var api = new FakeSessionApi { Current = Snap(state, 1, 1, 0) };
        editor.Load(api, songId ?? SongId, EditorTests.Bundle());
        var follower = new PlaybackFollower(karaoke, autoPoll: false);
        follower.Attach(api, Instance, new CancellationTokenSource().Token);
        follower.PollInterval = TimeSpan.FromHours(1);
        await follower.PollOnceAsync();
        // コマンドを状態へ写す: seekは位置とepochを進め、playはplaying、pauseはpausedにする。
        api.OnCommand = request =>
        {
            var now = api.Current!;
            return request.Command.Type switch
            {
                "seek" => Snap(now.State, request.ExpectedRevision + 1, now.TransportEpoch + 1, request.Command.SongSample!.Value),
                "play" => Snap("playing", request.ExpectedRevision + 1, now.TransportEpoch + 1, now.PositionSongSample),
                _ => Snap("paused", request.ExpectedRevision + 1, now.TransportEpoch + 1, now.PositionSongSample),
            };
        };
        return (api, follower, new PreviewController(follower, karaoke, editor), () => api.Current!);
    }

    /// {
    ///   責務: [Types: 送ったコマンドの種類を並べる]
    ///   引数: [api: Fake]
    ///   戻り値: [種類の一覧]
    /// }
    private static string[] Types(FakeSessionApi api) => api.Sent.Select(r => r.Command.Type).ToArray();

    /// {
    ///   責務: [Advance: Coreが再生位置を進めた状態を追従へ渡す]
    ///   引数: [rig: 組, position: 位置(sample)]
    ///   戻り値: []
    /// }
    private static void Advance((FakeSessionApi Api, PlaybackFollower Follower, PreviewController Preview, Func<KaraokeSessionSnapshot> Current) rig, ulong position)
    {
        var now = rig.Current();
        var next = Snap("playing", now.StateRevision, now.TransportEpoch, position);
        rig.Api.Current = next;
        rig.Follower.Apply(next, allowNewSession: true);
    }

    /// {
    ///   責務: [RangePlayback: 区間へシークして再生し、終端で一時停止を1回だけ送ることを確かめる]
    ///   処理: [
    ///     1: 再生可能(ready)のセッションで開始・再生の順に送られ、プレビュー中になる,
    ///     2: 終端の手前では止めず、終端到達で一時停止が1回だけ送られる,
    ///     3: 再生中のセッションでは再生を重ねて送らず、シークだけを送る,
    ///     4: 曲の長さを超える終端は曲末へ収める
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task RangePlayback()
    {
        var rig = await Rig("ready");
        await rig.Preview.PlayRangeAsync(1 * Second, 5 * Second);
        Check(Types(rig.Api).SequenceEqual(["seek", "play"]) && rig.Api.Sent[0].Command.SongSample == 1 * Second &&
            rig.Preview.IsPreviewing && rig.Follower.IsPlaying, "The range was not sought and played.");

        Advance(rig, 4 * Second);
        Check(rig.Preview.IsPreviewing && Types(rig.Api).Length == 2, "Playback stopped before the end of the range.");
        Advance(rig, 5 * Second);
        await rig.Preview.Idle;
        Check(Types(rig.Api).SequenceEqual(["seek", "play", "pause"]) && !rig.Preview.IsPreviewing && rig.Follower.State == "paused",
            "The preview did not pause once at the end.");
        Advance(rig, 6 * Second);
        Check(Types(rig.Api).Length == 3, "A late update after the end sent another command.");

        // 再生中のまま別の区間: 再生は重ねない。
        var live = await Rig("playing");
        await live.Preview.PlayRangeAsync(2 * Second, 3 * Second);
        Check(Types(live.Api).SequenceEqual(["seek"]) && live.Preview.IsPreviewing, "A playing session was sent play again.");

        // 終端は曲末へ収める。
        var tail = await Rig("ready");
        await tail.Preview.PlayRangeAsync(9 * Second, 99 * Second);
        Advance(tail, 10 * Second);
        await tail.Preview.Idle;
        Check(Types(tail.Api).SequenceEqual(["seek", "play", "pause"]), "The end was not clamped to the song length.");
    }

    /// {
    ///   責務: [Refusals: 送ってはいけない場合に何も送らず理由を示し、途中の失敗で予約を残さないことを確かめる]
    ///   処理: [
    ///     1: 空の区間, セッションの曲と編集中の曲が違う場合, セッションが無い場合は何も送らない,
    ///     2: シークが拒否されたら再生を送らず、プレビュー中にならない,
    ///     3: ユーザーの一時停止・別セッションへの切替で予約が捨てられ、後の位置で勝手に止めない,
    ///     4: StopAsyncはプレビュー中だけ一時停止を送る
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task Refusals()
    {
        var rig = await Rig("ready");
        await rig.Preview.PlayRangeAsync(3 * Second, 3 * Second);
        Check(rig.Api.Sent.Count == 0 && rig.Preview.HasNotice && !rig.Preview.IsPreviewing, "An empty range was sent.");

        var other = await Rig("ready", Guid.NewGuid());
        await other.Preview.PlayRangeAsync(1 * Second, 2 * Second);
        Check(other.Api.Sent.Count == 0 && other.Preview.Notice.Contains("一致"), "A different song was previewed.");
        Check(!other.Preview.CanPreview && rig.Preview.CanPreview, "CanPreview did not follow the song match.");

        var none = new PreviewController(new PlaybackFollower(new KaraokeViewModel(), autoPoll: false), new KaraokeViewModel(), new SongEditorViewModel());
        await none.PlayRangeAsync(0, Second);
        Check(none.HasNotice && !none.IsPreviewing, "A preview started without a song or session.");

        var refused = await Rig("ready");
        refused.Api.Fail = HttpStatusCode.UnprocessableEntity;
        await refused.Preview.PlayRangeAsync(1 * Second, 2 * Second);
        Check(Types(refused.Api).SequenceEqual(["seek"]) && !refused.Preview.IsPreviewing && refused.Preview.HasNotice,
            "A refused seek still started playing.");

        var paused = await Rig("ready");
        await paused.Preview.PlayRangeAsync(1 * Second, 5 * Second);
        var now = paused.Current();
        paused.Follower.Apply(Snap("paused", now.StateRevision + 1, now.TransportEpoch, 2 * Second), allowNewSession: false);
        Check(!paused.Preview.IsPreviewing, "A user pause kept the end reservation.");
        Advance(paused, 6 * Second);
        Check(Types(paused.Api).SequenceEqual(["seek", "play"]), "A cancelled preview paused the song later.");

        var stop = await Rig("ready");
        await stop.Preview.StopAsync();
        Check(stop.Api.Sent.Count == 0, "Stop sent a command without a preview.");
        await stop.Preview.PlayRangeAsync(1 * Second, 5 * Second);
        await stop.Preview.StopAsync();
        Check(Types(stop.Api).SequenceEqual(["seek", "play", "pause"]) && !stop.Preview.IsPreviewing && stop.Follower.State == "paused",
            "Stop did not pause the preview.");

        // 切断(Detach)で予約が消える。
        var detach = await Rig("ready");
        await detach.Preview.PlayRangeAsync(1 * Second, 5 * Second);
        detach.Follower.Detach();
        Check(!detach.Preview.IsPreviewing, "Disconnect kept the end reservation.");
    }

    /// {
    ///   責務: [ShellWiring: ShellViewModelがプレビューを持ち、行の範囲(sample)を渡せることを確かめる]
    ///   処理: [1: 行のStartSample/EndSampleが48kHz整数sampleである, 2: ShellにPreviewがありEditorと同じ曲で動く前提(CanPreview)になる]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static Task ShellWiring()
    {
        var shell = new ShellViewModel();
        Check(shell.Preview is not null && !shell.Preview.CanPreview && !shell.Preview.IsPreviewing, "The shell had no idle preview.");
        var editor = new SongEditorViewModel();
        editor.Load(new FakeSessionApi(), SongId, EditorTests.Bundle());
        var lyric = editor.LyricRows.Single(r => r.Id == "a");
        var note = editor.NoteRows.Single(r => r.Id == "n2");
        Check(lyric.StartSample == 1 * Second && lyric.EndSample == 5 * Second && note.StartSample == 3 * Second && note.EndSample == 4 * Second,
            "Row ranges were not exposed in samples.");
        return Task.CompletedTask;
    }
}
