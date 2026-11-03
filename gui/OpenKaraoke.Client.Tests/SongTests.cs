using System.Net;
using System.Text;
using OpenKaraoke.Client;
using static OpenKaraoke.Client.Tests.Program;

namespace OpenKaraoke.Client.Tests;

/// {
///   責務: [SongTests: 曲一覧・曲データの取得と、48kHz時間軸での歌詞・音程バー同期を検証する]
///   フィールド: []
/// }
internal static class SongTests
{
    private const ulong Second = SongTimeline.SampleRate;

    /// {
    ///   責務: [Song: テスト用の曲データを作る]
    ///   処理: [1: 指定の歌詞・ノートを48kHzの曲として組み立てる]
    ///   引数: [duration: 曲の長さ, lyrics: 歌詞, notes: ノート, sampleRate: sample rate, id: 曲ID]
    ///   戻り値: [曲データ]
    /// }
    internal static SongBundle Song(
        ulong duration, LyricSegment[] lyrics, ScoringNote[] notes, int sampleRate = SongTimeline.SampleRate, Guid? id = null) =>
        new(new SongManifest(1, (id ?? Guid.NewGuid()).ToString("D"), "テスト曲", "テスト歌手", sampleRate, duration),
            new LyricsDocument(lyrics), new NotesDocument(notes));

    /// {
    ///   責務: [Fixture: 並び順がばらばらで重なりを含む曲を作る]
    ///   処理: [1: 10秒の曲に歌詞3区間とノート3つを、意図的に逆順で並べる]
    ///   引数: []
    ///   戻り値: [曲データ]
    /// }
    internal static SongBundle Fixture() => Song(10 * Second,
        [
            new("c", "三行目", 6 * Second, 8 * Second),
            // 長い1行目の途中から2行目が始まる(重なり)。
            new("a", "一行目", 1 * Second, 5 * Second),
            new("b", "二行目", 3 * Second, 4 * Second),
        ],
        [
            new("n3", 6 * Second, 7 * Second, 64, "c"),
            new("n1", 0, 2 * Second, 60, "a"),
            new("n2", 3 * Second, 4 * Second, 67, "b"),
        ]);

    /// {
    ///   責務: [Conversions: sampleと秒の変換・表示書式を確かめる]
    ///   処理: [
    ///     1: 秒→sampleの丸めと、負・非数の扱いを確認する,
    ///     2: 表示書式を確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static Task Conversions()
    {
        Check(SongTimeline.FromSeconds(1.5) == 72_000 && SongTimeline.ToSeconds(72_000) == 1.5, "48 kHz conversion failed.");
        Check(SongTimeline.FromSeconds(0.5 / Second) == 1, "Half-sample rounding must go away from zero.");
        Check(SongTimeline.FromSeconds(-1) == 0 && SongTimeline.FromSeconds(double.NaN) == 0, "Invalid seconds were not clamped.");
        Check(SongTimeline.Format(65 * Second + Second * 3 / 10) == "1:05.3", "Position format changed.");
        Check(SongTimeline.Format(Second / 10 - 1) == "0:00.0", "Position format must truncate.");
        return Task.CompletedTask;
    }

    /// {
    ///   責務: [LyricSynchronization: 位置に対する現在・次の歌詞が境界と重なりで正しいか確かめる]
    ///   処理: [
    ///     1: 開始は含み終了は含まないことを確認する,
    ///     2: 重なりでは後から始まった行、短い行の終了後は長い行を返すことを確認する,
    ///     3: 歌詞の間と曲末の次の行を確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static Task LyricSynchronization()
    {
        var timeline = new SongTimeline(Fixture());
        string At(ulong sample) => timeline.LyricIndexAt(sample) is var i and >= 0 ? timeline.Lyrics[i].Text : "";
        string Next(ulong sample) => timeline.NextLyricIndexAfter(sample) is var i and >= 0 ? timeline.Lyrics[i].Text : "";
        Check(timeline.Lyrics.Select(s => s.Id).SequenceEqual(["a", "b", "c"]), "Lyrics were not ordered by start.");
        Check(At(Second - 1) == "" && At(Second) == "一行目", "Lyric start must be inclusive.");
        Check(At(3 * Second) == "二行目" && At(4 * Second) == "一行目", "Overlap must prefer the later line, then fall back.");
        Check(At(5 * Second - 1) == "一行目" && At(5 * Second) == "", "Lyric end must be exclusive.");
        Check(Next(5 * Second) == "三行目" && Next(3 * Second) == "三行目" && Next(6 * Second) == "", "Next lyric is wrong.");
        Check(Next(0) == "一行目", "Upcoming first lyric is missing.");
        return Task.CompletedTask;
    }

