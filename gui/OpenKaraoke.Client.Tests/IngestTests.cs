using System.Runtime.CompilerServices;
using System.Text.Json;
using OpenKaraoke.Client;
using static OpenKaraoke.Client.Tests.Program;

namespace OpenKaraoke.Client.Tests;

/// {
///   責務: [IngestTests: 元音源から曲登録までの連鎖(正規化→解析→登録)の段順・失敗・取消・要求本文を検証する]
///   フィールド: []
/// }
internal static class IngestTests
{
    private const string Wav = "imports/11111111-1111-4111-8111-111111111111/original_48k.wav";
    private static readonly string Source = Path.Combine(Path.GetTempPath(), "song.mp3");

    /// {
    ///   責務: [Success: 3段が順に作られ、前段の成果物が次段へ渡り、登録された曲が返ることを確かめる]
    ///   処理: [1: 全段を完了で返すFakeで連鎖を走らせる, 2: Jobの種別順・入力・題名を確認する, 3: 進捗の段が順に出ることを確認する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task Success()
    {
        var api = new IngestApi();
        var stages = new List<string>();
        var outcome = await new SongIngestFlow(api).RunAsync(
            new SongIngestRequest(Source, "題名", "model-a"),
            new SyncProgress(p => stages.Add(p.Stage)));
        Check(outcome.Succeeded && outcome.Song is { Reused: false }, "The chain did not succeed.");
        Check(api.Created.Select(j => j.Kind).SequenceEqual(["audio.import", "analysis.song", "song.import"]), "Stages ran in the wrong order.");
        var analysis = JsonSerializer.Serialize(api.Created[1].Analysis, CoreApiClient.JsonOptions);
        Check(analysis.Contains($"\"input_artifact\":\"{Wav}\"") && analysis.Contains("\"model_id\":\"model-a\""),
            "The analysis request lost the artifact or model: " + analysis);
        Check(analysis.Contains("\"title\":\"") && analysis.Contains("\\u984C\\u540D"), "The analysis request lost the title: " + analysis);
        var import = JsonSerializer.Serialize(api.Created[2].SongImport, CoreApiClient.JsonOptions);
        Check(import.Contains(api.Ids[1].ToString()), "song.import did not point at the analysis job: " + import);
        Check(stages.Distinct().SequenceEqual(["normalizing", "analyzing", "importing"]), "Progress stages were wrong.");
    }

    /// {
    ///   責務: [FailureStopsTheChain: 前段が失敗した時、後続Jobを作らず段名と理由を返すことを確かめる]
    ///   処理: [1: 正規化・解析・登録のそれぞれで失敗させる, 2: 作られたJob数と結末を確認する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task FailureStopsTheChain()
    {
        foreach (var (kind, stage, created) in new[] { ("audio.import", "normalizing", 1), ("analysis.song", "analyzing", 2), ("song.import", "importing", 3) })
        {
            var api = new IngestApi { FailKind = kind };
            var outcome = await new SongIngestFlow(api).RunAsync(new SongIngestRequest(Source, "t"));
            Check(outcome.Status == "failed" && outcome.Stage == stage && outcome.Error == "boom: " + kind && outcome.Song is null,
                $"{kind} failure was reported wrongly: {outcome}");
            Check(api.Created.Count == created, $"{kind} failure still created {api.Created.Count} jobs.");
        }
    }

    /// {
    ///   責務: [CancelStopsOnlyTheCurrentStage: 解析中の取消が現在のJobだけを取り消し、登録Jobを作らないことを確かめる]
    ///   処理: [1: 解析で止まるFakeを走らせる, 2: 取消を要求する, 3: 取り消されたJobと結末・作成数を確認する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task CancelStopsOnlyTheCurrentStage()
    {
        var api = new IngestApi { HoldKind = "analysis.song" };
        using var cts = new CancellationTokenSource();
        var run = new SongIngestFlow(api).RunAsync(new SongIngestRequest(Source, "t"), null, cts.Token);
        await api.Entered.Task.WaitAsync(TimeSpan.FromSeconds(10));
        await cts.CancelAsync();
        var outcome = await run;
        Check(outcome.Status == "cancelled" && outcome.Stage == "analyzing", $"Unexpected outcome: {outcome}");
        Check(api.Cancelled.SequenceEqual([api.Ids[1]]), "Only the analysis job should be cancelled.");
        Check(api.Created.Count == 2, "song.import must not be created after a cancel.");
    }

    /// {
    ///   責務: [InvalidInputAndContract: 不正な入力の拒否と、Coreの応答が契約外の時の例外を確かめる]
    ///   処理: [1: 相対パスと空の題名を拒否する, 2: 正規化WAVの成果物が無い完了を契約違反として拒否する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task InvalidInputAndContract()
    {
        var flow = new SongIngestFlow(new IngestApi());
        await Throws<ArgumentException>(() => flow.RunAsync(new SongIngestRequest("relative.mp3", "t")));
        await Throws<ArgumentException>(() => flow.RunAsync(new SongIngestRequest(Source, " ")));
        await Throws<InvalidDataException>(() => new SongIngestFlow(new IngestApi { NoWav = true }).RunAsync(new SongIngestRequest(Source, "t")));
        await Throws<InvalidDataException>(() => new SongIngestFlow(new IngestApi { NoTerminal = true }).RunAsync(new SongIngestRequest(Source, "t")));
    }

