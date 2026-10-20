[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) {
    throw 'The Windows .NET Framework C# compiler was not found. Enable .NET Framework 4.x before building.'
}

Add-Type -AssemblyName System.Drawing
$assets = Join-Path $PSScriptRoot 'assets'
New-Item -ItemType Directory -Path $assets -Force | Out-Null
$iconPath = Join-Path $assets 'telemetry.ico'
$images = New-Object 'System.Collections.Generic.List[byte[]]'
$sizes = @(16, 32, 48, 64, 128, 256)
foreach ($size in $sizes) {
    $bitmap = New-Object Drawing.Bitmap($size, $size)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    $path = New-Object Drawing.Drawing2D.GraphicsPath
    $background = New-Object Drawing.SolidBrush([Drawing.ColorTranslator]::FromHtml('#101820'))
    $border = New-Object Drawing.Pen([Drawing.ColorTranslator]::FromHtml('#3d5346'), 3)
    $grid = New-Object Drawing.Pen([Drawing.Color]::FromArgb(35, 109, 229, 220), 1)
    $glow = New-Object Drawing.Pen([Drawing.Color]::FromArgb(40, 181, 243, 107), 20)
    $line = New-Object Drawing.Pen([Drawing.ColorTranslator]::FromHtml('#b5f36b'), 9)
    $dot = New-Object Drawing.SolidBrush([Drawing.ColorTranslator]::FromHtml('#6de5dc'))
    $stream = New-Object IO.MemoryStream
    try {
        $graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.ScaleTransform($size / 256.0, $size / 256.0)
        $path.AddArc(8, 8, 56, 56, 180, 90)
        $path.AddArc(192, 8, 56, 56, 270, 90)
        $path.AddArc(192, 192, 56, 56, 0, 90)
        $path.AddArc(8, 192, 56, 56, 90, 90)
        $path.CloseFigure()
        $graphics.FillPath($background, $path)
        $graphics.DrawPath($border, $path)
        foreach ($position in @(64, 106, 148, 190)) {
            $graphics.DrawLine($grid, 32, $position, 224, $position)
            $graphics.DrawLine($grid, $position, 32, $position, 224)
        }
        $points = [Drawing.PointF[]]@(
            (New-Object Drawing.PointF(37, 145)),
            (New-Object Drawing.PointF(75, 145)),
            (New-Object Drawing.PointF(104, 82)),
            (New-Object Drawing.PointF(140, 183)),
            (New-Object Drawing.PointF(169, 119)),
            (New-Object Drawing.PointF(216, 119))
        )
        foreach ($pen in @($glow, $line)) {
            $pen.LineJoin = [Drawing.Drawing2D.LineJoin]::Round
            $pen.StartCap = [Drawing.Drawing2D.LineCap]::Round
            $pen.EndCap = [Drawing.Drawing2D.LineCap]::Round
            $graphics.DrawLines($pen, $points)
        }
        $graphics.FillEllipse($dot, 208, 111, 16, 16)
        $bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
        $images.Add($stream.ToArray())
    } finally {
        foreach ($resource in @($stream, $dot, $line, $glow, $grid, $border, $background, $path, $graphics, $bitmap)) {
            $resource.Dispose()
        }
    }
}

# Windows ICO files can hold PNG images for crisp rendering at multiple DPI scales.
$iconStream = [IO.File]::Create($iconPath)
$writer = New-Object IO.BinaryWriter($iconStream)
try {
    $writer.Write([uint16]0)
    $writer.Write([uint16]1)
    $writer.Write([uint16]$sizes.Count)
    $offset = 6 + 16 * $sizes.Count
    for ($index = 0; $index -lt $sizes.Count; $index++) {
        $dimension = $sizes[$index] % 256
        $writer.Write([byte]$dimension)
        $writer.Write([byte]$dimension)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]32)
        $writer.Write([uint32]$images[$index].Length)
        $writer.Write([uint32]$offset)
        $offset += $images[$index].Length
    }
    foreach ($image in $images) { $writer.Write([byte[]]$image) }
} finally {
    $writer.Dispose()
    $iconStream.Dispose()
}

$executable = Join-Path $PSScriptRoot 'Telemetry.exe'
$source = Join-Path $PSScriptRoot 'app\Launcher.cs'
& $compiler /nologo /target:winexe /optimize+ /platform:anycpu `
    "/win32icon:$iconPath" "/out:$executable" /reference:System.Windows.Forms.dll $source
if ($LASTEXITCODE -ne 0) { throw "App compilation failed with exit code $LASTEXITCODE." }
Write-Host "Built app: $executable"
Write-Host "Built icon: $iconPath"
