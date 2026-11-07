using System.ComponentModel;
using System.Net;

namespace OpenKaraoke.Client;

/// {
///   責務: [EditSnapshot: ある時点の歌詞とノートの全体。Undo/Redoと変更検出の単位(不変)]
///   フィールド: [Lyrics: 歌詞segment, Notes: 採点ノート]
/// }
internal sealed record EditSnapshot(LyricSegment[] Lyrics, ScoringNote[] Notes);

/// {
///   責務: [EditorLyricRow: 編集画面の歌詞1行。書き込みはSongEditorViewModelへ委譲する]
///   フィールド: [Id: segment ID, Text: 歌詞, StartSample/EndSample: 範囲(48kHz sample), _owner: 編集の持ち主]
///   補足: [画面の入力欄は値を確定したとき(フォーカスを外したとき)だけ書き込む]
/// }
public sealed class EditorLyricRow(SongEditorViewModel owner, LyricSegment segment)
{
    public string Id { get; } = segment.Id;
    public string Text
    {
        get => segment.Text;
        set => owner.UpdateLyric(segment.Id, value, segment.StartSample, segment.EndSample);
    }
    public double StartSeconds
    {
        get => SongTimeline.ToSeconds(segment.StartSample);
        set => owner.UpdateLyric(segment.Id, segment.Text, SongTimeline.FromSeconds(value), segment.EndSample);
    }
    public double EndSeconds
    {
        get => SongTimeline.ToSeconds(segment.EndSample);
        set => owner.UpdateLyric(segment.Id, segment.Text, segment.StartSample, SongTimeline.FromSeconds(value));
    }
    /// {
    ///   責務: [IsEdited: この歌詞がこのセッションで編集されたか(自動結果との区別表示用)]
    ///   処理: [1: 編集した歌詞IDの集合に含まれるか返す]
    ///   引数: []
    ///   戻り値: [編集済みならtrue]
    /// }
    public bool IsEdited => owner.IsLyricEdited(segment.Id);
    /// {
    ///   責務: [Delete: この歌詞を削除する(画面の削除ボタンから呼ぶ)]
    ///   処理: [1: 持ち主へ削除を依頼する]
    ///   引数: []
    ///   戻り値: [なし]
    /// }
    public void Delete() => owner.DeleteLyric(segment.Id);
}

/// {
///   責務: [EditorNoteRow: 編集画面のノート1行。書き込みはSongEditorViewModelへ委譲する]
///   フィールド: [Id: ノートID, MidiNote: 音高, StartSample/EndSample: 範囲(48kHz sample), _owner: 編集の持ち主]
/// }
public sealed class EditorNoteRow(SongEditorViewModel owner, ScoringNote note)
{
    public string Id { get; } = note.Id;
    public int MidiNote
    {
        get => note.MidiNote;
        set => owner.UpdateNote(note.Id, note.StartSample, note.EndSample, value);
    }
    public double StartSeconds
    {
        get => SongTimeline.ToSeconds(note.StartSample);
        set => owner.UpdateNote(note.Id, SongTimeline.FromSeconds(value), note.EndSample, note.MidiNote);
    }
    public double EndSeconds
    {
        get => SongTimeline.ToSeconds(note.EndSample);
        set => owner.UpdateNote(note.Id, note.StartSample, SongTimeline.FromSeconds(value), note.MidiNote);
    }
    /// {
    ///   責務: [IsEdited: このノートがこのセッションで編集されたか(自動結果との区別表示用)]
    ///   処理: [1: 編集したノートIDの集合に含まれるか返す]
    ///   引数: []
    ///   戻り値: [編集済みならtrue]
    /// }
    public bool IsEdited => owner.IsNoteEdited(note.Id);
    /// {
    ///   責務: [Delete: このノートを削除する(画面の削除ボタンから呼ぶ)]
    ///   処理: [1: 持ち主へ削除を依頼する]
    ///   引数: []
    ///   戻り値: [なし]
    /// }
    public void Delete() => owner.DeleteNote(note.Id);
}

