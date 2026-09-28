[CmdletBinding()]
param(
  [string]$ProjectRoot = '',
  [switch]$Build,
  [switch]$NoOpen,
  [switch]$SkipAgent
)

$ErrorActionPreference = 'Stop'

function Resolve-Directory([string]$PathValue) {
  if ([string]::IsNullOrWhiteSpace($PathValue)) {
    return (Resolve-Path -LiteralPath $PSScriptRoot).Path
  }
  if (-not (Test-Path -LiteralPath $PathValue -PathType Container)) {
    throw "项目目录不存在: $PathValue"
  }
  return (Resolve-Path -LiteralPath $PathValue).Path
}

function Invoke-NodeScript([string[]]$Arguments, [string]$Label) {
  Push-Location -LiteralPath $script:ResolvedProjectRoot
  try {
    & $script:NodePath @Arguments
    if ($LASTEXITCODE -ne 0) {
      throw "$Label 失败，退出码: $LASTEXITCODE"
    }
  } finally {
    Pop-Location
  }
}

$ResolvedProjectRoot = Resolve-Directory $ProjectRoot
$requiredFiles = @(
  'package.json',
  'build.mjs',
  'verify.mjs',
  'dist\tech-reader.html',
  'packaging\codex-skill\SKILL.md.template',
  'packaging\codex-skill\agents\openai.yaml'
)
foreach ($relativePath in $requiredFiles) {
  $absolutePath = Join-Path $ResolvedProjectRoot $relativePath
  if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) {
    throw "部署包缺少文件: $relativePath"
  }
}

$NodePath = $null
$NodeVersion = $null
$NodeMajor = 0
try {
  $nodeCommand = Get-Command node -ErrorAction Stop
  $NodePath = $nodeCommand.Source
  $NodeVersion = (& $NodePath --version 2>$null | Select-Object -First 1).ToString().Trim()
  if ($NodeVersion -match '^v(\d+)') {
    $NodeMajor = [int]$Matches[1]
  }
} catch {
  # 阅读器是单文件离线产物，未安装 Node 不影响打开它。
}

if ($Build) {
  if (-not $NodePath -or $NodeMajor -lt 20) {
    throw '指定了 -Build，但未找到 Node.js 20 或更高版本。请先安装 Node.js 20+，或去掉 -Build 直接打开已打包的阅读器。'
  }
  Invoke-NodeScript @('build.mjs') '构建'
  Invoke-NodeScript @('verify.mjs') '结构验证'
  Write-Host '已完成构建和结构验证。' -ForegroundColor Green
} elseif ($NodePath -and $NodeMajor -ge 20) {
  Write-Host "检测到 Node.js $NodeVersion；如需从源码重建，请运行: 一键部署.cmd -Build" -ForegroundColor DarkGray
} else {
  Write-Host '未检测到 Node.js 20+；将直接使用随包提供的离线阅读器。' -ForegroundColor Yellow
}

if (-not $SkipAgent) {
  $templatePath = Join-Path $ResolvedProjectRoot 'packaging\codex-skill\SKILL.md.template'
  $metadataPath = Join-Path $ResolvedProjectRoot 'packaging\codex-skill\agents\openai.yaml'
  $skillRoot = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\skills\tech-reader-agent'
  $skillAgentsRoot = Join-Path $skillRoot 'agents'
  New-Item -ItemType Directory -Force -Path $skillAgentsRoot | Out-Null

  $skillContent = [IO.File]::ReadAllText($templatePath)
  $skillContent = $skillContent.Replace('{{PROJECT_ROOT}}', $ResolvedProjectRoot)
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [IO.File]::WriteAllText((Join-Path $skillRoot 'SKILL.md'), $skillContent, $utf8NoBom)
  Copy-Item -LiteralPath $metadataPath -Destination (Join-Path $skillAgentsRoot 'openai.yaml') -Force
  Write-Host "已注册 Codex Agent: $skillRoot" -ForegroundColor Green
}

$outputPath = Join-Path $ResolvedProjectRoot 'dist\tech-reader.html'
$outputHash = (Get-FileHash -LiteralPath $outputPath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "项目目录: $ResolvedProjectRoot"
Write-Host "阅读器哈希: $outputHash"

if (-not $NoOpen) {
  Start-Process -FilePath $outputPath
  Write-Host '已打开离线阅读器。' -ForegroundColor Green
}
