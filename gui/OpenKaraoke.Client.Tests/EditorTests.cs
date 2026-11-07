using System.Net;
using System.Text.Json;
using OpenKaraoke.Client;
using static OpenKaraoke.Client.Tests.Program;

namespace OpenKaraoke.Client.Tests;

/// {
///   責務: [EditorTests: 歌詞・ノートの手編集(#38 38.4)のクライアント経路・Undo/Redo・失敗表示を検証する]
///   フィールド: []
/// }
internal static class EditorTests
{
    private const ulong Second = SongTimeline.SampleRate;

    /// {
    ///   責務: [Bundle: evidenceの世代付きのテスト曲を作る]
    ///   処理: [1: SongTestsの曲にevidenceの世代と再生成待ちの状態を付ける]
    ///   引数: [generation: evidenceの世代]
    ///   戻り値: [曲データ]
    /// }
    internal static SongBundle Bundle(ulong generation = 3)
    {
        var fixture = SongTests.Fixture();
        return fixture with
        {
            Song = fixture.Song with
            {
                Artifacts = new()
                {
                    ["lyrics"] = new ArtifactInfo(2, true),
                    ["notes"] = new ArtifactInfo(2, true),
                    ["evidence"] = new ArtifactInfo(generation, true),
                },
            },
        };
    }

    /// {
    ///   責務: [ClientRoute: PATCH /songs/{id} の経路・本文・状態コードの扱いを確かめる]
    ///   処理: [
    ///     1: 本文がbase_generationと編集op(snake_case・nullなし)であることを確認する,
    ///     2: 409/404/413/422/429がCoreApiExceptionで返り、自動再試行(2回目の要求)がないことを確認する,
    ///     3: 別の曲の応答を拒否し、GETのevidence世代を読めることを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task ClientRoute()
    {
        var id = Guid.NewGuid();
        var requests = new List<(HttpMethod Method, string Path, string Body)>();
        var status = HttpStatusCode.OK;
        var answer = id;
        using var client = SongTests.TestClient(async (request, _) =>
        {
            requests.Add((request.Method, request.RequestUri!.AbsolutePath, await request.Content!.ReadAsStringAsync()));
            return status == HttpStatusCode.OK
                ? SongTests.Raw($$"""{"song_id":"{{answer}}","generation":4,"pending":["notes"]}""")
                : new HttpResponseMessage(status);
        });
        var request = new EditSongRequest(3,
        [
            new SongEditOp("update_lyric", "a", "新", 0, 48_000),
            new SongEditOp("delete_note", "n1"),
        ]);
        var result = await client.EditSongAsync(id, request);
        Check(result.Generation == 4 && result.Pending.SequenceEqual(["notes"]), "Edit result was not parsed.");
        Check(requests[0].Method == HttpMethod.Patch && requests[0].Path == $"/songs/{id:D}", "Unexpected edit route.");
        using var body = JsonDocument.Parse(requests[0].Body);
        var ops = body.RootElement.GetProperty("ops");
        Check(body.RootElement.GetProperty("base_generation").GetUInt64() == 3 && body.RootElement.EnumerateObject().Count() == 2,
            "Body must be only base_generation and ops.");
        Check(ops[0].GetProperty("op").GetString() == "update_lyric" && ops[0].GetProperty("start_sample").GetUInt64() == 0 &&
            ops[0].GetProperty("end_sample").GetUInt64() == 48_000, "Lyric op was not serialized in snake_case.");
        Check(ops[1].EnumerateObject().Select(p => p.Name).SequenceEqual(["op", "id"]), "Null fields must not be sent.");

        foreach (var code in new[]
        {
            HttpStatusCode.Conflict, HttpStatusCode.NotFound, HttpStatusCode.RequestEntityTooLarge,
            HttpStatusCode.UnprocessableEntity, HttpStatusCode.TooManyRequests,
        })
        {
            status = code;
            requests.Clear();
            var error = await Throws<CoreApiException>(() => client.EditSongAsync(id, request));
            Check(error.StatusCode == code && requests.Count == 1, $"{code} must be reported once without retry.");
        }
        status = HttpStatusCode.OK;
        answer = Guid.NewGuid();
        await Throws<InvalidDataException>(() => client.EditSongAsync(id, request));
        await Throws<ArgumentException>(() => client.EditSongAsync(id, new EditSongRequest(1, [])));
        await Throws<ArgumentException>(() => client.EditSongAsync(Guid.Empty, request));

        Check(Bundle(7).EvidenceGeneration == 7 && SongTests.Fixture().EvidenceGeneration is null, "Evidence generation was not read.");
    }

