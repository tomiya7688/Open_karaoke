using System.ComponentModel;

namespace OpenKaraoke.Client;

/// {
///   責務: [SongImportViewModel: 元音源のパスと曲名を受け取り、取込の連鎖(SongIngestFlow)を実行して進捗・結果・取消を画面へ出す]
///   フィールド: [
///     _api: 接続中のCore API(未接続ならnull),
///     _token: 接続の寿命を表す中断token,
///     _run: 実行中の取込を止めるtoken元,
///     _generation: 接続の世代。切断後に届いた古い結果を捨てるために使う,
///     _sourcePath/_title: 入力欄,
///     _busy/_stage/_progress/_status: 実行状態の表示
///   ]
///   補足: [同時に実行できる取込は1件。完了すると`Imported`を通知し、曲一覧の更新は呼び出し側が行う。表示する文字列は制御文字を除き長さを抑える]
/// }
public sealed class SongImportViewModel : INotifyPropertyChanged
{
    private ICoreApi? _api;
    private CancellationToken _token;
    private CancellationTokenSource? _run;
    private long _generation;
    /// {
    ///   責務: [_sync: 状態の確定と進捗の反映を直列にする鍵]
    ///   処理: [new()で鍵用オブジェクトを作る]
    ///   引数: [[]]
    ///   戻り値: [[]]
    /// }
    private readonly object _sync = new();
    private string _sourcePath = "";
    private string _title = "";
    private bool _titleEdited;
    private bool _busy;
    private string _stage = "";
    private double _progress;
    private string _status = "Coreに接続すると曲を取り込めます";

    public event PropertyChangedEventHandler? PropertyChanged;

    /// <summary>取込が完了し、曲が登録された(または既存曲を再利用した)時に、その曲IDで通知する。</summary>
    public event Action<Guid>? Imported;

