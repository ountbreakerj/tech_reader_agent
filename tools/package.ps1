[CmdletBinding()]
param(
  [string]$OutputPath = '',
  [switch]$SkipValidation
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCommand) {
  throw '打包需要 Node.js。请先安装 Node.js 20+，或在已有构建产物的电脑上直接复制项目目录。'
}

function Invoke-Node([string[]]$Arguments, [string]$Label) {
  Push-Location -LiteralPath $projectRoot
  try {
    & $nodeCommand.Source @Arguments
    if ($LASTEXITCODE -ne 0) {
      throw "$Label 失败，退出码: $LASTEXITCODE"
    }
  } finally {
    Pop-Location
  }
}

if (-not $SkipValidation) {
  Invoke-Node @('build.mjs') '构建'
  Invoke-Node @('verify.mjs') '结构验证'
  Invoke-Node @('tools/assets.mjs', 'audit') '图片审计'
}

$packageJson = Get-Content -Raw -LiteralPath (Join-Path $projectRoot 'package.json') | ConvertFrom-Json
$version = [string]$packageJson.version
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
  $stamp = Get-Date -Format 'yyyyMMdd-HHmm'
  $OutputPath = Join-Path (Split-Path -Parent $projectRoot) "tech-reader-modular-$version-$stamp.zip"
} else {
  $OutputPath = [IO.Path]::GetFullPath($OutputPath)
}

$stageParent = Join-Path ([IO.Path]::GetTempPath()) ("tech-reader-package-" + [guid]::NewGuid().ToString('N'))
$stageRoot = Join-Path $stageParent 'tech-reader-modular'
New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null

try {
  $files = Get-ChildItem -LiteralPath $projectRoot -Recurse -File -Force | Where-Object {
    $relative = $_.FullName.Substring($projectRoot.Length + 1).Replace('\', '/')
    $relative -notmatch '^(?:\.git|\.codegraph|node_modules)(?:/|$)' -and
      $relative -notmatch '^reports/visual/latest(?:/|$)' -and
      $relative -notmatch '^release(?:/|$)'
  }
  foreach ($file in $files) {
    $relative = $file.FullName.Substring($projectRoot.Length + 1)
    $destination = Join-Path $stageRoot $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
  }

  $manifest = [ordered]@{
    package = 'tech-reader-modular'
    version = $version
    generatedAt = (Get-Date).ToUniversalTime().ToString('o')
    buildSha256 = (Get-FileHash -LiteralPath (Join-Path $projectRoot 'dist\tech-reader.html') -Algorithm SHA256).Hash.ToLowerInvariant()
    entrypoint = '一键部署.cmd'
    excluded = @('.git', '.codegraph', 'node_modules', 'reports/visual/latest', 'release')
  }
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [IO.File]::WriteAllText(
    (Join-Path $stageRoot 'PACKAGE-MANIFEST.json'),
    (($manifest | ConvertTo-Json -Depth 5) + "`n"),
    $utf8NoBom
  )

  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
  Compress-Archive -LiteralPath $stageRoot -DestinationPath $OutputPath -CompressionLevel Optimal -Force
  $size = (Get-Item -LiteralPath $OutputPath).Length
  Write-Host "打包完成: $OutputPath" -ForegroundColor Green
  Write-Host "压缩包大小: $size 字节"
} finally {
  if (Test-Path -LiteralPath $stageParent) {
    Remove-Item -LiteralPath $stageParent -Recurse -Force
  }
}
