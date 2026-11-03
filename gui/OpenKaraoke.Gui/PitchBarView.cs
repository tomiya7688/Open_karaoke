using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;
using OpenKaraoke.Client;

namespace OpenKaraoke.Gui;

/// {
///   責務: [PitchBarView: ViewModelが計算した音程バーと現在位置の線を描く]
///   フィールド: [
///     Bars: 0..1の比率で表した音程バー,
///     PlayheadX: 現在位置の線の横位置(比率),
///     BarBrush / ActiveBrush / PlayheadBrush / TrackBrush: 描画色(テーマのリソースを渡す)
///   ]
///   補足: [時刻や音高の計算はKaraokeViewModelが持つ。ここは描画だけで、音声処理を待たない]
/// }
public sealed class PitchBarView : Control
{
    public static readonly StyledProperty<IReadOnlyList<PitchBar>?> BarsProperty =
        AvaloniaProperty.Register<PitchBarView, IReadOnlyList<PitchBar>?>(nameof(Bars));
    public static readonly StyledProperty<double> PlayheadXProperty =
        AvaloniaProperty.Register<PitchBarView, double>(nameof(PlayheadX));
    public static readonly StyledProperty<IBrush?> BarBrushProperty =
        AvaloniaProperty.Register<PitchBarView, IBrush?>(nameof(BarBrush));
    public static readonly StyledProperty<IBrush?> ActiveBrushProperty =
        AvaloniaProperty.Register<PitchBarView, IBrush?>(nameof(ActiveBrush));
    public static readonly StyledProperty<IBrush?> PlayheadBrushProperty =
        AvaloniaProperty.Register<PitchBarView, IBrush?>(nameof(PlayheadBrush));
    public static readonly StyledProperty<IBrush?> TrackBrushProperty =
        AvaloniaProperty.Register<PitchBarView, IBrush?>(nameof(TrackBrush));

    static PitchBarView()
    {
        AffectsRender<PitchBarView>(
            BarsProperty, PlayheadXProperty, BarBrushProperty, ActiveBrushProperty, PlayheadBrushProperty, TrackBrushProperty);
    }

    public IReadOnlyList<PitchBar>? Bars { get => GetValue(BarsProperty); set => SetValue(BarsProperty, value); }
    public double PlayheadX { get => GetValue(PlayheadXProperty); set => SetValue(PlayheadXProperty, value); }
    public IBrush? BarBrush { get => GetValue(BarBrushProperty); set => SetValue(BarBrushProperty, value); }
    public IBrush? ActiveBrush { get => GetValue(ActiveBrushProperty); set => SetValue(ActiveBrushProperty, value); }
    public IBrush? PlayheadBrush { get => GetValue(PlayheadBrushProperty); set => SetValue(PlayheadBrushProperty, value); }
    public IBrush? TrackBrush { get => GetValue(TrackBrushProperty); set => SetValue(TrackBrushProperty, value); }

    /// {
    ///   責務: [Render: 背景・音程バー・現在位置の線を描く]
    ///   処理: [
    ///     1: 背景を角丸で塗る,
    ///     2: 描画領域の内側へ切り取り、各バーを比率から実座標へ写して描く,
    ///     3: 現在位置の縦線を描く
    ///   ]
    ///   引数: [context: 描画先]
    ///   戻り値: []
    /// }
    public override void Render(DrawingContext context)
    {
        var bounds = new Rect(Bounds.Size);
        if (bounds.Width <= 0 || bounds.Height <= 0) return;
        context.DrawRectangle(TrackBrush, null, bounds, 8, 8);
        using (context.PushClip(bounds))
        {
            foreach (var bar in Bars ?? [])
            {
                // 細すぎる音符も見えるよう、幅と高さに最小値を設ける。
                var rect = new Rect(
                    bar.X * bounds.Width,
                    bar.Y * bounds.Height + 1,
                    Math.Max(bar.Width * bounds.Width, 3),
                    Math.Max(bar.Height * bounds.Height - 2, 3));
                context.DrawRectangle(bar.IsActive ? ActiveBrush : BarBrush, null, rect, 3, 3);
            }
        }
        if (PlayheadBrush is not null)
        {
            var x = Math.Round(PlayheadX * bounds.Width) + 0.5;
            context.DrawLine(new Pen(PlayheadBrush, 2), new Point(x, 0), new Point(x, bounds.Height));
        }
    }
}
