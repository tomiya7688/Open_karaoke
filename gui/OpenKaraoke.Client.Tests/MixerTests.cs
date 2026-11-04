using System.Net;
using System.Text.Json;
using OpenKaraoke.Client;
using static OpenKaraoke.Client.Tests.Program;

namespace OpenKaraoke.Client.Tests;

/// {
///   責務: [MixerTests: ミキサー設定の取得・送信と、画面の状態(丸め・順序・切断)を検証する]
///   フィールド: []
/// }
internal static class MixerTests
{
    /// {
    ///   責務: [ClientRoute: GET/PUT mixerの経路・本文・不正な値の拒否を確かめる]
    ///   処理: [1: 取得と送信の経路と本文を確認する, 2: 非有限値の送信と範囲外の応答を拒否する, 3: 422がCoreApiExceptionになる]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task ClientRoute()
    {
        var calls = new List<string>();
        using var client = SongTests.TestClient(async (request, _) =>
        {
            var body = request.Content is null ? "" : await request.Content.ReadAsStringAsync();
            calls.Add($"{request.Method} {request.RequestUri!.PathAndQuery} {body}");
            return SongTests.Raw(ToJson(MixerViewModel.Defaults));
        });
        var read = await client.GetMixerAsync();
        var wanted = MixerViewModel.Defaults with { EchoDelayMs = 350, MicMonitor = true };
        var saved = await client.SetMixerAsync(wanted);
        Check(read == MixerViewModel.Defaults && saved == MixerViewModel.Defaults, "Mixer settings were not parsed.");
        Check(calls.Count == 2 && calls[0] == "GET /mixer " && calls[1].StartsWith("PUT /mixer {\"music_gain\":1,") &&
            calls[1].Contains("\"echo_delay_ms\":350") && calls[1].Contains("\"mic_monitor\":true"),
            "Unexpected mixer requests: " + string.Join("|", calls));
        await Throws<ArgumentException>(() => client.SetMixerAsync(wanted with { MicGain = double.NaN }));

        using var outOfRange = SongTests.TestClient((_, _) =>
            Task.FromResult(SongTests.Raw(ToJson(MixerViewModel.Defaults with { MusicGain = 3 }))));
        await Throws<InvalidDataException>(() => outOfRange.GetMixerAsync());
        using var empty = SongTests.TestClient((_, _) => Task.FromResult(SongTests.Raw("{}")));
        await Throws<InvalidDataException>(() => empty.GetMixerAsync());
        using var denied = SongTests.TestClient((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.UnprocessableEntity)));
        var error = await Throws<CoreApiException>(() => denied.SetMixerAsync(wanted));
        Check(error.StatusCode == HttpStatusCode.UnprocessableEntity, "Mixer rejection status was lost.");
    }

    /// {
    ///   責務: [ViewModel: 読込・編集の送信・Coreの丸めの反映・失敗・切断を確かめる]
    ///   処理: [
    ///     1: 接続前は操作できず、接続後にCoreの値が出る,
    ///     2: 連続した編集は最新値だけが順に送られ、Coreが丸めた値が画面へ戻る,
    ///     3: 送信失敗は状態文に出て、次の編集で再開する,
    ///     4: 切断で既定値に戻り、以降の編集は送られない,
    ///     5: ミキサー非対応のCoreでも接続は成功する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task ViewModel()
    {
        var api = new FakeMixerApi { Stored = MixerViewModel.Defaults with { MusicGain = 0.5 } };
        var mixer = new MixerViewModel();
        mixer.MusicGain = 1.5;
        Check(!mixer.IsAvailable && api.Sent.Count == 0, "An unattached mixer sent a change.");
        mixer.Detach();
        await mixer.AttachAsync(api, CancellationToken.None);
        Check(mixer.IsAvailable && mixer.MusicGain == 0.5, "Core settings were not shown.");

        api.Hold = new TaskCompletionSource();
        mixer.MicGain = 0.3;
        await api.Entered.Task;
        mixer.MicGain = 0.4;
        mixer.EchoLevel = 0.6;
        mixer.EchoDelayMs = 5000;
        api.Hold.SetResult();
        api.Hold = null;
        await mixer.Idle;
        Check(api.Sent.Count == 2, $"Expected the first and the coalesced latest value, got {api.Sent.Count}.");
        Check(api.Sent[0].MicGain == 0.3 && api.Sent[1].MicGain == 0.4 && api.Sent[1].EchoLevel == 0.6, "Latest edits were lost.");
        Check(mixer.EchoDelayMs == 1000 && api.Sent[1].EchoDelayMs == 1000, "The delay was not limited to the Core range.");

        api.Round = settings => settings with { ReverbLevel = 0.1 };
        mixer.ReverbLevel = 0.9;
        await mixer.Idle;
        Check(mixer.ReverbLevel == 0.1, "The value rounded by Core did not come back.");
        api.Round = null;

        api.Fail = true;
        mixer.EffectsBypass = true;
        await mixer.Idle;
        Check(mixer.Status.Contains("送れません"), "A failed send was not shown: " + mixer.Status);
        api.Fail = false;
        mixer.MicMonitor = true;
        await mixer.Idle;
        Check(api.Stored.MicMonitor && api.Stored.EffectsBypass, "Sending did not resume after a failure.");

        var sentBefore = api.Sent.Count;
        mixer.Detach();
        Check(!mixer.IsAvailable && mixer.Settings == MixerViewModel.Defaults, "Detach did not reset the mixer.");
        mixer.MusicGain = 0.2;
        await mixer.Idle;
        Check(api.Sent.Count == sentBefore, "A detached mixer still sent a change.");

        await using var shell = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(new FakeMixerConnection(api)));
        await shell.ConnectAsync();
        await Task.Yield();
        Check(shell.IsConnected && shell.Mixer.IsAvailable, "The shell did not load the mixer on connect.");
        await shell.DisconnectAsync();
        Check(!shell.Mixer.IsAvailable, "The shell kept the mixer after disconnect.");

        await using var old = new ShellViewModel((_, _, _, _) => Task.FromResult(Program.NewFake([])));
        await old.ConnectAsync();
        await Task.Yield();
        Check(old.IsConnected && !old.Mixer.IsAvailable && old.Mixer.Status.Contains("読めません"),
            "A Core without a mixer broke the connection.");
    }

    private static string ToJson(MixerSettings settings) => JsonSerializer.Serialize(settings, CoreApiClient.JsonOptions);

    /// {
    ///   責務: [FakeMixerApi: ミキサーの保存・失敗・丸め・保留をテストから操作できるFake]
    ///   フィールド: [InstanceId: セッションID, Stored: 保存中の設定, Sent: 受け取った送信の記録, Hold: 完了まで応答を止める, Entered: 保留に入った合図, Round: Coreの丸めを真似る, Fail: 送信を失敗させる]
    /// }
    private sealed class FakeMixerApi : ICoreApi
    {
        public Guid InstanceId { get; } = Guid.NewGuid();
        public MixerSettings Stored { get; set; } = MixerViewModel.Defaults;
        public List<MixerSettings> Sent { get; } = [];
        public TaskCompletionSource? Hold { get; set; }
        public TaskCompletionSource Entered { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public Func<MixerSettings, MixerSettings>? Round { get; set; }
        public bool Fail { get; set; }
        public Task<MixerSettings> GetMixerAsync(CancellationToken cancellationToken = default) => Task.FromResult(Stored);
        public async Task<MixerSettings> SetMixerAsync(MixerSettings settings, CancellationToken cancellationToken = default)
        {
            if (Hold is { } hold)
            {
                Entered.TrySetResult();
                await hold.Task;
            }
            if (Fail) throw new CoreApiException(HttpStatusCode.UnprocessableEntity);
            Sent.Add(settings);
            Stored = Round?.Invoke(settings) ?? settings;
            return Stored;
        }
        public Task<CoreIdentity> GetIdentityAsync(CancellationToken cancellationToken = default) => Task.FromResult(new CoreIdentity("open-karaoke-core", 1, InstanceId));
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
    ///   責務: [FakeMixerConnection: FakeMixerApiを持つ接続]
    ///   フィールド: [api: 共有するFakeMixerApi]
    /// }
    private sealed class FakeMixerConnection(FakeMixerApi api) : ICoreConnection
    {
        public ICoreApi Api => api;
        public Guid InstanceId => api.InstanceId;
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }
}
