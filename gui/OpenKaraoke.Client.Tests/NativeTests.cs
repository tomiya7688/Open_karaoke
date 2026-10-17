using System.Diagnostics;
using System.Net;
using System.Text;
using OpenKaraoke.Client;

namespace OpenKaraoke.Client.Tests;

internal static class NativeTests
{
    public static async Task RunAsync(string executable, string python)
    {
        executable = Path.GetFullPath(executable);
        python = Path.GetFullPath(python);
        var root = Path.Combine(Path.GetTempPath(), "open-karaoke-desktop-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(90));
        var token = timeout.Token;
        try
        {
            await RejectInvalidBootstrapAsync(executable, Path.Combine(root, "invalid"), token);
            using var cancelled = new CancellationTokenSource();
            cancelled.Cancel();
            await Program.Throws<OperationCanceledException>(() => CoreConnection.StartAsync(executable, root, python, cancelled.Token));

            Guid firstInstance;
            Guid previousJob;
            var data = Path.Combine(root, "session");
            await using (var connection = await CoreConnection.StartAsync(executable, data, python, token))
            {
                firstInstance = connection.InstanceId;
                Program.Check((await connection.Api.GetIdentityAsync(token)).InstanceId == firstInstance, "Ready/auth identity mismatch.");
                Program.Check((await connection.Api.GetModelsAsync(token)).GetProperty("models").GetArrayLength() > 0, "Worker registry unavailable.");
                using var unauthenticated = new HttpClient(new HttpClientHandler { UseProxy = false, AllowAutoRedirect = false });
                using var denied = await unauthenticated.GetAsync(new Uri(connection.Endpoint, "session"), token);
                Program.Check(denied.StatusCode == HttpStatusCode.Unauthorized, "Executable did not enforce auth.");
                using var publicHealth = await unauthenticated.GetAsync(new Uri(connection.Endpoint, "health"), token);
                Program.Check(publicHealth.IsSuccessStatusCode, "Public health failed.");

                var job = await connection.Api.CreateJobAsync(new CreateJobRequest("analysis.mock", new
                {
                    model_id = "mock-v1", options = new { steps = 2, delay_ms = 10 },
                }), token);
                previousJob = job.Id;
                var completed = await TerminalAsync(connection.Api, job.Id, token);
                Program.Check(completed.Status == "completed" && completed.Artifacts.Length > 0, "Real worker did not publish an artifact.");
                foreach (var artifact in completed.Artifacts)
                    Program.Check(File.Exists(Path.Combine(data, "artifacts", artifact.Replace('/', Path.DirectorySeparatorChar))), "Artifact missing on disk.");
                Program.Check((await connection.Api.GetJobAsync(job.Id, token)).Status == "completed", "Snapshot reconnect lost terminal status.");

                var longJob = await connection.Api.CreateJobAsync(new CreateJobRequest("analysis.mock", new
                {
                    model_id = "mock-v1", options = new { steps = 100, delay_ms = 100 },
                }), token);
                await connection.Api.CancelJobAsync(longJob.Id, token);
                Program.Check((await TerminalAsync(connection.Api, longJob.Id, token)).Status == "cancelled", "Cancellation did not reach Python.");

                var failing = await connection.Api.CreateJobAsync(new CreateJobRequest("analysis.mock", new
                {
                    model_id = "mock-v1", options = new { fail = true },
                }), token);
                var failed = await TerminalAsync(connection.Api, failing.Id, token);
                Program.Check(failed.Status == "failed" && !string.IsNullOrEmpty(failed.Error), "Worker failure was not exposed.");
            }
            CheckRuntimeClean(data);
            await using (var restarted = await CoreConnection.StartAsync(executable, data, python, token))
            {
                Program.Check(restarted.InstanceId != firstInstance, "Restart reused instance identity.");
                var expired = await Program.Throws<CoreApiException>(() => restarted.Api.GetJobAsync(previousJob, token));
                Program.Check(expired.StatusCode == HttpStatusCode.NotFound, "Old job was replayed into a new session.");
            }
            CheckRuntimeClean(data);
        }
        finally { Directory.Delete(root, recursive: true); }
    }

    private static async Task<JobSnapshot> TerminalAsync(ICoreApi api, Guid id, CancellationToken token)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(15));
        await foreach (var job in api.WatchJobAsync(id, deadline.Token))
            if (job.IsTerminal) return job;
        throw new InvalidOperationException("Observation ended before a terminal state.");
    }

    private static void CheckRuntimeClean(string data)
    {
        var runtime = Path.Combine(data, "artifacts", ".runtime");
        Program.Check(!Directory.Exists(runtime) || !Directory.EnumerateFileSystemEntries(runtime).Any(),
            "Owner-pipe shutdown did not clean up the supervised worker runtime.");
    }

    private static async Task RejectInvalidBootstrapAsync(string executable, string data, CancellationToken token)
    {
        var info = new ProcessStartInfo(executable)
        {
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false),
        };
        info.Environment["OPEN_KARAOKE_BIND"] = "127.0.0.1:0";
        info.Environment["OPEN_KARAOKE_DATA_DIR"] = data;
        using var process = Process.Start(info) ?? throw new IOException("Invalid-bootstrap fixture could not start.");
        try
        {
            var output = process.StandardOutput.ReadToEndAsync(token);
            var errors = process.StandardError.ReadToEndAsync(token);
            var invalid = new string('G', 64);
            await process.StandardInput.WriteAsync((invalid + "\n").AsMemory(), token);
            process.StandardInput.Close();
            await process.WaitForExitAsync(token);
            Program.Check(process.ExitCode != 0 && !(await output).Contains("core_ready"), "Invalid bootstrap opened the server.");
            Program.Check(!(await errors).Contains(invalid), "Invalid secret leaked into diagnostics.");
            Program.Check(!Directory.Exists(data), "Worker started before bootstrap validation.");
        }
        finally
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
        }
    }
}
