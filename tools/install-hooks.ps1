# Enables the commit guard of this repository (tools/git-hooks) for this clone.
#
# Usage (from any directory):
#   .\tools\install-hooks.ps1            # enable
#   .\tools\install-hooks.ps1 -Check     # show the current setting, change nothing
#   .\tools\install-hooks.ps1 -Uninstall # disable again
#
# What it does: git config core.hooksPath tools/git-hooks
# The hooks run tools/publish_check.py --tree-only before every commit (staged files, identity)
# and check the commit message. Details: docs/19_sicherer_veroeffentlichungsprozess.md
param(
    [switch]$Check,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'

$repo = Split-Path $PSScriptRoot -Parent
Push-Location $repo
try {
    $current = (git config --local --get core.hooksPath) 2>$null
    if ($Check) {
        if ($current) { "core.hooksPath = $current" } else { 'core.hooksPath is not set (hooks of this repository are off)' }
        return
    }
    if ($Uninstall) {
        if ($current) { git config --local --unset core.hooksPath }
        'Commit guard disabled (core.hooksPath removed).'
        return
    }

    foreach ($hook in 'pre-commit', 'commit-msg') {
        if (-not (Test-Path (Join-Path $repo "tools\git-hooks\$hook"))) {
            throw "tools\git-hooks\$hook is missing."
        }
    }

    # Python 3.10+ is needed by the hooks.
    $py = $null
    foreach ($cand in @('python', 'py')) {
        if (Get-Command $cand -ErrorAction SilentlyContinue) {
            $pyArgs = @()
            if ($cand -eq 'py') { $pyArgs = @('-3') }
            & $cand @pyArgs -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { $py = $cand; break }
        }
    }
    if (-not $py) { Write-Warning 'Python 3.10 or newer not found. The hooks will stop every commit until it is installed.' }

    $terms = Join-Path $env:USERPROFILE '.datadrive\publish_secrets.txt'
    if (-not (Test-Path $terms)) {
        Write-Warning "Private terms file not found: $terms. The hooks stop commits until it exists (an empty file is enough; see docs/19)."
    }

    git config --local core.hooksPath tools/git-hooks
    'Commit guard enabled: core.hooksPath = tools/git-hooks'
    'Test it once with:  python tools/publish_check.py --tree-only'
} finally {
    Pop-Location
}
