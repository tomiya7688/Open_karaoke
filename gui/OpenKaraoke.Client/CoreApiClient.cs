using System.Net;
using System.Net.Http.Headers;
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

/// {
///   責務: [SongListItem: 曲一覧の1件。破損した曲も隠さずErrorを付けて返る]
///   フィールド: [SongId: 曲ID, Title: 曲名, Artist: 歌手, DurationSamples: 48kHzでの長さ, Error: 読めない理由]
/// }
public sealed record SongListItem(Guid SongId, string? Title, string? Artist, ulong? DurationSamples, string? Error);

/// {
///   責務: [SongPage: 曲一覧の1ページ]
///   フィールド: [Songs: 曲ID順の一覧, NextAfter: 続きがあるときの次ページ開始ID]
/// }
public sealed record SongPage(SongListItem[] Songs, Guid? NextAfter);

/// {
///   責務: [SongManifest: 再生・表示に使う曲の基本情報]
///   フィールド: [FormatVersion: 形式の版, SongId: 曲ID, Title: 曲名, Artist: 歌手, SampleRate: 内部sample rate, DurationSamples: 長さ]
/// }
public sealed record SongManifest(int FormatVersion, string SongId, string? Title, string? Artist, int SampleRate, ulong DurationSamples);

/// {
///   責務: [LyricSegment: 歌詞の1区間。時刻は48kHz sampleの[開始, 終了)]
///   フィールド: [Id: segment ID, Text: 歌詞, StartSample: 開始, EndSample: 終了(含まない)]
/// }
public sealed record LyricSegment(string Id, string Text, ulong StartSample, ulong EndSample);

/// {
///   責務: [LyricsDocument: 曲の歌詞一覧]
///   フィールド: [Segments: 歌詞segment(並び順は保証されない)]
/// }
public sealed record LyricsDocument(LyricSegment[] Segments);

/// {
///   責務: [ScoringNote: 採点・音程バー用の目標ノート]
///   フィールド: [Id: ノートID, StartSample: 開始, EndSample: 終了(含まない), MidiNote: 音高(MIDIノート番号), LyricSegmentId: 対応する歌詞]
/// }
public sealed record ScoringNote(string Id, ulong StartSample, ulong EndSample, int MidiNote, string? LyricSegmentId);

/// {
///   責務: [NotesDocument: 曲の目標ノート一覧]
///   フィールド: [Notes: ノート(並び順は保証されない)]
/// }
public sealed record NotesDocument(ScoringNote[] Notes);

/// {
///   責務: [SongBundle: GET /songs/{id} のうちGUIが使う部分。evidenceは読み飛ばす]
///   フィールド: [Song: 基本情報, Lyrics: 歌詞, Notes: 目標ノート]
/// }
public sealed record SongBundle(SongManifest Song, LyricsDocument Lyrics, NotesDocument Notes);

public interface ICoreApi
{
    Task<CoreIdentity> GetIdentityAsync(CancellationToken cancellationToken = default);
    Task<JsonElement> GetModelsAsync(CancellationToken cancellationToken = default);
    Task<JobSnapshot> CreateJobAsync(CreateJobRequest job, CancellationToken cancellationToken = default);
    Task<JobSnapshot> GetJobAsync(Guid id, CancellationToken cancellationToken = default);
    Task<JobSnapshot> CancelJobAsync(Guid id, CancellationToken cancellationToken = default);
    IAsyncEnumerable<JobSnapshot> WatchJobAsync(Guid id, CancellationToken cancellationToken = default);
    Task<SongPage> ListSongsAsync(Guid? after = null, CancellationToken cancellationToken = default);
    Task<SongBundle> GetSongAsync(Guid id, CancellationToken cancellationToken = default);
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
    private const long DefaultResponseLimit = 1_048_576;
    // Coreは曲の文書1つを16MiBまで許すため、song.json・歌詞・ノート・evidenceの合計に余裕を足した上限。
    private const long SongResponseLimit = 4 * 16 * 1_048_576 + DefaultResponseLimit;
    // 曲データは大きくなり得るため、受信完了までの期限を他のAPI(10秒)より長くする。
    private static readonly TimeSpan SongDeadline = TimeSpan.FromSeconds(60);
    private readonly HttpClient _http;
    public Uri Endpoint { get; }
    // 要求から本文の受信完了までの既定の期限。曲データだけSongDeadlineを使う。テストでは短くする。
    internal TimeSpan Timeout { get; set; } = TimeSpan.FromSeconds(10);

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
            // HttpClient.Timeoutはヘッダー受信までしか見ず、曲の長い期限も短い方で切ってしまう。
            // 期限はSendAsyncが要求ごとに本文の受信完了まで含めて管理する。
            Timeout = System.Threading.Timeout.InfiniteTimeSpan,
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

