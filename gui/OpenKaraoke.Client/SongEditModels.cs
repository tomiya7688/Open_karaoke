namespace OpenKaraoke.Client;

/// {
///   責務: [SongEditOp: PATCH /songs/{id} に送る編集op1件。opの種類ごとに使う項目だけを埋める]
///   フィールド: [
///     Op: 種類(update_lyric/update_note/add_lyric/add_note/delete_lyric/delete_note),
///     Id: 対象ID, Text: 歌詞本文, StartSample: 開始(48kHz sample), EndSample: 終了(含まない),
///     MidiNote: 音高(0..127), LyricSegmentId: 追加ノートが参照する歌詞
///   ]
///   補足: [nullの項目はJSONへ出さない。名前はsnake_caseに変換される]
/// }
public sealed record SongEditOp(
    string Op, string Id, string? Text = null, ulong? StartSample = null, ulong? EndSample = null,
    int? MidiNote = null, string? LyricSegmentId = null);

/// {
///   責務: [EditSongRequest: PATCH /songs/{id} の本文]
///   フィールド: [BaseGeneration: 編集の土台にした evidence の世代, Ops: 順に適用する編集op]
/// }
public sealed record EditSongRequest(ulong BaseGeneration, SongEditOp[] Ops);

/// {
///   責務: [EditedSongResult: 保存に成功した編集の結果]
///   フィールド: [SongId: 曲ID, Generation: 保存後の世代(次のBaseGeneration), Pending: 再生成待ち(valid=false)のartifact名]
/// }
public sealed record EditedSongResult(Guid SongId, ulong Generation, string[] Pending);

/// {
///   責務: [SongEditState: 編集画面の状態。保存可否と案内文の切り替えに使う]
///   フィールド: []
///   バリアント: [
///     NoSong: 曲が未読込, Unsupported: Coreが世代を返さず編集できない, Clean: 未保存の変更なし,
///     Dirty: 未保存の変更あり, Saving: 保存中, Conflict: 他で更新された(409。再読込が必要),
///     Missing: 曲が見つからない(404。再読込が必要)
///   ]
/// }
public enum SongEditState
{
    NoSong,
    Unsupported,
    Clean,
    Dirty,
    Saving,
    Conflict,
    Missing,
}