/// {
///   責務: [SongEditorViewModel: 歌詞・採点ノートの手編集状態(Undo/Redo付き)を持ち、Core APIへ保存する]
///   処理: [
///     1: 読み込んだ曲を基準(baseline)と作業中(working)の2つで持つ,
///     2: 編集ごとに直前のworkingをUndoへ積み、基準との差分を保存時にopへ変換する,
///     3: 保存はPATCH /songs/{id} を1回だけ送る。409は自動再試行せず競合として示す,
///     4: 422/413は何も保存されていないので作業中を基準へ戻し、拒否された状態はRedoで取り戻せる
///   ]
///   フィールド: [
///     _baseline: 最後に保存した(または読み込んだ)状態, _working: 画面上の状態,
///     _undo/_redo: 履歴, _generation: 次の保存のbase_generation,
///     _loadVersion: 読み込みの世代(保存中に曲が替わったら結果を捨てる)
///   ]
///   補足: [確定はCore。ここはUI状態だけを持ち、SongBundleを直接書かない。UIスレッドから呼ぶ]
/// }
public sealed class SongEditorViewModel : INotifyPropertyChanged
{
    // Coreの MAX_EDIT_OPS と同じ。超える変更は保存前に知らせる。
    public const int MaxOps = 1024;
    /// {
    ///   責務: [_undo: 取り消せる過去の状態(新しいものが先頭)]
    ///   処理: [1: 編集ごとに直前の作業中を積む]
    ///   引数: []
    ///   戻り値: []
    /// }
    private readonly Stack<EditSnapshot> _undo = new();
    /// {
    ///   責務: [_redo: やり直せる状態(取り消した状態、または保存で拒否された状態)]
    ///   処理: [1: 取り消しで積み、新しい編集で空にする]
    ///   引数: []
    ///   戻り値: []
    /// }
    private readonly Stack<EditSnapshot> _redo = new();
    private readonly HashSet<string> _touchedLyrics = [];
    private readonly HashSet<string> _touchedNotes = [];
    private ICoreApi? _api;
    private Guid _songId;
    private SongManifest? _manifest;
    private ulong _generation;
    private long _loadVersion;
    /// {
    ///   責務: [_baseline: 最後に保存した(または読み込んだ)状態。保存時の差分の基準]
    ///   処理: [1: 読込・保存成功・巻き戻しで更新する]
    ///   引数: []
    ///   戻り値: []
    /// }
    private EditSnapshot _baseline = new([], []);
    /// {
    ///   責務: [_working: 画面上の作業中の状態]
    ///   処理: [1: 編集・Undo・Redoで更新する]
    ///   引数: []
    ///   戻り値: []
    /// }
    private EditSnapshot _working = new([], []);
    private SongEditState _state = SongEditState.NoSong;
    private string _notice = "";
    private string[] _pending = [];
    private IReadOnlyList<EditorLyricRow> _lyricRows = [];
    private IReadOnlyList<EditorNoteRow> _noteRows = [];

    public event PropertyChangedEventHandler? PropertyChanged;
    /// <summary>保存に成功した直後に、保存した内容(歌詞・ノート)と共に通知する。表示の更新に使う。</summary>
    public event Action<SongBundle>? Saved;

