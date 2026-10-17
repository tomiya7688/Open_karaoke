using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.Json.Serialization;

[assembly: InternalsVisibleTo("OpenKaraoke.Client.Tests")]

namespace OpenKaraoke.Client;

public sealed record CoreIdentity(string Service, int ApiVersion, Guid InstanceId);
public sealed record CreateJobRequest(string Kind, object? Analysis = null);
public sealed record JobSnapshot(
    Guid Id, string Kind, string Status, double Progress, string Stage,
    string? Error, string[] Artifacts)
{
    public bool IsTerminal => Status is "completed" or "failed" or "cancelled";
}

public interface ICoreApi
{
    Task<CoreIdentity> GetIdentityAsync(CancellationToken cancellationToken = default);
    Task<JsonElement> GetModelsAsync(CancellationToken cancellationToken = default);
    Task<JobSnapshot> CreateJobAsync(CreateJobRequest job, CancellationToken cancellationToken = default);
    Task<JobSnapshot> GetJobAsync(Guid id, CancellationToken cancellationToken = default);
    Task<JobSnapshot> CancelJobAsync(Guid id, CancellationToken cancellationToken = default);
    IAsyncEnumerable<JobSnapshot> WatchJobAsync(Guid id, CancellationToken cancellationToken = default);
}

public sealed class CoreApiException(HttpStatusCode statusCode)
    : Exception($"Core API: {(int)statusCode} ({statusCode})")
{
    public HttpStatusCode StatusCode { get; } = statusCode;
}

/// <summary>Only fixed Core routes on a numeric loopback endpoint can receive the token.</summary>
public sealed class CoreApiClient : ICoreApi, IDisposable
{
    internal static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };
    private readonly HttpClient _http;
    public Uri Endpoint { get; }

    public CoreApiClient(Uri endpoint, string token)
        : this(endpoint, token, new HttpClientHandler
        {
            UseProxy = false,
            AllowAutoRedirect = false,
            UseCookies = false,
        })
    { }

    internal CoreApiClient(Uri endpoint, string token, HttpMessageHandler handler)
    {
        if (!endpoint.IsAbsoluteUri || endpoint.Scheme != "http" ||
            !IPAddress.TryParse(endpoint.Host.Trim('[', ']'), out var ip) ||
            !IPAddress.IsLoopback(ip) || endpoint.UserInfo.Length != 0 ||
            endpoint.Query.Length != 0 || endpoint.Fragment.Length != 0 ||
            endpoint.AbsolutePath != "/")
        {
            handler.Dispose();
            throw new ArgumentException("Core endpoint must be a numeric loopback HTTP root.", nameof(endpoint));
        }
        if (token.Length != 64 || token.Any(c => !(c is >= '0' and <= '9' or >= 'a' and <= 'f')))
        {
            handler.Dispose();
            throw new ArgumentException("Invalid Core session credential format.", nameof(token));
        }
        Endpoint = endpoint;
        _http = new HttpClient(handler, disposeHandler: true)
        {
            BaseAddress = endpoint,
            Timeout = TimeSpan.FromSeconds(10),
            MaxResponseContentBufferSize = 1_048_576,
        };
        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        _http.DefaultRequestHeaders.Add("X-Open-Karaoke-Api-Version", "1");
    }

    public async Task<CoreIdentity> GetIdentityAsync(CancellationToken cancellationToken = default)
    {
        var identity = await SendAsync<CoreIdentity>(HttpMethod.Get, "session", null, cancellationToken).ConfigureAwait(false);
        if (identity.Service != "open-karaoke-core" || identity.ApiVersion != 1 || identity.InstanceId == Guid.Empty)
            throw new InvalidDataException("Core identity or protocol version mismatch.");
        return identity;
    }

    public async Task<JsonElement> GetModelsAsync(CancellationToken cancellationToken = default)
    {
        var value = await SendAsync<JsonElement>(HttpMethod.Get, "models", null, cancellationToken).ConfigureAwait(false);
        if (value.ValueKind != JsonValueKind.Object || !value.TryGetProperty("models", out var models) ||
            models.ValueKind != JsonValueKind.Array)
            throw new InvalidDataException("Invalid Core model list.");
        return value;
    }

    public async Task<JobSnapshot> CreateJobAsync(CreateJobRequest job, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(job.Kind)) throw new ArgumentException("Job kind is required.", nameof(job));
        return Validate(await SendAsync<JobSnapshot>(HttpMethod.Post, "jobs", job, cancellationToken).ConfigureAwait(false));
    }

    public async Task<JobSnapshot> GetJobAsync(Guid id, CancellationToken cancellationToken = default)
    {
        var job = Validate(await SendAsync<JobSnapshot>(HttpMethod.Get, JobPath(id), null, cancellationToken).ConfigureAwait(false));
        if (job.Id != id) throw new InvalidDataException("Core returned another job.");
        return job;
    }

    public async Task<JobSnapshot> CancelJobAsync(Guid id, CancellationToken cancellationToken = default)
    {
        var job = Validate(await SendAsync<JobSnapshot>(HttpMethod.Post, JobPath(id) + "/cancel", null, cancellationToken).ConfigureAwait(false));
        if (job.Id != id) throw new InvalidDataException("Core returned another job.");
        return job;
    }

    // Bounded-rate snapshot polling is deliberate for the initial GUI. Core SSE
    // remains available; a network loss never re-submits a job in a new session.
    public async IAsyncEnumerable<JobSnapshot> WatchJobAsync(
        Guid id, [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        while (true)
        {
            var job = await GetJobAsync(id, cancellationToken).ConfigureAwait(false);
            yield return job;
            if (job.IsTerminal) yield break;
            await Task.Delay(200, cancellationToken).ConfigureAwait(false);
        }
    }

    private async Task<T> SendAsync<T>(HttpMethod method, string path, object? body, CancellationToken cancellationToken)
    {
        using var request = new HttpRequestMessage(method, path);
        if (body is not null)
        {
            var bytes = JsonSerializer.SerializeToUtf8Bytes(body, JsonOptions);
            if (bytes.Length > 65_536) throw new ArgumentException("Core request exceeds 64 KiB.", nameof(body));
            request.Content = new ByteArrayContent(bytes);
            request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
        }
        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseContentRead, cancellationToken).ConfigureAwait(false);
        // Do not expose untrusted response text, URL or request headers in errors.
        if (!response.IsSuccessStatusCode) throw new CoreApiException(response.StatusCode);
        return await response.Content.ReadFromJsonAsync<T>(JsonOptions, cancellationToken).ConfigureAwait(false)
            ?? throw new InvalidDataException("Core returned an empty response.");
    }

    private static string JobPath(Guid id) => id != Guid.Empty ? $"jobs/{id:D}" : throw new ArgumentException("Empty job ID.", nameof(id));

    private static JobSnapshot Validate(JobSnapshot job)
    {
        if (job.Id == Guid.Empty || string.IsNullOrEmpty(job.Kind) || job.Stage is null || job.Artifacts is null ||
            !double.IsFinite(job.Progress) || job.Progress is < 0 or > 1 ||
            job.Status is not ("queued" or "running" or "completed" or "failed" or "cancelled"))
            throw new InvalidDataException("Invalid Core job snapshot.");
        return job;
    }

    public void Dispose() => _http.Dispose();
}
