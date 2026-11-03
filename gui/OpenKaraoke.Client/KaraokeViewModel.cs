using System.ComponentModel;

namespace OpenKaraoke.Client;

/// {
///   責務: [PitchBar: 音程バー1本の描画位置。座標は描画領域に対する0..1の比率]
///   フィールド: [
///     X: 左端, Width: 幅, Y: 上端(高い音ほど小さい), Height: 1半音分の高さ,
///     MidiNote: 音高, IsActive: 現在位置を含むか
///   ]
/// }
public sealed record PitchBar(double X, double Width, double Y, double Height, int MidiNote, bool IsActive);

/// {
///   責務: [KaraokeViewModel: 読み込んだ曲の表示位置と、その位置の歌詞・音程バーを画面へ渡す]
///   フィールド: [
///     _timeline: 表示中の曲(未読込ならnull),
///     _position: 表示位置(48kHz sample),
///     _lowestNote: 音程バーの下端の音高,
///     _highestNote: 音程バーの上端の音高
///   ]
///   補足: [音声の再生・マイク入力・採点は扱わない(#12・#14)。位置は指定された値をそのまま表示する]
/// }
public sealed class KaraokeViewModel : INotifyPropertyChanged
{
    // 音程バーは現在位置の1秒前から5秒後までを表示し、現在位置を左から1/6の所に置く。
    public static readonly ulong LeadSamples = 1 * (ulong)SongTimeline.SampleRate;
    public static readonly ulong WindowSamples = 6 * (ulong)SongTimeline.SampleRate;
    private SongTimeline? _timeline;
    private ulong _position;
    private int _lowestNote;
    private int _highestNote;

    public event PropertyChangedEventHandler? PropertyChanged;

    public bool HasSong => _timeline is not null;
    public string Title => _timeline?.Title ?? "曲を選んでください";
    public string Artist => _timeline?.Artist ?? "";
    public ulong PositionSamples => _position;
    public double PositionSeconds
    {
        get => SongTimeline.ToSeconds(_position);
        set => Seek(SongTimeline.FromSeconds(value));
    }
    public double DurationSeconds => _timeline is null ? 0 : SongTimeline.ToSeconds(_timeline.DurationSamples);
    public string PositionText => _timeline is null
        ? "—"
        : $"{SongTimeline.Format(_position)} / {SongTimeline.Format(_timeline.DurationSamples)}";
    public string CurrentLyric => LyricText(_timeline?.LyricIndexAt(_position) ?? -1);
    public string NextLyric => LyricText(_timeline?.NextLyricIndexAfter(_position) ?? -1);
    public double PlayheadX => LeadSamples / (double)WindowSamples;
    public IReadOnlyList<PitchBar> Bars => BuildBars();

    /// {
    ///   責務: [Load: 曲を表示対象にし、位置を曲頭へ戻す]
    ///   処理: [
    ///     1: 曲と音程バーの音域(前後2半音の余白付き)を記録する,
    ///     2: 位置を0にして変更を通知する
    ///   ]
    ///   引数: [timeline: 検証済みの曲]
    ///   戻り値: []
    /// }
    public void Load(SongTimeline timeline)
    {
        ArgumentNullException.ThrowIfNull(timeline);
        _timeline = timeline;
        _lowestNote = timeline.Notes.Count == 0 ? 57 : Math.Max(0, timeline.Notes.Min(n => n.MidiNote) - 2);
        _highestNote = timeline.Notes.Count == 0 ? 72 : Math.Min(127, timeline.Notes.Max(n => n.MidiNote) + 2);
        _position = 0;
        Changed();
    }

    /// {
    ///   責務: [Clear: 表示中の曲を外す(切断時など)]
    ///   処理: [1: 曲と位置を消して変更を通知する]
    ///   引数: []
    ///   戻り値: []
    /// }
    public void Clear()
    {
        _timeline = null;
        _position = 0;
        Changed();
    }

    /// {
    ///   責務: [Seek: 表示位置を移す]
    ///   処理: [
    ///     1: 曲がなければ何もしない,
    ///     2: 曲の範囲へ収めた位置にし、変わったときだけ通知する
    ///   ]
    ///   引数: [sample: 移動先(48kHz sample)]
    ///   戻り値: []
    /// }
    public void Seek(ulong sample)
    {
        if (_timeline is null) return;
        var clamped = _timeline.Clamp(sample);
        if (clamped == _position) return;
        _position = clamped;
        Changed();
    }

    /// {
    ///   責務: [BuildBars: 表示範囲と重なるノートを描画用の比率へ変換する]
    ///   処理: [
    ///     1: 現在位置の1秒前から5秒後までのノートを引く,
    ///     2: 時刻を横方向、音高を縦方向の比率へ写す
    ///   ]
    ///   引数: []
    ///   戻り値: [音程バーの一覧(曲がなければ空)]
    /// }
    private IReadOnlyList<PitchBar> BuildBars()
    {
        if (_timeline is null) return [];
        // 曲頭では左側に何もない区間を残し、現在位置の線を常に同じ場所へ置く。
        var windowStart = (double)_position - LeadSamples;
        var from = _position > LeadSamples ? _position - LeadSamples : 0;
        var to = _position + (WindowSamples - LeadSamples);
        var rows = _highestNote - _lowestNote + 1;
        return _timeline.NotesOverlapping(from, to).Select(note => new PitchBar(
            X: (note.StartSample - windowStart) / WindowSamples,
            Width: (note.EndSample - note.StartSample) / (double)WindowSamples,
            Y: (_highestNote - note.MidiNote) / (double)rows,
            Height: 1.0 / rows,
            MidiNote: note.MidiNote,
            IsActive: note.StartSample <= _position && _position < note.EndSample)).ToArray();
    }

    /// {
    ///   責務: [LyricText: 添字の歌詞を返す]
    ///   処理: [1: 範囲外なら空文字にする]
    ///   引数: [index: Lyrics内の添字]
    ///   戻り値: [歌詞]
    /// }
    private string LyricText(int index) =>
        _timeline is not null && index >= 0 ? _timeline.Lyrics[index].Text : "";

    /// {
    ///   責務: [Changed: 派生値を含む全プロパティの変更を通知する]
    ///   処理: [1: 空の名前で通知する]
    ///   引数: []
    ///   戻り値: []
    /// }
    private void Changed() => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));
}
