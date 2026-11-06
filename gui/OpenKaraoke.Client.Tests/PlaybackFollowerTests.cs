using System.Net;
using System.Text.Json;
using OpenKaraoke.Client;
using static OpenKaraoke.Client.Tests.Program;

namespace OpenKaraoke.Client.Tests;

/// {
///   責務: [PlaybackFollowerTests: 歌唱セッションの取得・操作のAPI経路と、PlaybackFollowerの受理規則(stale破棄・シーク往復・Core切断表示)を検証する]
///   フィールド: []
/// }
internal static class PlaybackFollowerTests
{
    private const ulong Second = SongTimeline.SampleRate;
    private static readonly Guid Instance = Guid.Parse("11111111-1111-1111-1111-111111111111");
    private static readonly Guid Session = Guid.Parse("22222222-2222-2222-2222-222222222222");
    private static readonly Guid SongId = Guid.Parse("33333333-3333-3333-3333-333333333333");

    /// {
    ///   責務: [Snap: テスト用の状態を作る]
    ///   引数: [state, revision, epoch, position(sample), session, instance, duration]
    ///   戻り値: [状態]
    /// }
    private static KaraokeSessionSnapshot Snap(
        string state, ulong revision, ulong epoch, ulong position, Guid? session = null, Guid? instance = null, ulong duration = 10 * Second) =>
        new(session ?? Session, instance ?? Instance, SongId, state, revision, epoch, position,
            state == "playing" ? "presented" : "committed", new ClockQualityInfo("unknown"), duration, "practice",
            new ScoreEligibilityInfo("practice"), null, 0);