    /// {
    ///   責務: [Changed: 全プロパティの変更を画面へ通知する]
    ///   処理: [PropertyChangedを空名で発火する]
    ///   引数: [[]]
    ///   戻り値: [[]]
    /// }
    private void Changed() => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));

    public string SourcePath
    {
        get => _sourcePath;
        set
        {
            _sourcePath = value ?? "";
            // 曲名を手で直していない間は、ファイル名(拡張子なし)を既定にする。
            if (!_titleEdited) _title = DefaultTitle(_sourcePath);
            Changed();
        }
    }

    public string Title
    {
        get => _title;
        set { _title = value ?? ""; _titleEdited = _title.Length > 0; Changed(); }
    }

    public bool IsBusy => _busy;
    public string Stage => _stage;
    public double Progress => _progress * 100;
    public string Status => _status;
    /// {
    ///   責務: [CanStart: 取込を開始できるかを返す]
    ///   処理: [接続済み・実行中でない・絶対パス・曲名ありを全て満たすか調べる]
    ///   引数: [[]]
    ///   戻り値: [[開始できるならtrue]]
    /// }
    public bool CanStart => _api is not null && !_busy && Path.IsPathRooted(_sourcePath.Trim()) && _title.Trim().Length > 0;
    public bool CanCancel => _busy && _run is { IsCancellationRequested: false };

    /// {
    ///   責務: [Attach: 接続したCoreを取込の宛先にする]
    ///   処理: [1: 世代を進めて古い実行を無効にする, 2: APIと寿命tokenを保持する, 3: 状態を初期表示へ戻す]
    ///   引数: [api: 接続中のCore API, token: 接続の寿命]
    ///   戻り値: []
    /// }
    public void Attach(ICoreApi api, CancellationToken token)
    {
        Detach();
        _api = api;
        _token = token;
        _status = "取り込む音源を選んでください";
        Changed();
    }

    /// {
    ///   責務: [Detach: 切断時に実行中の取込を止め、入力欄以外の状態を初期へ戻す]
    ///   処理: [1: 世代を進める, 2: 実行中ならtokenで止める(Core側のJobは接続ごと終了する), 3: 表示を初期化する]
    ///   引数: []
    ///   戻り値: []
    /// }
    public void Detach()
    {
        _generation++;
        try { _run?.Cancel(); } catch (ObjectDisposedException) { }
        _run = null;
        _api = null;
        _busy = false;
        _stage = "";
        _progress = 0;
        _status = "Coreに接続すると曲を取り込めます";
        Changed();
    }

    /// {
    ///   責務: [Cancel: 実行中の取込に取消を要求する。現在の段のJobだけが取り消される]
    ///   処理: [1: 実行中でなければ何もしない, 2: 取消tokenを立てて表示を更新する]
    ///   引数: []
    ///   戻り値: []
    /// }
    public void Cancel()
    {
        if (!CanCancel) return;
        _status = "取消中…";
        // 終了と同時に呼ばれても、破棄済みのtoken元で例外にしない。
        try { _run!.Cancel(); } catch (ObjectDisposedException) { }
        Changed();
    }

    /// {
    ///   責務: [StartAsync: 入力を検証して取込の連鎖を実行し、結果を表示する]
    ///   処理: [1: 開始できる状態か確かめる, 2: SongIngestFlowを実行して段・進捗を反映する, 3: 結果を日本語の状態にし、成功なら`Imported`を通知する, 4: 切断済み(世代が違う)なら結果を捨てる]
    ///   引数: []
    ///   戻り値: [完了(結果は表示に反映済み)]
    /// }
    public async Task StartAsync()
    {
        if (!CanStart || _api is not { } api) return;
        var generation = _generation;
        using var run = CancellationTokenSource.CreateLinkedTokenSource(_token);
        _run = run;
        _busy = true;
        _stage = "normalizing";
        _progress = 0;
        _status = StageLabel("normalizing");
        Changed();
        string status;
        Guid? imported = null;
        try
        {
            var outcome = await new SongIngestFlow(api).RunAsync(
                new SongIngestRequest(_sourcePath.Trim(), _title.Trim()),
                new Progress<SongIngestProgress>(p => OnProgress(generation, p)), run.Token).ConfigureAwait(true);
            if (generation != _generation) return;
            switch (outcome.Status)
            {
                case "completed":
                    imported = outcome.Song!.SongId;
                    status = outcome.Song.Reused ? "登録済みの曲を再利用しました" : "曲を登録しました";
                    break;
                case "cancelled":
                    status = $"取り消しました({StageLabel(outcome.Stage)}で停止。曲は登録されていません)";
                    break;
                default:
                    status = $"失敗しました({StageLabel(outcome.Stage)}): {Plain(outcome.Error)}";
                    break;
            }
        }
        catch (OperationCanceledException) when (generation != _generation || _token.IsCancellationRequested)
        {
            if (generation == _generation) Settle("接続が終了したため中断しました");
            return;
        }
        catch (Exception error) when (error is InvalidDataException or CoreApiException or ArgumentException or HttpRequestException)
        {
            if (generation != _generation) return;
            status = $"失敗しました: {Plain(error is CoreApiException api2 ? "HTTP " + (int)api2.StatusCode : error.Message)}";
        }
        catch (Exception error)
        {
            // 想定外の例外でも実行中のまま残さず、画面へ理由を出す(async voidの呼び出し元を落とさない)。
            if (generation != _generation) return;
            status = $"失敗しました: {Plain(error.Message)}";
        }
        Settle(status, imported is not null);
        if (imported is { } id) Imported?.Invoke(id);
    }

    // 終了状態を確定する。遅れて届く進捗通知が後から上書きしないよう、進捗の反映と同じ鍵の中で行う。
    /// {
    ///   責務: [Settle: 取込の終了状態を確定する]
    ///   処理: [1: 鍵の中で実行状態を終了にする, 2: 状態文言を設定する, 3: 成功なら進捗を100%にする, 4: 通知する]
    ///   引数: [[`status`: 表示文言, `complete`: 成功したか]]
    ///   戻り値: [[]]
    /// }
    private void Settle(string status, bool complete = false)
    {
        lock (_sync)
        {
            _busy = false;
            _run = null;
            _stage = "";
            _status = status;
            if (complete) _progress = 1;
        }
        Changed();
    }

    // 進捗の通知は切断後にも届き得るため、世代が同じ間だけ反映する。
    /// {
    ///   責務: [OnProgress: 進捗通知を表示へ反映する]
    ///   処理: [世代が同じで実行中の間だけ、段と進捗と文言を鍵の中で更新して通知する]
    ///   引数: [[`generation`: 開始時の世代, `progress`: 連鎖の進捗]]
    ///   戻り値: [[]]
    /// }
    private void OnProgress(long generation, SongIngestProgress progress)
    {
        lock (_sync)
        {
            if (generation != _generation || !_busy) return;
            _stage = progress.Stage;
            _progress = Math.Clamp(progress.Progress, 0, 1);
            _status = StageLabel(progress.Stage);
        }
        Changed();
    }

    /// {
    ///   責務: [StageLabel: 段名を日本語の表示にする]
    ///   処理: [段名に対応する文言を返し、未知の段はそのまま返す]
    ///   引数: [[`stage`: 段名]]
    ///   戻り値: [[表示文言]]
    /// }
    private static string StageLabel(string stage) => stage switch
    {
        "normalizing" => "音源を変換中",
        "analyzing" => "曲を解析中",
        "importing" => "曲ライブラリへ登録中",
        _ => stage,
    };

    /// {
    ///   責務: [DefaultTitle: パスから曲名の既定値を作る]
    ///   処理: [絶対パスならファイル名(拡張子なし)を表示用に整えて返し、そうでなければ空にする]
    ///   引数: [[`path`: 入力されたパス]]
    ///   戻り値: [[曲名の既定値]]
    /// }
    private static string DefaultTitle(string path)
    {
        var text = path.Trim();
        if (!Path.IsPathRooted(text)) return "";
        return Plain(Path.GetFileNameWithoutExtension(text));
    }

    // Coreやファイル名由来の文字列は、表示前に制御文字を除き長さを抑える。
    /// {
    ///   責務: [Plain: 表示前に制御文字を除き長さを抑える]
    ///   処理: [制御文字を空白にし、120文字を超えたら省略記号で切る]
    ///   引数: [[`value`: 元の文字列]]
    ///   戻り値: [[表示用文字列]]
    /// }
    private static string Plain(string? value)
    {
        var text = new string((value ?? "").Select(c => char.IsControl(c) ? ' ' : c).ToArray());
        return text.Length > 120 ? text[..120] + "…" : text;
    }
}