    /// {
    ///   責務: [UndoRedo: 編集のUndo/Redo往復・変更なしの扱い・不正入力の拒否を確かめる]
    ///   処理: [
    ///     1: 歌詞更新・ノート追加・ノート削除を行い、3回のUndoで保存済みの状態に戻ることを確認する,
    ///     2: 3回のRedoで編集後に戻り、新しい編集でRedoが消えることを確認する,
    ///     3: 範囲・音高・空本文・参照中の歌詞の削除が状態を変えず理由を示すことを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static Task UndoRedo()
    {
        var editor = new SongEditorViewModel();
        editor.Load(new Recorder(), Guid.NewGuid(), Bundle());
        Check(editor.State == SongEditState.Clean && editor.Generation == 3 && !editor.CanUndo && !editor.CanSave, "Initial state was wrong.");
        Check(editor.LyricRows.Select(r => r.Id).SequenceEqual(["a", "b", "c"]), "Rows must be in time order.");

        Check(editor.UpdateLyric("a", "新しい一行目", 1 * Second, 5 * Second), "Lyric update failed.");
        var noteId = editor.AddNote(8 * Second, 9 * Second, 62, "c");
        Check(noteId is not null && editor.DeleteNote("n2"), "Note edits failed.");
        Check(editor.State == SongEditState.Dirty && editor.CanSave && editor.IsDirty && editor.NoteRows.Count == 3, "Edits were not applied.");
        Check(editor.LyricRows.Single(r => r.Id == "a").Text == "新しい一行目" && editor.LyricRows.Single(r => r.Id == "a").IsEdited,
            "Edited lyric was not shown.");

        Check(editor.Undo() && editor.Undo() && editor.Undo() && !editor.Undo(), "Undo count was wrong.");
        Check(editor.State == SongEditState.Clean && !editor.IsDirty && !editor.CanSave && editor.CanRedo, "Undo did not return to the saved state.");
        Check(editor.LyricRows.Single(r => r.Id == "a").Text == "一行目" && editor.NoteRows.Count == 3 &&
            editor.NoteRows.Any(r => r.Id == "n2"), "Undo did not restore the original content.");
        Check(editor.Redo() && editor.Redo() && editor.Redo() && !editor.Redo(), "Redo count was wrong.");
        Check(editor.State == SongEditState.Dirty && editor.NoteRows.All(r => r.Id != "n2") && editor.NoteRows.Any(r => r.Id == noteId),
            "Redo did not restore the edits.");

        editor.Undo();
        Check(editor.UpdateNote("n1", 0, 2 * Second, 61) && !editor.CanRedo, "A new edit must discard the redo history.");
        // 同じ値の更新は履歴を増やさない。
        var depth = 0;
        while (editor.Undo()) depth++;
        Check(depth == 3, $"Expected 3 undo steps, got {depth}.");
        editor.Redo();
        editor.Redo();
        editor.Redo();
        Check(!editor.UpdateNote("n1", 0, 2 * Second, 61), "A no-op update must not be recorded.");
        editor.RevertToSaved();
        Check(editor.State == SongEditState.Clean && !editor.CanUndo && !editor.CanRedo, "Revert did not clear the history.");

        var before = editor.NoteRows.Count;
        Check(!editor.UpdateNote("n1", 2 * Second, 2 * Second, 60) && editor.Notice.Contains("開始"), "An empty range was accepted.");
        Check(!editor.UpdateNote("n1", 0, 11 * Second, 60) && editor.Notice.Contains("曲の長さ"), "A range beyond the song was accepted.");
        Check(!editor.UpdateNote("n1", 0, Second, 128) && editor.Notice.Contains("0〜127"), "An out-of-range pitch was accepted.");
        Check(!editor.UpdateLyric("a", "  ", Second, 2 * Second) && editor.Notice.Contains("空"), "A blank lyric was accepted.");
        Check(!editor.UpdateLyric("zzz", "x", 0, Second) && !editor.DeleteNote("zzz"), "An unknown ID was accepted.");
        Check(!editor.DeleteLyric("a") && editor.Notice.Contains("参照"), "A referenced lyric was deleted.");
        Check(editor.AddNote(0, Second, 60, "missing") is null && editor.NoteRows.Count == before && !editor.IsDirty,
            "A rejected edit changed the state.");
        return Task.CompletedTask;
    }

