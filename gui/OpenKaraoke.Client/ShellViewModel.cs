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
    public bool IsConnected => _connection is not null && !_disconnecting;
    public bool IsTransitioning => _connecting || _disconnecting;
    public bool HasError => _error.Length > 0;
    public bool HasJob => _job is not null;
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
            if (epoch != _epoch || token.IsCancellationRequested) return;
            _connection = pending;
            pending = null;
            _models = modelSummary;
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
        _connecting = _submitting = _cancelling = false;
        _job = null;
        _models = "未取得";
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