    public SongEditState State => _state;
    public Guid SongId => _songId;
    public ulong Generation => _generation;
    public IReadOnlyList<EditorLyricRow> LyricRows => _lyricRows;
    public IReadOnlyList<EditorNoteRow> NoteRows => _noteRows;
    public IReadOnlyList<string> PendingArtifacts => _pending;
    /// {
    ///   責務: [HasSong: 編集対象の曲があるか]
    ///   処理: [1: 未読込と編集不可以外ならtrue]
    ///   引数: []
    ///   戻り値: [編集画面を出せるならtrue]
    /// }
    public bool HasSong => _state is not (SongEditState.NoSong or SongEditState.Unsupported);
    /// {
    ///   責務: [IsDirty: 未保存の変更があるか]
    ///   処理: [1: 基準と作業中の内容を比べる]
    ///   引数: []
    ///   戻り値: [差があればtrue]
    /// }
    public bool IsDirty => !SameContent(_baseline, _working);
    public bool CanEdit => _state is SongEditState.Clean or SongEditState.Dirty;
    public bool CanSave => _state == SongEditState.Dirty && _api is not null;
    public bool CanUndo => CanEdit && _undo.Count > 0;
    public bool CanRedo => CanEdit && _redo.Count > 0;
    public bool CanRevert => CanEdit && IsDirty;
    /// <summary>競合・曲なしの場合に「最新を読み直す」を促す。</summary>
    public bool NeedsReload => _state is SongEditState.Conflict or SongEditState.Missing;
    public string Notice => _notice;
    public bool HasNotice => _notice.Length > 0;
    public string StatusText => _state switch
    {
        SongEditState.NoSong => "曲を表示すると編集できます",
        SongEditState.Unsupported => "このCoreは曲の世代を返さないため編集できません",
        SongEditState.Clean => _pending.Length == 0
            ? $"変更なし(世代 {_generation})"
            : $"保存済み(世代 {_generation})。再生成待ち: {string.Join(", ", _pending)}",
        SongEditState.Dirty => "未保存の変更があります",
        SongEditState.Saving => "保存中…",
        SongEditState.Conflict => "競合: 他の操作でこの曲が更新されました",
        SongEditState.Missing => "曲が見つかりません",
        _ => "",
    };

    /// {
    ///   責務: [Load: 読み込んだ曲を編集対象にし、履歴と未保存の変更を捨てる]
    ///   処理: [
    ///     1: 読み込みの世代を進め、保存中の結果を無効にする,
    ///     2: 基準と作業中を同じ内容で作り、履歴を空にする,
    ///     3: Coreが世代を返さない曲は編集不可にする
    ///   ]
    ///   引数: [api: 保存先のCore API, songId: 曲ID, bundle: 読み込んだ曲]
    ///   戻り値: []
    /// }
    public void Load(ICoreApi api, Guid songId, SongBundle bundle)
    {
        ArgumentNullException.ThrowIfNull(api);
        ArgumentNullException.ThrowIfNull(bundle);
        _loadVersion++;
        _api = api;
        _songId = songId;
        _manifest = bundle.Song;
        var snapshot = new EditSnapshot(
            bundle.Lyrics.Segments.ToArray(), bundle.Notes.Notes.ToArray());
        _baseline = _working = Sorted(snapshot);
        _undo.Clear();
        _redo.Clear();
        _touchedLyrics.Clear();
        _touchedNotes.Clear();
        _pending = bundle.Song.Artifacts?
            .Where(pair => pair.Value is { Valid: false }).Select(pair => pair.Key).Order(StringComparer.Ordinal).ToArray() ?? [];
        _notice = "";
        if (bundle.EvidenceGeneration is { } generation)
        {
            _generation = generation;
            _state = SongEditState.Clean;
        }
        else
        {
            _generation = 0;
            _state = SongEditState.Unsupported;
        }
        Changed();
    }

    /// {
    ///   責務: [Clear: 編集対象を外す(切断・曲の取り下げ時)]
    ///   処理: [1: 読み込みの世代を進めて保存中の結果を無効にする, 2: 状態と履歴を空にする]
    ///   引数: []
    ///   戻り値: []
    /// }
    public void Clear()
    {
        _loadVersion++;
        _api = null;
        _songId = Guid.Empty;
        _manifest = null;
        _baseline = _working = new EditSnapshot([], []);
        _undo.Clear();
        _redo.Clear();
        _touchedLyrics.Clear();
        _touchedNotes.Clear();
        _pending = [];
        _notice = "";
        _generation = 0;
        _state = SongEditState.NoSong;
        Changed();
    }