    /// {
    ///   責務: [SaveOps: 差分のop変換・順序・成功後の世代更新・変更なしの扱いを確かめる]
    ///   処理: [
    ///     1: 追加→更新→ノート削除→歌詞削除の順でopになり、base_generationが読込時の世代であることを確認する,
    ///     2: 成功で世代・再生成待ち・保存済み状態が更新されSavedが通知されることを確認する,
    ///     3: 次の保存は新しい世代を使い、変更がなければ要求しないことを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task SaveOps()
    {
        var api = new Recorder();
        var editor = new SongEditorViewModel();
        editor.Load(api, Guid.NewGuid(), Bundle());
        var saved = 0;
        editor.Saved += bundle => { saved++; Check(bundle.Lyrics.Segments.Any(s => s.Text == "追加"), "Saved bundle lacked the edit."); };
        var lyricId = editor.AddLyric("追加", 8 * Second, 9 * Second);
        editor.UpdateNote("n1", 0, 2 * Second, 61);
        editor.AddNote(8 * Second, 9 * Second, 70, lyricId);
        editor.DeleteNote("n3");
        editor.DeleteLyric("c");
        await editor.SaveAsync();
        var sent = api.Requests.Single();
        Check(sent.BaseGeneration == 3, "The load-time generation was not sent.");
        Check(sent.Ops.Select(o => o.Op).SequenceEqual(["add_lyric", "add_note", "update_note", "delete_note", "delete_lyric"]),
            "Ops were not ordered adds, updates, note deletes, lyric deletes.");
        Check(sent.Ops[1].LyricSegmentId == lyricId && sent.Ops[2].MidiNote == 61 && sent.Ops[3].Id == "n3" && sent.Ops[4].Id == "c",
            "Op contents were wrong.");
        Check(editor.State == SongEditState.Clean && editor.Generation == 4 && editor.PendingArtifacts.SequenceEqual(["notes"]) &&
            !editor.IsDirty && saved == 1 && editor.StatusText.Contains("再生成待ち: notes"), "Save success was not reflected.");

        await editor.SaveAsync();
        Check(api.Requests.Count == 1, "A save without changes must not call Core.");
        editor.UpdateLyric(lyricId!, "二回目", 8 * Second, 9 * Second);
        await editor.SaveAsync();
        Check(api.Requests.Count == 2 && api.Requests[1].BaseGeneration == 4 &&
            api.Requests[1].Ops.Single().Op == "update_lyric", "The next save did not use the new generation.");
        // 保存後もUndoでき、取り消した結果は新しい未保存の変更になる。
        Check(editor.Undo() && editor.State == SongEditState.Dirty, "Undo after a save must produce an unsaved change.");
    }

