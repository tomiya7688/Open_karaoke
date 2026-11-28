namespace OpenKaraoke.Client;

/// {
///   責務: [SongIngestRequest: 元音源から曲を登録する連鎖の入力]
///   フィールド: [SourcePath: 利用者が選んだ元音源の絶対パス, Title: 曲名(解析に必須), ModelId: 解析に使うモデルID(省略可)]
/// }
public sealed record SongIngestRequest(string SourcePath, string Title, string? ModelId = null);

/// {
///   責務: [SongIngestProgress: 連鎖の現在の段と進捗]
///   フィールド: [Stage: normalizing/analyzing/importing, JobId: 現在の段のJob ID, Progress: その段の進捗(0〜1)]
/// }
public sealed record SongIngestProgress(string Stage, Guid JobId, double Progress);

/// {
///   責務: [SongIngestOutcome: 連鎖の結末。成功なら曲、失敗・取消なら止まった段と理由]
///   フィールド: [Status: completed/failed/cancelled, Stage: 止まった段(成功時は最後の段), Song: 登録された曲(成功時のみ), Error: Coreのerror文字列(失敗時のみ。"code: message"形式)]
/// }
public sealed record SongIngestOutcome(string Status, string Stage, ImportedSongInfo? Song, string? Error)
{
    public bool Succeeded => Status == "completed";
}

/// {
///   責務: [SongIngestFlow: audio.import → analysis.song → song.import をクライアントで連鎖させ、失敗・取消では後続段を作らない]
///   フィールド: [_api: Core API]
/// }
public sealed class SongIngestFlow(ICoreApi api)
{
    private const string AudioFile = "original_48k.wav";

    /// {
    ///   責務: [RunAsync: 元音源を正規化・解析・登録し、結末を返す]
    ///   処理: [1: 入力を検証する, 2: 段ごとにJobを作って終端まで見る, 3: 前段が完了した時だけ次段を作る, 4: 取消要求は現在の段のJobだけを取り消し、cancelledを返す]
    ///   引数: [request: 入力, progress: 進捗の通知先(省略可), cancellationToken: 取消]
    ///   戻り値: [結末。Core側の失敗は例外にせずfailedで返す]
    ///   エラー: [ArgumentException: パス・曲名が不正, InvalidDataException: Coreの応答が連鎖の契約と合わない]
    /// }
    public async Task<SongIngestOutcome> RunAsync(
        SongIngestRequest request, IProgress<SongIngestProgress>? progress = null, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(request.SourcePath) || !Path.IsPathRooted(request.SourcePath))
            throw new ArgumentException("An absolute source path is required.", nameof(request));
        if (string.IsNullOrWhiteSpace(request.Title))
            throw new ArgumentException("A title is required.", nameof(request));

        var audio = await RunStageAsync(
            "normalizing", new CreateJobRequest("audio.import", AudioImport: new { source_path = request.SourcePath }),
            progress, cancellationToken).ConfigureAwait(false);
        if (audio.Failure is { } audioFailure) return audioFailure;
        var wav = audio.Job!.Artifacts.FirstOrDefault(a => a.EndsWith("/" + AudioFile, StringComparison.Ordinal))
            ?? throw new InvalidDataException("audio.import did not publish the normalized WAV.");

        var options = new Dictionary<string, object> { ["title"] = request.Title };
        var analysis = await RunStageAsync(
            "analyzing",
            new CreateJobRequest("analysis.song", Analysis: new { model_id = request.ModelId, input_artifact = wav, options }),
            progress, cancellationToken).ConfigureAwait(false);
        if (analysis.Failure is { } analysisFailure) return analysisFailure;

        var import = await RunStageAsync(
            "importing", new CreateJobRequest("song.import", SongImport: new { analysis_job_id = analysis.Job!.Id }),
            progress, cancellationToken).ConfigureAwait(false);
        if (import.Failure is { } importFailure) return importFailure;
        var song = import.Job!.ImportedSong
            ?? throw new InvalidDataException("song.import completed without an imported song.");
        return new SongIngestOutcome("completed", "importing", song, null);
    }

    /// {
    ///   責務: [RunStageAsync: 1段のJobを作って終端まで見る。取消要求ではその段のJobだけを取り消す]
    ///   処理: [1: Jobを作る, 2: 進捗を通知しながら終端まで見る, 3: 取消要求なら現在のJobの取消をトークンなしで送る, 4: 完了以外はfailed/cancelledの結末にする]
    ///   引数: [stage: 段名, job: 作成要求, progress: 通知先, cancellationToken: 取消]
    ///   戻り値: [完了したJob、または止まった結末]
    /// }
    private async Task<(JobSnapshot? Job, SongIngestOutcome? Failure)> RunStageAsync(
        string stage, CreateJobRequest job, IProgress<SongIngestProgress>? progress, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var created = await api.CreateJobAsync(job, cancellationToken).ConfigureAwait(false);
        var last = created;
        try
        {
            await foreach (var snapshot in api.WatchJobAsync(created.Id, cancellationToken).ConfigureAwait(false))
            {
                last = snapshot;
                progress?.Report(new SongIngestProgress(stage, snapshot.Id, snapshot.Progress));
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            // 取消要求は現在の段のJobだけを止める。後続段は作らない。
            try { await api.CancelJobAsync(created.Id, CancellationToken.None).ConfigureAwait(false); }
            catch (CoreApiException) { }
            return (null, new SongIngestOutcome("cancelled", stage, null, null));
        }
        return last.Status switch
        {
            "completed" => (last, null),
            "cancelled" => (null, new SongIngestOutcome("cancelled", stage, null, last.Error)),
            _ => (null, new SongIngestOutcome("failed", stage, null, last.Error ?? "unknown error")),
        };
    }
}