    /// {
    ///   責務: [UpdateLyric: 歌詞1件の本文と範囲を変更する]
    ///   処理: [1: 範囲と本文を確認する, 2: 変わらなければ何もしない, 3: 履歴へ積んで反映する]
    ///   引数: [id: 対象, text: 新しい本文, start/end: 新しい範囲(48kHz sample)]
    ///   戻り値: [反映したらtrue。拒否・変更なしはfalse(拒否理由はNotice)]
    /// }
    public bool UpdateLyric(string id, string text, ulong start, ulong end)
    {
        if (!BeginEdit()) return false;
        var index = Array.FindIndex(_working.Lyrics, l => l.Id == id);
        if (index < 0) return Reject("その歌詞は存在しません。");
        if (string.IsNullOrWhiteSpace(text)) return Reject("歌詞の本文を空にはできません。");
        if (RangeError(start, end) is { } rangeError) return Reject(rangeError);
        var updated = _working.Lyrics[index] with { Text = text, StartSample = start, EndSample = end };
        if (updated == _working.Lyrics[index]) { Changed(); return false; }
        var lyrics = _working.Lyrics.ToArray();
        lyrics[index] = updated;
        Apply(new EditSnapshot(lyrics, _working.Notes));
        _touchedLyrics.Add(id);
        return true;
    }

    /// {
    ///   責務: [AddLyric: 新しい歌詞を末尾に追加する]
    ///   処理: [1: 範囲と本文を確認する, 2: 手編集用のIDを採番する, 3: 履歴へ積んで反映する]
    ///   引数: [text: 本文, start/end: 範囲(48kHz sample)]
    ///   戻り値: [追加したIDまたはnull(拒否理由はNotice)]
    /// }
    public string? AddLyric(string text, ulong start, ulong end)
    {
        if (!BeginEdit()) return null;
        if (string.IsNullOrWhiteSpace(text)) { Reject("歌詞の本文を空にはできません。"); return null; }
        if (RangeError(start, end) is { } rangeError) { Reject(rangeError); return null; }
        var id = $"manual-lyric-{Guid.NewGuid():N}";
        Apply(new EditSnapshot([.. _working.Lyrics, new LyricSegment(id, text, start, end)], _working.Notes));
        _touchedLyrics.Add(id);
        return id;
    }

    /// {
    ///   責務: [DeleteLyric: 歌詞1件を削除する]
    ///   処理: [1: 参照しているノートがあれば拒否する, 2: 履歴へ積んで反映する]
    ///   引数: [id: 対象]
    ///   戻り値: [削除したらtrue]
    /// }
    public bool DeleteLyric(string id)
    {
        if (!BeginEdit()) return false;
        if (!_working.Lyrics.Any(l => l.Id == id)) return Reject("その歌詞は存在しません。");
        if (_working.Notes.Any(n => n.LyricSegmentId == id))
            return Reject("この歌詞を参照するノートがあります。先にノートを削除してください。");
        Apply(new EditSnapshot(_working.Lyrics.Where(l => l.Id != id).ToArray(), _working.Notes));
        return true;
    }

    /// {
    ///   責務: [UpdateNote: ノート1件の範囲と音高を変更する]
    ///   処理: [1: 範囲と音高(0..127)を確認する, 2: 変わらなければ何もしない, 3: 履歴へ積んで反映する]
    ///   引数: [id: 対象, start/end: 新しい範囲(48kHz sample), midiNote: 新しい音高]
    ///   戻り値: [反映したらtrue]
    /// }
    public bool UpdateNote(string id, ulong start, ulong end, int midiNote)
    {
        if (!BeginEdit()) return false;
        var index = Array.FindIndex(_working.Notes, n => n.Id == id);
        if (index < 0) return Reject("そのノートは存在しません。");
        if (midiNote is < 0 or > 127) return Reject("音高は0〜127で指定してください。");
        if (RangeError(start, end) is { } rangeError) return Reject(rangeError);
        var updated = _working.Notes[index] with { StartSample = start, EndSample = end, MidiNote = midiNote };
        if (updated == _working.Notes[index]) { Changed(); return false; }
        var notes = _working.Notes.ToArray();
        notes[index] = updated;
        Apply(new EditSnapshot(_working.Lyrics, notes));
        _touchedNotes.Add(id);
        return true;
    }

