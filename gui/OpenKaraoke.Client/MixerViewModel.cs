using System.ComponentModel;

namespace OpenKaraoke.Client;

/// {
///   責務: [MixerViewModel: モニタ音量・エコー・リバーブ・bypassのスライダー状態を持ち、変更をCoreへ送る]
///   フィールド: [
///     _api: 接続中のCore API(未接続ならnull),
///     _token: 接続の寿命を表す中断token,
///     _settings: 画面に出している設定,
///     _dirty: まだCoreへ送っていない変更があるか,
///     _pump: 送信ループ(1度に1つだけ動く),
///     _generation: 接続の世代。古い応答を捨てるために使う
///   ]
///   補足: [送信は最新値だけを順番に1件ずつ行う。Coreが丸めた値は画面へ戻す。物理遅延の測定は扱わない]
/// }
public sealed class MixerViewModel : INotifyPropertyChanged
{
    private ICoreApi? _api;
    private CancellationToken _token;
    private MixerSettings _settings = Defaults;
    private bool _dirty;
    private bool _loaded;
    private Task _pump = Task.CompletedTask;
    private long _generation;
    private string _status = "Coreに接続すると調整できます";

    /// <summary>Core側の既定値と同じ。未接続時の表示にだけ使う。</summary>
    public static readonly MixerSettings Defaults = new(1.0, 1.0, 0.25, 200, 0.35, 0.2, false, false);

    public event PropertyChangedEventHandler? PropertyChanged;

    private void Changed() => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));

    public bool IsAvailable => _api is not null && _loaded;
    public string Status => _status;
    public MixerSettings Settings => _settings;

    public double MusicGain { get => _settings.MusicGain; set => Edit(_settings with { MusicGain = Clamp(value, 2) }); }
    public double MicGain { get => _settings.MicGain; set => Edit(_settings with { MicGain = Clamp(value, 2) }); }
    public double EchoLevel { get => _settings.EchoLevel; set => Edit(_settings with { EchoLevel = Clamp(value, 1) }); }
    public double EchoDelayMs { get => _settings.EchoDelayMs; set => Edit(_settings with { EchoDelayMs = (int)Math.Clamp(Math.Round(double.IsFinite(value) ? value : 1), 1, 1000) }); }
    public double EchoFeedback { get => _settings.EchoFeedback; set => Edit(_settings with { EchoFeedback = Clamp(value, 0.9) }); }
    public double ReverbLevel { get => _settings.ReverbLevel; set => Edit(_settings with { ReverbLevel = Clamp(value, 1) }); }
    public bool EffectsBypass { get => _settings.EffectsBypass; set => Edit(_settings with { EffectsBypass = value }); }
    public bool MicMonitor { get => _settings.MicMonitor; set => Edit(_settings with { MicMonitor = value }); }

    /// <summary>送信中の変更がすべて送られるまで待つ。テストと切断処理用。</summary>
    public Task Idle => _pump;

    private static double Clamp(double value, double max) => double.IsFinite(value) ? Math.Clamp(value, 0, max) : 0;

    /// {
    ///   責務: [Edit: 画面の値を更新し、接続中なら送信を予約する]
    ///   処理: [1: 値が変わらなければ何もしない, 2: 更新して通知する, 3: 読み込み済みなら送信ループを起動する]
    ///   引数: [next: 新しい設定]
    ///   戻り値: []
    /// }
    private void Edit(MixerSettings next)
    {
        if (next == _settings) return;
        _settings = next;
        if (_api is not null && _loaded)
        {
            _dirty = true;
            if (_pump.IsCompleted) _pump = PumpAsync(_generation);
        }
        Changed();
    }

    /// {
    ///   責務: [AttachAsync: 接続した直後にCoreの現在設定を読み、以降の変更を送れるようにする]
    ///   処理: [1: 世代を進め接続を記録する, 2: GET mixerで設定を読む, 3: 失敗しても接続は保ち、状態文だけ変える]
    ///   引数: [api: 接続済みのCore API, token: 接続の寿命]
    ///   戻り値: []
    /// }
    public async Task AttachAsync(ICoreApi api, CancellationToken token)
    {
        var generation = ++_generation;
        _api = api;
        _token = token;
        _loaded = false;
        _dirty = false;
        _status = "ミキサー設定を読み込み中…";
        Changed();
        try
        {
            var settings = await api.GetMixerAsync(token);
            if (generation != _generation) return;
            _settings = settings;
            _loaded = true;
            _status = "変更はすぐに反映されます(物理遅延は未測定)";
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception error)
        {
            if (generation == _generation) _status = $"ミキサー設定を読めません: {error.Message}";
        }
        if (generation == _generation) Changed();
    }

    /// {
    ///   責務: [Detach: 切断時に接続と未送信の変更を捨て、既定値の表示へ戻す]
    ///   引数: []
    ///   戻り値: []
    /// }
    public void Detach()
    {
        _generation++;
        _api = null;
        _loaded = false;
        _dirty = false;
        _settings = Defaults;
        _status = "Coreに接続すると調整できます";
        Changed();
    }

    /// {
    ///   責務: [PumpAsync: 未送信の最新値を1件ずつCoreへ送り、丸められた値を画面へ戻す]
    ///   処理: [
    ///     1: 変更がある間、最新値を取り出して送る,
    ///     2: 応答が来た時点でさらに編集されていれば画面の値は上書きしない,
    ///     3: 失敗したら状態文に出して止める(次の編集で再開する),
    ///     4: 切断などで世代が変わったら結果を捨てる
    ///   ]
    ///   引数: [generation: 開始時の接続世代]
    ///   戻り値: []
    /// }
    private async Task PumpAsync(long generation)
    {
        await Task.Yield();
        while (_dirty && generation == _generation && _api is { } api)
        {
            _dirty = false;
            var sent = _settings;
            try
            {
                var applied = await api.SetMixerAsync(sent, _token);
                if (generation != _generation) return;
                if (!_dirty && applied != _settings) _settings = applied;
                _status = "変更はすぐに反映されます(物理遅延は未測定)";
            }
            catch (OperationCanceledException) when (_token.IsCancellationRequested) { return; }
            catch (Exception error)
            {
                if (generation != _generation) return;
                _status = $"ミキサー設定を送れません: {error.Message}";
                _dirty = false;
            }
            Changed();
        }
    }
}
