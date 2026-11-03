using OpenKaraoke.Client;
using static OpenKaraoke.Client.Tests.Program;

namespace OpenKaraoke.Client.Tests;

/// {
///   責務: [ModelPackageTests: モデルパッケージ一覧の取得と画面表示を検証する]
///   フィールド: []
/// }
internal static class ModelPackageTests
{
    /// {
    ///   責務: [ClientRoute: GET model-packagesの経路・読み取り・不正な応答の拒否を確かめる]
    ///   処理: [1: 利用可と不正の2件を読む, 2: 空のIDと上限超過を拒否する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task ClientRoute()
    {
        var paths = new List<string>();
        using var client = SongTests.TestClient((request, _) =>
        {
            paths.Add($"{request.Method} {request.RequestUri!.PathAndQuery}");
            return Task.FromResult(SongTests.Raw("""
                {"packages":[
                  {"id":"demo","status":"available","name":"Demo","version":"1","role":"mock","backend":"mock-v1","origin":"user_supplied"},
                  {"id":"broken","status":"invalid","error":"manifest.json is not valid JSON"}]}
                """));
        });
        var packages = await client.GetModelPackagesAsync();
        Check(paths.SequenceEqual(["GET /model-packages"]), "Unexpected model package route.");
        Check(packages.Length == 2 && packages[0].Name == "Demo" && packages[1].Error is not null, "Packages were not parsed.");

        using var noId = SongTests.TestClient((_, _) => Task.FromResult(SongTests.Raw("""{"packages":[{"id":"","status":"available"}]}""")));
        await Throws<InvalidDataException>(() => noId.GetModelPackagesAsync());
        using var missing = SongTests.TestClient((_, _) => Task.FromResult(SongTests.Raw("{}")));
        await Throws<InvalidDataException>(() => missing.GetModelPackagesAsync());
    }

    /// {
    ///   責務: [Summary: 接続時の表示文と、取得失敗でも接続が成功することを確かめる]
    ///   処理: [1: 0件・利用可と不正の混在・取得失敗の3通りで接続する]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task Summary()
    {
        await Connect(Program.NewFake([]), text => Check(text == "追加のモデルパッケージはありません", "Empty summary: " + text));
        ModelPackageItem[] mixed =
        [
            new("demo", "available", "Demo", "2", "mock", null),
            new("noversion", "available", "NoVersion", null, "mock", null),
            new("multi", "invalid", null, null, null, "line1\nline2"),
            new("broken", "invalid", null, null, null, "manifest.json is not valid JSON"),
        ];
        await Connect(Program.NewFake(mixed), text =>
            Check(text.StartsWith("4件(利用可 2件)") && text.Contains("Demo 2") && text.Contains("\nNoVersion\n") &&
                text.Contains("broken: 利用できません(invalid)") && text.Contains("line1 line2") && text.Split('\n').Length == 5, "Mixed summary: " + text));
        await Connect(Program.NewFake(null), text => Check(text == "取得できません", "Failure summary: " + text));
    }

    /// {
    ///   責務: [SelectionClient: GET/POST model-selectionの経路・本文・不正な応答とエラー状態を確かめる]
    ///   処理: [1: 取得と保存・解除の経路と本文を確認する, 2: 不正な引数と応答を拒否する, 3: 409がCoreApiExceptionになる]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task SelectionClient()
    {
        var calls = new List<string>();
        using var client = SongTests.TestClient(async (request, _) =>
        {
            var body = request.Content is null ? "" : await request.Content.ReadAsStringAsync();
            calls.Add($"{request.Method} {request.RequestUri!.PathAndQuery} {body}");
            return SongTests.Raw("""{"selection":{"mock":"demo"}}""");
        });
        var read = await client.GetModelSelectionAsync();
        var saved = await client.SetModelSelectionAsync("mock", "demo");
        await client.SetModelSelectionAsync("mock", null);
        Check(read["mock"] == "demo" && saved["mock"] == "demo", "Selection was not parsed.");
        Check(calls.SequenceEqual([
            "GET /model-selection ",
            """POST /model-selection {"role":"mock","model_id":"demo"}""",
            """POST /model-selection {"role":"mock"}""",
        ]), "Unexpected selection requests: " + string.Join("|", calls));
        await Throws<ArgumentException>(() => client.SetModelSelectionAsync("", "demo"));
        await Throws<ArgumentException>(() => client.SetModelSelectionAsync("mock", ""));
        await Throws<ArgumentException>(() => client.SetModelSelectionAsync("mock", new string('a', 129)));

        using var missing = SongTests.TestClient((_, _) => Task.FromResult(SongTests.Raw("{}")));
        await Throws<InvalidDataException>(() => missing.GetModelSelectionAsync());
        using var empty = SongTests.TestClient((_, _) => Task.FromResult(SongTests.Raw("""{"selection":{"mock":""}}""")));
        await Throws<InvalidDataException>(() => empty.GetModelSelectionAsync());
        using var denied = SongTests.TestClient((_, _) => Task.FromResult(new HttpResponseMessage(System.Net.HttpStatusCode.Conflict)));
        var error = await Throws<CoreApiException>(() => denied.SetModelSelectionAsync("mock", "ghost"));
        Check(error.StatusCode == System.Net.HttpStatusCode.Conflict, "Conflict status was lost.");
    }