    /// {
    ///   責務: [AddNote: 新しいノートを追加する]
    ///   処理: [1: 範囲・音高・歌詞参照を確認する, 2: 手編集用のIDを採番する, 3: 履歴へ積んで反映する]
    ///   引数: [start/end: 範囲, midiNote: 音高, lyricSegmentId: 対応する歌詞(なければnull)]
    ///   戻り値: [追加したIDまたはnull]
    /// }
    public string? AddNote(ulong start, ulong end, int midiNote, string? lyricSegmentId = null)
    {
        if (!BeginEdit()) return null;
        if (midiNote is < 0 or > 127) { Reject("音高は0〜127で指定してください。"); return null; }
        if (RangeError(start, end) is { } rangeError) { Reject(rangeError); return null; }
        if (lyricSegmentId is not null && !_working.Lyrics.Any(l => l.Id == lyricSegmentId))
        {
            Reject("参照する歌詞が存在しません。");
            return null;
        }
        var id = $"manual-note-{Guid.NewGuid():N}";
        Apply(new EditSnapshot(_working.Lyrics, [.. _working.Notes, new ScoringNote(id, start, end, midiNote, lyricSegmentId)]));
        _touchedNotes.Add(id);
        return id;
    }

    /// {
    ///   責務: [DeleteNote: ノート1件を削除する]
    ///   処理: [1: 存在を確認する, 2: 履歴へ積んで反映する]
    ///   引数: [id: 対象]
    ///   戻り値: [削除したらtrue]
    /// }
    public bool DeleteNote(string id)
    {
        if (!BeginEdit()) return false;
        if (!_working.Notes.Any(n => n.Id == id)) return Reject("そのノートは存在しません。");
        Apply(new EditSnapshot(_working.Lyrics, _working.Notes.Where(n => n.Id != id).ToArray()));
        return true;
    }

    /// {
    ///   責務: [Undo: 直前の編集を取り消す]
    ///   処理: [1: 作業中をRedoへ移し、Undoの先頭を作業中に戻す]
    ///   引数: []
    ///   戻り値: [取り消したらtrue]
    /// }
    public bool Undo()
    {
        if (!CanUndo) return false;
        _redo.Push(_working);
        _working = _undo.Pop();
        AfterHistoryMove();
        return true;
    }

    /// {
    ///   責務: [Redo: 取り消した編集をやり直す。保存で拒否された状態の復元にも使う]
    ///   処理: [1: 作業中をUndoへ移し、Redoの先頭を作業中に戻す]
    ///   引数: []
    ///   戻り値: [やり直したらtrue]
    /// }
    public bool Redo()
    {
        if (!CanRedo) return false;
        _undo.Push(_working);
        _working = _redo.Pop();
        AfterHistoryMove();
        return true;
    }

    /// {
    ///   責務: [RevertToSaved: 未保存の変更をすべて捨てて基準に戻す]
    ///   処理: [1: 作業中を基準にし、履歴を空にする]
    ///   引数: []
    ///   戻り値: []
    /// }
    public void RevertToSaved()
    {
        if (!CanRevert) return;
        _working = _baseline;
        _undo.Clear();
        _redo.Clear();
        _notice = "";
        _state = SongEditState.Clean;
        Changed();
    }

