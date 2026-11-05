# Gera o dist\Siteblock-Instalador.exe: o launcher.cs com o siteblock.py, o instalar.cmd e o logo dentro.
# Usa o compilador de C# que vem com o Windows, entao nao precisa instalar nada.
#
#   powershell -ExecutionPolicy Bypass -File windows-installer\build.ps1 -Version 1.2.3
#
# As mensagens deste arquivo nao tem acentos: o Windows PowerShell le arquivos .ps1 sem BOM fora do UTF-8.
param([string]$Version = "0.0.0")

$ErrorActionPreference = "Stop"
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Use -Version no formato 1.2.3 (recebi '$Version')."
}

$root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $root "dist"
$work = Join-Path $dist "obj"
$exe = Join-Path $dist "Siteblock-Instalador.exe"
$logo = Join-Path $root "logo-block-site.jpg"
$icon = Join-Path $work "siteblock.ico"
$versionFile = Join-Path $work "version.cs"
New-Item -ItemType Directory -Force $work | Out-Null

# O icone do .exe: o escudo do logo, recortado em quadrado, nos tamanhos que o Windows usa.
Add-Type -AssemblyName System.Drawing
$image = [System.Drawing.Image]::FromFile($logo)
$crop = New-Object System.Drawing.Rectangle ([int]($image.Width * 0.215)), ([int]($image.Height * 0.125)), ([int]($image.Width * 0.58)), ([int]($image.Width * 0.58))
$sizes = 16, 32, 48, 256
$pngs = foreach ($size in $sizes) {
    $bitmap = New-Object System.Drawing.Bitmap $size, $size
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.DrawImage($image, (New-Object System.Drawing.Rectangle 0, 0, $size, $size), $crop, [System.Drawing.GraphicsUnit]::Pixel)
    $stream = New-Object System.IO.MemoryStream
    $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose()
    $bitmap.Dispose()
    , $stream.ToArray()
}
$image.Dispose()

# Um .ico e um cabecalho, uma entrada de 16 bytes por tamanho e, em seguida, as imagens (aqui, em PNG).
$writer = New-Object System.IO.BinaryWriter ([System.IO.File]::Create($icon))
foreach ($field in 0, 1, $sizes.Count) { $writer.Write([uint16]$field) }
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $side = [byte]($sizes[$i] % 256)  # 256 e gravado como 0
    foreach ($field in $side, $side, 0, 0) { $writer.Write([byte]$field) }
    foreach ($field in 1, 32) { $writer.Write([uint16]$field) }
    foreach ($field in $pngs[$i].Length, $offset) { $writer.Write([uint32]$field) }
    $offset += $pngs[$i].Length
}
foreach ($png in $pngs) { $writer.Write([byte[]]$png) }
$writer.Close()

# A versao aparece nas propriedades do .exe.
Set-Content -Path $versionFile -Encoding ASCII -Value @"
using System.Reflection;
[assembly: AssemblyVersion("$Version.0")]
[assembly: AssemblyFileVersion("$Version.0")]
[assembly: AssemblyInformationalVersion("$Version")]
"@

$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe" }

$arguments = @(
    "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+", "/codepage:65001",
    "/out:$exe", "/win32icon:$icon", "/reference:System.Windows.Forms.dll"
)
# Cada arquivo embutido e gravado pelo instalador com o nome que vem depois da virgula.
foreach ($file in (Join-Path $root "siteblock.py"), (Join-Path $PSScriptRoot "instalar.cmd"), $logo) {
    $arguments += "/resource:$file,$(Split-Path -Leaf $file)"
}
& $csc @arguments (Join-Path $PSScriptRoot "launcher.cs") $versionFile
if ($LASTEXITCODE -ne 0) { throw "O compilador de C# terminou com o codigo $LASTEXITCODE." }

Remove-Item -Recurse -Force $work
Write-Host "Pronto: $exe (versao $Version)"
