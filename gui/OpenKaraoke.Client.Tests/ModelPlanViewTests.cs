using OpenKaraoke.Client;
using static OpenKaraoke.Client.Tests.Program;

namespace OpenKaraoke.Client.Tests;

/// {
///   責務: [ModelPlanViewTests: Jobのmodel_plan/provenanceがGUI表示用データになること(Issue #109 109.3)を検証する]
///   フィールド: []
/// }
internal static class ModelPlanViewTests
{
    /// {
    ///   責務: [Mapping: Core応答のmodel_planを読み、出所・選択ID・fallback理由・digest/runtimeの有無・注記を表示データにする]
    ///   処理: [1: song Jobの応答を実際のクライアントで読む, 2: 各段の行を確認する, 3: 出所なしのJobは空になる]
    ///   引数: []
    ///   戻り値: []
    /// }
    internal static async Task Mapping()
    {
        var id = Guid.NewGuid();
        using var client = SongTests.TestClient((_, _) => Task.FromResult(SongTests.Raw($$$"""
            {"id":"{{{id}}}","kind":"analysis.song","status":"completed","progress":1,"stage":"done","artifacts":[],
             "model_id":"mock-v1","model_source":"default_fallback","selected_model_id":"gone","selection_fallback_reason":"package missing",
             "model_plan":{"resolved_at":"2026-11-05T00:00:00Z",
               "stems":{"source":"selection","selected_model_id":"demo","fallback_reason":null,"weights_sha256":"{{{new string('a', 64)}}}","runtime_versions":{"torch":"2"},"device":"cpu"},
               "lyrics":{"source":"default_fallback","selected_model_id":"gone","fallback_reason":"package missing"},
               "pitch":{"source":"weird"} } }
            """)));
        var job = await client.GetJobAsync(id);
        var view = ModelPlanView.From(job);
        Check(view.HasPlan && view.ResolvedAt == "2026-11-05T00:00:00Z" && view.Rows.Count == 3, "Plan not mapped.");
        Check(view.Summary.Contains("mock-v1") && view.Summary.Contains("gone") && view.Summary.Contains("package missing"), "Summary lacks provenance.");
        var stems = view.Rows.Single(r => r.Stage == "stems");
        Check(stems.Source == "保存済みの選択" && stems.Runtime == "digestあり / runtimeあり", "Digest/runtime presence wrong.");
        var lyrics = view.Rows.Single(r => r.Stage == "lyrics");
        Check(lyrics.Fallback == "package missing" && lyrics.Runtime == "digest未報告 / runtime未報告", "Fallback row wrong.");
        Check(view.Rows.Single(r => r.Stage == "pitch").Source == "不明", "Unknown source must not be hidden.");
        Check(view.Note.Contains("backend"), "Backend-only note missing.");
        Check(!ModelPlanView.From(null).HasPlan, "Null job must be empty.");
        Check(!ModelPlanView.From(new JobSnapshot(id, "analysis.mock", "running", 0, "x", null, [])).HasPlan, "Job without provenance must be empty.");
    }
}