    /// {
    ///   責務: [SaveAsync: 基準との差分を1回のPATCHで保存する]
    ///   処理: [
    ///     1: 差分を編集opへ変換する(追加→更新→ノート削除→歌詞削除の順),
    ///     2: base_generationを付けて1回だけ送る(自動再試行しない),
    ///     3: 成功なら世代と基準を更新して通知する,
    ///     4: 409は競合として未保存の変更を残し、404は曲なしとして残す,
    ///     5: 422/413は何も保存されていないので基準へ戻し、拒否された状態はRedoに残す,
    ///     6: 429と通信失敗は未保存のまま再実行を促す,
    ///     7: 保存中に曲が替わっていたら結果を捨てる
    ///   ]
    ///   引数: [cancellationToken: 中断(切断時)]
    ///   戻り値: []
    /// }
    public async Task SaveAsync(CancellationToken cancellationToken = default)
    {
        if (!CanSave || _api is null) return;
        var ops = BuildOps(_baseline, _working);
        if (ops.Length == 0) { _state = SongEditState.Clean; Changed(); return; }
        if (ops.Length > MaxOps)
        {
            _notice = $"変更が{ops.Length}件あり、一度に保存できる{MaxOps}件を超えています。いくつかを取り消してください。";
            Changed();
            return;
        }
        var version = _loadVersion;
        var api = _api;
        var sent = _working;
        var songId = _songId;
        _state = SongEditState.Saving;
        _notice = "";
        Changed();
        try
        {
            var result = await api.EditSongAsync(songId, new EditSongRequest(_generation, ops), cancellationToken);
            if (version != _loadVersion) return;
            _baseline = sent;
            _generation = result.Generation;
            _pending = result.Pending;
            _state = SongEditState.Clean;
            _notice = "";
            Changed();
            if (_manifest is not null) Saved?.Invoke(new SongBundle(_manifest, new LyricsDocument(sent.Lyrics), new NotesDocument(sent.Notes)));
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            if (version == _loadVersion) { _state = SongEditState.Dirty; Changed(); }
        }
        catch (Exception error)
        {
            if (version != _loadVersion) return;
            OnSaveFailed(error, sent);
        }
    }

    /// {
    ///   責務: [IsLyricEdited: 歌詞がこのセッションで編集されたかを返す]
    ///   処理: [1: 編集した歌詞IDの集合を引く]
    ///   引数: [id: 歌詞ID]
    ///   戻り値: [編集済みならtrue]
    /// }
    internal bool IsLyricEdited(string id) => _touchedLyrics.Contains(id);

    /// {
    ///   責務: [IsNoteEdited: ノートがこのセッションで編集されたかを返す]
    ///   処理: [1: 編集したノートIDの集合を引く]
    ///   引数: [id: ノートID]
    ///   戻り値: [編集済みならtrue]
    /// }
    internal bool IsNoteEdited(string id) => _touchedNotes.Contains(id);

    /// {
    ///   責務: [OnSaveFailed: 保存失敗をHTTP状態ごとに状態と案内へ写す]
    ///   処理: [409→競合, 404→曲なし, 422/413→基準へ戻す, 429→再実行を促す, その他→未保存のまま失敗表示]
    ///   引数: [error: 例外, sent: 送った状態]
    ///   戻り値: []
    /// }
    private void OnSaveFailed(Exception error, EditSnapshot sent)
    {
        switch (error)
        {
            case CoreApiException { StatusCode: HttpStatusCode.Conflict }:
                _state = SongEditState.Conflict;
                _notice = "他の操作でこの曲が先に更新されたため保存できませんでした。自動では再試行しません。" +
                    "「最新を読み直す」で取得し直してください(読み直すとこの画面の未保存の変更は破棄されます)。";
                break;
            case CoreApiException { StatusCode: HttpStatusCode.NotFound }:
                _state = SongEditState.Missing;
                _notice = "曲がCoreに見つかりません(削除された可能性があります)。曲一覧を更新してください。";
                break;
            case CoreApiException { StatusCode: HttpStatusCode.UnprocessableEntity }:
                RollBack(sent, "Coreが編集内容を拒否しました(範囲・音高・参照などが不正)。保存されていません。" +
                    "画面を保存済みの状態へ戻しました。「やり直し」で拒否された編集を取り戻して直せます。");
                break;
            case CoreApiException { StatusCode: HttpStatusCode.RequestEntityTooLarge }:
                RollBack(sent, "変更が大きすぎて保存できませんでした。保存されていません。" +
                    "画面を保存済みの状態へ戻しました。変更を分けて保存してください(「やり直し」で取り戻せます)。");
                break;
            case CoreApiException { StatusCode: HttpStatusCode.TooManyRequests }:
                _state = SongEditState.Dirty;
                _notice = "Coreが別の書き込みで混み合っています。少し待ってから、もう一度「保存」を押してください(自動では再試行しません)。";
                break;
            case CoreApiException api:
                _state = SongEditState.Dirty;
                _notice = $"保存に失敗しました(HTTP {(int)api.StatusCode})。変更は残っています。";
                break;
            case ArgumentException:
                _state = SongEditState.Dirty;
                _notice = "変更が大きすぎて送信できません(64KiB超)。いくつかを取り消してから保存してください。";
                break;
            default:
                _state = SongEditState.Dirty;
                _notice = $"保存に失敗しました: {error.Message}。変更は残っています。";
                break;
        }
        Changed();
    }