    /// {
    ///   責務: [Failures: 409/404/413/422/429/通信失敗の表示と編集内容の扱いを確かめる]
    ///   処理: [
    ///     1: 409は自動再試行せず競合を示し、未保存の変更を残して保存と編集を止めることを確認する,
    ///     2: 422と413は基準へ戻し、拒否された編集をRedoで取り戻せることを確認する,
    ///     3: 429と通信失敗は変更を残し、手動の再保存が成功することを確認する,
    ///     4: 404は曲なしを示す,保存中に曲が替わったら結果を捨てることを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task Failures()
    {
        var api = new Recorder();
        var editor = new SongEditorViewModel();
        editor.Load(api, Guid.NewGuid(), Bundle());

        // 409: 競合。1回だけ送り、変更は残し、再読込まで保存も編集もできない。
        editor.UpdateLyric("a", "競合", Second, 5 * Second);
        api.Next = HttpStatusCode.Conflict;
        await editor.SaveAsync();
        Check(api.Requests.Count == 1 && editor.State == SongEditState.Conflict && editor.NeedsReload && !editor.CanSave &&
            editor.Notice.Contains("自動では再試行しません") && editor.LyricRows.Any(r => r.Text == "競合"), "A 409 was not shown as a conflict.");
        Check(!editor.UpdateLyric("b", "x", 3 * Second, 4 * Second) && editor.Notice.Contains("読み直"), "Editing continued during a conflict.");
        await editor.SaveAsync();
        Check(api.Requests.Count == 1, "A conflict must not be retried.");
        editor.Load(api, Guid.NewGuid(), Bundle(9));
        Check(editor.State == SongEditState.Clean && editor.Generation == 9 && !editor.NeedsReload, "Reload did not recover from a conflict.");

        // 422: 拒否。保存済みの状態へ戻し、Redoで拒否された編集を取り戻せる。
        editor.UpdateLyric("a", "拒否される", Second, 5 * Second);
        api.Next = HttpStatusCode.UnprocessableEntity;
        await editor.SaveAsync();
        Check(editor.State == SongEditState.Clean && !editor.IsDirty && editor.LyricRows.Single(r => r.Id == "a").Text == "一行目" &&
            editor.Notice.Contains("拒否") && editor.CanRedo && !editor.CanUndo, "A 422 did not roll the screen back.");
        Check(editor.Redo() && editor.State == SongEditState.Dirty && editor.LyricRows.Single(r => r.Id == "a").Text == "拒否される",
            "The rejected edit could not be restored.");

        // 413: 同様に戻す。
        api.Next = HttpStatusCode.RequestEntityTooLarge;
        await editor.SaveAsync();
        Check(editor.State == SongEditState.Clean && editor.Notice.Contains("大きすぎ") && editor.CanRedo, "A 413 did not roll back.");
        editor.Redo();

        // 429: 変更を残し、手動の再保存が通る。
        api.Next = HttpStatusCode.TooManyRequests;
        await editor.SaveAsync();
        Check(editor.State == SongEditState.Dirty && editor.CanSave && editor.Notice.Contains("混み合") && api.Requests.Count == 4,
            "A 429 must keep the edit and allow a manual retry.");
        await editor.SaveAsync();
        Check(editor.State == SongEditState.Clean && api.Requests.Count == 5 && api.Requests[^1].BaseGeneration == 9, "Manual retry failed.");

        // 通信失敗(例外)も未保存のまま残す。
        editor.UpdateLyric("b", "通信失敗", 3 * Second, 4 * Second);
        api.Failure = new HttpRequestException("down");
        await editor.SaveAsync();
        Check(editor.State == SongEditState.Dirty && editor.Notice.Contains("down"), "A network failure lost the edit.");
        api.Failure = null;

        // 404: 曲なし。
        api.Next = HttpStatusCode.NotFound;
        await editor.SaveAsync();
        Check(editor.State == SongEditState.Missing && editor.NeedsReload && editor.Notice.Contains("見つかりません"), "A 404 was not shown.");

        // 保存中に別の曲を読み込んだら、遅れて届いた結果で新しい曲の状態を壊さない。
        var slow = new Recorder { Gate = new TaskCompletionSource() };
        var second = new SongEditorViewModel();
        second.Load(slow, Guid.NewGuid(), Bundle(5));
        second.UpdateLyric("a", "遅い", Second, 5 * Second);
        var pending = second.SaveAsync();
        Check(second.State == SongEditState.Saving && !second.UpdateLyric("b", "x", 3 * Second, 4 * Second) && !second.CanSave,
            "Editing or saving was allowed while saving.");
        second.Load(slow, Guid.NewGuid(), Bundle(20));
        slow.Gate.SetResult();
        await pending;
        Check(second.Generation == 20 && second.State == SongEditState.Clean && !second.IsDirty, "A late save result overwrote the new song.");

        var unsupported = new SongEditorViewModel();
        unsupported.Load(api, Guid.NewGuid(), SongTests.Fixture());
        Check(unsupported.State == SongEditState.Unsupported && !unsupported.CanEdit, "A song without a generation was editable.");
    }

    /// {
    ///   責務: [ShellFlow: 曲の表示・保存・表示への反映・切断時の消去を確かめる]
    ///   処理: [
    ///     1: 曲を表示すると編集対象になることを確認する,
    ///     2: 保存すると歌詞の表示が更新され、表示位置が保たれることを確認する,
    ///     3: 保存の競合では表示を変えず、切断で編集状態が消えることを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task ShellFlow()
    {
        var item = new SongListItem(Guid.NewGuid(), "曲", null, 10 * Second, null);
        var fake = new EditConnection { Song = Bundle(), Item = item };
        await using var model = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(fake));
        await model.ConnectAsync();
        await model.RefreshSongsAsync();
        model.SelectedSong = item;
        await model.LoadSelectedSongAsync();
        Check(model.Editor.State == SongEditState.Clean && model.Editor.SongId == item.SongId && model.Editor.LyricRows.Count == 3,
            "The shown song was not offered for editing.");

        model.Karaoke.Seek(2 * Second);
        model.Editor.UpdateLyric("a", "編集後", Second, 5 * Second);
        await model.SaveEditsAsync();
        Check(fake.Requests.Single().BaseGeneration == 3 && model.Editor.Generation == 4, "Save did not go through Core.");
        Check(model.Karaoke.CurrentLyric == "編集後" && model.Karaoke.PositionSamples == 2 * Second, "The display did not follow the saved edit.");

