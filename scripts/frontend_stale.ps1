# Exit 1 if the frontend needs a rebuild, 0 if backend\static is up to date.
# A rebuild is needed when any source file is newer than the built bundle.
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot

$out = Join-Path $root 'backend\static\index.html'
$outTime = if (Test-Path $out) { (Get-Item $out).LastWriteTime } else { [datetime]::MinValue }

$newest = $null
$srcDirs  = @((Join-Path $root 'frontend\src'))
$srcFiles = @(
  (Join-Path $root 'frontend\index.html'),
  (Join-Path $root 'frontend\vite.config.ts'),
  (Join-Path $root 'frontend\package.json'),
  (Join-Path $root 'frontend\tsconfig.json')
)

foreach ($d in $srcDirs) {
  Get-ChildItem -Recurse -File -Path $d | ForEach-Object {
    if (-not $newest -or $_.LastWriteTime -gt $newest.LastWriteTime) { $newest = $_ }
  }
}
foreach ($f in $srcFiles) {
  if (Test-Path $f) {
    $fi = Get-Item $f
    if (-not $newest -or $fi.LastWriteTime -gt $newest.LastWriteTime) { $newest = $fi }
  }
}

if ($newest -and $newest.LastWriteTime -gt $outTime) { exit 1 } else { exit 0 }
