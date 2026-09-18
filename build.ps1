param(
    [switch]$SkipSync
)

$ErrorActionPreference = "Stop"
$Root = [System.IO.Path]::GetFullPath($PSScriptRoot)
Set-Location $Root

$CacheRoot = Join-Path $Root ".cache"
New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null
$env:UV_CACHE_DIR = Join-Path $CacheRoot "uv"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv не найден. Установите его: https://docs.astral.sh/uv/getting-started/installation/"
}

if (-not $SkipSync) {
    Write-Host "[1/7] Синхронизирую зависимости из uv.lock..."
    uv sync --locked --no-default-groups --group build
    if ($LASTEXITCODE -ne 0) { throw "uv sync завершился с ошибкой ($LASTEXITCODE)" }
} else {
    Write-Host "[1/7] Синхронизация зависимостей пропущена."
}

$VersionLine = Select-String -Path (Join-Path $Root "pyproject.toml") -Pattern '^version = "(.+)"$'
$Version = $VersionLine.Matches[0].Groups[1].Value
try {
    $ExactTag = git describe --tags --exact-match HEAD 2>$null
    if ($ExactTag) { $Version = $ExactTag.TrimStart("v") }
} catch {}
$VersionParts = (($Version -split '\.') + @("0", "0", "0", "0"))[0..3]
$FileVersion = $VersionParts -join "."

$AppName = "GetCourseVideoDownloader"
$Resources = Join-Path $Root "resources"
New-Item -ItemType Directory -Force -Path $Resources | Out-Null

Write-Host "[2/7] Проверяю FFmpeg..."
$FfmpegExe = Join-Path $Resources "ffmpeg.exe"
$FfprobeExe = Join-Path $Resources "ffprobe.exe"
if (-not (Test-Path -LiteralPath $FfmpegExe) -or -not (Test-Path -LiteralPath $FfprobeExe)) {
    $TempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $Extract = Join-Path $TempRoot ("gcd-ffmpeg-" + [guid]::NewGuid().ToString("N"))
    $Archive = Join-Path $TempRoot ("gcd-ffmpeg-" + [guid]::NewGuid().ToString("N") + ".zip")
    New-Item -ItemType Directory -Path $Extract | Out-Null
    try {
        Write-Host "  Скачиваю ffmpeg-release-essentials..."
        curl.exe -L --fail -o $Archive "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        if ($LASTEXITCODE -ne 0) { throw "Не удалось скачать FFmpeg" }
        Expand-Archive -LiteralPath $Archive -DestinationPath $Extract -Force
        $FoundFfmpeg = Get-ChildItem -LiteralPath $Extract -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
        $FoundFfprobe = Get-ChildItem -LiteralPath $Extract -Recurse -Filter "ffprobe.exe" | Select-Object -First 1
        if (-not $FoundFfmpeg -or -not $FoundFfprobe) {
            throw "ffmpeg.exe или ffprobe.exe отсутствует в загруженном архиве"
        }
        Copy-Item -LiteralPath $FoundFfmpeg.FullName -Destination $FfmpegExe -Force
        Copy-Item -LiteralPath $FoundFfprobe.FullName -Destination $FfprobeExe -Force
    } finally {
        if (Test-Path -LiteralPath $Archive) { Remove-Item -LiteralPath $Archive -Force }
        $ResolvedExtract = [System.IO.Path]::GetFullPath($Extract)
        if ($ResolvedExtract.StartsWith($TempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $ResolvedExtract -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "[3/7] Проверяю Firefox для Playwright..."
$PlaywrightSource = Join-Path $env:LOCALAPPDATA "ms-playwright"
uv run --no-sync playwright install firefox
if ($LASTEXITCODE -ne 0) { throw "Playwright не смог установить Firefox" }

Write-Host "[4/7] Копирую браузер в resources..."
$PlaywrightDestination = Join-Path $Resources "ms-playwright"
New-Item -ItemType Directory -Force -Path $PlaywrightDestination | Out-Null
Get-ChildItem -LiteralPath $PlaywrightSource -Directory -Filter "firefox-*" | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $PlaywrightDestination -Recurse -Force
}

Write-Host "[5/7] Собираю Windows-приложение..."
uv run --no-sync flet pack main.py -y -D -n $AppName `
    --product-name "GetCourse Video Downloader" `
    --file-description "Загрузка видео с GetCourse" `
    --company-name "GetCourseVideoDownloader" `
    --product-version $Version `
    --file-version $FileVersion
if ($LASTEXITCODE -ne 0) { throw "flet pack завершился с ошибкой ($LASTEXITCODE)" }

$DistApp = Join-Path $Root "dist\$AppName"
Copy-Item -LiteralPath $Resources -Destination $DistApp -Recurse -Force

Write-Host "[6/7] Проверяю worker внутри EXE..."
$Executable = Join-Path $DistApp "$AppName.exe"
$WorkerSmokeTest = Start-Process -FilePath $Executable `
    -ArgumentList "--download-worker", "--help" `
    -Wait -PassThru -WindowStyle Hidden
if ($WorkerSmokeTest.ExitCode -ne 0) {
    throw "Собранный EXE не прошёл worker smoke-test ($($WorkerSmokeTest.ExitCode))"
}

Write-Host "[7/7] Создаю архив и SHA-256..."
$ZipPath = Join-Path $Root "dist\$AppName-win-x64.zip"
if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
Compress-Archive -LiteralPath $DistApp -DestinationPath $ZipPath -CompressionLevel Optimal
$Hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$ZipPath.sha256" -Value "$Hash  $([System.IO.Path]::GetFileName($ZipPath))" -Encoding ascii
uv export --preview-features sbom-export --locked --no-default-groups --format cyclonedx1.5 --output-file (Join-Path $Root "dist\sbom.cdx.json") | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Не удалось сформировать SBOM" }

Write-Host ""
Write-Host "Готово: $ZipPath"
Write-Host "SHA-256: $Hash"
Write-Host "SBOM: $(Join-Path $Root 'dist\sbom.cdx.json')"