    /// {
    ///   責務: [ClientRoutes: GET current / POST commands の経路・本文・404・不正値の拒否を確かめる]
    ///   処理: [1: currentの解析(measured/downgraded含む)と404のnull化, 2: コマンド本文の形(seekだけsong_sample), 3: 契約外の応答・不正な要求を拒否する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task ClientRoutes()
    {
        var calls = new List<string>();
        var body = $$"""
            {"session_id":"{{Session}}","core_instance_id":"{{Instance}}","song_id":"{{SongId}}","state":"playing",
            "state_revision":4,"transport_epoch":2,"position_song_sample":96000,"position_source":"presented",
            "clock_quality":{"kind":"measured","uncertainty_samples":240},"duration_samples":480000,"scoring_mode":"whole_song",
            "score_eligibility":{"kind":"downgraded","reason":"seek"},"error_code":null,"stale_rejections":1}
            """;
        using var client = SongTests.TestClient(async (request, _) =>
        {
            calls.Add($"{request.Method} {request.RequestUri!.PathAndQuery} {(request.Content is null ? "" : await request.Content.ReadAsStringAsync())}");
            return request.Method == HttpMethod.Get
                ? SongTests.Raw(body)
                : SongTests.Raw($$"""{"disposition":"applied","snapshot":{{body}}}""");
        });
        var current = await client.GetCurrentKaraokeSessionAsync();
        Check(current is { State: "playing", StateRevision: 4, TransportEpoch: 2, PositionSongSample: 96000 } &&
            current.ClockQuality is { Kind: "measured", UncertaintySamples: 240 } &&
            current.ScoreEligibility is { Kind: "downgraded", Reason: "seek" }, "The session snapshot was not parsed.");
        var seek = new KaraokeCommandRequest(Instance, Guid.Parse("44444444-4444-4444-4444-444444444444"), 4, new("seek", 48000));
        var result = await client.SendKaraokeCommandAsync(Session, seek);
        await client.SendKaraokeCommandAsync(Session, seek with { Command = new("pause") });
        Check(result.Disposition == "applied" && calls[0] == "GET /karaoke-sessions/current ", "Unexpected current request: " + calls[0]);
        Check(calls[1].StartsWith($"POST /karaoke-sessions/{Session:D}/commands {{") && calls[1].Contains("\"expected_revision\":4") &&
            calls[1].Contains("\"command\":{\"type\":\"seek\",\"song_sample\":48000}") && calls[1].Contains("\"core_instance_id\""),
            "Unexpected seek request: " + calls[1]);
        Check(calls[2].Contains("\"command\":{\"type\":\"pause\"}"), "A pause carried a position: " + calls[2]);

        using var none = SongTests.TestClient((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound)));
        Check(await none.GetCurrentKaraokeSessionAsync() is null, "404 was not shown as no session.");
        using var broken = SongTests.TestClient((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.InternalServerError)));
        await Throws<CoreApiException>(() => broken.GetCurrentKaraokeSessionAsync());
        using var badState = SongTests.TestClient((_, _) => Task.FromResult(SongTests.Raw(body.Replace("\"playing\"", "\"warp\""))));
        await Throws<InvalidDataException>(() => badState.GetCurrentKaraokeSessionAsync());
        using var beyond = SongTests.TestClient((_, _) => Task.FromResult(SongTests.Raw(body.Replace("96000", "999999"))));
        await Throws<InvalidDataException>(() => beyond.GetCurrentKaraokeSessionAsync());
        using var other = SongTests.TestClient((_, _) => Task.FromResult(SongTests.Raw($$"""{"disposition":"applied","snapshot":{{body}}}""")));
        await Throws<InvalidDataException>(() => other.SendKaraokeCommandAsync(Guid.NewGuid(), seek));
        await Throws<ArgumentException>(() => client.SendKaraokeCommandAsync(Session, seek with { Command = new("seek") }));
        await Throws<ArgumentException>(() => client.SendKaraokeCommandAsync(Session, seek with { Command = new("play", 1) }));
        await Throws<ArgumentException>(() => client.SendKaraokeCommandAsync(Session, seek with { Command = new("reboot") }));
        await Throws<ArgumentException>(() => client.SendKaraokeCommandAsync(Guid.Empty, seek));
        var denied = SongTests.TestClient((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.Conflict)));
        Check((await Throws<CoreApiException>(() => denied.SendKaraokeCommandAsync(Session, seek))).StatusCode == HttpStatusCode.Conflict,
            "A command conflict lost its status.");
    }

    /// {
    ///   責務: [StaleRules: 古いrevision/epoch・後退・別Core・別セッションの更新を捨て、新epochを受理して通知することを確かめる]
    ///   処理: [
    ///     1: 最初の状態で位置が表示へ出る,
    ///     2: revisionが古い更新(StaleRevision)とepochが古い更新(StaleEpoch)を、版が新しくても捨てる,
    ///     3: 同じ版での再生位置の後退を捨て、別Core世代は捨てる,
    ///     4: 新しいepochは受理して現行epochを更新しEpochChangedを出す,
    ///     5: 別セッションはコマンド応答では捨て、取得では乗り換える,
    ///     6: 表示中の曲と別の曲では位置を動かさない
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static void StaleRules()
    {
        var karaoke = new KaraokeViewModel();
        karaoke.Load(new SongTimeline(SongTests.Song(10 * Second, [], [])), SongId);
        var follower = new PlaybackFollower(karaoke, autoPoll: false);
        var api = new FakeSessionApi();
        follower.Attach(api, Instance, new CancellationTokenSource().Token);
        var epochEvents = 0;
        follower.EpochChanged += (_, _) => epochEvents++;

        Check(follower.Apply(Snap("playing", 3, 2, 2 * Second), true) == ApplyResult.NewSession && karaoke.PositionSamples == 2 * Second,
            "The first snapshot was not shown.");
        Check(follower.Apply(Snap("playing", 3, 2, 3 * Second), true) == ApplyResult.Applied && karaoke.PositionSamples == 3 * Second,
            "A position update at the same revision was refused.");
        Check(follower.Apply(Snap("paused", 2, 2, 9 * Second), true) == ApplyResult.StaleRevision, "An old revision was accepted.");
        Check(follower.Apply(Snap("playing", 5, 1, 9 * Second), true) == ApplyResult.StaleEpoch, "An old epoch was accepted.");
        Check(follower.Apply(Snap("playing", 3, 2, Second), true) == ApplyResult.Regressed, "A same-revision regression was accepted.");
        Check(follower.Apply(Snap("playing", 9, 9, 9 * Second, instance: Guid.NewGuid()), true) == ApplyResult.WrongInstance,
            "Another Core generation was accepted.");
        Check(karaoke.PositionSamples == 3 * Second && follower.Revision == 3 && follower.Epoch == 2 && follower.Discarded == 4,
            $"Discarded updates changed the display: {karaoke.PositionSamples} rev {follower.Revision} epoch {follower.Epoch} dropped {follower.Discarded}.");
        Check(epochEvents == 1, "The first session should have announced a fresh epoch once.");

        Check(follower.Apply(Snap("playing", 4, 3, 7 * Second), true) == ApplyResult.EpochAdvanced && follower.Epoch == 3 &&
            karaoke.PositionSamples == 7 * Second && epochEvents == 2 && follower.EpochResets == 2,
            "A newer epoch was not accepted and announced.");
        Check(follower.Apply(Snap("paused", 5, 3, 7 * Second), true) == ApplyResult.Applied && epochEvents == 2,
            "A same-epoch update announced a reset.");

        var other = Guid.NewGuid();
        Check(follower.Apply(Snap("playing", 1, 1, Second, session: other), false) == ApplyResult.WrongSession && follower.Session!.SessionId == Session,
            "A command result for another session replaced the current one.");
        Check(follower.Apply(Snap("ready", 1, 1, 0, session: other), true) == ApplyResult.NewSession && follower.Revision == 1 && epochEvents == 3,
            "A new session was not adopted by polling.");

        var foreign = new KaraokeViewModel();
        foreign.Load(new SongTimeline(SongTests.Song(10 * Second, [], [])), Guid.NewGuid());
        var guarded = new PlaybackFollower(foreign, autoPoll: false);
        guarded.Attach(api, Instance, new CancellationTokenSource().Token);
        guarded.Apply(Snap("playing", 1, 1, 5 * Second), true);
        Check(foreign.PositionSamples == 0 && guarded.StatusText.Contains("別の曲"), "A different song's position moved the display.");
    }

    /// {
    ///   責務: [SeekRoundTrip: シーク往復(依頼→新epoch受理→遅れて届く旧状態の破棄)と、競合・範囲外の扱いを確かめる]
    ///   処理: [
    ///     1: 再生中の状態を取得する,
    ///     2: 曲の終端を超えるシークは終端直前へ収めて送り、最新revisionと宛先Core世代が付く,
    ///     3: 応答の新epochが反映され、遅れて届いた旧epochの状態は捨てられる,
    ///     4: 再生/一時停止/停止が順に送られ、IDが毎回違う,
    ///     5: 409は再送せず、取得し直して知らせる, 6: 422は範囲外として知らせる
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task SeekRoundTrip()
    {
        var karaoke = new KaraokeViewModel();
        karaoke.Load(new SongTimeline(SongTests.Song(10 * Second, [], [])), SongId);
        var ids = new Queue<Guid>(Enumerable.Range(1, 20).Select(_ => Guid.NewGuid()));
        var follower = new PlaybackFollower(karaoke, ids.Dequeue, autoPoll: false);
        var api = new FakeSessionApi { Current = Snap("playing", 3, 1, 2 * Second) };
        follower.Attach(api, Instance, new CancellationTokenSource().Token);
        follower.PollInterval = TimeSpan.FromHours(1);
        await follower.PollOnceAsync();
        Check(follower.IsPlaying && follower.CanSeek && follower.CanPause && !follower.CanPlay && karaoke.PositionSamples == 2 * Second,
            "A playing session was not followed.");

        api.OnCommand = request => Snap("playing", request.ExpectedRevision + 1, 2, request.Command.SongSample!.Value);
        await follower.SeekAsync(99 * Second);
        var sent = api.Sent.Single();
        Check(sent.Command.Type == "seek" && sent.Command.SongSample == 10 * Second - 1 && sent.ExpectedRevision == 3 && sent.CoreInstanceId == Instance,
            "The seek request was wrong.");
        Check(follower.Epoch == 2 && follower.Revision == 4 && karaoke.PositionSamples == 10 * Second - 1, "The seek result was not shown.");

        api.Current = Snap("playing", 3, 1, 2 * Second);
        await follower.PollOnceAsync();
        Check(follower.Epoch == 2 && karaoke.PositionSamples == 10 * Second - 1 && follower.Discarded == 1,
            "A late snapshot from before the seek replaced the new position.");

        api.OnCommand = request => Snap("paused", request.ExpectedRevision + 1, request.Command.Type == "pause" ? 3UL : 2UL, 5 * Second);
        await follower.PauseAsync();
        Check(follower.State == "paused" && follower.CanPlay && !follower.IsPlaying, "Pause was not reflected.");
        api.OnCommand = request => Snap("playing", request.ExpectedRevision + 1, 4, 5 * Second);
        await follower.PlayAsync();
        api.OnCommand = request => Snap("stopped", request.ExpectedRevision + 1, 5, 5 * Second);
        await follower.StopAsync();
        Check(api.Sent.Select(r => r.Command.Type).SequenceEqual(["seek", "pause", "play", "stop"]) &&
            api.Sent.Select(r => r.CommandId).Distinct().Count() == 4 && follower.State == "stopped" && !follower.CanStop,
            "Pause/play/stop did not reach Core in order.");
        await follower.PlayAsync();
        Check(api.Sent.Count == 4 && follower.Notice.Length > 0, "A command was sent after the session ended.");

        // 409: 再送せず、状態を取得し直す。
        var live = new FakeSessionApi { Current = Snap("playing", 3, 1, 2 * Second) };
        var again = new PlaybackFollower(new KaraokeViewModel(), autoPoll: false);
        again.Attach(live, Instance, new CancellationTokenSource().Token);
        again.PollInterval = TimeSpan.FromHours(1);
        await again.PollOnceAsync();
        live.Current = Snap("paused", 7, 2, 4 * Second);
        live.Fail = HttpStatusCode.Conflict;
        await again.SeekAsync(Second);
        Check(live.Sent.Count == 1 && again.Revision == 7 && again.State == "paused" && again.Notice.Contains("もう一度"),
            "A conflict was retried or the state was not refreshed.");
        live.Fail = HttpStatusCode.UnprocessableEntity;
        await again.SeekAsync(Second);
        Check(live.Sent.Count == 2 && again.Notice.Contains("シークできません") && again.Link == FollowerLink.Following, "A rejected seek was not explained.");
        live.Fail = HttpStatusCode.ServiceUnavailable;
        await again.PlayAsync();
        Check(again.Link == FollowerLink.CoreLost && !again.CanPlay, "A failed command did not show the lost Core.");
    }

    /// {
    ///   責務: [CoreLoss: Core切断・中断・セッション消滅・再接続を、旧位置で再生中に見せず、自動再開もしないことを確かめる]
    ///   処理: [
    ///     1: 取得失敗でCoreLostになり、再生中・操作可でなくなる,
    ///     2: 次の取得成功で復帰する,
    ///     3: interruptedは再生中と見せず位置も動かさない,
    ///     4: 404(null)でセッション無しになる,
    ///     5: Detachで未接続になり、再接続(新Core世代)では旧世代の状態を捨て、コマンドを自動で送らない
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task CoreLoss()
    {
        var karaoke = new KaraokeViewModel();
        karaoke.Load(new SongTimeline(SongTests.Song(10 * Second, [], [])), SongId);
        var follower = new PlaybackFollower(karaoke, autoPoll: false);
        var api = new FakeSessionApi { Current = Snap("playing", 3, 1, 2 * Second) };
        Check(follower.Link == FollowerLink.Detached && follower.StatusText.Contains("接続すると"), "A detached follower claimed a state.");
        follower.Attach(api, Instance, new CancellationTokenSource().Token);
        follower.PollInterval = TimeSpan.FromHours(1);
        await follower.PollOnceAsync();
        Check(follower.IsPlaying, "Setup failed.");

        api.ThrowOnPoll = new HttpRequestException("down");
        await follower.PollOnceAsync();
        Check(follower.Link == FollowerLink.CoreLost && !follower.IsPlaying && !follower.CanPause && !follower.CanStop &&
            follower.StatusText.Contains("通信できません") && follower.ClockQualityText == "—", "A lost Core still looked playing.");
        api.ThrowOnPoll = null;
        await follower.PollOnceAsync();
        Check(follower.IsPlaying, "The follower did not recover.");

        api.Current = Snap("interrupted", 4, 2, 2 * Second);
        await follower.PollOnceAsync();
        Check(!follower.IsPlaying && !follower.CanPlay && !follower.CanSeek && follower.CanStop && follower.StatusText.Contains("中断") &&
            karaoke.PositionSamples == 2 * Second, "An interrupted session looked playing.");

        api.Current = null;
        await follower.PollOnceAsync();
        Check(follower.Link == FollowerLink.NoSession && follower.Session is null && !follower.IsPlaying, "A vanished session was kept.");

        api.Current = Snap("playing", 5, 2, 2 * Second);
        await follower.PollOnceAsync();
        follower.Detach();
        Check(follower.Link == FollowerLink.Detached && follower.Session is null && !follower.CanStop, "Detach kept the session.");

        // 再接続は新しいCore世代。旧世代のセッションは受理せず、何も自動送信しない。
        var restarted = Guid.NewGuid();
        follower.Attach(api, restarted, new CancellationTokenSource().Token);
        follower.PollInterval = TimeSpan.FromHours(1);
        await follower.PollOnceAsync();
        Check(follower.Link == FollowerLink.NoSession && follower.Discarded == 1 && api.Sent.Count == 0 && !follower.IsPlaying,
            "A stale-generation session was resumed after reconnecting.");
    }

    /// {
    ///   責務: [PollingLoop: 取得ループが一定間隔で動き、Detachで止まることを確かめる]
    ///   処理: [1: 10ms間隔で数回取得される, 2: Detach後は取得が増えない, 3: 切断中の取得失敗でも停止しない]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task PollingLoop()
    {
        var api = new FakeSessionApi { Current = Snap("ready", 1, 1, 0), ThrowOnPoll = new HttpRequestException("down") };
        var follower = new PlaybackFollower(new KaraokeViewModel()) { PollInterval = TimeSpan.FromMilliseconds(10) };
        follower.Attach(api, Instance, CancellationToken.None);
        await WaitFor(() => api.Polls >= 3);
        Check(follower.Link == FollowerLink.CoreLost, "A failing poll stopped the loop or hid the loss.");
        api.ThrowOnPoll = null;
        await WaitFor(() => follower.Link == FollowerLink.Following);
        follower.Detach();
        await follower.Idle.WaitAsync(TimeSpan.FromSeconds(5));
        var polls = api.Polls;
        await Task.Delay(80);
        Check(api.Polls == polls, "The loop kept polling after Detach.");
    }

    /// {
    ///   責務: [ShellLifecycle: 画面の接続でPlaybackが始まり、切断で未接続へ戻ることを確かめる]
    ///   処理: [1: 接続後に状態を取得して追従する, 2: 切断でPlaybackが未接続になる]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task ShellLifecycle()
    {
        var api = new FakeSessionApi { Current = Snap("ready", 1, 1, 0) };
        await using var shell = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(new FakeSessionConnection(api)));
        await shell.ConnectAsync();
        await WaitFor(() => shell.Playback.Link == FollowerLink.Following);
        Check(shell.Playback.CanPlay, "The shell did not attach the follower.");
        await shell.DisconnectAsync();
        Check(shell.Playback.Link == FollowerLink.Detached && !shell.Playback.CanPlay, "The shell kept the session after disconnect.");
    }

    private static async Task WaitFor(Func<bool> condition)
    {
        for (var i = 0; i < 500 && !condition(); i++) await Task.Delay(10);
        Check(condition(), "The expected state did not appear in time.");
    }

    /// {
    ///   責務: [FakeSessionApi: 歌唱セッションの取得・コマンドをテストから操作できるFake]
    ///   フィールド: [Current: 取得で返す状態, ThrowOnPoll: 取得を失敗させる, Fail: コマンドをこのHTTP状態で拒否する, OnCommand: コマンドから結果の状態を作る, Sent: 受け取ったコマンド, Polls: 取得回数]
    /// }
    private sealed class FakeSessionApi : ICoreApi
    {
        public KaraokeSessionSnapshot? Current { get; set; }
        public Exception? ThrowOnPoll { get; set; }
        public HttpStatusCode? Fail { get; set; }
        public Func<KaraokeCommandRequest, KaraokeSessionSnapshot>? OnCommand { get; set; }
        public List<KaraokeCommandRequest> Sent { get; } = [];
        public int Polls;

        public Task<KaraokeSessionSnapshot?> GetCurrentKaraokeSessionAsync(CancellationToken cancellationToken = default)
        {
            Interlocked.Increment(ref Polls);
            return ThrowOnPoll is { } error ? Task.FromException<KaraokeSessionSnapshot?>(error) : Task.FromResult(Current);
        }

        public Task<KaraokeCommandResult> SendKaraokeCommandAsync(Guid sessionId, KaraokeCommandRequest request, CancellationToken cancellationToken = default)
        {
            Sent.Add(request);
            if (Fail is { } status) return Task.FromException<KaraokeCommandResult>(new CoreApiException(status));
            var snapshot = OnCommand!(request);
            Current = snapshot;
            return Task.FromResult(new KaraokeCommandResult("applied", snapshot));
        }

        public Task<CoreIdentity> GetIdentityAsync(CancellationToken cancellationToken = default) => Task.FromResult(new CoreIdentity("open-karaoke-core", 1, Instance));
        public Task<JsonElement> GetModelsAsync(CancellationToken cancellationToken = default) => Task.FromResult(JsonSerializer.SerializeToElement(new { models = Array.Empty<object>() }));
        public Task<ModelPackageItem[]> GetModelPackagesAsync(CancellationToken cancellationToken = default) => Task.FromResult<ModelPackageItem[]>([]);
        public Task<JobSnapshot> CreateJobAsync(CreateJobRequest job, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<JobSnapshot> GetJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<JobSnapshot> CancelJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public IAsyncEnumerable<JobSnapshot> WatchJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<SongPage> ListSongsAsync(Guid? after = null, CancellationToken cancellationToken = default) => Task.FromResult(new SongPage([], null));
        public Task<SongBundle> GetSongAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    }

    /// {
    ///   責務: [FakeSessionConnection: FakeSessionApiを持つ接続]
    ///   フィールド: [api: 共有するFakeSessionApi]
    /// }
    private sealed class FakeSessionConnection(FakeSessionApi api) : ICoreConnection
    {
        public ICoreApi Api => api;
        public Guid InstanceId => Instance;
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }
}
