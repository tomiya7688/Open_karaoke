using System.Text.Json;

namespace OpenKaraoke.Client;

/// {
///   責務: [ModelPlanStageRow: 1段分の表示用の行。backendが答えた事実だけを文字列にしたもの]
///   フィールド: [Stage: 段名, Source: 出所の表示, Selected: 保存済み選択ID, Fallback: fallback理由, Runtime: weight digest/runtimeの有無の表示]
/// }
public sealed record ModelPlanStageRow(string Stage, string Source, string Selected, string Fallback, string Runtime);

/// {
///   責務: [ModelPlanView: Jobの model_plan/provenance を画面表示用にまとめる。ShellViewModelの状態には依存せず、JobSnapshotだけから作る]
///   フィールド: [HasPlan: 表示する情報があるか, Summary: Job全体の選択結果, ResolvedAt: 解決時刻, Rows: 段別の行, Note: backend参照のみの注記]
/// }
public sealed record ModelPlanView(bool HasPlan, string Summary, string ResolvedAt, IReadOnlyList<ModelPlanStageRow> Rows, string Note)
{
    /// <summary>manifestがbackend参照のみのpackageは重み差し替えではない旨(設計書 受入条件7)。</summary>
    public const string BackendOnlyNote =
        "注: manifestがbackend参照のみのモデルパッケージは、重みの差し替えではなく、manifestの backend が指す既存Adapterで動きます。";

    public static ModelPlanView Empty { get; } = new(false, "", "", [], "");

    /// {
    ///   責務: [From: JobSnapshotから表示用データを作る。出所情報が無いJobは空を返す]
    ///   処理: [1: model_source/selected_model_id等を読む, 2: model_planの各段を行にする, 3: 不正な形の段は黙って捨てず「不明」と表示する]
    ///   引数: [job: 現在のJob(null可)]
    ///   戻り値: [表示用データ]
    /// }
    public static ModelPlanView From(JobSnapshot? job)
    {
        if (job is null) return Empty;
        var rows = new List<ModelPlanStageRow>();
        var resolvedAt = "";
        if (job.ModelPlan is { ValueKind: JsonValueKind.Object } plan)
        {
            foreach (var property in plan.EnumerateObject())
            {
                if (property.Name == "resolved_at")
                {
                    resolvedAt = property.Value.ValueKind == JsonValueKind.String ? property.Value.GetString() ?? "" : "";
                    continue;
                }
                rows.Add(Row(property.Name, property.Value));
            }
        }
        if (job.ModelSource is null && job.ModelId is null && rows.Count == 0) return Empty;
        var summary = $"実際に動いたモデル: {job.ModelId ?? "未報告"} / 出所: {SourceLabel(job.ModelSource)}" +
            $" / 保存済み選択: {job.SelectedModelId ?? "なし"}" +
            (string.IsNullOrEmpty(job.SelectionFallbackReason) ? "" : $" / 既定へ戻した理由: {job.SelectionFallbackReason}");
        return new ModelPlanView(true, summary, resolvedAt, rows, BackendOnlyNote);
    }

    private static ModelPlanStageRow Row(string stage, JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object) return new(stage, "不明", "—", "—", "—");
        var source = Text(value, "source");
        var selected = Text(value, "selected_model_id");
        var fallback = Text(value, "fallback_reason");
        var digest = value.TryGetProperty("weights_sha256", out var d) && d.ValueKind == JsonValueKind.String ? "digestあり" : "digest未報告";
        var runtime = value.TryGetProperty("runtime_versions", out var r) && r.ValueKind == JsonValueKind.Object ? "runtimeあり" : "runtime未報告";
        return new(stage, SourceLabel(source), selected ?? "なし", fallback ?? "なし", $"{digest} / {runtime}");
    }

    private static string? Text(JsonElement obj, string name) =>
        obj.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static string SourceLabel(string? source) => source switch
    {
        "request" => "明示指定",
        "selection" => "保存済みの選択",
        "default" => "公式既定",
        "default_fallback" => "既定へ切替(選択が使えなかった)",
        null => "未報告",
        _ => "不明",
    };
}
