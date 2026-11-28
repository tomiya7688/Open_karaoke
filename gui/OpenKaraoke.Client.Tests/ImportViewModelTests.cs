using OpenKaraoke.Client;
using static OpenKaraoke.Client.Tests.Program;

namespace OpenKaraoke.Client.Tests;

/// {
///   責務: [ImportViewModelTests: 取込画面の状態(入力検証・進捗・結果・取消・切断)と、完了後の曲一覧更新を検証する]
///   フィールド: []
/// }
internal static class ImportViewModelTests
{
    private static readonly string Source = Path.Combine(Path.GetTempPath(), "My Song.mp3");

    /// {
    ///   責務: [Validation: 開始できる条件と、曲名の既定値(ファイル名)を確かめる]
    ///   処理: [1: 未接続・相対パス・空の曲名では開始できない, 2: 絶対パスで曲名が既定になる, 3: 曲名を手で直すと上書きされない]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static void Validation()
    {
        var model = new SongImportViewModel();
        model.SourcePath = Source;
        Check(!model.CanStart, "An unattached import could start.");
        model.Attach(new IngestTests.IngestApi(), CancellationToken.None);
        Check(model.CanStart && model.Title == "My Song", "A rooted path did not give a default title: " + model.Title);
        model.SourcePath = "relative.mp3";
        Check(!model.CanStart, "A relative path could start.");
        model.SourcePath = Source;
        model.Title = "   ";
        Check(!model.CanStart, "A blank title could start.");
        model.Title = "自分の題名";
        model.SourcePath = Path.Combine(Path.GetTempPath(), "other.wav");
        Check(model.Title == "自分の題名", "A typed title was overwritten.");
    }

    /// {
    ///   責務: [SuccessAndFailure: 完了で曲IDが通知され、失敗では段と理由が表示されて通知されないことを確かめる]
    ///   処理: [1: 成功させて状態と通知を確認する, 2: 解析で失敗させて文言と通知なしを確認する, 3: 制御文字が表示に残らない]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task SuccessAndFailure()
    {
        var api = new IngestTests.IngestApi();
        var model = new SongImportViewModel();
        var imported = new List<Guid>();
        model.Imported += imported.Add;
        model.Attach(api, CancellationToken.None);
        model.SourcePath = Source;
        await model.StartAsync();
        Check(!model.IsBusy && model.Status == "曲を登録しました" && model.Progress == 100, $"Unexpected success state: {model.Status}");
        Check(imported.SequenceEqual([api.ImportedId]), "The imported song id was not announced.");

        var failing = new IngestTests.IngestApi { FailKind = "analysis.song" };
        var other = new SongImportViewModel();
        var announced = false;
        other.Imported += _ => announced = true;
        other.Attach(failing, CancellationToken.None);
        other.SourcePath = Source;
        await other.StartAsync();
        Check(!other.IsBusy && other.Status == "失敗しました(曲を解析中): boom: analysis.song" && !announced, "Failure was shown wrongly: " + other.Status);
    }

    /// {
    ///   責務: [CancelAndDetach: 取消が現在の段だけを止めて表示され、切断後の結果は捨てられることを確かめる]
    ///   処理: [1: 解析で止めて取消し、文言とJob取消を確認する, 2: 別の実行を切断し、通知と状態更新が起きないことを確認する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task CancelAndDetach()
    {
        var api = new IngestTests.IngestApi { HoldKind = "analysis.song" };
        var model = new SongImportViewModel();
        model.Attach(api, CancellationToken.None);
        model.SourcePath = Source;
        var run = model.StartAsync();
        await api.Entered.Task.WaitAsync(TimeSpan.FromSeconds(10));
        Check(model.IsBusy && model.CanCancel, "A running import could not be cancelled.");
        model.Cancel();
        await run;
        Check(!model.IsBusy && model.Status.StartsWith("取り消しました") && api.Cancelled.Count == 1, "Cancel was shown wrongly: " + model.Status);

        var held = new IngestTests.IngestApi { HoldKind = "analysis.song" };
        var detached = new SongImportViewModel();
        var announced = false;
        detached.Imported += _ => announced = true;
        detached.Attach(held, CancellationToken.None);
        detached.SourcePath = Source;
        var late = detached.StartAsync();
        await held.Entered.Task.WaitAsync(TimeSpan.FromSeconds(10));
        detached.Detach();
        await late;
        Check(!announced && !detached.IsBusy && detached.Status.Contains("接続"), "A detached import still reported: " + detached.Status);
    }

    /// {
    ///   責務: [ShellWiring: 接続でAttach・切断でDetachされ、完了後に曲一覧が読み直されて曲が選ばれることを確かめる]
    ///   処理: [1: 接続して取込を実行する, 2: 一覧の再取得と選択を確認する, 3: 切断で状態が戻ることを確認する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task ShellWiring()
    {
        var api = new IngestTests.IngestApi();
        var shell = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(new Connection(api)));
        Check(!shell.Import.CanStart, "Import was available before connecting.");
        await shell.ConnectAsync();
        api.Songs.Add(new SongListItem(api.ImportedId, "My Song", null, 48000, null));
        shell.Import.SourcePath = Source;
        var before = api.ListCalls;
        await shell.Import.StartAsync();
        for (var i = 0; i < 200 && shell.SelectedSong is null; i++) await Task.Delay(25);
        Check(api.ListCalls > before && shell.SelectedSong?.SongId == api.ImportedId, "The imported song was not listed and selected.");
        await shell.DisconnectAsync();
        Check(!shell.Import.CanStart && !shell.Import.IsBusy, "Disconnect did not reset the import.");
        await shell.DisposeAsync();
    }

    /// {
    ///   責務: [Connection: FakeのCore APIを持つ接続]
    ///   フィールド: [api: 共有するFake]
    /// }
    private sealed class Connection(IngestTests.IngestApi api) : ICoreConnection
    {
        public ICoreApi Api => api;
        public Guid InstanceId => api.InstanceId;
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }
}
