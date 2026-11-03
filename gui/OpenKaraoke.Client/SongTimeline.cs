namespace OpenKaraoke.Client;

/// {
///   責務: [SongTimeline: 曲の48kHz sample軸で、ある位置の歌詞と表示範囲の音符を引く]
///   フィールド: [
///     Lyrics: 開始sample順に並べた歌詞segment,
///     Notes: 開始sample順に並べた採点用ノート,
///     DurationSamples: 曲の長さ(sample)
///   ]
///   補足: [GUIライブラリに依存しない。位置の正本はCoreの再生位置で、GUIは独自の時計を作らない]
/// }
public sealed class SongTimeline
{
    public const int SampleRate = 48_000;
    private readonly ulong _longestLyric;
    private readonly ulong _longestNote;

    /// {
    ///   責務: [SongTimeline: 読み込んだ曲を検証し、検索用に並べ替える]
    ///   処理: [
    ///     1: sample rateと曲の長さを確認する,
    ///     2: 歌詞とノートの範囲が曲の内側にあるか確認する,
    ///     3: 開始sample順(同じなら終了順)に並べ、最長の長さを記録する
    ///   ]
    ///   引数: [bundle: Coreが返した曲データ]
    ///   戻り値: []
    ///   エラー: [InvalidDataException: sample rate・長さ・範囲・音高のいずれかが不正]
    /// }
    public SongTimeline(SongBundle bundle)
    {
        ArgumentNullException.ThrowIfNull(bundle);
        var song = bundle.Song ?? throw new InvalidDataException("Song manifest is missing.");
        if (song.SampleRate != SampleRate) throw new InvalidDataException("Song must use the 48 kHz timeline.");
        if (song.DurationSamples == 0) throw new InvalidDataException("Song duration must be positive.");
        DurationSamples = song.DurationSamples;
        Title = song.Title ?? "";
        Artist = song.Artist;
        var lyrics = bundle.Lyrics?.Segments ?? throw new InvalidDataException("Lyrics document is missing.");
        var notes = bundle.Notes?.Notes ?? throw new InvalidDataException("Notes document is missing.");
        foreach (var segment in lyrics)
        {
            if (segment is null || segment.Text is null) throw new InvalidDataException("Invalid lyric segment.");
            CheckRange(segment.StartSample, segment.EndSample);
        }
        foreach (var note in notes)
        {
            if (note is null || note.MidiNote > 127) throw new InvalidDataException("Invalid scoring note.");
            CheckRange(note.StartSample, note.EndSample);
        }
        // Coreの保存形式は並び順を保証しないため、ここで二分探索できる順にする。
        Lyrics = lyrics.OrderBy(s => s.StartSample).ThenBy(s => s.EndSample).ToArray();
        Notes = notes.OrderBy(n => n.StartSample).ThenBy(n => n.EndSample).ToArray();
        _longestLyric = Lyrics.Count == 0 ? 0 : Lyrics.Max(s => s.EndSample - s.StartSample);
        _longestNote = Notes.Count == 0 ? 0 : Notes.Max(n => n.EndSample - n.StartSample);
    }

    public string Title { get; }
    public string? Artist { get; }
    public ulong DurationSamples { get; }
    public IReadOnlyList<LyricSegment> Lyrics { get; }
    public IReadOnlyList<ScoringNote> Notes { get; }

    /// {
    ///   責務: [Clamp: 位置を曲の範囲[0, DurationSamples]へ収める]
    ///   処理: [1: 長さを超える位置を曲末へ丸める]
    ///   引数: [sample: 指定位置]
    ///   戻り値: [範囲内の位置]
    /// }
    public ulong Clamp(ulong sample) => Math.Min(sample, DurationSamples);

    /// {
    ///   責務: [LyricIndexAt: 指定位置で歌っている歌詞segmentを返す]
    ///   処理: [
    ///     1: 開始が位置以下の最後のsegmentを二分探索する,
    ///     2: 重なりを考慮し、最長segment分だけ前へ遡って位置を含むものを探す
    ///   ]
    ///   引数: [sample: 曲内の位置]
    ///   戻り値: [Lyrics内の添字。歌詞の間なら-1]
    /// }
    public int LyricIndexAt(ulong sample)
    {
        // 重なる場合は後から始まったsegmentを優先する(新しい行を表示する)。
        for (var i = LastStartingAtOrBefore(Lyrics, s => s.StartSample, sample); i >= 0; i--)
        {
            var segment = Lyrics[i];
            if (sample < segment.EndSample) return i;
            if (sample - segment.StartSample >= _longestLyric) break;
        }
        return -1;
    }

