using Avalonia.Controls;
using Avalonia.Interactivity;
using OpenKaraoke.Client;

namespace OpenKaraoke.Gui;

public sealed partial class MainWindow : Window
{
    private readonly ShellViewModel _model = new();
    private bool _closing;
    private bool _allowClose;

    public MainWindow()
    {
        InitializeComponent();
        DataContext = _model;
        Closing += OnClosing;
    }

    private async void ConnectClick(object? sender, RoutedEventArgs args) => await _model.ConnectAsync();
    private async void DisconnectClick(object? sender, RoutedEventArgs args) => await _model.DisconnectAsync();
    private async void RunClick(object? sender, RoutedEventArgs args) => await _model.StartTestJobAsync();
    private async void CancelClick(object? sender, RoutedEventArgs args) => await _model.CancelJobAsync();
    private async void RefreshSongsClick(object? sender, RoutedEventArgs args) => await _model.RefreshSongsAsync();
    private async void LoadSongClick(object? sender, RoutedEventArgs args) => await _model.LoadSelectedSongAsync();
    /// {
    ///   責務: [PlayClick: 再生ボタン]
    ///   処理: [1: Coreへ再生を依頼する]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private async void PlayClick(object? sender, RoutedEventArgs args) => await _model.Playback.PlayAsync();
    /// {
    ///   責務: [PauseClick: 一時停止ボタン]
    ///   処理: [1: Coreへ一時停止を依頼する]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private async void PauseClick(object? sender, RoutedEventArgs args) => await _model.Playback.PauseAsync();
    /// {
    ///   責務: [StopClick: 停止ボタン]
    ///   処理: [1: Coreへ停止を依頼する]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private async void StopClick(object? sender, RoutedEventArgs args) => await _model.Playback.StopAsync();
    /// {
    ///   責務: [SeekClick: シーク先へ移動するボタン]
    ///   処理: [1: 指定のシーク先へのシークをCoreへ依頼する]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private async void SeekClick(object? sender, RoutedEventArgs args) => await _model.Playback.SeekToTargetAsync();
    /// {
    ///   責務: [SaveEditsClick: 歌詞・ノートの編集を保存するボタン]
    ///   処理: [1: PATCH /songs/{id} を1回だけ依頼する]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private async void SaveEditsClick(object? sender, RoutedEventArgs args) => await _model.SaveEditsAsync();
    /// {
    ///   責務: [UndoEditClick: 編集を元に戻すボタン]
    ///   処理: [1: 直前の編集を取り消す]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private void UndoEditClick(object? sender, RoutedEventArgs args) => _model.Editor.Undo();
    /// {
    ///   責務: [RedoEditClick: 編集をやり直すボタン]
    ///   処理: [1: 取り消した編集(または拒否された編集)を戻す]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private void RedoEditClick(object? sender, RoutedEventArgs args) => _model.Editor.Redo();
    /// {
    ///   責務: [RevertEditsClick: 未保存の編集をすべて捨てるボタン]
    ///   処理: [1: 保存済みの状態へ戻す]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private void RevertEditsClick(object? sender, RoutedEventArgs args) => _model.Editor.RevertToSaved();
    /// {
    ///   責務: [AddLyricClick: 表示位置から2秒の歌詞を追加するボタン]
    ///   処理: [1: 表示位置から2秒(曲末で丸め)の範囲に「歌詞」という本文で追加する]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private void AddLyricClick(object? sender, RoutedEventArgs args)
    {
        var (start, end) = NewRange(2);
        _model.Editor.AddLyric("歌詞", start, end);
    }
    /// {
    ///   責務: [AddNoteClick: 表示位置から1秒のノート(C4)を追加するボタン]
    ///   処理: [1: 表示位置から1秒(曲末で丸め)の範囲に音高60で追加する]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private void AddNoteClick(object? sender, RoutedEventArgs args)
    {
        var (start, end) = NewRange(1);
        _model.Editor.AddNote(start, end, 60);
    }
    /// {
    ///   責務: [PreviewLyricClick: 行の歌詞の区間を試聴するボタン]
    ///   処理: [1: ボタンの行(EditorLyricRow)の区間をプレビュー再生する]
    ///   引数: [sender: ボタン, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private async void PreviewLyricClick(object? sender, RoutedEventArgs args)
    {
        if ((sender as Control)?.DataContext is EditorLyricRow row) await _model.Preview.PlayRangeAsync(row.StartSample, row.EndSample);
    }
    /// {
    ///   責務: [PreviewNoteClick: 行のノートの区間を試聴するボタン]
    ///   処理: [1: ボタンの行(EditorNoteRow)の区間をプレビュー再生する]
    ///   引数: [sender: ボタン, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private async void PreviewNoteClick(object? sender, RoutedEventArgs args)
    {
        if ((sender as Control)?.DataContext is EditorNoteRow row) await _model.Preview.PlayRangeAsync(row.StartSample, row.EndSample);
    }
    /// {
    ///   責務: [StopPreviewClick: 区間の試聴を止めるボタン]
    ///   処理: [1: 終端の予約を捨てて一時停止を依頼する]
    ///   引数: [sender, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private async void StopPreviewClick(object? sender, RoutedEventArgs args) => await _model.Preview.StopAsync();
    /// {
    ///   責務: [DeleteLyricClick: 行の歌詞を削除するボタン]
    ///   処理: [1: ボタンの行(EditorLyricRow)を削除する]
    ///   引数: [sender: ボタン, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private void DeleteLyricClick(object? sender, RoutedEventArgs args)
    {
        if ((sender as Control)?.DataContext is EditorLyricRow row) row.Delete();
    }
    /// {
    ///   責務: [DeleteNoteClick: 行のノートを削除するボタン]
    ///   処理: [1: ボタンの行(EditorNoteRow)を削除する]
    ///   引数: [sender: ボタン, args: Avaloniaのイベント]
    ///   戻り値: [なし]
    /// }
    private void DeleteNoteClick(object? sender, RoutedEventArgs args)
    {
        if ((sender as Control)?.DataContext is EditorNoteRow row) row.Delete();
    }
    /// {
    ///   責務: [NewRange: 表示位置から指定秒数の新しい範囲を作る]
    ///   処理: [1: 開始を表示位置(曲末の1sample前まで)にする, 2: 終了を曲末で丸める]
    ///   引数: [seconds: 範囲の長さ(秒)]
    ///   戻り値: [開始と終了(48kHz sample)]
    /// }
    private (ulong Start, ulong End) NewRange(int seconds)
    {
        var duration = SongTimeline.FromSeconds(_model.Karaoke.DurationSeconds);
        var start = duration == 0 ? 0 : Math.Min(_model.Karaoke.PositionSamples, duration - 1);
        var end = Math.Min(duration, start + (ulong)seconds * SongTimeline.SampleRate);
        return (start, end);
    }
    private async void RefreshModelsClick(object? sender, RoutedEventArgs args) => await _model.RefreshModelPackagesAsync();
    private async void ApplyModelChoiceClick(object? sender, RoutedEventArgs args) => await _model.ApplyModelChoiceAsync();

    private async void OnClosing(object? sender, WindowClosingEventArgs args)
    {
        if (_allowClose) return;
        args.Cancel = true;
        if (_closing) return;
        _closing = true;
        IsEnabled = false;
        try { await _model.DisposeAsync(); }
        finally
        {
            _allowClose = true;
            Close();
        }
    }
}
