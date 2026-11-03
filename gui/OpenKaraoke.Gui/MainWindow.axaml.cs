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