    /// {
    ///   責務: [RollBack: 拒否された編集を基準へ戻し、拒否された状態だけをRedoに残す]
    ///   処理: [1: 作業中を基準にする, 2: 履歴を空にして拒否された状態をRedoへ積む, 3: 案内を設定する]
    ///   引数: [rejected: 拒否された状態, notice: 画面に出す案内]
    ///   戻り値: [なし]
    /// }
    private void RollBack(EditSnapshot rejected, string notice)
    {
        _working = _baseline;
        _undo.Clear();
        _redo.Clear();
        _redo.Push(rejected);
        _state = SongEditState.Clean;
        _notice = notice;
    }

    /// {
    ///   責務: [BeginEdit: 編集を受け付けられる状態か確かめる]
    ///   処理: [1: 編集可能なら案内を消す, 2: 保存中・競合中などは拒否の案内を出す]
    ///   引数: []
    ///   戻り値: [受け付けられるならtrue]
    /// }
    private bool BeginEdit()
    {
        if (CanEdit) { _notice = ""; return true; }
        _notice = _state switch
        {
            SongEditState.Saving => "保存中は編集できません。",
            SongEditState.Conflict or SongEditState.Missing => "「最新を読み直す」を先に行ってください。",
            _ => "編集できる曲がありません。",
        };
        Changed();
        return false;
    }

    /// {
    ///   責務: [Reject: 入力を拒否して案内を出す]
    ///   処理: [1: 案内を設定して通知する(状態は変えない)]
    ///   引数: [message: 案内]
    ///   戻り値: [常にfalse]
    /// }
    private bool Reject(string message)
    {
        _notice = message;
        Changed();
        return false;
    }

    /// {
    ///   責務: [RangeError: 範囲が空でなく曲の内側かを確かめる]
    ///   処理: [1: start<endを確認する, 2: endが曲の長さ以下か確認する]
    ///   引数: [start: 開始sample, end: 終了sample(含まない)]
    ///   戻り値: [問題があれば案内文、なければnull]
    /// }
    private string? RangeError(ulong start, ulong end) =>
        start >= end ? "開始は終了より前にしてください。" :
        end > (_manifest?.DurationSamples ?? 0) ? "終了が曲の長さを超えています。" : null;

    /// {
    ///   責務: [Apply: 新しい作業中の状態を採用する]
    ///   処理: [1: 直前をUndoへ積む, 2: Redoを捨てる, 3: 並べ替えて状態を更新する]
    ///   引数: [next: 新しい状態]
    ///   戻り値: [なし]
    /// }
    private void Apply(EditSnapshot next)
    {
        _undo.Push(_working);
        _redo.Clear();
        _working = Sorted(next);
        _state = IsDirty ? SongEditState.Dirty : SongEditState.Clean;
        Changed();
    }

    /// {
    ///   責務: [AfterHistoryMove: Undo/Redo後の状態を整える]
    ///   処理: [1: 未保存かどうかで状態を決める, 2: 案内を消して通知する]
    ///   引数: []
    ///   戻り値: [なし]
    /// }
    private void AfterHistoryMove()
    {
        _state = IsDirty ? SongEditState.Dirty : SongEditState.Clean;
        _notice = "";
        Changed();
    }

