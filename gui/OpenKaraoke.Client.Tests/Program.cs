using System.Net;
using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Text.Json;
using OpenKaraoke.Client;

namespace OpenKaraoke.Client.Tests;

internal static class Program
{
    private const string Token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    private static readonly Uri Endpoint = new("http://127.0.0.1:32145/");

    private static async Task<int> Main(string[] args)
    {
        if (!(args.Length == 1 && args[0] == "--unit") && args.Length != 2)
        {
            Console.Error.WriteLine("Use --unit, or pass absolute Core and Python executable paths for live integration.");
            return 2;
        }
        var tests = new List<(string Name, Func<Task> Run)>
        {
            ("reject non-loopback and non-root endpoints", RejectEndpoints),
            ("reject invalid credential formats", RejectTokens),
            ("send bearer and version only to Core", HeadersAndIdentity),
            ("reject mismatched protocol and job identity", InvalidResponses),
            ("surface HTTP failures without response secrets", HttpErrors),
            ("bound JSON before dispatch", RequestBudget),
            ("stop observing terminal jobs", TerminalObservation),
            ("honor request cancellation", RequestCancellation),
            ("show connection failures and allow retry", ConnectionFailure),
            ("show mock job completion", ViewModelCompletion),
            ("cancel current job", ViewModelCancellation),
            ("discard late startup after disconnect", StaleStartup),
            ("discard late job after disconnect", StaleJob),
        };
        if (args.Length == 2) tests.Add(("live C# to Rust to Python lifecycle", () => NativeTests.RunAsync(args[0], args[1])));
        var passed = 0;
        foreach (var (name, run) in tests)
        {
            try
            {
                await run().WaitAsync(TimeSpan.FromSeconds(120));
                passed++;
                Console.WriteLine($"PASS {name}");
            }
            catch (Exception error)
            {
                Console.Error.WriteLine($"FAIL {name}: {error}");
                return 1;
            }
        }
        Console.WriteLine($"{passed}/{tests.Count} contract scenarios passed. Live integration: {args.Length == 2}.");
        return 0;
    }

    internal static void Check(bool value, string reason)
    {
        if (!value) throw new InvalidOperationException(reason);
    }

    internal static async Task<T> Throws<T>(Func<Task> action) where T : Exception
    {
        try { await action(); }
        catch (T error) { return error; }
        throw new InvalidOperationException($"Expected {typeof(T).Name}.");
    }

    private static HttpResponseMessage Json(object value) => new(HttpStatusCode.OK)
    {
        Content = JsonContent.Create(value, options: CoreApiClient.JsonOptions),
    };

    private static JobSnapshot Job(Guid id, string status) => new(id, "test", status, status == "completed" ? 1 : 0, status, null, []);
    private static CoreApiClient Client(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> send) => new(Endpoint, Token, new Handler(send));

    private static async Task RejectEndpoints()
    {
        foreach (var uri in new[] { "https://127.0.0.1/", "http://example.invalid/", "http://127.0.0.1/jobs", "http://user@127.0.0.1/", "http://127.0.0.1/?x=1", "http://127.0.0.1/#x" })
            await Throws<ArgumentException>(() => { using var client = new CoreApiClient(new Uri(uri), Token); return Task.CompletedTask; });
    }

    private static async Task RejectTokens()
    {
        foreach (var token in new[] { "", "short", new string('a', 65), Token.ToUpperInvariant() })
            await Throws<ArgumentException>(() => { using var client = new CoreApiClient(Endpoint, token); return Task.CompletedTask; });
    }

    private static async Task HeadersAndIdentity()
    {
        var id = Guid.NewGuid();
        using var client = Client((request, _) =>
        {
            Check(request.RequestUri == new Uri(Endpoint, "session"), "Unexpected destination.");
            Check(request.Headers.Authorization?.Scheme == "Bearer" && request.Headers.Authorization.Parameter == Token, "Missing auth.");
            Check(request.Headers.GetValues("X-Open-Karaoke-Api-Version").Single() == "1", "Missing version.");
            return Task.FromResult(Json(new CoreIdentity("open-karaoke-core", 1, id)));
        });
        Check((await client.GetIdentityAsync()).InstanceId == id, "Identity changed.");
    }

    private static async Task InvalidResponses()
    {
        using var version = Client((_, _) => Task.FromResult(Json(new CoreIdentity("open-karaoke-core", 2, Guid.NewGuid()))));
        await Throws<InvalidDataException>(() => version.GetIdentityAsync());
        using var wrongJob = Client((_, _) => Task.FromResult(Json(Job(Guid.NewGuid(), "completed"))));
        await Throws<InvalidDataException>(() => wrongJob.GetJobAsync(Guid.NewGuid()));
        using var progress = Client((_, _) => Task.FromResult(Json(Job(Guid.NewGuid(), "running") with { Progress = 2 })));
        await Throws<InvalidDataException>(() => progress.CreateJobAsync(new CreateJobRequest("test")));
    }

