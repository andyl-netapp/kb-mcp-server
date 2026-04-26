<#
.SYNOPSIS
    Set up or refresh kb.netapp.com SSO cookies for the KB MCP Server.
.DESCRIPTION
    Opens a browser window so you can complete NetApp SSO login.
    Session cookies are captured automatically and stored securely
    in Windows Credential Manager.

    Run this script ONCE initially, then again whenever the MCP server
    reports that your session has expired.

.PARAMETER Username
    Optional. Your NetApp username/email.
    If not provided, defaults to your Windows username.
.EXAMPLE
    .\Set-KBCookies.ps1
.EXAMPLE
    .\Set-KBCookies.ps1 -Username "andy.liao@netapp.com"
#>

param(
    [Parameter(Mandatory=$false)]
    [string]$Username
)

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$LoginScript = Join-Path $ScriptDir "login_helper.py"

Write-Host ""
Write-Host "=== KB NetApp MCP Server — Login Setup ===" -ForegroundColor Cyan
Write-Host ""

# Validate prerequisites
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python not found. Please install Python 3.10+ and add it to PATH."
    exit 1
}
if (-not (Test-Path $LoginScript)) {
    Write-Error "login_helper.py not found at: $LoginScript"
    exit 1
}

# Set username env var if provided
$env:KB_USERNAME = if ($Username) { $Username } else { $env:USERNAME }

Write-Host "Username : $($env:KB_USERNAME)" -ForegroundColor Yellow
Write-Host ""
Write-Host "Launching browser for SSO login..." -ForegroundColor White
Write-Host "(A Microsoft Edge window will open. Log in with your NetApp SSO credentials.)" -ForegroundColor Gray
Write-Host ""

python $LoginScript

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "✅ Login successful! Session cookies saved." -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Yellow
    Write-Host "  1. Ensure your mcp-config.json points to this server (see README.md)." -ForegroundColor White
    Write-Host "  2. Run /restart in Copilot CLI to reload the MCP server." -ForegroundColor White
    Write-Host ""
    Write-Host "MCP config entry:" -ForegroundColor Yellow
    $escaped = ($ScriptDir -replace '\\', '\\\\')
    Write-Host @"
  {
    "mcpServers": {
      "kb-netapp": {
        "command": "python",
        "args": ["$escaped\\kb_mcp.py"],
        "env": {
          "KB_USERNAME": "$($env:KB_USERNAME)"
        }
      }
    }
  }
"@ -ForegroundColor DarkCyan
} else {
    Write-Host ""
    Write-Host "❌ Login failed or timed out. Please try again." -ForegroundColor Red
    Write-Host ""
    Write-Host "Troubleshooting:" -ForegroundColor Yellow
    Write-Host "  - Make sure Playwright is installed: pip install playwright" -ForegroundColor Gray
    Write-Host "  - Make sure Microsoft Edge is installed on this machine." -ForegroundColor Gray
    Write-Host "  - Ensure you have a network connection to kb.netapp.com (VPN may be required)." -ForegroundColor Gray
    Write-Host "  - If Edge crashes immediately, reset the browser profile and retry:" -ForegroundColor Gray
    Write-Host "      Remove-Item -Recurse -Force `"`$env:USERPROFILE\.copilot\.netapp_browser_data`"" -ForegroundColor Gray
    exit 1
}

# Clean up env var
$env:KB_USERNAME = $null
