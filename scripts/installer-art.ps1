param([Parameter(Mandatory = $true)][string]$OutputDir)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

function New-Logo([int]$Size) {
    $bitmap = [System.Drawing.Bitmap]::new($Size, $Size)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = "AntiAlias"
    $graphics.TextRenderingHint = "AntiAliasGridFit"
    $gradient = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
        [System.Drawing.Rectangle]::new(0, 0, $Size, $Size),
        [System.Drawing.ColorTranslator]::FromHtml("#2463a0"),
        [System.Drawing.ColorTranslator]::FromHtml("#12314f"), 45.0)
    $font = [System.Drawing.Font]::new("Segoe UI", ($Size * 0.72), [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
    $format = [System.Drawing.StringFormat]::new()
    $format.Alignment = "Center"
    $format.LineAlignment = "Center"
    try {
        $graphics.FillRectangle($gradient, 0, 0, $Size, $Size)
        $graphics.DrawString("V", $font, [System.Drawing.Brushes]::White,
            [System.Drawing.RectangleF]::new(0, -($Size * 0.04), $Size, $Size), $format)
    } finally {
        $format.Dispose(); $font.Dispose(); $gradient.Dispose(); $graphics.Dispose()
    }
    return $bitmap
}

$small = New-Logo 64
try { $small.Save((Join-Path $OutputDir "small.png"), [System.Drawing.Imaging.ImageFormat]::Png) }
finally { $small.Dispose() }

$panel = [System.Drawing.Bitmap]::new(240, 600)
$graphics = [System.Drawing.Graphics]::FromImage($panel)
$graphics.SmoothingMode = "AntiAlias"
$graphics.TextRenderingHint = "AntiAliasGridFit"
$gradient = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
    [System.Drawing.Rectangle]::new(0, 0, 240, 600),
    [System.Drawing.ColorTranslator]::FromHtml("#2463a0"),
    [System.Drawing.ColorTranslator]::FromHtml("#0c2035"), 90.0)
$title = [System.Drawing.Font]::new("Segoe UI", 27, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
$subtitle = [System.Drawing.Font]::new("Segoe UI", 15, [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
$accent = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml("#72c7ed"))
$logo = New-Logo 136
try {
    $graphics.FillRectangle($gradient, 0, 0, 240, 600)
    $graphics.DrawImage($logo, 52, 80, 136, 136)
    $graphics.DrawString("VoxTypeX", $title, [System.Drawing.Brushes]::White, 55, 245)
    $graphics.DrawString("Голосовой ввод", $subtitle, [System.Drawing.Brushes]::White, 61, 288)
    $heights = @(18, 32, 52, 76, 108, 76, 52, 32, 18)
    for ($index = 0; $index -lt $heights.Length; $index++) {
        $height = $heights[$index]
        $graphics.FillRectangle($accent, (52 + $index * 16), (410 - $height / 2), 7, $height)
    }
    $panel.Save((Join-Path $OutputDir "wizard.png"), [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
    $logo.Dispose(); $accent.Dispose(); $title.Dispose(); $subtitle.Dispose()
    $gradient.Dispose(); $graphics.Dispose(); $panel.Dispose()
}

$images = foreach ($size in @(16, 32, 48, 256)) {
    $bitmap = New-Logo $size
    $stream = [System.IO.MemoryStream]::new()
    try {
        $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
        [pscustomobject]@{ Size = $size; Bytes = $stream.ToArray() }
    } finally { $stream.Dispose(); $bitmap.Dispose() }
}
$file = [System.IO.File]::Create((Join-Path $OutputDir "VoxTypeX.ico"))
$writer = [System.IO.BinaryWriter]::new($file)
try {
    $writer.Write([uint16]0); $writer.Write([uint16]1); $writer.Write([uint16]$images.Count)
    $offset = 6 + 16 * $images.Count
    foreach ($image in $images) {
        $dimension = if ($image.Size -eq 256) { 0 } else { $image.Size }
        $writer.Write([byte]$dimension); $writer.Write([byte]$dimension)
        $writer.Write([byte]0); $writer.Write([byte]0)
        $writer.Write([uint16]1); $writer.Write([uint16]32)
        $writer.Write([uint32]$image.Bytes.Length); $writer.Write([uint32]$offset)
        $offset += $image.Bytes.Length
    }
    foreach ($image in $images) { $writer.Write([byte[]]$image.Bytes) }
} finally { $writer.Dispose() }