    /// {
    ///   責務: [Sorted: 歌詞とノートを表示順(開始・終了・ID)に並べる]
    ///   処理: [1: それぞれ開始→終了→IDで整列する]
    ///   引数: [snapshot: 並べ替え前]
    ///   戻り値: [並べ替えた新しい状態]
    /// }
    private static EditSnapshot Sorted(EditSnapshot snapshot) => new(
        snapshot.Lyrics.OrderBy(l => l.StartSample).ThenBy(l => l.EndSample).ThenBy(l => l.Id, StringComparer.Ordinal).ToArray(),
        snapshot.Notes.OrderBy(n => n.StartSample).ThenBy(n => n.EndSample).ThenBy(n => n.Id, StringComparer.Ordinal).ToArray());

    /// {
    ///   責務: [SameContent: 2つの状態の内容が同じか比べる]
    ///   処理: [1: 歌詞とノートを要素ごとに比べる]
    ///   引数: [a: 状態, b: 状態]
    ///   戻り値: [同じならtrue]
    /// }
    private static bool SameContent(EditSnapshot a, EditSnapshot b) =>
        a.Lyrics.SequenceEqual(b.Lyrics) && a.Notes.SequenceEqual(b.Notes);

    /// {
    ///   責務: [BuildOps: 基準から作業中への差分を、Coreが順に適用できるopへ変換する]
    ///   処理: [追加→更新→ノート削除→歌詞削除の順にする(参照中の歌詞を先に消さない)]
    ///   引数: [baseline: 保存済み, working: 作業中]
    ///   戻り値: [編集op(同じ入力なら同じ順序)]
    /// }
    internal static SongEditOp[] BuildOps(EditSnapshot baseline, EditSnapshot working)
    {
        var oldLyrics = baseline.Lyrics.ToDictionary(l => l.Id);
        var newLyrics = working.Lyrics.ToDictionary(l => l.Id);
        var oldNotes = baseline.Notes.ToDictionary(n => n.Id);
        var newNotes = working.Notes.ToDictionary(n => n.Id);
        var ops = new List<SongEditOp>();
        ops.AddRange(working.Lyrics.Where(l => !oldLyrics.ContainsKey(l.Id))
            .Select(l => new SongEditOp("add_lyric", l.Id, l.Text, l.StartSample, l.EndSample)));
        ops.AddRange(working.Lyrics.Where(l => oldLyrics.TryGetValue(l.Id, out var before) && before != l)
            .Select(l => new SongEditOp("update_lyric", l.Id, l.Text, l.StartSample, l.EndSample)));
        ops.AddRange(working.Notes.Where(n => !oldNotes.ContainsKey(n.Id))
            .Select(n => new SongEditOp("add_note", n.Id, null, n.StartSample, n.EndSample, n.MidiNote, n.LyricSegmentId)));
        ops.AddRange(working.Notes.Where(n => oldNotes.TryGetValue(n.Id, out var before) && before != n)
            .Select(n => new SongEditOp("update_note", n.Id, null, n.StartSample, n.EndSample, n.MidiNote)));
        ops.AddRange(baseline.Notes.Where(n => !newNotes.ContainsKey(n.Id)).Select(n => new SongEditOp("delete_note", n.Id)));
        ops.AddRange(baseline.Lyrics.Where(l => !newLyrics.ContainsKey(l.Id)).Select(l => new SongEditOp("delete_lyric", l.Id)));
        return ops.ToArray();
    }

    /// {
    ///   責務: [Changed: 画面用の行を作り直し、全プロパティの変更を通知する]
    ///   処理: [1: 作業中から行を作る, 2: 空の名前で通知する]
    ///   引数: []
    ///   戻り値: [なし]
    /// }
    private void Changed()
    {
        _lyricRows = _working.Lyrics.Select(l => new EditorLyricRow(this, l)).ToArray();
        _noteRows = _working.Notes.Select(n => new EditorNoteRow(this, n)).ToArray();
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));
    }

}