    /// {
    ///   責務: [RequestBodies: Job作成の本文がaudio_import/song_importをsnake_caseで送り、nullを省くことを確かめる]
    ///   処理: [1: 本物のCoreApiClientでaudio.importとsong.importを作る, 2: 送信本文を確認する, 3: imported_songの読取を確認する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task RequestBodies()
    {
        var bodies = new List<string>();
        var id = Guid.NewGuid();
        var song = Guid.NewGuid();
        using var client = SongTests.TestClient(async (request, _) =>
        {
            bodies.Add(await request.Content!.ReadAsStringAsync());
            return SongTests.Raw($"{{\"id\":\"{id}\",\"kind\":\"song.import\",\"status\":\"completed\",\"progress\":1,\"stage\":\"done\",\"error\":null,\"artifacts\":[],\"imported_song\":{{\"song_id\":\"{song}\",\"reused\":true}}}}");
        });
        await client.CreateJobAsync(new CreateJobRequest("audio.import", AudioImport: new { source_path = Source }));
        var done = await client.CreateJobAsync(new CreateJobRequest("song.import", SongImport: new { analysis_job_id = id }));
        Check(bodies[0].Contains("\"audio_import\":{\"source_path\"") && !bodies[0].Contains("analysis") && !bodies[0].Contains("song_import"), "audio.import body: " + bodies[0]);
        Check(bodies[1].Contains($"\"song_import\":{{\"analysis_job_id\":\"{id}\"}}"), "song.import body: " + bodies[1]);
        Check(done.ImportedSong == new ImportedSongInfo(song, true), "imported_song was not parsed.");
    }

    /// {
    ///   責務: [SyncProgress: 通知を呼び出し元のスレッドで同期的に受け取るIProgress(Progress&lt;T&gt;の遅延を避ける)]
    ///   フィールド: [handler: 受け取る処理]
    /// }
    private sealed class SyncProgress(Action<SongIngestProgress> handler) : IProgress<SongIngestProgress>
    {
        public void Report(SongIngestProgress value) => handler(value);
    }

    /// {
    ///   責務: [IngestApi: 連鎖のJobを段ごとに完了・失敗・保留させられるFake]
    ///   フィールド: [Created: 作られた要求, Ids: 作られたJob ID, Cancelled: 取り消されたJob ID, FailKind: 失敗させる種別, HoldKind: 取消まで止める種別, NoWav: 正規化WAVの成果物を返さない, Entered: 保留に入った合図]
    /// }
    internal sealed class IngestApi : ICoreApi
    {
        public List<CreateJobRequest> Created { get; } = [];
        public List<Guid> Ids { get; } = [];
        public List<Guid> Cancelled { get; } = [];
        public string? FailKind { get; init; }
        public string? HoldKind { get; init; }
        public bool NoWav { get; init; }
        public bool NoTerminal { get; init; }
        public Guid InstanceId { get; } = Guid.NewGuid();
        public Guid ImportedId { get; } = Guid.NewGuid();
        public List<SongListItem> Songs { get; } = [];
        public int ListCalls { get; private set; }
        public TaskCompletionSource Entered { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public Task<JobSnapshot> CreateJobAsync(CreateJobRequest job, CancellationToken cancellationToken = default)
        {
            lock (Created)
            {
                Created.Add(job);
                var id = Guid.NewGuid();
                Ids.Add(id);
                return Task.FromResult(Snapshot(job.Kind, id, "queued"));
            }
        }

        public async IAsyncEnumerable<JobSnapshot> WatchJobAsync(Guid id, [EnumeratorCancellation] CancellationToken cancellationToken = default)
        {
            var kind = Created[Ids.IndexOf(id)].Kind;
            yield return Snapshot(kind, id, "running", 0.5);
            if (kind == HoldKind)
            {
                Entered.TrySetResult();
                await Task.Delay(Timeout.Infinite, cancellationToken);
            }
            if (NoTerminal) yield break;
            yield return Snapshot(kind, id, kind == FailKind ? "failed" : "completed", 1);
        }

        public Task<JobSnapshot> CancelJobAsync(Guid id, CancellationToken cancellationToken = default)
        {
            Cancelled.Add(id);
            return Task.FromResult(Snapshot(Created[Ids.IndexOf(id)].Kind, id, "cancelled"));
        }

        private JobSnapshot Snapshot(string kind, Guid id, string status, double progress = 0) => new(
            id, kind, status, progress, status,
            status == "failed" ? "boom: " + kind : null,
            status == "completed" && kind == "audio.import" ? (NoWav ? ["imports/x/normalized.json"] : [Wav, "imports/x/normalized.json"]) : [],
            ImportedSong: status == "completed" && kind == "song.import" ? new ImportedSongInfo(ImportedId, false) : null);

        public Task<CoreIdentity> GetIdentityAsync(CancellationToken cancellationToken = default) => Task.FromResult(new CoreIdentity("open-karaoke-core", 1, InstanceId));
        public Task<JsonElement> GetModelsAsync(CancellationToken cancellationToken = default) => Task.FromResult(JsonSerializer.SerializeToElement(new { models = Array.Empty<object>() }));
        public Task<ModelPackageItem[]> GetModelPackagesAsync(CancellationToken cancellationToken = default) => Task.FromResult<ModelPackageItem[]>([]);
        public Task<JobSnapshot> GetJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<SongPage> ListSongsAsync(Guid? after = null, CancellationToken cancellationToken = default)
        {
            ListCalls++;
            return Task.FromResult(new SongPage([.. Songs], null));
        }
        public Task<SongBundle> GetSongAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    }
}