    private static async Task HttpErrors()
    {
        using var client = Client((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.Unauthorized) { Content = new StringContent(Token) }));
        var error = await Throws<CoreApiException>(() => client.GetIdentityAsync());
        Check(error.StatusCode == HttpStatusCode.Unauthorized && !error.Message.Contains(Token), "Leaked response or status lost.");
        using var redirect = Client((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.Redirect)));
        await Throws<CoreApiException>(() => redirect.GetIdentityAsync());
    }

    private static async Task RequestBudget()
    {
        var called = false;
        using var client = Client((_, _) => { called = true; return Task.FromResult(Json(Job(Guid.NewGuid(), "queued"))); });
        await Throws<ArgumentException>(() => client.CreateJobAsync(new CreateJobRequest("test", new { padding = new string('a', 70_000) })));
        Check(!called, "Oversized request was sent.");
    }

    private static async Task TerminalObservation()
    {
        var id = Guid.NewGuid();
        var calls = 0;
        using var client = Client((_, _) => Task.FromResult(Json(Job(id, ++calls == 1 ? "running" : "completed"))));
        var seen = new List<string>();
        await foreach (var job in client.WatchJobAsync(id)) seen.Add(job.Status);
        Check(seen.SequenceEqual(new[] { "running", "completed" }) && calls == 2, "Watcher did not stop.");
    }

    private static async Task RequestCancellation()
    {
        using var client = Client(async (_, token) => { await Task.Delay(Timeout.Infinite, token); return Json(new { }); });
        using var stop = new CancellationTokenSource(TimeSpan.FromMilliseconds(50));
        await Throws<OperationCanceledException>(() => client.GetIdentityAsync(stop.Token));
    }

    private static async Task ConnectionFailure()
    {
        await using var model = new ShellViewModel((_, _, _, _) => throw new IOException("fixture failure"));
        await model.ConnectAsync();
        Check(model.CanConnect && !model.CanRun && model.Error.Contains("fixture failure"), "Failure did not restore state.");
    }

    private static async Task ViewModelCompletion()
    {
        var fake = new FakeConnection();
        await using var model = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(fake));
        await model.ConnectAsync();
        Check(model.CanRun, "Not connected.");
        await model.StartTestJobAsync();
        Check(model.CurrentJob?.Status == "completed" && model.Progress == 100 && model.CanRun, "Completion not published.");
    }

    private static async Task ViewModelCancellation()
    {
        var fake = new FakeConnection { Completion = new(TaskCreationOptions.RunContinuationsAsynchronously) };
        await using var model = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(fake));
        await model.ConnectAsync();
        var running = model.StartTestJobAsync();
        Check(model.CanCancel, "Cancellation not available.");
        await model.CancelJobAsync();
        await running;
        Check(model.CurrentJob?.Status == "cancelled", "Cancellation not observed.");
    }

    private static async Task StaleStartup()
    {
        var pending = new TaskCompletionSource<ICoreConnection>(TaskCreationOptions.RunContinuationsAsynchronously);
        var fake = new FakeConnection();
        await using var model = new ShellViewModel((_, _, _, _) => pending.Task);
        var connecting = model.ConnectAsync();
        await model.DisconnectAsync();
        pending.SetResult(fake);
        await connecting;
        Check(fake.Disposed && model.CanConnect && !model.CanRun, "Late connection replaced disconnected state.");
    }

    private static async Task StaleJob()
    {
        var fake = new FakeConnection { Completion = new(TaskCreationOptions.RunContinuationsAsynchronously) };
        await using var model = new ShellViewModel((_, _, _, _) => Task.FromResult<ICoreConnection>(fake));
        await model.ConnectAsync();
        var running = model.StartTestJobAsync();
        await model.DisconnectAsync();
        fake.Completion.SetResult();
        await running;
        Check(model.CurrentJob is null && model.CanConnect && fake.Disposed, "Late job resurrected a session.");
    }

    private sealed class Handler(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> send) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) => send(request, cancellationToken);
    }

    private sealed class FakeConnection : ICoreConnection, ICoreApi
    {
        public Guid InstanceId { get; } = Guid.NewGuid();
        public ICoreApi Api => this;
        public bool Disposed { get; private set; }
        public bool Cancelled { get; private set; }
        public TaskCompletionSource? Completion { get; init; }
        public Task<CoreIdentity> GetIdentityAsync(CancellationToken cancellationToken = default) => Task.FromResult(new CoreIdentity("open-karaoke-core", 1, InstanceId));
        public Task<JsonElement> GetModelsAsync(CancellationToken cancellationToken = default) => Task.FromResult(JsonSerializer.SerializeToElement(new { models = Array.Empty<object>() }));
        public Task<JobSnapshot> CreateJobAsync(CreateJobRequest job, CancellationToken cancellationToken = default) => Task.FromResult(Job(Guid.NewGuid(), "queued"));
        public Task<JobSnapshot> GetJobAsync(Guid id, CancellationToken cancellationToken = default) => Task.FromResult(Job(id, "running"));
        public Task<JobSnapshot> CancelJobAsync(Guid id, CancellationToken cancellationToken = default)
        {
            Cancelled = true;
            Completion?.TrySetResult();
            return Task.FromResult(Job(id, "running"));
        }
        public async IAsyncEnumerable<JobSnapshot> WatchJobAsync(Guid id, [EnumeratorCancellation] CancellationToken cancellationToken = default)
        {
            yield return Job(id, "running");
            // Intentionally ignore cancellation to exercise the ViewModel epoch guard.
            if (Completion is not null) await Completion.Task;
            yield return Job(id, Cancelled ? "cancelled" : "completed");
        }
        public ValueTask DisposeAsync() { Disposed = true; return ValueTask.CompletedTask; }
    }
}
