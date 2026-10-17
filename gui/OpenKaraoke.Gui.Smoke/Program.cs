using System.Diagnostics;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Interactivity;
using Avalonia.Threading;
using Avalonia.VisualTree;
using OpenKaraoke.Client;
using OpenKaraoke.Gui;

namespace OpenKaraoke.Gui.Smoke;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length != 3)
        {
            Console.Error.WriteLine("Pass output directory, absolute Core path and absolute Python path.");
            return 2;
        }
        var output = Path.GetFullPath(args[0]);
        var root = Path.Combine(Path.GetTempPath(), "open-karaoke-ui-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(output);
        Directory.CreateDirectory(root);
        MainWindow? window = null;
        try
        {
            AppBuilder.Configure<App>()
                .UseSkia()
                .UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false })
                .SetupWithoutStarting();
            Check(SynchronizationContext.Current is AvaloniaSynchronizationContext, "Missing UI synchronization context.");
            window = new MainWindow();
            window.Show();
            Flush();
            var model = (ShellViewModel)(window.DataContext ?? throw new InvalidOperationException("Missing ViewModel."));
            var connect = Button(window, "Coreに接続");
            var start = Button(window, "接続テストJobを開始");
            var cancel = Button(window, "キャンセル");
            Check(connect.IsEnabled && !start.IsEnabled && !cancel.IsEnabled, "Initial button bindings failed.");
            Save(window, output, "01-initial.png");

            var tabs = window.GetVisualDescendants().OfType<TabControl>().Single();
            tabs.SelectedIndex = 1;
            Flush();
            var textBoxes = window.GetVisualDescendants().OfType<TextBox>().Where(box => box.IsVisible).ToArray();
            Check(textBoxes.Length == 3, "Settings navigation failed.");
            var nonexistent = Path.Combine(root, "missing-core");
            textBoxes[0].Text = nonexistent;
            Flush();
            Check(model.CoreExecutable == nonexistent, "Settings two-way binding failed.");
            Click(connect);
            PumpUntil(() => model.CanConnect && model.Error.Length > 0, "Missing executable error");
            Check(!model.CanRun, "Failed startup enabled job execution.");
            window.Width = 780;
            window.Height = 580;
            Save(window, output, "02-settings-error-compact.png");

            model.CoreExecutable = Path.GetFullPath(args[1]);
            model.PythonExecutable = Path.GetFullPath(args[2]);
            model.DataDirectory = Path.Combine(root, "data");
            tabs.SelectedIndex = 0;
            window.Width = 1024;
            window.Height = 720;
            Flush();
            Click(connect);
            PumpUntil(() => model.CanRun || (model.CanConnect && model.Error.Length > 0), "Real Core startup");
            Check(model.CanRun && model.Error.Length == 0, "Real GUI connection failed: " + model.Error);
            Check(start.IsEnabled && !connect.IsEnabled, "Connected button bindings failed.");
            Save(window, output, "03-connected.png");

            Click(start);
            PumpUntil(() => model.CurrentJob is { Status: "running" } || model.Error.Length > 0, "Running job");
            Check(model.CurrentJob?.Status == "running" && cancel.IsEnabled, "Progress/cancel bindings failed.");
            Save(window, output, "04-running.png");
            PumpUntil(() => model.CurrentJob?.IsTerminal == true || model.Error.Length > 0, "Completed job");
            Check(model.CurrentJob?.Status == "completed" && model.Progress == 100, "GUI did not show completion: " + model.Error);
            Save(window, output, "05-completed.png");

            PumpUntil(() => start.IsEnabled, "Re-enable start");
            Click(start);
            PumpUntil(() => cancel.IsEnabled, "Cancelable job");
            Click(cancel);
            PumpUntil(() => model.CurrentJob?.IsTerminal == true || model.Error.Length > 0, "Cancelled job");
            Check(model.CurrentJob?.Status == "cancelled", "GUI cancellation did not reach the worker.");
            Save(window, output, "06-cancelled.png");
            window.Close();
            PumpUntil(() => !window.IsVisible, "Owned Core shutdown");
            var runtime = Path.Combine(root, "data", "artifacts", ".runtime");
            Check(!Directory.Exists(runtime) || !Directory.EnumerateFileSystemEntries(runtime).Any(), "GUI close left a worker runtime.");
            Console.WriteLine("PASS: actual XAML, settings/error, authenticated Core connection, job progress/completion/cancellation, shutdown and six rendered frames.");
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            return 1;
        }
        finally
        {
            if (window?.IsVisible == true)
            {
                window.Close();
                PumpUntil(() => !window.IsVisible, "Cleanup");
            }
            Directory.Delete(root, recursive: true);
        }
    }

    private static Button Button(Window window, string content) => window.GetVisualDescendants()
        .OfType<Button>().Single(button => Equals(button.Content, content));

    private static void Click(Button button)
    {
        Check(button.IsEnabled, "Cannot click a disabled control.");
        button.RaiseEvent(new RoutedEventArgs(Avalonia.Controls.Button.ClickEvent));
        Flush();
    }

    private static void Flush()
    {
        Dispatcher.UIThread.RunJobs();
        AvaloniaHeadlessPlatform.ForceRenderTimerTick();
    }

    private static void PumpUntil(Func<bool> ready, string stage)
    {
        var deadline = Stopwatch.StartNew();
        while (!ready())
        {
            Flush();
            if (deadline.Elapsed > TimeSpan.FromSeconds(60)) throw new TimeoutException(stage);
            Thread.Sleep(10);
        }
        Flush();
    }

    private static void Save(Window window, string output, string name)
    {
        Flush();
        using var frame = window.CaptureRenderedFrame() ?? throw new InvalidOperationException("No rendered frame.");
        Check(frame.PixelSize.Width >= 780 && frame.PixelSize.Height >= 580, "Unexpected rendered dimensions.");
        frame.Save(Path.Combine(output, name));
    }

    private static void Check(bool value, string reason)
    {
        if (!value) throw new InvalidOperationException(reason);
    }
}
