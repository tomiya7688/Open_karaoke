using System.ComponentModel;

namespace OpenKaraoke.Client;

/// <summary>UI-independent session state; call on the UI synchronization context.</summary>
public sealed class ShellViewModel : INotifyPropertyChanged, IAsyncDisposable
{
    private readonly Func<string, string, string?, CancellationToken, Task<ICoreConnection>> _launch;
    private ICoreConnection? _connection;
    private CancellationTokenSource? _lifetime;
    private Task _shutdown = Task.CompletedTask;
    private long _epoch;
    private bool _connecting;
    private bool _disconnecting;
    private bool _disposed;
    private bool _submitting;
    private bool _cancelling;
    private JobSnapshot? _job;
    private string _error = "";
    private string _models = "未取得";
    private string _modelPackages = "未取得";
    private IReadOnlyList<SongListItem> _songs = [];
    private SongListItem? _selectedSong;
    private bool _songsLoaded;
    private bool _songsTruncated;
    private bool _songBusy;
    private string _executable = Environment.GetEnvironmentVariable("OPEN_KARAOKE_CORE") ??
        Path.Combine(AppContext.BaseDirectory, OperatingSystem.IsWindows() ? "open-karaoke-core.exe" : "open-karaoke-core");
    private string _python = Environment.GetEnvironmentVariable("OPEN_KARAOKE_PYTHON") ?? "";
    private string _data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "OpenKaraoke");

    public ShellViewModel(Func<string, string, string?, CancellationToken, Task<ICoreConnection>>? launch = null)
    {
        _launch = launch ?? (async (executable, data, python, token) =>
            await CoreConnection.StartAsync(executable, data, python, token));
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    private void Changed() => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));
    public string CoreExecutable { get => _executable; set { _executable = value; Changed(); } }
    public string PythonExecutable { get => _python; set { _python = value; Changed(); } }
    public string DataDirectory { get => _data; set { _data = value; Changed(); } }
    public string Error => _error;
    public string Models => _models;
    public string ModelPackages => _modelPackages;
    public string ConnectionStatus => _disconnecting ? "Coreを終了中" : _connecting ? "Coreを起動中" : _connection is null ? "未接続" : "認証済み・接続中";
    public string SessionId => _connection?.InstanceId.ToString() ?? "—";
    public JobSnapshot? CurrentJob => _job;
    public string JobStatus => _job is null ? "Jobなし" : $"{_job.Status} / {_job.Stage}";
    public string JobId => _job?.Id.ToString() ?? "—";
    public double Progress => (_job?.Progress ?? 0) * 100;
    public bool CanConnect => !_disposed && !_connecting && !_disconnecting && _connection is null;
    public bool CanDisconnect => !_disconnecting && (_connecting || _connection is not null);
    public bool CanRun => !_disposed && _connection is not null && !_submitting && (_job is null || _job.IsTerminal);
    public bool CanCancel => !_disposed && _connection is not null && _job is not null && !_job.IsTerminal && !_cancelling;

    // 画面の見た目(状態色・表示切替)専用の派生値。状態の正本は上のフィールドで、ここでは保持しない。
    public bool IsConnected => _connection is not null && !_connecting && !_disconnecting;
    public bool IsTransitioning => _connecting || _disconnecting;
    public bool HasError => _error.Length > 0;
    public bool HasJob => _job is not null;
    // 曲一覧と表示中の曲。曲の取得失敗はCore接続の失敗とは扱わず、エラー表示だけにする。
    public KaraokeViewModel Karaoke { get; } = new();
    public IReadOnlyList<SongListItem> Songs => _songs;
    // 読み込み中は選択を変えない(完了した曲と選択がずれないようにする)。
    public SongListItem? SelectedSong
    {
        get => _selectedSong;
        set { if (!_songBusy) _selectedSong = value; Changed(); }
    }
    public bool CanRefreshSongs => !_disposed && IsConnected && !_songBusy;
    public bool CanLoadSong => CanRefreshSongs && _selectedSong is { Error: null };
    public string SongLibraryStatus =>
        !IsConnected ? "Coreに接続すると曲を読み込めます" :
        _songBusy ? "読み込み中…" :
        !_songsLoaded ? "「曲一覧を更新」で取り込み済みの曲を表示します" :
        _songs.Count == 0 ? "取り込み済みの曲がありません" :
        _songsTruncated ? $"先頭の{_songs.Count}曲を表示しています(続きは未取得)" :
        $"{_songs.Count}曲";

    public string ProgressText => _job is null ? "—" : $"{Progress:0}%";
    public string JobStatusLabel => _job?.Status switch
    {
        null => "まだJobを実行していません",
        "queued" => "待機中",
        "running" => "実行中",
        "completed" => "完了",
        "failed" => "失敗",
        "cancelled" => "キャンセル済み",
        var other => other,
    };

    public async Task ConnectAsync()
    {
        if (!CanConnect) return;
        var epoch = ++_epoch;
        _lifetime?.Cancel();
        _lifetime?.Dispose();
        _lifetime = new CancellationTokenSource();
        var token = _lifetime.Token;
        _connecting = true;
        _error = "";
        Changed();
        ICoreConnection? pending = null;
        try
        {
            pending = await _launch(CoreExecutable, DataDirectory, PythonExecutable, token);
            var identity = await pending.Api.GetIdentityAsync(token);
            if (identity.Service != "open-karaoke-core" || identity.ApiVersion != 1 || identity.InstanceId != pending.InstanceId)
                throw new InvalidDataException("Coreセッションが一致しません。");
            var models = await pending.Api.GetModelsAsync(token);
            var modelSummary = $"登録Adapter: {models.GetProperty("models").GetArrayLength()}件（重みの導入状態とは別です）";
            var packageSummary = await SummarizeModelPackagesAsync(pending.Api, token);
            if (epoch != _epoch || token.IsCancellationRequested) return;
            _connection = pending;
            pending = null;
            _models = modelSummary;
            _modelPackages = packageSummary;
            _ = MonitorConnectionAsync(epoch, _connection, token);
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception error)
        {
            if (epoch == _epoch) _error = error.Message;
        }
        finally
        {
            if (pending is not null)
            {
                try { await pending.DisposeAsync(); }
                catch (Exception error)
                {
                    if (epoch == _epoch) _error = $"Coreの終了を確認できません: {error.Message}";
                }
            }
            if (epoch == _epoch) { _connecting = false; Changed(); }
        }
    }

    // manifest由来の文字列は表示前に制御文字を除き、長さを抑える(複数行の表示を崩さないため)。
    private static string Plain(string? value)
    {
        var text = new string((value ?? "").Select(c => char.IsControl(c) ? ' ' : c).ToArray());
        return text.Length > 80 ? text[..80] + "…" : text;
    }

    // モデルパッケージの検出は補助情報。取得に失敗してもCoreへの接続は成功のままにする。
    private static async Task<string> SummarizeModelPackagesAsync(ICoreApi api, CancellationToken token)
    {
        try
        {
            var packages = await api.GetModelPackagesAsync(token);
            if (packages.Length == 0) return "追加のモデルパッケージはありません";
            var usable = packages.Count(p => p.Status == "available");
            var lines = packages.Take(5).Select(p => p.Status == "available"
                ? $"{Plain(p.Name ?? p.Id)}{(p.Version is null ? "" : " " + Plain(p.Version))}"
                : $"{Plain(p.Id)}: 利用できません({Plain(p.Status)}){(p.Error is null ? "" : " " + Plain(p.Error))}");
            var more = packages.Length > 5 ? $"\n他{packages.Length - 5}件" : "";
            return $"{packages.Length}件(利用可 {usable}件)\n{string.Join("\n", lines)}{more}";
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { throw; }
        catch (InvalidDataException) { return "応答の形式が不正です"; }
        catch (Exception) { return "取得できません"; }
    }

    public async Task StartTestJobAsync()
    {
        if (!CanRun || _connection is null || _lifetime is null) return;
        var connection = _connection;
        var epoch = _epoch;
        var token = _lifetime.Token;
        _submitting = true;
        _job = null;
        _error = "";
        Changed();
        try
        {
            var request = new CreateJobRequest("analysis.mock", new
            {
                model_id = "mock-v1",
                options = new { steps = 50, delay_ms = 100 },
            });
            var job = await connection.Api.CreateJobAsync(request, token);
            if (epoch != _epoch) return;
            Publish(job);
            await foreach (var snapshot in connection.Api.WatchJobAsync(job.Id, token))
            {
                if (epoch != _epoch) return;
                Publish(snapshot);
            }
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception error) { await ConnectionFailedAsync(epoch, error); }
        finally
        {
            if (epoch == _epoch) { _submitting = false; Changed(); }
        }
    }

    public async Task CancelJobAsync()
    {
        if (!CanCancel || _connection is null || _job is null || _lifetime is null) return;
        var epoch = _epoch;
        var id = _job.Id;
        var token = _lifetime.Token;
        _cancelling = true;
        Changed();
        try
        {
            var snapshot = await _connection.Api.CancelJobAsync(id, token);
            if (epoch == _epoch && _job?.Id == id && !_job.IsTerminal) Publish(snapshot);
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception error) { await ConnectionFailedAsync(epoch, error); }
        finally
        {
            if (epoch == _epoch) { _cancelling = false; Changed(); }
        }
    }

    /// {
    ///   責務: [RefreshSongsAsync: 取り込み済みの曲一覧をすべてのページから読み直す]
    ///   処理: [
    ///     1: NextAfterをたどって最大50ページ(5000曲)まで取得し、打ち切ったら状態に示す,
    ///     2: 同じ位置を繰り返すページ指定は不正として中止する,
    ///     3: 読み直し中に切断されたら結果を捨てる,
    ///     4: 選択中の曲が一覧から消えていれば選択と表示を外す
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    public async Task RefreshSongsAsync()
    {
        if (!CanRefreshSongs || _connection is null || _lifetime is null) return;
        var connection = _connection;
        var epoch = _epoch;
        var token = _lifetime.Token;
        _songBusy = true;
        _error = "";
        Changed();
        try
        {
            var songs = new List<SongListItem>();
            Guid? after = null;
            var truncated = false;
            for (var page = 0; ; page++)
            {
                if (page == 50) { truncated = true; break; }
                var result = await connection.Api.ListSongsAsync(after, token);
                songs.AddRange(result.Songs);
                if (result.NextAfter is null) break;
                if (result.NextAfter == after) throw new InvalidDataException("Core repeated the song list page.");
                after = result.NextAfter;
            }
            if (epoch != _epoch) return;
            _songs = songs;
            _songsLoaded = true;
            _songsTruncated = truncated;
            if (_selectedSong is not null)
            {
                // 画面の選択と一致するよう、読み直した一覧の同じ曲へ差し替える。
                // 消えた曲や、読み直しで破損と分かった曲は表示も消す。
                _selectedSong = songs.FirstOrDefault(song => song.SongId == _selectedSong.SongId);
                if (_selectedSong is not { Error: null }) Karaoke.Clear();
            }
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception error)
        {
            if (epoch == _epoch) _error = $"曲一覧を取得できません: {error.Message}";
        }
        finally
        {
            if (epoch == _epoch) { _songBusy = false; Changed(); }
        }
    }

    /// {
    ///   責務: [LoadSelectedSongAsync: 選択中の曲の歌詞・目標ノートを読み、表示する]
    ///   処理: [
    ///     1: Coreから曲データを取得する,
    ///     2: 48kHzの時間軸として検証する,
    ///     3: 切断や別操作で古くなっていなければ表示へ渡す,
    ///     4: 失敗したら前の曲の表示を消し、エラーだけを示す
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    public async Task LoadSelectedSongAsync()
    {
        if (!CanLoadSong || _connection is null || _lifetime is null || _selectedSong is null) return;
        var connection = _connection;
        var epoch = _epoch;
        var token = _lifetime.Token;
        var id = _selectedSong.SongId;
        _songBusy = true;
        _error = "";
        Changed();
        try
        {
            var timeline = new SongTimeline(await connection.Api.GetSongAsync(id, token));
            if (epoch == _epoch && _selectedSong?.SongId == id) Karaoke.Load(timeline);
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception error)
        {
            if (epoch == _epoch)
            {
                _error = $"曲を読み込めません: {error.Message}";
                Karaoke.Clear();
            }
        }
        finally
        {
            if (epoch == _epoch) { _songBusy = false; Changed(); }
        }
    }

    private void Publish(JobSnapshot snapshot)
    {
        // A delayed cancellation response must not regress a terminal snapshot.
        if (_job?.Id == snapshot.Id && _job.IsTerminal) return;
        _job = snapshot;
        if (snapshot.Error is not null) _error = snapshot.Error;
        Changed();
    }

    private async Task MonitorConnectionAsync(long epoch, ICoreConnection connection, CancellationToken token)
    {
        try
        {
            while (true)
            {
                await Task.Delay(TimeSpan.FromSeconds(2), token);
                var identity = await connection.Api.GetIdentityAsync(token);
                if (identity.InstanceId != connection.InstanceId)
                    throw new InvalidDataException("Coreが別セッションへ切り替わりました。再接続してください。");
            }
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception error) { await ConnectionFailedAsync(epoch, error); }
    }

    private async Task ConnectionFailedAsync(long epoch, Exception error)
    {
        if (epoch != _epoch) return;
        _error = $"Core接続を終了しました: {error.Message}";
        await DisconnectAsync();
    }

    public Task DisconnectAsync()
    {
        // Window close and a simultaneous disconnect/heartbeat failure must
        // await the same shutdown, not report completion while Core still runs.
        if (_disconnecting) return _shutdown;
        var completion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        _shutdown = completion.Task;
        _disconnecting = true;
        _ = DisconnectCoreAsync(completion);
        return _shutdown;
    }

    private async Task DisconnectCoreAsync(TaskCompletionSource completion)
    {
        var epoch = ++_epoch;
        _lifetime?.Cancel();
        _lifetime?.Dispose();
        _lifetime = null;
        var old = _connection;
        _connection = null;
        _connecting = _submitting = _cancelling = _songBusy = false;
        _job = null;
        _models = "未取得";
        _modelPackages = "未取得";
        _songs = [];
        _selectedSong = null;
        _songsLoaded = _songsTruncated = false;
        Karaoke.Clear();
        Changed();
        try
        {
            if (old is not null) await old.DisposeAsync();
        }
        catch (Exception error)
        {
            if (epoch == _epoch) _error = $"Coreの終了を確認できません: {error.Message}";
        }
        finally
        {
            if (epoch == _epoch) { _disconnecting = false; Changed(); }
            completion.TrySetResult();
        }
    }

    public async ValueTask DisposeAsync()
    {
        _disposed = true;
        await DisconnectAsync();
    }
}