    /// {
    ///   責務: [SeekRegression: シークと範囲外の位置で表示が追従し、曲内へ収まるか確かめる]
    ///   処理: [
    ///     1: 先へ・後ろへシークして歌詞とバーの強調が変わることを確認する,
    ///     2: 曲末を超える位置を曲末へ丸めることを確認する,
    ///     3: 表示範囲より前に始まった長いノートも表示することを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static Task SeekRegression()
    {
        var model = new KaraokeViewModel();
        var notifications = 0;
        model.PropertyChanged += (_, _) => notifications++;
        model.Seek(5 * Second);
        Check(model.PositionSamples == 0 && notifications == 0, "Seeking without a song changed state.");
        model.Load(new SongTimeline(Fixture()));
        Check(model.HasSong && model.PositionSamples == 0 && model.NextLyric == "一行目", "Load did not start at the beginning.");
        model.PositionSeconds = 3.5;
        Check(model.PositionSamples == 168_000 && model.CurrentLyric == "二行目" && model.NextLyric == "三行目", "Forward seek did not sync.");
        Check(model.Bars.Single(bar => bar.IsActive).MidiNote == 67, "Active pitch bar did not follow the seek.");
        model.Seek(Second);
        Check(model.CurrentLyric == "一行目" && model.Bars.Single(bar => bar.IsActive).MidiNote == 60, "Backward seek did not sync.");
        var before = notifications;
        model.Seek(Second);
        Check(notifications == before, "Seeking to the same position notified again.");
        model.Seek(ulong.MaxValue);
        Check(model.PositionSamples == 10 * Second && model.CurrentLyric == "" && model.PositionText == "0:10.0 / 0:10.0",
            "Seek past the end was not clamped.");
        // 現在位置1.5秒: 表示範囲は0.5秒〜6.5秒。0秒開始のn1(〜2秒)も範囲に重なる。
        model.Seek(Second * 3 / 2);
        var bars = model.Bars;
        Check(bars.Select(bar => bar.MidiNote).SequenceEqual([60, 67, 64]), "Window notes are wrong.");
        Check(bars[0].X < 0 && Math.Abs(bars[1].X - (3 - 0.5) / 6) < 1e-9, "Bar position is not relative to the playhead.");
        Check(bars.All(bar => bar.Y is >= 0 and < 1) && bars[1].Y < bars[0].Y, "Higher notes must be drawn higher.");
        Check(Math.Abs(model.PlayheadX - 1.0 / 6) < 1e-9, "Playhead moved.");
        model.Clear();
        Check(!model.HasSong && model.Bars.Count == 0 && model.CurrentLyric == "", "Clear left song state.");
        return Task.CompletedTask;
    }

    /// {
    ///   責務: [InvalidTimelines: 48kHz以外・範囲外・空区間・音高範囲外の曲を拒否するか確かめる]
    ///   処理: [1: 不正な曲ごとにInvalidDataExceptionになることを確認する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task InvalidTimelines()
    {
        var cases = new[]
        {
            Song(Second, [], [], sampleRate: 44_100),
            Song(0, [], []),
            Song(Second, [new("a", "x", 0, Second + 1)], []),
            Song(Second, [new("a", "x", 10, 10)], []),
            Song(Second, [], [new("n", 0, 10, 128, null)]),
            Song(Second, [], []) with { Lyrics = null! },
            Song(Second, [new("a", null!, 0, 10)], []),
        };
        foreach (var song in cases)
            await Throws<InvalidDataException>(() => { _ = new SongTimeline(song); return Task.CompletedTask; });
    }