        model.Editor.UpdateLyric("a", "競合", Second, 5 * Second);
        fake.Next = HttpStatusCode.Conflict;
        await model.SaveEditsAsync();
        Check(model.Editor.State == SongEditState.Conflict && model.Karaoke.CurrentLyric == "編集後" && model.Error.Length == 0,
            "A conflict must not change the display or disconnect Core.");
        await model.DisconnectAsync();
        Check(model.Editor.State == SongEditState.NoSong && model.Editor.LyricRows.Count == 0, "Disconnect left the edit state.");
    }

    /// {
    ///   責務: [Recorder: 編集の要求を記録し、指定した失敗を1回だけ返すCore APIのFake]
    ///   フィールド: [Requests: 受け取った要求, Next: 次に返す失敗の状態, Failure: 次に投げる例外, Gate: 完了を保留する合図]
    /// }
    private sealed class Recorder : ICoreApi
    {
        private ulong _generation = 3;
        public List<EditSongRequest> Requests { get; } = [];
        public HttpStatusCode? Next { get; set; }
        public Exception? Failure { get; set; }
        public TaskCompletionSource? Gate { get; init; }

        public async Task<EditedSongResult> EditSongAsync(Guid id, EditSongRequest request, CancellationToken cancellationToken = default)
        {
            Requests.Add(request);
            if (Gate is not null) await Gate.Task;
            if (Failure is not null) throw Failure;
            if (Next is { } status) { Next = null; throw new CoreApiException(status); }
            _generation = request.BaseGeneration + 1;
            return new EditedSongResult(id, _generation, ["notes"]);
        }

        public Task<CoreIdentity> GetIdentityAsync(CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<JsonElement> GetModelsAsync(CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<ModelPackageItem[]> GetModelPackagesAsync(CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<JobSnapshot> CreateJobAsync(CreateJobRequest job, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<JobSnapshot> GetJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<JobSnapshot> CancelJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public IAsyncEnumerable<JobSnapshot> WatchJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<SongPage> ListSongsAsync(Guid? after = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
        public Task<SongBundle> GetSongAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    }

    /// {
    ///   責務: [EditConnection: 曲の一覧・取得・編集だけを備えたCore接続のFake]
    ///   フィールド: [Song: 返す曲, Item: 一覧の1件, Next: 次の編集で返す失敗の状態, Requests: 受け取った編集]
    /// }
    private sealed class EditConnection : ICoreConnection
    {
        private readonly Recorder _recorder = new();
        private readonly EditApi _api;
        public EditConnection() => _api = new EditApi(this);
        public Guid InstanceId { get; } = Guid.NewGuid();
        public ICoreApi Api => _api;
        public SongBundle Song { get; init; } = SongTests.Fixture();
        public SongListItem Item { get; init; } = new(Guid.NewGuid(), "曲", null, null, null);
        public HttpStatusCode? Next { get => _recorder.Next; set => _recorder.Next = value; }
        public List<EditSongRequest> Requests => _recorder.Requests;
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;

        private sealed class EditApi(EditConnection owner) : ICoreApi
        {
            public Task<CoreIdentity> GetIdentityAsync(CancellationToken cancellationToken = default) =>
                Task.FromResult(new CoreIdentity("open-karaoke-core", 1, owner.InstanceId));
            public Task<JsonElement> GetModelsAsync(CancellationToken cancellationToken = default) =>
                Task.FromResult(JsonSerializer.SerializeToElement(new { models = Array.Empty<object>() }));
            public Task<ModelPackageItem[]> GetModelPackagesAsync(CancellationToken cancellationToken = default) => Task.FromResult(Array.Empty<ModelPackageItem>());
            public Task<JobSnapshot> CreateJobAsync(CreateJobRequest job, CancellationToken cancellationToken = default) => throw new NotSupportedException();
            public Task<JobSnapshot> GetJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
            public Task<JobSnapshot> CancelJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
            public IAsyncEnumerable<JobSnapshot> WatchJobAsync(Guid id, CancellationToken cancellationToken = default) => throw new NotSupportedException();
            public Task<SongPage> ListSongsAsync(Guid? after = null, CancellationToken cancellationToken = default) =>
                Task.FromResult(new SongPage([owner.Item], null));
            public Task<SongBundle> GetSongAsync(Guid id, CancellationToken cancellationToken = default) => Task.FromResult(owner.Song);
            public Task<EditedSongResult> EditSongAsync(Guid id, EditSongRequest request, CancellationToken cancellationToken = default) =>
                owner._recorder.EditSongAsync(id, request, cancellationToken);
        }
    }
}
