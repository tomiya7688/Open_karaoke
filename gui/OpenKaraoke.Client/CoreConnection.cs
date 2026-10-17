using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace OpenKaraoke.Client;

public interface ICoreConnection : IAsyncDisposable
{
    ICoreApi Api { get; }
    Guid InstanceId { get; }
}

/// <summary>Owns exactly the child it launched. Never attaches to an arbitrary local process.</summary>
public sealed class CoreConnection : ICoreConnection
{
    private readonly Process _process;
    private readonly Task _stderrDrain;
    private Task _stdoutDrain = Task.CompletedTask;
    private CoreApiClient? _client;
    private int _disposed;
    public ICoreApi Api => _client ?? throw new InvalidOperationException("Core is not connected.");
    public Guid InstanceId { get; private set; }
    public Uri Endpoint => _client?.Endpoint ?? throw new InvalidOperationException("Core is not connected.");

    private CoreConnection(Process process)
    {
        _process = process;
        _stderrDrain = DrainAsync(process.StandardError);
    }

    public static async Task<CoreConnection> StartAsync(
        string executable, string dataDirectory, string? pythonExecutable = null,
        CancellationToken cancellationToken = default)
    {
        if (!Path.IsPathFullyQualified(executable) || !File.Exists(executable))
            throw new FileNotFoundException("Core実行ファイルの絶対パスを設定してください。");
        if (!Path.IsPathFullyQualified(dataDirectory))
            throw new ArgumentException("データ保存先には絶対パスを指定してください。", nameof(dataDirectory));
        if (!string.IsNullOrWhiteSpace(pythonExecutable) &&
            (!Path.IsPathFullyQualified(pythonExecutable) || !File.Exists(pythonExecutable)))
            throw new FileNotFoundException("Python実行ファイルの絶対パスを確認してください。");
        cancellationToken.ThrowIfCancellationRequested();
        Directory.CreateDirectory(dataDirectory);
        var info = new ProcessStartInfo(executable)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false),
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
            WorkingDirectory = Path.GetDirectoryName(executable)!,
        };
        info.Environment["OPEN_KARAOKE_BIND"] = "127.0.0.1:0";
        info.Environment["OPEN_KARAOKE_DATA_DIR"] = dataDirectory;
        if (!string.IsNullOrWhiteSpace(pythonExecutable))
            info.Environment["OPEN_KARAOKE_PYTHON"] = pythonExecutable;
        var token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32)).ToLowerInvariant();
        var process = Process.Start(info) ?? throw new IOException("Coreを起動できませんでした。");
        var owner = new CoreConnection(process);
        try
        {
            using var startup = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            startup.CancelAfter(TimeSpan.FromSeconds(45));
            // Explicit LF and BOM-free UTF-8 match the bounded Rust bootstrap.
            await process.StandardInput.WriteAsync((token + "\n").AsMemory(), startup.Token).ConfigureAwait(false);
            await process.StandardInput.FlushAsync(startup.Token).ConfigureAwait(false);
            var line = await process.StandardOutput.ReadLineAsync(startup.Token).ConfigureAwait(false);
            if (line is null || line.Length > 8192)
                throw new IOException("Coreの起動応答がありません。実行ファイルとPython環境を確認してください。");
            var ready = JsonSerializer.Deserialize<Ready>(line, CoreApiClient.JsonOptions);
            if (ready is null || ready.Event != "core_ready" || ready.ApiVersion != 1 ||
                ready.InstanceId == Guid.Empty || ready.Port is < 1 or > 65535)
                throw new InvalidDataException("Coreの起動プロトコルが一致しません。");
            owner._stdoutDrain = DrainAsync(process.StandardOutput);
            owner._client = new CoreApiClient(new Uri($"http://127.0.0.1:{ready.Port}/"), token);
            var identity = await owner._client.GetIdentityAsync(startup.Token).ConfigureAwait(false);
            if (identity.InstanceId != ready.InstanceId)
                throw new InvalidDataException("起動したCoreと接続先のセッションが一致しません。");
            startup.Token.ThrowIfCancellationRequested();
            owner.InstanceId = identity.InstanceId;
            return owner;
        }
        catch
        {
            await owner.DisposeAsync().ConfigureAwait(false);
            throw;
        }
    }

    private sealed record Ready(string Event, int ApiVersion, Guid InstanceId, int Port);

    private static async Task DrainAsync(StreamReader reader)
    {
        var buffer = new char[4096];
        try
        {
            while (await reader.ReadAsync(buffer.AsMemory()).ConfigureAwait(false) != 0) { }
        }
        catch (IOException) { }
        catch (ObjectDisposedException) { }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0) return;
        _client?.Dispose();
        try
        {
            try { _process.StandardInput.Close(); }
            catch (IOException) { }
            if (!_process.HasExited)
            {
                using var shutdown = new CancellationTokenSource(TimeSpan.FromSeconds(8));
                try { await _process.WaitForExitAsync(shutdown.Token).ConfigureAwait(false); }
                catch (OperationCanceledException)
                {
                    // Only this owned process and its descendants, never a discovered PID.
                    try { _process.Kill(entireProcessTree: true); }
                    catch (InvalidOperationException) when (_process.HasExited) { }
                    using var killed = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                    await _process.WaitForExitAsync(killed.Token).ConfigureAwait(false);
                }
            }
            try
            {
                await Task.WhenAll(_stderrDrain, _stdoutDrain)
                    .WaitAsync(TimeSpan.FromSeconds(2)).ConfigureAwait(false);
            }
            catch (TimeoutException) { }
        }
        finally { _process.Dispose(); }
    }
}