    /// {
    ///   責務: [ClientSongRoutes: 曲一覧・曲データのルート・検証・応答上限を確かめる]
    ///   処理: [
    ///     1: 一覧とページ指定のパスを確認する,
    ///     2: evidenceなど未使用の項目を読み飛ばして曲を読めることを確認する,
    ///     3: 別の曲・不整合なページ指定・上限超過の応答を拒否することを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task ClientSongRoutes()
    {
        var id = Guid.NewGuid();
        var paths = new List<string>();
        using var client = TestClient((request, _) =>
        {
            paths.Add(request.RequestUri!.PathAndQuery);
            return Task.FromResult(request.RequestUri.AbsolutePath == "/songs"
                ? Raw($$"""{"songs":[{"song_id":"{{id}}","title":"曲","artist":null,"duration_samples":48000,"error":null}],"next_after":null}""")
                : Raw($$$"""
                    {"song":{"format_version":1,"song_id":"{{{id}}}","title":"曲","artist":null,"sample_rate":48000,
                    "duration_samples":96000,"analysis_version":1,"files":{},"artifacts":{}},
                    "lyrics":{"format_version":1,"analysis_version":1,"segments":[{"id":"a","text":"歌","start_sample":0,
                    "end_sample":48000,"text_confidence":null,"timing_confidence":null,"source":null}]},
                    "notes":{"format_version":1,"analysis_version":1,"notes":[{"id":"n","start_sample":0,"end_sample":48000,
                    "midi_note":60,"pitch_confidence":0.9,"lyric_segment_id":"a"}]},
                    "evidence":{"format_version":1,"analysis_version":1,"items":[{"target_id":"a","evidence":{"model":{"x":1} } }] } }
                    """));
        });
        var page = await client.ListSongsAsync();
        Check(page.Songs.Single().SongId == id && page.NextAfter is null, "Song list was not parsed.");
        await client.ListSongsAsync(id);
        var bundle = await client.GetSongAsync(id);
        Check(new SongTimeline(bundle).Notes.Single().MidiNote == 60, "Song bundle was not parsed.");
        Check(paths.SequenceEqual(["/songs?limit=100", $"/songs?limit=100&after={id:D}", $"/songs/{id:D}"]), "Unexpected song routes.");
        await Throws<InvalidDataException>(() => client.GetSongAsync(Guid.NewGuid()));

        using var mismatch = TestClient((_, _) => Task.FromResult(
            Raw($$"""{"songs":[{"song_id":"{{id}}","title":"曲"}],"next_after":"{{Guid.NewGuid()}}"}""")));
        await Throws<InvalidDataException>(() => mismatch.ListSongsAsync());

        // Content-Lengthのない1MiB超の応答も、読みながら打ち切る。
        using var oversized = TestClient((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StreamContent(new UnsizedStream(Encoding.UTF8.GetBytes(
                "{\"songs\":[],\"next_after\":null,\"padding\":\"" + new string('a', 1_100_000) + "\"}"))),
        }));
        await Throws<InvalidDataException>(() => oversized.ListSongsAsync());
    }

    /// {
    ///   責務: [ViewModelSongs: 曲一覧の全ページ取得・読み込み・切断時の消去を確かめる]
    ///   処理: [
    ///     1: 2ページの一覧を連結して読むことを確認する,
    ///     2: 破損した曲は表示できず、正常な曲は表示できることを確認する,
    ///     3: 切断で一覧と表示中の曲が消えることを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task ViewModelSongs()
    {
        var good = new SongListItem(Guid.NewGuid(), "曲", null, 10 * Second, null);
        var broken = new SongListItem(Guid.NewGuid(), null, null, null, "song_corrupt");
        var fake = new FakeSongConnection
        {
            Pages = new([new SongPage([broken], broken.SongId), new SongPage([good], null)]),
            Song = Fixture(),
        };
        await using var model = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(fake));
        Check(!model.CanRefreshSongs, "Songs were refreshable before connecting.");
        await model.ConnectAsync();
        await model.RefreshSongsAsync();
        Check(model.Songs.SequenceEqual([broken, good]) && model.SongLibraryStatus == "2曲", "Pages were not concatenated.");
        model.SelectedSong = broken;
        Check(!model.CanLoadSong, "A damaged song was loadable.");
        model.SelectedSong = good;
        await model.LoadSelectedSongAsync();
        Check(model.Karaoke.HasSong && model.Error.Length == 0 && fake.RequestedSong == good.SongId, "Song was not loaded.");
        fake.Failure = new CoreApiException(HttpStatusCode.UnprocessableEntity);
        await model.LoadSelectedSongAsync();
        Check(model.Error.Contains("曲を読み込めません") && model.CanRefreshSongs, "A song failure must not disconnect Core.");
        await model.DisconnectAsync();
        Check(model.Songs.Count == 0 && model.SelectedSong is null && !model.Karaoke.HasSong, "Disconnect left song state.");
    }

    /// {
    ///   責務: [StaleSong: 切断後に遅れて届いた曲データを表示しないか確かめる]
    ///   処理: [
    ///     1: 曲の取得を保留したまま切断する,
    ///     2: 遅れて届いた曲が表示されないことを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task StaleSong()
    {
        var item = new SongListItem(Guid.NewGuid(), "曲", null, 10 * Second, null);
        var fake = new FakeSongConnection
        {
            Pages = new([new SongPage([item], null)]),
            Pending = new(TaskCreationOptions.RunContinuationsAsynchronously),
        };
        await using var model = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(fake));
        await model.ConnectAsync();
        await model.RefreshSongsAsync();
        model.SelectedSong = item;
        var loading = model.LoadSelectedSongAsync();
        await model.DisconnectAsync();
        fake.Pending.SetResult(Fixture());
        await loading;
        Check(!model.Karaoke.HasSong && model.CanConnect, "A late song resurrected a disconnected session.");
    }

    private static CoreApiClient TestClient(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> send) =>
        new(new Uri("http://127.0.0.1:32145/"), new string('a', 64), new TestHandler(send));

    private static HttpResponseMessage Raw(string json) =>
        new(HttpStatusCode.OK) { Content = new StringContent(json, Encoding.UTF8, "application/json") };

    /// {
    ///   責務: [TestHandler: 要求を関数へ渡すHTTP handler]
    ///   フィールド: [send: 応答を作る関数]
    /// }
    private sealed class TestHandler(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> send) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            send(request, cancellationToken);
    }

    /// {
    ///   責務: [UnsizedStream: 長さを報告しないstream(Content-Lengthなしの応答を再現する)]
    ///   フィールド: []
    /// }
    private sealed class UnsizedStream(byte[] data) : MemoryStream(data)
    {
        public override bool CanSeek => false;
    }

    /// {
    ///   責務: [FakeSongConnection: 曲APIだけを差し替えたCore接続]
    ///   フィールド: [
    ///     Pages: 順に返す曲一覧ページ, Song: 返す曲, Pending: 保留中の曲取得,
    ///     Failure: 曲取得で投げる例外, RequestedSong: 最後に要求された曲ID
    ///   ]
    /// }
    private sealed class FakeSongConnection : ICoreConnection, ICoreApi
    {
        public Guid InstanceId { get; } = Guid.NewGuid();
        public ICoreApi Api => this;
        public Queue<SongPage> Pages { get; init; } = new();
        public SongBundle? Song { get; init; }
        public TaskCompletionSource<SongBundle>? Pending { get; init; }
        public Exception? Failure { get; set; }
        public Guid? RequestedSong { get; private set; }
        public Task<CoreIdentity> GetIdentityAsync(CancellationToken cancellationToken = default) =>
            Task.FromResult(new CoreIdentity("open-karaoke-core", 1, InstanceId));
        public Task<System.Text.Json.JsonElement> GetModelsAsync(CancellationToken cancellationToken = default) =>
            Task.FromResult(System.Text.Json.JsonSerializer.SerializeToElement(new { models = Array.Empty<object>() }));
        public Task<JobSnapshot> CreateJobAsync(CreateJobRequest job, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<JobSnapshot> GetJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<JobSnapshot> CancelJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public IAsyncEnumerable<JobSnapshot> WatchJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<SongPage> ListSongsAsync(Guid? after = null, CancellationToken cancellationToken = default) =>
            Task.FromResult(Pages.Dequeue());
        public Task<SongBundle> GetSongAsync(Guid id, CancellationToken cancellationToken = default)
        {
            RequestedSong = id;
            if (Failure is not null) return Task.FromException<SongBundle>(Failure);
            // 保留中の取得は、切断(中断)されても結果が遅れて届く状況を再現するため中断を無視する。
            return Pending?.Task ?? Task.FromResult(Song ?? throw new InvalidOperationException("No song."));
        }
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }
}