    /// {
    ///   責務: [ListSongsAsync: 曲一覧を1ページ(最大100件)取得する]
    ///   処理: [
    ///     1: after以降の曲を要求する,
    ///     2: IDと次ページ指定の整合を確認する
    ///   ]
    ///   引数: [after: 前ページのNextAfter(最初はnull), cancellationToken: 中断]
    ///   戻り値: [曲一覧の1ページ]
    ///   エラー: [InvalidDataException: 一覧の形式が不正, CoreApiException: Coreが失敗を返した]
    /// }
    public async Task<SongPage> ListSongsAsync(Guid? after = null, CancellationToken cancellationToken = default)
    {
        var path = after is { } start ? $"songs?limit=100&after={start:D}" : "songs?limit=100";
        var page = await SendAsync<SongPage>(HttpMethod.Get, path, null, cancellationToken).ConfigureAwait(false);
        if (page.Songs is null || page.Songs.Length > 100 || page.Songs.Any(song => song is null || song.SongId == Guid.Empty) ||
            (page.NextAfter is { } next && (page.Songs.Length == 0 || page.Songs[^1].SongId != next)))
            throw new InvalidDataException("Invalid Core song list.");
        return page;
    }

    /// {
    ///   責務: [GetSongAsync: 曲の基本情報・歌詞・目標ノートを取得する]
    ///   処理: [
    ///     1: 曲IDの文書一式を、曲用の上限サイズで受け取る,
    ///     2: 要求した曲であることを確認する
    ///   ]
    ///   引数: [id: 曲ID, cancellationToken: 中断]
    ///   戻り値: [曲データ]
    ///   エラー: [InvalidDataException: 別の曲・形式不正・上限超過, CoreApiException: Coreが失敗を返した]
    /// }
    public async Task<SongBundle> GetSongAsync(Guid id, CancellationToken cancellationToken = default)
    {
        if (id == Guid.Empty) throw new ArgumentException("Empty song ID.", nameof(id));
        var bundle = await SendAsync<SongBundle>(HttpMethod.Get, $"songs/{id:D}", null, cancellationToken, SongResponseLimit, SongDeadline)
            .ConfigureAwait(false);
        if (bundle.Song is null || !Guid.TryParse(bundle.Song.SongId, out var returned) || returned != id)
            throw new InvalidDataException("Core returned another song.");
        return bundle;
    }

    /// {
    ///   責務: [SendAsync: 固定ルートへ要求を送り、上限内の応答JSONを型へ変換する]
    ///   処理: [
    ///     1: 本文を64KiB以内で直列化する,
    ///     2: 失敗状態は応答本文を出さずに例外にする,
    ///     3: 応答をresponseLimitまで読み、超えたら中止する,
    ///     4: JSONを変換する,
    ///     5: 本文の受信まで含めて期限を過ぎたら中止する
    ///   ]
    ///   引数: [
    ///     method: HTTP method, path: Core内の相対パス, body: 要求本文, cancellationToken: 中断,
    ///     responseLimit: 応答の最大byte数, deadline: 要求から本文の受信完了までの期限(既定はTimeout)
    ///   ]
    ///   戻り値: [変換した応答]
    ///   エラー: [CoreApiException: 失敗状態, InvalidDataException: 空・上限超過の応答, TimeoutException: 期限切れ]
    /// }
    private async Task<T> SendAsync<T>(
        HttpMethod method, string path, object? body, CancellationToken cancellationToken,
        long responseLimit = DefaultResponseLimit, TimeSpan? deadline = null)
    {
        using var request = new HttpRequestMessage(method, path);
        if (body is not null)
        {
            var bytes = JsonSerializer.SerializeToUtf8Bytes(body, JsonOptions);
            if (bytes.Length > 65_536) throw new ArgumentException("Core request exceeds 64 KiB.", nameof(body));
            request.Content = new ByteArrayContent(bytes);
            request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
        }
        // ヘッダー待ちも、本文が止まった応答も、同じ期限で打ち切る。
        using var limit = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        limit.CancelAfter(deadline ?? Timeout);
        var token = limit.Token;
        try
        {
            using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, token).ConfigureAwait(false);
            // Do not expose untrusted response text, URL or request headers in errors.
            if (!response.IsSuccessStatusCode) throw new CoreApiException(response.StatusCode);
            if (response.Content.Headers.ContentLength > responseLimit)
                throw new InvalidDataException("Core response exceeds the size limit.");
            await using var stream = await response.Content.ReadAsStreamAsync(token).ConfigureAwait(false);
            // Content-Lengthがない応答も、読みながら上限で打ち切る。
            using var buffer = new MemoryStream();
            var chunk = new byte[81_920];
            int read;
            while ((read = await stream.ReadAsync(chunk, token).ConfigureAwait(false)) > 0)
            {
                if (buffer.Length + read > responseLimit) throw new InvalidDataException("Core response exceeds the size limit.");
                buffer.Write(chunk, 0, read);
            }
            buffer.Position = 0;
            return await JsonSerializer.DeserializeAsync<T>(buffer, JsonOptions, token).ConfigureAwait(false)
                ?? throw new InvalidDataException("Core returned an empty response.");
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            throw new TimeoutException("Core response did not complete in time.");
        }
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
