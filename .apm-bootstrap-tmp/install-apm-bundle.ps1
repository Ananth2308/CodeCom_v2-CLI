<#
.SYNOPSIS
    APM Bundle Installer (Windows)

.DESCRIPTION
    Downloads and installs an APM bundle from the GitLab Generic Package Registry.
    Supports two install modes:
      - standard   — projects runtime-only files (e.g. .github/) for consumers.
      - expandable — extracts full source with manifest-based tracking for
                     customisation.

    Supports modular pack selection via -Pack parameter:
      - full       — install everything (default, backward-compatible)
      - core       — foundation orchestration, knowledge, governance
      - security   — CI gates, PR validation, compliance
      - sdlc       — full SDLC harness (BA, Tech, Steer, Test)
      - delivery   — feature implementation, bug fixing
      - spec       — specification-driven delivery
      - modernization — refactoring & migration lifecycle
      - branding   — brand compliance & document generation
      - tech-react, tech-drupal, tech-dotnet, tech-cloud — technology skills
      - integrations — platform integrations (Jira, ADO, Git, Playwright)

    Multiple packs can be specified as comma-separated values. The 'core' pack
    is always included implicitly.

.PARAMETER Version
    Semantic version to install (required). Example: 1.2.0

.PARAMETER Target
    Target bundle: copilot, claude, all (default: copilot)

.PARAMETER Pack
    Comma-separated pack names to install (default: full).
    Use 'list' to display available packs from the catalog.
    Examples: core,security,delivery  or  full

.PARAMETER Destination
    Destination directory (default: .\.apm-dist)

.PARAMETER Mode
    Install mode: standard (runtime projection only) or expandable (full source
    with local override scaffolding). Default: standard.

.PARAMETER Provider
    Provider adapter key as defined in apm.yml. Default: github-copilot.

.PARAMETER RegistryUrl
    Full Generic Package Registry URL.
    Alternative: provide ProjectId + GitLabUrl.

.PARAMETER ProjectId
    GitLab project ID (used to construct registry URL).

