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