    /// {
    ///   責務: [SelectionViewModel: 候補の作成・保存・解除・再読込・失敗・切断時の消去を確かめる]
    ///   処理: [
    ///     1: 接続時に組み込みと利用可パッケージだけが役割別の候補になる,
    ///     2: 選んで保存し、同じ選択は保存できない、解除もできる,
    ///     3: 失敗はエラー表示だけで接続を保つ,
    ///     4: 再読込で消えたパッケージの候補が外れる,
    ///     5: 切断で候補と選択が消える
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task SelectionViewModel()
    {
        var fake = Program.NewSelectionFake(
        [
            new("demo", "available", "Demo", "1", "mock", null),
            new("broken", "invalid", null, null, null, "bad"),
            new("lyr", "available", "Lyr", "1", "lyrics", null),
            new("wrong", "unsupported_backend", "Wrong", "1", "stems", "backend is not an installed adapter"),
        ]);
        fake.Stored["mock"] = "demo";
        await using var model = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(fake));
        Check(!model.CanRefreshModelPackages && model.ModelRoles.Count == 0, "Controls must be off before connecting.");
        await model.ConnectAsync();
        Check(model.ModelRoles.SequenceEqual(["lyrics", "mock", "stems"]), "Roles: " + string.Join(",", model.ModelRoles));
        Check(model.SelectedModelRole == "lyrics" && model.ModelChoices.Select(c => c.Id).SequenceEqual([null, "lyr"]), "Lyrics choices.");
        Check(!model.CanApplyModelChoice, "Nothing to apply while the saved choice is shown.");
        model.SelectedModelRole = "mock";
        Check(model.ModelChoices.Select(c => c.Id).SequenceEqual([null, "mock-v1", "demo"]) && model.SelectedModelChoice?.Id == "demo", "Mock choices.");
        Check(model.ModelSelectionSummary == "保存済み: mock=demo", "Summary: " + model.ModelSelectionSummary);

        model.SelectedModelChoice = model.ModelChoices.First(c => c.Id == "mock-v1");
        Check(model.CanApplyModelChoice, "Changed choice must be savable.");
        await model.ApplyModelChoiceAsync();
        Check(fake.Stored["mock"] == "mock-v1" && !model.CanApplyModelChoice && !model.HasError, "Choice was not saved.");
        await model.ApplyModelChoiceAsync();
        Check(fake.SetCalls == 1, "An unchanged choice must not be sent again.");

        model.SelectedModelChoice = model.ModelChoices.First(c => c.Id is null);
        await model.ApplyModelChoiceAsync();
        Check(!fake.Stored.ContainsKey("mock") && model.ModelSelectionSummary == "保存済みの選択はありません", "Choice was not cleared.");

        model.SelectedModelChoice = model.ModelChoices.First(c => c.Id == "demo");
        fake.FailSet = true;
        await model.ApplyModelChoiceAsync();
        Check(model.HasError && model.IsConnected && model.CanRefreshModelPackages, "A rejected choice must only show an error.");

        fake.FailSet = false;
        fake.Packages = [fake.Packages[2]];
        fake.Stored["mock"] = "demo";
        await model.RefreshModelPackagesAsync();
        Check(!model.HasError && model.ModelRoles.SequenceEqual(["lyrics", "mock", "stems"]) && model.ModelChoices.All(c => c.Id != "demo"), "Removed package stayed selectable.");
        Check(model.ModelPackages.StartsWith("1件"), "Package summary was not refreshed: " + model.ModelPackages);

        fake.FailGet = true;
        await model.RefreshModelPackagesAsync();
        Check(model.HasError && model.IsConnected, "A failed refresh must only show an error.");

        await model.DisconnectAsync();
        Check(model.ModelRoles.Count == 0 && model.ModelChoices.Count == 0 && model.SelectedModelRole is null && !model.CanApplyModelChoice, "Selection survived disconnect.");

        var offline = Program.NewSelectionFake([]);
        offline.FailGet = true;
        await using var other = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(offline));
        await other.ConnectAsync();
        Check(other.IsConnected && !other.HasError && other.ModelSelectionSummary == "保存済みの選択はありません", "Selection failure must not break connect.");
    }

    private static async Task Connect(ICoreConnection fake, Action<string> check)
    {
        await using var model = new ShellViewModel((_, _, _, _) => Task.FromResult(fake));
        await model.ConnectAsync();
        Check(model.IsConnected && !model.HasError, "Package failure must not break the connection.");
        check(model.ModelPackages);
        await model.DisconnectAsync();
        Check(model.ModelPackages == "未取得", "Summary was not reset on disconnect.");
    }
}