    /// {
    ///   責務: [NextLyricIndexAfter: 指定位置より後に始まる最初の歌詞segmentを返す]
    ///   処理: [1: 開始が位置以下の最後の添字の次を返す]
    ///   引数: [sample: 曲内の位置]
    ///   戻り値: [Lyrics内の添字。以降の歌詞がなければ-1]
    /// }
    public int NextLyricIndexAfter(ulong sample)
    {
        var next = LastStartingAtOrBefore(Lyrics, s => s.StartSample, sample) + 1;
        return next < Lyrics.Count ? next : -1;
    }

    /// {
    ///   責務: [NotesOverlapping: [from, to)と重なるノートを開始順に返す]
    ///   処理: [
    ///     1: 最長ノート分だけ前から探索を始める,
    ///     2: 開始がtoに達するまで、終了がfromより後のノートを集める
    ///   ]
    ///   引数: [from: 範囲の開始sample, to: 範囲の終了sample(含まない)]
    ///   戻り値: [重なるノートの一覧]
    /// }
    public IReadOnlyList<ScoringNote> NotesOverlapping(ulong from, ulong to)
    {
        var result = new List<ScoringNote>();
        if (to <= from) return result;
        var earliest = from > _longestNote ? from - _longestNote : 0;
        var start = LastStartingAtOrBefore(Notes, n => n.StartSample, earliest);
        for (var i = Math.Max(start, 0); i < Notes.Count && Notes[i].StartSample < to; i++)
        {
            if (Notes[i].EndSample > from) result.Add(Notes[i]);
        }
        return result;
    }

    /// {
    ///   責務: [ToSeconds: sample位置を秒へ変換する]
    ///   処理: [1: 48kHzで割る]
    ///   引数: [sample: 曲内の位置]
    ///   戻り値: [秒]
    /// }
    public static double ToSeconds(ulong sample) => sample / (double)SampleRate;

    /// {
    ///   責務: [FromSeconds: 秒をsample位置へ変換する]
    ///   処理: [
    ///     1: 負・非数は0にする,
    ///     2: 最も近いsampleへ丸める
    ///   ]
    ///   引数: [seconds: 秒]
    ///   戻り値: [sample位置]
    /// }
    public static ulong FromSeconds(double seconds)
    {
        if (!double.IsFinite(seconds) || seconds <= 0) return 0;
        var samples = Math.Round(seconds * SampleRate, MidpointRounding.AwayFromZero);
        return samples >= ulong.MaxValue ? ulong.MaxValue : (ulong)samples;
    }

    /// {
    ///   責務: [Format: sample位置を「分:秒.10分の1秒」で表す]
    ///   処理: [1: 10分の1秒単位へ切り捨てて整形する]
    ///   引数: [sample: 曲内の位置]
    ///   戻り値: [例「1:05.3」]
    /// }
    public static string Format(ulong sample)
    {
        var tenths = sample / (SampleRate / 10);
        return $"{tenths / 600}:{tenths / 10 % 60:00}.{tenths % 10}";
    }

    /// {
    ///   責務: [CheckRange: 区間が空でなく曲の内側にあるか確かめる]
    ///   処理: [1: start < end <= DurationSamples を確認する]
    ///   引数: [start: 開始sample, end: 終了sample(含まない)]
    ///   戻り値: []
    ///   エラー: [InvalidDataException: 範囲外または空の区間]
    /// }
    private void CheckRange(ulong start, ulong end)
    {
        if (start >= end || end > DurationSamples)
            throw new InvalidDataException("Lyric or note timing is outside the song.");
    }

    /// {
    ///   責務: [LastStartingAtOrBefore: 開始sampleが指定位置以下となる最後の添字を探す]
    ///   処理: [1: 開始順に並んだ一覧を二分探索する]
    ///   引数: [items: 開始順の一覧, start: 要素の開始sample, sample: 位置]
    ///   戻り値: [添字。該当がなければ-1]
    /// }
    private static int LastStartingAtOrBefore<T>(IReadOnlyList<T> items, Func<T, ulong> start, ulong sample)
    {
        int low = 0, high = items.Count;
        while (low < high)
        {
            var middle = low + (high - low) / 2;
            if (start(items[middle]) <= sample) low = middle + 1;
            else high = middle;
        }
        return low - 1;
    }
}
