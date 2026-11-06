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
