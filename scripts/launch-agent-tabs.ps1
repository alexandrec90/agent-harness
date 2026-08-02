<#
.SYNOPSIS
    Opens ONE external Windows Terminal window with a pwsh tab per worktree,
    each running a coding-agent CLI (claude or codex) in that folder.

.DESCRIPTION
    Backs the workspace-level VS Code task "Agents: Open Tabs (External Terminal)"
    in alex-projects.code-workspace, whose picker passes claude or codex through
    -Agent. Machine-scoped, so it takes no project argument.

    VanillaLand is deliberately NOT given its own tab: it is a reference/edit
    target for Carameli work, never a standalone agent root. Instead it is
    mounted as a writable extra directory (--add-dir) on the Carameli tabs
    listed in -VanillaLandOwners. NOTE: it is a single shared checkout, so when
    more than one owner is set both agents can edit the same files at once.

.EXAMPLE
    pwsh -File launch-agent-tabs.ps1 -Agent codex
.EXAMPLE
    pwsh -File launch-agent-tabs.ps1 -Agent claude -VanillaLandOwners carameli
.EXAMPLE
    pwsh -File launch-agent-tabs.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [ValidateSet('claude', 'codex')]
    [string]$Agent = 'claude',

    [string]$Root = (Join-Path $env:USERPROFILE 'Desktop\vs_code'),

    # One tab per entry, in order. VanillaLand is intentionally absent.
    [string[]]$Folders = @('carameli', 'carameli-b', 'ibkr_trader', 'ibkr_trader-b', 'devkit'),

    # Carameli tabs that get the shared VanillaLand checkout mounted as a
    # writable --add-dir. Both by default (@() = none).
    [string[]]$VanillaLandOwners = @('carameli', 'carameli-b'),

    # Print the wt.exe command line instead of launching it.
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$wt = Get-Command wt.exe -ErrorAction SilentlyContinue
if (-not $wt) {
    throw 'Windows Terminal (wt.exe) not found. Install it from the Microsoft Store, or run the agents manually.'
}

$vanillaLand = Join-Path $Root 'VanillaLand'
$hasVanillaLand = Test-Path -LiteralPath $vanillaLand

# wt.exe re-parses its own command line, so the agent command must reach the
# per-tab pwsh as separate, SPACE-FREE tokens — never one quoted string. Two
# facts make that work (both verified against this machine):
#   1. `pwsh -Command <a> <b> <c>` concatenates trailing tokens into one command.
#   2. wt passes space-free tokens straight through to the tab's executable.
# So we append 'claude'/'codex' and any '--add-dir <path>' as discrete tokens.
# Do NOT put ';' inside a tab command — wt treats ';' as its tab delimiter.
# (The worktree and VanillaLand paths contain no spaces; if that ever changes,
# a token with a space would need explicit wt-level quoting.)
if ($vanillaLand -match '\s') {
    Write-Warning "VanillaLand path contains a space; --add-dir may not parse correctly: $vanillaLand"
}

# -w -1 forces a brand-new window instead of adding tabs to an existing one.
$wtArgs = @('-w', '-1')
$tabs = 0

foreach ($folder in $Folders) {
    $dir = Join-Path $Root $folder
    if (-not (Test-Path -LiteralPath $dir)) {
        Write-Warning "Skipping missing folder: $dir"
        continue
    }

    if ($tabs -gt 0) { $wtArgs += ';' }   # wt's subcommand separator

    # -NoExit keeps the tab's shell alive after the agent exits.
    $wtArgs += @(
        'new-tab',
        '--title', "$folder ($Agent)",
        '-d', $dir,
        'pwsh.exe', '-NoLogo', '-NoExit', '-Command', $Agent
    )
    if ($hasVanillaLand -and $VanillaLandOwners -contains $folder) {
        $wtArgs += @('--add-dir', $vanillaLand)
    }
    $tabs++
}

if ($tabs -eq 0) { throw "No worktree folders found under $Root" }

$wtArgs += @(';', 'focus-tab', '-t', '0')

if ($DryRun) {
    Write-Host "wt.exe $($wtArgs -join ' ')"
    return
}

Write-Host "Opening $tabs $Agent tab(s) in a new Windows Terminal window..."
& $wt.Source @wtArgs