.PARAMETER GitLabUrl
    GitLab instance base URL (default: https://gitlab.com).

.PARAMETER Token
    Private or job token for authentication.
    Falls back to $env:GITLAB_TOKEN.

.PARAMETER NoVerify
    Skip SHA-256 checksum verification.

.PARAMETER Source
    Version resolution source: release (default) or registry.
      - release:  version was resolved from GitLab Releases (stable)
      - registry: version was resolved from git tags / package registry (bleeding-edge)

.EXAMPLE
    .\install-apm-bundle.ps1 -Version 1.2.0 -ProjectId 12345 -Token $env:GITLAB_TOKEN

.EXAMPLE
    .\install-apm-bundle.ps1 -Version 2.0.0 -Target copilot -Mode expandable -ProjectId 12345

.EXAMPLE
    .\install-apm-bundle.ps1 -Version 1.0.0 -Pack "core,security,delivery" -ProjectId 12345

.EXAMPLE
    .\install-apm-bundle.ps1 -Version 1.0.0 -Pack list -ProjectId 12345

.EXAMPLE
    .\install-apm-bundle.ps1 -Version 1.0.0 `
        -RegistryUrl https://gitlab.example.com/api/v4/projects/42/packages/generic
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Version,

    # Repair-only mode: detect and fix a poisoned runtime layout (nested
    # .github/p-*/.github staging dirs, leaked .apm-install-tmp-* folders)
    # left behind by an interrupted or retried install. No download needed.
    [switch]$Repair,

    [ValidateSet('copilot', 'claude', 'all')]
    [string]$Target = 'copilot',

    [string]$Pack = 'full',

    [string]$Destination = '.\.apm-dist',

    [ValidateSet('standard', 'expandable')]
    [string]$Mode = 'standard',

    [string]$Provider = 'github-copilot',

    [string]$RegistryUrl = $env:APM_REGISTRY_URL,

    [string]$ProjectId = $(if ($env:APM_PROJECT_ID) { $env:APM_PROJECT_ID } else { '545119' }),

    [string]$GitLabUrl = $(if ($env:APM_GITLAB_URL) { $env:APM_GITLAB_URL } else { 'https://innersource.soprasteria.com' }),

    [string]$Token = $env:GITLAB_TOKEN,

    [switch]$NoVerify,

    [ValidateSet('release', 'registry')]
    [string]$Source = 'release'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PackageName = 'ssg-ai-backbone'

# --- Helpers ---
function Write-Step  { param([string]$msg) Write-Host "`n── $msg ──" -ForegroundColor Cyan }
function Write-Ok    { param([string]$msg) Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Info  { param([string]$msg) Write-Host "[..] $msg" }
function Write-Err   { param([string]$msg) Write-Host "[!!] $msg" -ForegroundColor Red }

# --- Dot-source lock file helper ---
. (Join-Path $PSScriptRoot 'lib/apm-lock.ps1')

# --- Retry helper: Windows file watchers (VS Code, Copilot indexer, AV) take
# transient locks on runtime files; retry short-lived FS operations instead of
# failing the whole install (root cause of the "file locking issue" reports).
function Invoke-WithRetry {
    param([scriptblock]$Action, [string]$What, [int]$Attempts = 4, [int]$DelayMs = 400)
    for ($i = 1; $i -le $Attempts; $i++) {
        try { & $Action; return $true }
        catch {
            if ($i -eq $Attempts) {
                Write-Err "Failed after ${Attempts} attempts: $What — $($_.Exception.Message)"
                throw
            }
            Start-Sleep -Milliseconds ($DelayMs * $i)
        }
    }
}

# --- Repair helper: heal a poisoned runtime layout ---------------------------
# An interrupted/retried install can leave (a) staging dirs NESTED inside the
# runtime (e.g. .github/p-20260713124723/.github/agents/... — the classic
# Move-Item-into-existing-directory pitfall: providers only discover agents at
# .github/agents/, so everything "looks installed" but is invisible), and
# (b) leaked .apm-install-tmp-*/p-* folders at the repo root.
function Repair-NestedRuntime {
    param([string]$RepoRoot, [string]$RuntimeDirName = '.github')
    $fixed = 0
    $runtime = Join-Path $RepoRoot $RuntimeDirName
    # Staging-directory names seen in the field. Covers our own temp dirs, the
    # Backbone AI Configurator's per-attempt dirs (p-<ts>), and dot-less
    # variants (install-tmp-<ts>) produced by older/third-party installers —
    # a nested staging dir makes the runtime undiscoverable regardless of who
    # created it, so repair must not depend on the exact prefix.
    $stagingPattern = '^(\.?apm-install-tmp-\d{6,14}|install-tmp-\d{6,14}|p-\d{6,14}|\.apm-bootstrap-tmp)$'

    if (Test-Path $runtime) {
        # (a) staging dirs nested inside the runtime, each holding its own runtime subtree
        Get-ChildItem $runtime -Directory | Where-Object { $_.Name -match $stagingPattern } |
            Sort-Object Name | ForEach-Object {
                # The staging dir either wraps a runtime (staging/<runtime>/agents/…)
                # or IS the runtime laid out flat (staging/agents/…). Salvage both
                # shapes — deleting a flat staging dir would destroy the install.
                $stagingRoot = $_.FullName
                $stagingName = $_.Name
                $nested = Join-Path $stagingRoot $RuntimeDirName
                $mergeFrom = $null
                if (Test-Path $nested) {
                    $mergeFrom = $nested
                    Write-Info "Repair: merging nested runtime $stagingName/$RuntimeDirName -> $RuntimeDirName"
                } else {
                    $looksLikeRuntime = @('agents', 'prompts', 'skills', 'workflows', '.apm-manifest.json') |
                        Where-Object { Test-Path (Join-Path $stagingRoot $_) }
                    if ($looksLikeRuntime) {
                        $mergeFrom = $stagingRoot
                        Write-Info "Repair: merging flat staging runtime $stagingName/ -> $RuntimeDirName"
                    }
                }
                if ($mergeFrom) {
                    Get-ChildItem $mergeFrom -File -Recurse | ForEach-Object {
                        $srcFile = $_.FullName
                        $rel = $srcFile.Substring($mergeFrom.Length).TrimStart('\', '/')
                        $dest = Join-Path $runtime $rel
                        $destDir = Split-Path $dest -Parent
                        if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
                        Invoke-WithRetry -What "move $rel" -Action { Move-Item $srcFile -Destination $dest -Force }.GetNewClosure() | Out-Null
                    }
                }
                $stagingDir = $_.FullName
                Invoke-WithRetry -What "remove $($_.Name)" -Action { Remove-Item $stagingDir -Recurse -Force }.GetNewClosure() | Out-Null
                $fixed++
            }
        # Degenerate double-nesting: .github/.github
        $doubled = Join-Path $runtime $RuntimeDirName
        if (Test-Path $doubled) {
            Write-Info "Repair: flattening $RuntimeDirName/$RuntimeDirName"
            Get-ChildItem $doubled -File -Recurse | ForEach-Object {
                $rel = $_.FullName.Substring($doubled.Length).TrimStart('\', '/')
                $dest = Join-Path $runtime $rel
                $destDir = Split-Path $dest -Parent
                if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
                Move-Item $_.FullName -Destination $dest -Force
            }
            Remove-Item $doubled -Recurse -Force
            $fixed++
        }
    }

    # (b) leaked staging folders at the repo root: salvage any runtime subtree, then remove
    Get-ChildItem $RepoRoot -Directory -Force | Where-Object { $_.Name -match $stagingPattern } |
        Sort-Object Name | ForEach-Object {
            $inner = Join-Path $_.FullName $RuntimeDirName
            if ((Test-Path $inner) -and (Test-Path $runtime)) {
                Write-Info "Repair: salvaging runtime files from leaked $($_.Name)"
                Get-ChildItem $inner -File -Recurse | ForEach-Object {
                    $rel = $_.FullName.Substring($inner.Length).TrimStart('\', '/')
                    $dest = Join-Path $runtime $rel
                    if (-not (Test-Path $dest)) {   # never overwrite the live runtime from stale debris
                        $destDir = Split-Path $dest -Parent
                        if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
                        Move-Item $_.FullName -Destination $dest -Force
                    }
                }
            }
            try {
                $leakedDir = $_.FullName
                Invoke-WithRetry -What "remove leaked $($_.Name)" -Action { Remove-Item $leakedDir -Recurse -Force }.GetNewClosure() | Out-Null
                $fixed++
            } catch {
                Write-Err "Could not remove $($_.Name) (still locked) — delete it manually; it is safe to remove"
            }
        }

    return $fixed
}

# --- Consumer .gitignore seeding -------------------------------------------
# APM runtime produces per-repo local artifacts OUTSIDE the runtime dirs
# (knowledge-plane index, interactive audit traces, staging debris, Python
# bytecode). Append one clearly-marked, idempotent block to the consumer's
# root .gitignore so these never show up as pending changes after install.
function Add-ApmGitignoreBlock {
    param([string]$RepoRoot)
    $marker = '# --- APM runtime artifacts (auto-added by install-apm-bundle) ---'
    $gitignorePath = Join-Path $RepoRoot '.gitignore'
    $existing = if (Test-Path $gitignorePath) { Get-Content $gitignorePath -Raw -Encoding UTF8 } else { '' }
    if ($existing -match [regex]::Escape($marker)) { return }
    $block = @(
        ''
        $marker
        '__pycache__/'
        '*.pyc'
        'outputs/knowledge-plane/'
        'outputs/runs/_interactive/'
        '.apm-install-tmp-*/'
        '.apm-bootstrap-tmp/'
        '# --- end APM runtime artifacts ---'
        ''
    ) -join "`n"
    [System.IO.File]::AppendAllText($gitignorePath, $block, (New-Object System.Text.UTF8Encoding $false))
    Write-Ok 'Seeded APM runtime-artifact entries in .gitignore'
}

# --- Repair-only invocation ---
if ($Repair) {
    $repoRootRepair = (Get-Location).Path
    Write-Step "Repairing runtime layout in $repoRootRepair"
    $count = Repair-NestedRuntime -RepoRoot $repoRootRepair
    if ($count -gt 0) { Write-Ok "Repair complete: $count staging artifact(s) healed/removed. Reload your editor window." }
    else { Write-Ok "No poisoned staging artifacts found — layout is clean." }
    exit 0
}

if (-not $Version) {
    Write-Err "-Version is required (or use -Repair to fix a broken install layout)"
    exit 1
}

# --- Strip leading 'v' ---
$Version = $Version -replace '^v', ''

# --- Initialise checksum (may be set later by verification step) ---
$actualHash = ''

# --- Consumer repo root (CWD) used for standard-mode lock/runtime resolution ---
$repoRoot = (Get-Location).Path

# --- Validate semver ---
if ($Version -notmatch '^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?(\+[a-zA-Z0-9.]+)?$') {
    Write-Err "Invalid semver: '$Version'"
    exit 1
}

# --- Resolve registry URL ---
if (-not $RegistryUrl) {
    if (-not $ProjectId) {
        Write-Err 'Provide -RegistryUrl or -ProjectId'
        exit 1
    }
    $RegistryUrl = "$GitLabUrl/api/v4/projects/$ProjectId/packages/generic"
}

# --- Prepare headers ---
$headers = @{}
if ($Token) {
    $headers['PRIVATE-TOKEN'] = $Token
}

# --- Resolve packs ---
$BaseUrl = "$RegistryUrl/$PackageName/$Version"

# Handle 'list' command — fetch and display catalog, then exit
if ($Pack -eq 'list') {
    Write-Step "Available packs ($PackageName v$Version)"
    $catalogUrl = "$BaseUrl/pack-catalog.json"
    try {
        $catalogJson = Invoke-WebRequest -Uri $catalogUrl -Headers $headers -UseBasicParsing
        $catalog = $catalogJson.Content | ConvertFrom-Json
        Write-Host ""
        Write-Host "  Pack                  Description" -ForegroundColor White
        Write-Host "  ----                  -----------"
        foreach ($p in $catalog.packs) {
            $name = $p.name.PadRight(20)
            $deps = if ($p.requires -and $p.requires.Count -gt 0) { " (requires: $($p.requires -join ', '))" } else { "" }
            Write-Host "  $name  $($p.description)$deps"
        }
        Write-Host ""
        Write-Host "  Install: .\install-apm-bundle.ps1 -Version $Version -Pack core,security -ProjectId <id>" -ForegroundColor DarkGray
    }
    catch {
        Write-Err "Could not fetch pack catalog from $catalogUrl"
        Write-Err $_.Exception.Message
    }
    exit 0
}

# Parse pack selection into list — always include 'core' unless installing 'full'
$packList = ($Pack -split ',') | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ }
if ($packList -notcontains 'full' -and $packList -notcontains 'core') {
    $packList = @('core') + $packList
    Write-Info "Auto-added 'core' pack (always required)"
}

# If 'full' is in the list, just install the full bundle (original behavior)
if ($packList -contains 'full') {
    $packList = @('full')
}

# Determine if branding assets are needed (large download — ~52 MB)
$needsBranding = ($packList -contains 'full') -or ($packList -contains 'branding')

# --- Prepare destination ---
Write-Step "Installing $PackageName v$Version (target: $Target, packs: $($packList -join ', '))"
New-Item -ItemType Directory -Path $Destination -Force | Out-Null

# --- Download base archive (always — ~1.6 MB without branding) ---
$ArchiveName = "$PackageName-$Target.tar.gz"
$DownloadUrl = "$BaseUrl/$ArchiveName"
$ArchivePath = Join-Path $Destination $ArchiveName

Write-Info "Downloading base archive: $ArchiveName"
try {
    Invoke-WebRequest -Uri $DownloadUrl -Headers $headers -OutFile $ArchivePath -UseBasicParsing
    Write-Ok "Downloaded: $ArchiveName"
}
catch {
    Write-Err "Download failed: $DownloadUrl"
    Write-Err $_.Exception.Message
    exit 1
}

# --- Verify checksum ---
if (-not $NoVerify) {
    $ChecksumUrl = "$BaseUrl/SHA256SUMS"
    $ChecksumPath = Join-Path $Destination 'SHA256SUMS'

    try {
        Write-Info 'Downloading checksums'
        Invoke-WebRequest -Uri $ChecksumUrl -Headers $headers -OutFile $ChecksumPath -UseBasicParsing
        $checksumLine = Get-Content $ChecksumPath | Where-Object { $_ -match [regex]::Escape($ArchiveName) }
        if ($checksumLine) {
            $expectedHash = ($checksumLine -split '\s+')[0]
            $actualHash = (Get-FileHash -Path $ArchivePath -Algorithm SHA256).Hash.ToLower()
            if ($actualHash -eq $expectedHash) {
                Write-Ok 'Checksum verified'
            }
            else {
                Write-Err "Checksum mismatch: expected $expectedHash, got $actualHash"
                exit 1
            }
        }
        else {
            Write-Info "No checksum entry for $ArchiveName — skipping"
        }
    }
    catch {
        Write-Info 'Checksums not available — skipping verification'
    }
}

# --- Extract base archive ---
Write-Info 'Extracting base archive'
tar -xzf $ArchivePath -C $Destination
Write-Ok 'Extracted base archive'

# --- Download and extract branding add-on if needed ---
if ($needsBranding) {
    $brandingArchive = "$PackageName-branding-$Version.tar.gz"
    $brandingUrl = "$BaseUrl/$brandingArchive"
    $brandingPath = Join-Path $Destination $brandingArchive

    Write-Info "Downloading branding add-on: $brandingArchive"
    try {
        Invoke-WebRequest -Uri $brandingUrl -Headers $headers -OutFile $brandingPath -UseBasicParsing
        Write-Ok "Downloaded: $brandingArchive"
        tar -xzf $brandingPath -C $Destination
        Write-Ok 'Extracted branding add-on'
    }
    catch {
        Write-Info 'Branding add-on not available — skipping (brand assets will not be included)'
    }
}

# --- Filter extracted content to selected packs ---
if ($packList -notcontains 'full') {
    Write-Step "Filtering to selected packs: $($packList -join ', ')"

    # Load pack catalog (bundled in base archive or fetch from registry)
    $catalogPath = Join-Path $Destination 'pack-catalog.json'
    if (-not (Test-Path $catalogPath)) {
        try {
            Invoke-WebRequest -Uri "$BaseUrl/pack-catalog.json" -Headers $headers -OutFile $catalogPath -UseBasicParsing
        }
        catch {
            Write-Info 'Pack catalog not available — skipping filter (installing all content)'
            $packList = @('full')
        }
    }

    if ($packList -notcontains 'full' -and (Test-Path $catalogPath)) {
        $catalog = Get-Content $catalogPath -Raw | ConvertFrom-Json

        # ── Resolve transitive pack dependencies (requires field) ─────
        $resolved = [System.Collections.Generic.HashSet[string]]::new()
        $queue = [System.Collections.Generic.Queue[string]]::new()
        foreach ($p in $packList) { $queue.Enqueue($p) }
        while ($queue.Count -gt 0) {
            $current = $queue.Dequeue()
            if (-not $resolved.Add($current)) { continue }
            $packDef = $catalog.packs | Where-Object { $_.name -eq $current }
            if ($packDef -and $packDef.requires) {
                foreach ($dep in $packDef.requires) {
                    if (-not $resolved.Contains($dep)) {
                        $queue.Enqueue($dep)
                    }
                }
            }
        }
        $added = @($resolved | Where-Object { $packList -notcontains $_ })
        if ($added.Count -gt 0) {
            Write-Info "Auto-added transitive dependencies: $($added -join ', ')"
        }
        $packList = @($resolved)

        # Collect all agents, skills, and workflows from selected packs
        $allowedAgents = [System.Collections.Generic.HashSet[string]]::new()
        $allowedSkills = [System.Collections.Generic.HashSet[string]]::new()
        $allowedWorkflows = [System.Collections.Generic.HashSet[string]]::new()

        foreach ($p in $catalog.packs) {
            if ($packList -notcontains $p.name) { continue }

            if ($p.agents) {
                foreach ($a in $p.agents) {
                    if ($a -eq 'all') { $allowedAgents = $null; break }
                    [void]$allowedAgents.Add($a)
                }
            }
            if ($null -eq $allowedAgents) { break }

            if ($p.skills) {
                foreach ($s in $p.skills) {
                    if ($s -eq 'all') { $allowedSkills = $null; break }
                    [void]$allowedSkills.Add($s)
                }
            }

            if ($p.workflows) {
                foreach ($w in $p.workflows) {
                    [void]$allowedWorkflows.Add($w)
                }
            }
        }

        # Remove agents not in selected packs
        $agentsDir = Join-Path $Destination '.apm/agents'
        if ($null -ne $allowedAgents -and (Test-Path $agentsDir)) {
            $removed = 0
            Get-ChildItem $agentsDir -Filter '*.md' | ForEach-Object {
                $agentName = $_.BaseName
                if (-not $allowedAgents.Contains($agentName)) {
                    Remove-Item $_.FullName -Force
                    $removed++
                }
            }
            Write-Info "Agents: kept $($allowedAgents.Count), removed $removed"
        }

        # Remove skills not in selected packs
        $skillsDir = Join-Path $Destination '.apm/skills'
        if ($null -ne $allowedSkills -and (Test-Path $skillsDir)) {
            $removed = 0
            Get-ChildItem $skillsDir -Directory | ForEach-Object {
                if (-not $allowedSkills.Contains($_.Name)) {
                    Remove-Item $_.FullName -Recurse -Force
                    $removed++
                }
            }
            Write-Info "Skills: kept $($allowedSkills.Count), removed $removed"
        }

        # Remove workflows not in selected packs
        # Keep underscore-prefixed files (schema/support files like _schema.md)
        $workflowsDir = Join-Path $Destination '.apm/workflows'
        if ($null -ne $allowedWorkflows -and (Test-Path $workflowsDir)) {
            $removed = 0
            Get-ChildItem $workflowsDir -File | Where-Object { $_.Extension -in '.yml', '.md' } | ForEach-Object {
                $wfName = $_.BaseName
                # Keep underscore-prefixed files (schema/support files)
                if ($wfName.StartsWith('_')) { return }
                if (-not $allowedWorkflows.Contains($wfName)) {
                    Remove-Item $_.FullName -Force
                    $removed++
                }
            }
            Write-Info "Workflows: kept $($allowedWorkflows.Count), removed $removed"
        }

        # --- Filter provider runtime assets to match selected packs ---
        # Provider agents follow the naming convention <canonical-name>.agent.md
        $providerDirs = @(
            (Join-Path $Destination 'providers/github-copilot'),
            (Join-Path $Destination 'providers/claude-code')
        )
        foreach ($provDir in $providerDirs) {
            if (-not (Test-Path $provDir)) { continue }
            $provName = Split-Path $provDir -Leaf

            # Filter provider agents
            $provAgentsDir = Join-Path $provDir 'agents'
            if ($null -ne $allowedAgents -and (Test-Path $provAgentsDir)) {
                $removed = 0
                Get-ChildItem $provAgentsDir -Filter '*.md' | ForEach-Object {
                    # Strip .agent.md suffix to get canonical name
                    $agentName = $_.BaseName -replace '\.agent$', ''
                    if (-not $allowedAgents.Contains($agentName)) {
                        Remove-Item $_.FullName -Force
                        $removed++
                    }
                }
                Write-Info "Provider $provName agents: removed $removed"
            }

            # Filter provider skills
            $provSkillsDir = Join-Path $provDir 'skills'
            if ($null -ne $allowedSkills -and (Test-Path $provSkillsDir)) {
                $removed = 0
                Get-ChildItem $provSkillsDir -Directory | ForEach-Object {
                    if (-not $allowedSkills.Contains($_.Name)) {
                        Remove-Item $_.FullName -Recurse -Force
                        $removed++
                    }
                }
                Write-Info "Provider $provName skills: removed $removed"
            }
        }

        # Filter .github/agents if already projected
        $ghAgentsDir = Join-Path $Destination '.github/agents'
        if ($null -ne $allowedAgents -and (Test-Path $ghAgentsDir)) {
            $removed = 0
            Get-ChildItem $ghAgentsDir -Filter '*.md' | ForEach-Object {
                $agentName = $_.BaseName -replace '\.agent$', ''
                if (-not $allowedAgents.Contains($agentName)) {
                    Remove-Item $_.FullName -Force
                    $removed++
                }
            }
            Write-Info ".github agents: removed $removed"
        }

        Write-Ok 'Pack filter applied'
    }
}

# For lock file metadata
$lockArchive = if ($packList -contains 'full') { "$PackageName-$Target.tar.gz" } else { "packs:$($packList -join ',')" }

# --- Check for existing lock file ---
# Standard mode writes the lock at repo root; expandable writes at $Destination.
# Try both locations so updates are detected regardless of prior mode.
$existingLock = Read-ApmLock -Path $Destination
if (-not $existingLock) {
    $existingLock = Read-ApmLock -Path $repoRoot
}
if ($existingLock) {
    Write-Info "Existing install detected: v$($existingLock['version']) ($($existingLock['mode']) mode)"
}

# --- Helper: parse runtime path from apm.yml in extracted content ---
function Get-ApmRuntime {
    param([string]$ApmRoot, [string]$ProviderKey)
    $apmCfg = Join-Path $ApmRoot 'apm.yml'
    if (-not (Test-Path $apmCfg)) { return $null }
    $lines = Get-Content $apmCfg -Encoding UTF8
    $inProviders = $false
    $inTarget = $false
    foreach ($l in $lines) {
        if ($l -match '^providers:\s*$')                                   { $inProviders = $true; continue }
        if ($inProviders -and $l -match '^\S' -and $l -notmatch '^providers:') { $inProviders = $false; $inTarget = $false; continue }
        if (-not $inProviders) { continue }
        if ($l -match "^  ${ProviderKey}:\s*$")                             { $inTarget = $true; continue }
        if ($inTarget -and $l -match '^\s{2}\S' -and $l -notmatch "^  ${ProviderKey}:") { break }
        if (-not $inTarget) { continue }
        if ($l -match '^\s{4}runtime:\s*(.+)$') { return $Matches[1].Trim() }
    }
    return $null
}

# --- Helper: merge a projected runtime directory into an existing consumer directory ---
# Instead of rm -rf + copy, this function:
#   1. Reads the previous .apm-manifest.json from the consumer directory (if present)
#   2. Reads the new .apm-manifest.json from the source directory
#   3. Removes stale managed files (in old manifest but not in new manifest)
#   4. Copies all new files from source into destination (overwriting managed files)
#   5. Cleans up empty directories left behind
# Consumer-added files (not in any manifest) are preserved.
function Merge-RuntimeDirectory {
    param(
        [string]$Source,       # Projected runtime in temp/staging dir
        [string]$Destination,  # Consumer repo runtime dir (e.g. .github/)
        [switch]$MoveInstead   # Use Move-Item instead of Copy-Item (for expandable mode)
    )

    # Normalise BOTH paths to absolute before any string arithmetic below.
    # $file.FullName is always absolute; if $Source arrives relative (it does —
    # $tempDir is built as ".\.apm-install-tmp-<ts>"), Substring($Source.Length)
    # slices the wrong number of characters and yields a corrupted relative
    # path such as "nstall-tmp-<ts>\.github\agents\x.md", which then recreates
    # the whole runtime NESTED inside .github and makes every agent invisible.
    $Source = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::Combine((Get-Location).Path, $Source))
    $Destination = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::Combine((Get-Location).Path, $Destination))

    # First install: create the destination and fall through to the per-file
    # merge below. NEVER `Move-Item <dir> <dir>` here — if the destination
    # appears between the check and the move (concurrent watcher, retried
    # install), PowerShell NESTS the source inside it (.github/p-*/.github),
    # making every agent invisible to the provider. Per-file merge is
    # race-proof and idempotent for both first installs and updates.
    if (-not (Test-Path $Destination)) {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
        Write-Info "Created runtime directory (first install)"
    }

    # Read previous manifest from consumer directory
    $prevManifestPath = Join-Path $Destination '.apm-manifest.json'
    $prevManaged = @()
    if (Test-Path $prevManifestPath) {
        try {
            $prevManaged = @((Get-Content $prevManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).managed_files)
        } catch {
            Write-Info "Could not parse previous manifest — treating as empty"
        }
    }

    # Read new manifest from source directory
    $newManifestPath = Join-Path $Source '.apm-manifest.json'
    $newManaged = @()
    if (Test-Path $newManifestPath) {
        try {
            $newManaged = @((Get-Content $newManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).managed_files)
        } catch {
            Write-Info "Could not parse new manifest — will overwrite without stale removal"
        }
    }

    # Remove stale managed files (in old manifest but not in new)
    if ($prevManaged.Count -gt 0) {
        $newSet = [System.Collections.Generic.HashSet[string]]::new([string[]]$newManaged, [System.StringComparer]::OrdinalIgnoreCase)
        $staleRemoved = 0
        foreach ($rel in $prevManaged) {
            if (-not $newSet.Contains($rel)) {
                $absPath = Join-Path $Destination $rel
                if (Test-Path $absPath) {
                    Remove-Item $absPath -Force
                    $staleRemoved++
                }
            }
        }
        if ($staleRemoved -gt 0) {
            Write-Info "Removed $staleRemoved stale managed files"
        }
    }

    # Copy/move all new files into destination (overwriting managed files, preserving consumer files)
    $sourceFiles = Get-ChildItem $Source -File -Recurse
    foreach ($file in $sourceFiles) {
        $relativePath = $file.FullName.Substring($Source.Length).TrimStart('\', '/')
        $destFile = Join-Path $Destination $relativePath
        $destDir = Split-Path $destFile -Parent
        if (-not (Test-Path $destDir)) {
            New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        }
        $srcPath = $file.FullName
        if ($MoveInstead) {
            Invoke-WithRetry -What "move $relativePath" -Action { Move-Item $srcPath -Destination $destFile -Force }.GetNewClosure() | Out-Null
        } else {
            Invoke-WithRetry -What "copy $relativePath" -Action { Copy-Item $srcPath -Destination $destFile -Force }.GetNewClosure() | Out-Null
        }
    }

    # Clean up empty directories left behind by stale removal
    if ($prevManaged.Count -gt 0) {
        Get-ChildItem $Destination -Directory -Recurse -ErrorAction SilentlyContinue |
            Sort-Object { $_.FullName.Length } -Descending |
            Where-Object { @(Get-ChildItem $_.FullName -Force -ErrorAction SilentlyContinue).Count -eq 0 } |
            ForEach-Object { Remove-Item $_.FullName -Force }
    }

    Write-Info "Merged runtime directory (consumer files preserved)"
}

# Fetch the Atlassian OAuth client secret from the GitLab project CI/CD variables
# (same project used to download the bundle — ProjectId + GitLabUrl already known).
# Non-fatal: logs a warning and continues if the fetch fails.
# function Invoke-AtlassianSecretSeed {
#     param(
#         [string]$ProjectId,
#         [string]$GitLabBaseUrl,
#         [string]$AuthToken,
#         [string]$EnvFile
#     )

#     $clientId = 'ri3TRgWzA8FafT3PU7etx3kF5j6sHt7D'
#     $clientSecret = ''

#     if ($AuthToken -and $ProjectId) {
#         $apiUrl = "$GitLabBaseUrl/api/v4/projects/$ProjectId/variables/ATLASSIAN_OAUTH_CLIENT_SECRET"
#         Write-Info "Fetching Atlassian OAuth client secret from GitLab project $ProjectId"
#         try {
#             $resp = Invoke-WebRequest -Uri $apiUrl -Headers @{'PRIVATE-TOKEN' = $AuthToken} -UseBasicParsing
#             $data = $resp.Content | ConvertFrom-Json
#             $clientSecret = $data.value
#             if ($clientSecret) {
#                 Write-Ok 'Atlassian OAuth client secret retrieved'
#             } else {
#                 Write-Info "Could not retrieve Atlassian OAuth client secret — set ATLASSIAN_OAUTH_CLIENT_SECRET manually in $EnvFile"
#             }
#         }
#         catch {
#             Write-Info "Could not retrieve Atlassian OAuth client secret — set ATLASSIAN_OAUTH_CLIENT_SECRET manually in $EnvFile"
#         }
#     } else {
#         Write-Info 'Skipping Atlassian OAuth credential seeding (no token or project ID available)'
#     }

#     # Create parent directory if needed
#     $envDir = Split-Path $EnvFile -Parent
#     if ($envDir -and -not (Test-Path $envDir)) {
#         New-Item -ItemType Directory -Path $envDir -Force | Out-Null
#     }

#     # Skip if CLIENT_ID already present to avoid duplicates
#     if ((Test-Path $EnvFile) -and ([System.IO.File]::ReadAllText($EnvFile) -match 'ATLASSIAN_OAUTH_CLIENT_ID')) {
#         Write-Info "Atlassian OAuth vars already present in $EnvFile — skipping"
#         return
#     }

#     # Append to .env using BOM-free UTF-8 — never log the secret value
#     $append = "`n# Atlassian OAuth (Sopra Steria Cloud Constellation)`nATLASSIAN_OAUTH_CLIENT_ID=$clientId`nATLASSIAN_OAUTH_CLIENT_SECRET=$clientSecret`n"
#     $encoding = New-Object System.Text.UTF8Encoding $false
#     if (Test-Path $EnvFile) {
#         $existing = [System.IO.File]::ReadAllText($EnvFile, $encoding)
#         [System.IO.File]::WriteAllText($EnvFile, $existing + $append, $encoding)
#     } else {
#         [System.IO.File]::WriteAllText($EnvFile, $append.TrimStart("`n"), $encoding)
#     }
#     Write-Ok "Atlassian OAuth credentials written to $EnvFile"
# }

# --- Preflight: heal debris from any earlier interrupted/retried install ---
# (nested .github/p-*/.github staging, leaked .apm-install-tmp-* folders)
$healed = Repair-NestedRuntime -RepoRoot $repoRoot
if ($healed -gt 0) { Write-Ok "Preflight repair: healed $healed staging artifact(s) from a previous attempt" }

# --- Install mode logic ---
Write-Step "Applying install mode: $Mode"

if ($Mode -eq 'standard') {
    # ── Standard mode: project runtime-only files ──────────────────────
    # NOTE: runtime is merged using manifest-aware logic that preserves
    # consumer-added files (CI pipelines, custom agents, etc.).

    # Create temp working directory alongside destination
    $tempDir = Join-Path (Split-Path $Destination -Parent) ".apm-install-tmp-$(Get-Date -Format 'yyyyMMddHHmmss')"
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    Write-Info "Temp working directory: $tempDir"

    try {
        # Move only APM-extracted content to temp dir.
        # SAFETY: only move known APM directories/files — never touch .git,
        # .cursor, .vscode, node_modules, or any other consumer content.
        # This prevents catastrophic data loss if $Destination is the repo root.
        $apmContentNames = @('.apm', 'providers', 'scripts', 'apm.yml',
                             'pack-catalog.json', 'SHA256SUMS', 'CLAUDE.md',
                             '.apm-inspect', '.claude')
        Get-ChildItem -Path $Destination -Force | Where-Object {
            $_.FullName -ne (Resolve-Path $ArchivePath -ErrorAction SilentlyContinue).Path -and
            ($_.Name -in $apmContentNames -or $_.Name -match '^ssg-ai-backbone.*\.(tar\.gz|zip)$')
        } | ForEach-Object {
            $destItem = Join-Path $tempDir $_.Name
            Move-Item $_.FullName -Destination $destItem -Force
        }

        # --- Determine which providers to project ---
        $providersToProject = switch ($Provider) {
            'all'          { @('github-copilot', 'claude-code') }
            'claude-code'  { @('claude-code') }
            default        { @('github-copilot') }
        }

        foreach ($currentProvider in $providersToProject) {
            Write-Step "Projecting provider: $currentProvider"

            if ($currentProvider -eq 'claude-code') {
                # ── Claude Code projection ──────────────────────────────
                $claudeScript = Join-Path $tempDir '.apm/scripts/powershell/project-claude.ps1'
                if (-not (Test-Path $claudeScript)) {
                    Write-Err "Claude projection script not found at $claudeScript"
                    exit 1
                }

                Write-Info "Running Claude Code projection (-Full -Clean)"
                & $claudeScript -Full -Clean -Destination $repoRoot
                Write-Ok "Claude Code projection complete"
            }
            else {
                # ── Copilot projection ──────────────────────────────────
                $projectionScript = Join-Path $tempDir '.apm/scripts/powershell/project-copilot.ps1'
                if (-not (Test-Path $projectionScript)) {
                    Write-Err "Projection script not found at $projectionScript"
                    exit 1
                }

                Write-Info "Running Copilot projection (-Full -Clean)"
                & $projectionScript -Provider $currentProvider -Full -Clean

                # Resolve runtime path from apm.yml
                $runtimeDir = Get-ApmRuntime -ApmRoot $tempDir -ProviderKey $currentProvider
                if (-not $runtimeDir) {
                    Write-Err "Could not resolve runtime path for provider '$currentProvider' from apm.yml"
                    exit 1
                }
                Write-Info "Runtime directory: $runtimeDir"

                $runtimeSrc = Join-Path $tempDir $runtimeDir
                if (-not (Test-Path $runtimeSrc)) {
                    Write-Err "Projected runtime directory not found: $runtimeSrc"
                    exit 1
                }

                # Remove repo-specific files that should not leak into consumer installs.
                $repoOnlyFile = Join-Path $runtimeSrc 'copilot-instructions.md'
                if (Test-Path $repoOnlyFile) {
                    Remove-Item $repoOnlyFile -Force
                    Write-Info 'Removed repo-specific copilot-instructions.md'
                }

                # Merge runtime directory into consumer repo root
                # (preserves consumer-added files like CI pipelines, custom agents)
                $runtimeDst = Join-Path $repoRoot $runtimeDir

                Merge-RuntimeDirectory -Source $runtimeSrc -Destination $runtimeDst
                Write-Ok "Merged runtime: $runtimeDir"

                # --- Post-install validation: never report success on a layout
                # the provider cannot discover (the "everything looks installed
                # but no agents appear" failure mode). ---
                $nested = @(Get-ChildItem $runtimeDst -Directory -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match '^(p-\d{8,14}|\.apm-install-tmp-\d{8,14})$' -or
                                   (Test-Path (Join-Path $_.FullName (Split-Path $runtimeDir -Leaf))) })
                $agentCount = @(Get-ChildItem (Join-Path $runtimeDst 'agents') -Filter '*.agent.md' -ErrorAction SilentlyContinue).Count
                if ($nested.Count -gt 0) {
                    Write-Err "Post-install check FAILED: staging directories nested inside ${runtimeDir}: $($nested.Name -join ', '). Run install-apm-bundle.ps1 -Repair"
                    exit 1
                }
                if ($agentCount -eq 0) {
                    Write-Err "Post-install check FAILED: no agents at $runtimeDir/agents/ — the provider will not discover anything. Run install-apm-bundle.ps1 -Repair and retry."
                    exit 1
                }
                Write-Ok "Post-install check: $agentCount agents discoverable at $runtimeDir/agents/, layout flat"

                # Post-install validation: an install that providers cannot
                # discover must fail loudly, not report success.
                $agentsDir = Join-Path $runtimeDst 'agents'
                $agentCount = @(Get-ChildItem $agentsDir -Filter '*.agent.md' -ErrorAction SilentlyContinue).Count
                $nestedDebris = @(Get-ChildItem $runtimeDst -Directory -ErrorAction SilentlyContinue |
                    Where-Object { Test-Path (Join-Path $_.FullName (Split-Path $runtimeDst -Leaf)) })
                if ($agentCount -eq 0) {
                    Write-Err "Validation failed: no agents at $agentsDir — providers will not discover this install. Run with -Repair, then reinstall."
                    exit 1
                }
                if ($nestedDebris.Count -gt 0) {
                    Write-Err "Validation failed: nested runtime dir(s) inside ${runtimeDst}: $($nestedDebris.Name -join ', ') — run with -Repair."
                    exit 1
                }
                Write-Ok "Validated: $agentCount agents discoverable, no nested staging"
            }
        }

        # Seed hook-config.json if not already present
        # Determine the hook template path based on primary provider
        $primaryRuntime = if ($Provider -eq 'claude-code') { '.claude' } else {
            Get-ApmRuntime -ApmRoot $tempDir -ProviderKey 'github-copilot'
        }
        if ($primaryRuntime) {
            $hookCfgTpl = Join-Path $tempDir "$primaryRuntime/templates/hook-config.json"
            $hookCfgDst = Join-Path $repoRoot 'hook-config.json'
            if ((Test-Path $hookCfgTpl) -and -not (Test-Path $hookCfgDst)) {
                Copy-Item $hookCfgTpl -Destination $hookCfgDst
                Write-Ok 'Seeded hook-config.json (edit to customise hooks)'
            }
        }

        # Write lock file at repo root
        Write-ApmLock -Path $repoRoot -Version $Version -Mode 'standard' -Provider $Provider -Archive $lockArchive -Checksum $actualHash -SourceType $Source
        Write-Ok 'Lock file written'

        # Seed consumer .gitignore with APM runtime-artifact entries
        Add-ApmGitignoreBlock -RepoRoot $repoRoot
    }
    finally {
        # Clean up temp directory — locked files must not fail the install or
        # leave a mystery folder without explanation (retry, then warn).
        if (Test-Path $tempDir) {
            try {
                Invoke-WithRetry -What "remove temp dir" -Action { Remove-Item $tempDir -Recurse -Force }.GetNewClosure() | Out-Null
                Write-Info 'Cleaned up temp directory'
            } catch {
                Write-Err "Temp dir still locked: $tempDir — it is SAFE to delete manually (the next install also auto-removes it)"
            }
        }
    }
}
elseif ($Mode -eq 'expandable') {
    # ── Expandable mode: full source with manifest-based tracking ─────
    # Consumer customisations placed directly in the runtime directory
    # (e.g. .github/agents/) are preserved across updates via the manifest.

    if ($existingLock) {
        # Remove only APM-extracted content (keep archive and consumer files), re-extract.
        # SAFETY: only delete known APM directories/files — never touch .git,
        # .cursor, .vscode, node_modules, or any other consumer content.
        # This prevents catastrophic data loss if $Destination is the repo root.
        $apmExtractedNames = @('.apm', 'providers', 'scripts', 'apm.yml',
                               'pack-catalog.json', 'SHA256SUMS', 'CLAUDE.md',
                               '.apm-inspect', '.claude')
        Get-ChildItem -Path $Destination -Force | Where-Object {
            $_.Name -ne $ArchiveName -and
            $_.Name -ne (Split-Path $ArchivePath -Leaf) -and
            ($_.Name -in $apmExtractedNames -or $_.Name -match '^ssg-ai-backbone.*\.(tar\.gz|zip)$')
        } | ForEach-Object {
            Remove-Item $_.FullName -Recurse -Force
        }

        tar -xzf $ArchivePath -C $Destination
        Write-Info 'Re-extracted archive for update'
    }

    # Run projection (-Full -Clean for expandable — projects ALL content
    # including skills, workflows, knowledge into .github/ so Copilot
    # can discover them. Manifest-based clean preserves consumer additions.)
    $expandableProviders = switch ($Provider) {
        'all'          { @('github-copilot', 'claude-code') }
        'claude-code'  { @('claude-code') }
        default        { @('github-copilot') }
    }

    foreach ($currentProvider in $expandableProviders) {
        if ($currentProvider -eq 'claude-code') {
            $claudeScript = Join-Path $Destination '.apm/scripts/powershell/project-claude.ps1'
            if (-not (Test-Path $claudeScript)) {
                Write-Err "Claude projection script not found at $claudeScript"
                exit 1
            }
            Write-Info "Running Claude Code projection (-Full -Clean) for expandable mode"
            & $claudeScript -Full -Clean -Destination $Destination
        }
        else {
            $projectionScript = Join-Path $Destination '.apm/scripts/powershell/project-copilot.ps1'
            if (-not (Test-Path $projectionScript)) {
                Write-Err "Projection script not found at $projectionScript"
                exit 1
            }
            Write-Info "Running Copilot projection (-Full -Clean) for expandable mode"
            & $projectionScript -Provider $currentProvider -Full -Clean
        }
    }

    # Write lock file
    Write-ApmLock -Path $Destination -Version $Version -Mode 'expandable' -Provider $Provider -Archive $lockArchive -Checksum $actualHash -SourceType $Source

    # Seed consumer .gitignore with APM runtime-artifact entries
    Add-ApmGitignoreBlock -RepoRoot $Destination
    Write-Ok 'Lock file written'

    # NOTE: .gitignore is now generated per-file by the projection script
    # inside the runtime directory (e.g. .github/.gitignore) based on
    # .apm-manifest.json. No directory-level .gitignore needed here.

    # Promote only the runtime directory and lock file to repo root
    # (not .apm/, providers/, scripts/ — those are source tree artifacts
    # that are no longer needed since -Full projects everything into .github/)
    $repoRoot = (Get-Location).Path

    # Copilot runtime
    if ($Provider -ne 'claude-code') {
        $runtimeDir = Get-ApmRuntime -ApmRoot $Destination -ProviderKey 'github-copilot'
        if ($runtimeDir) {
            $runtimeSrc = Join-Path $Destination $runtimeDir
            $runtimeDst = Join-Path $repoRoot $runtimeDir

            # Remove repo-specific files that should not leak into consumer installs.
            $repoOnlyFile = Join-Path $runtimeSrc 'copilot-instructions.md'
            if (Test-Path $repoOnlyFile) {
                Remove-Item $repoOnlyFile -Force
                Write-Info 'Removed repo-specific copilot-instructions.md'
            }

            Merge-RuntimeDirectory -Source $runtimeSrc -Destination $runtimeDst -MoveInstead
            Write-Ok "Promoted runtime: $runtimeDir"
        }
    }

    # Claude Code runtime
    if ($Provider -eq 'claude-code' -or $Provider -eq 'all') {
        $claudeMdSrc = Join-Path $Destination 'CLAUDE.md'
        $claudeDirSrc = Join-Path $Destination '.claude'
        if (Test-Path $claudeMdSrc) {
            Move-Item $claudeMdSrc -Destination (Join-Path $repoRoot 'CLAUDE.md') -Force
        }
        if (Test-Path $claudeDirSrc) {
            $claudeDirDst = Join-Path $repoRoot '.claude'
            Merge-RuntimeDirectory -Source $claudeDirSrc -Destination $claudeDirDst -MoveInstead
        }
        Write-Ok "Promoted Claude Code runtime"
    }

    # Lock file
    $lockSrc = Join-Path $Destination '.apm.lock.yaml'
    if (Test-Path $lockSrc) {
        Move-Item $lockSrc -Destination (Join-Path $repoRoot '.apm.lock.yaml') -Force
    }

    # Seed hook-config.json if not already present
    $primaryRuntime = if ($Provider -eq 'claude-code') { '.claude' } else {
        Get-ApmRuntime -ApmRoot $Destination -ProviderKey 'github-copilot'
    }
    if ($primaryRuntime) {
        $hookCfgTpl = Join-Path $Destination "$primaryRuntime/templates/hook-config.json"
        if (-not (Test-Path $hookCfgTpl)) {
            $hookCfgTpl = Join-Path $Destination ".apm/templates/hook-config.json"
        }
        $hookCfgDst = Join-Path $repoRoot 'hook-config.json'
        if ((Test-Path $hookCfgTpl) -and -not (Test-Path $hookCfgDst)) {
            Copy-Item $hookCfgTpl -Destination $hookCfgDst
            Write-Ok 'Seeded hook-config.json (edit to customise hooks)'
        }
    }

    Write-Ok "Expandable install complete — only .github/ delivered"
}

# --- Summary ---
Write-Step 'Installation Complete'
$repoRoot = (Get-Location).Path
# --- Cleanup staging directory ---
# SAFETY: only remove $Destination if it is the default staging dir (.apm-dist).
# If $Destination was set to the repo root (e.g. "."), we must NOT delete it.
$resolvedDest = (Resolve-Path $Destination -ErrorAction SilentlyContinue).Path
$resolvedCwd  = (Get-Location).Path
if ($resolvedDest -and $resolvedDest -ne $resolvedCwd -and
    -not (Test-Path (Join-Path $resolvedDest '.git')) -and
    (Test-Path $Destination)) {
    Remove-Item $Destination -Recurse -Force
} elseif ($resolvedDest -eq $resolvedCwd -or (Test-Path (Join-Path $Destination '.git'))) {
    # Destination is the repo root — only clean up APM staging artifacts
    @('pack-catalog.json', 'SHA256SUMS', '.apm-inspect') | ForEach-Object {
        $p = Join-Path $Destination $_
        if (Test-Path $p) { Remove-Item $p -Force }
    }
    Get-ChildItem -Path $Destination -Filter 'ssg-ai-backbone*.tar.gz' -File | Remove-Item -Force
    Write-Info 'Cleaned up staging artifacts (destination is repo root — skipped full removal)'
}
Write-Ok "$PackageName v$Version ($Target, $Mode mode) installed to $repoRoot"

# --- Seed Atlassian OAuth credentials ---
# $atlassianEnvFile = Join-Path $repoRoot '.env'
# Invoke-AtlassianSecretSeed -ProjectId $ProjectId -GitLabBaseUrl $GitLabUrl -AuthToken $Token -EnvFile $atlassianEnvFile

# --- Check for conflicting Copilot settings ---
Write-Step 'Checking for conflicting Copilot settings'
$vscodeDirs = @(
    (Join-Path $repoRoot '.vscode'),
    (Join-Path $env:APPDATA 'Code/User')
)

$conflictPatterns = @(
    'Do NOT create markdown files',
    'Do not create markdown',
    'Do not write files unless requested',
    'never create files',
    'do not create files'
)

$conflictFound = $false
foreach ($dir in $vscodeDirs) {
    $settingsPath = Join-Path $dir 'settings.json'
    if (Test-Path $settingsPath) {
        $settingsContent = Get-Content $settingsPath -Raw -ErrorAction SilentlyContinue
        if ($settingsContent) {
            foreach ($pattern in $conflictPatterns) {
                if ($settingsContent -match [regex]::Escape($pattern)) {
                    Write-Host ""
                    Write-Host "  ⚠️  FILE-WRITE CONFLICT DETECTED in $settingsPath" -ForegroundColor Yellow
                    Write-Host "      Found: '$pattern'" -ForegroundColor Yellow
                    Write-Host "      This setting will prevent agents from writing deliverable files to disk." -ForegroundColor Yellow
                    Write-Host "      Remove this from 'github.copilot.chat.reminderInstructions' or add an override:" -ForegroundColor Yellow
                    Write-Host '      { "text": "Agents from ai-sdlc-foundation MUST write deliverable files to disk under outputs/." }' -ForegroundColor Yellow
                    Write-Host ""
                    $conflictFound = $true
                    break
                }
            }
        }
    }
}

# Ensure workspace .vscode/settings.json has the file-write override
$workspaceSettings = Join-Path $repoRoot '.vscode/settings.json'
$overrideText = 'Agents from ai-sdlc-foundation MUST write deliverable files to disk under outputs/.'

if (Test-Path $workspaceSettings) {
    $existingContent = Get-Content $workspaceSettings -Raw -ErrorAction SilentlyContinue
    if ($existingContent -and $existingContent -notmatch [regex]::Escape($overrideText)) {
        Write-Info "Consider adding the file-write override to $workspaceSettings :"
        Write-Host '  "github.copilot.chat.reminderInstructions": [{ "text": "Agents from ai-sdlc-foundation MUST write deliverable files to disk under outputs/." }]' -ForegroundColor Cyan
    }
} elseif (-not $conflictFound) {
    Write-Ok 'No conflicting Copilot settings detected'
}
Get-ChildItem -Path $repoRoot | Format-Table Name, Length, LastWriteTime -AutoSize

