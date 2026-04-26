<#
.SYNOPSIS
    Remove stored kb.netapp.com cookies from Windows Credential Manager.
.DESCRIPTION
    Deletes the session cookies saved by Set-KBCookies.ps1.
    Use this to force a fresh login or to clean up your credentials.

.PARAMETER Username
    Optional. The username whose cookies should be removed.
    Defaults to your Windows username.
.EXAMPLE
    .\Remove-KBCookies.ps1
.EXAMPLE
    .\Remove-KBCookies.ps1 -Username "andy.liao@netapp.com"
#>

param(
    [Parameter(Mandatory=$false)]
    [string]$Username
)

if (-not $Username) {
    $Username = $env:USERNAME
}

Write-Host ""
Write-Host "=== KB NetApp MCP Server — Remove Cookies ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Removing cookies for user: " -NoNewline
Write-Host $Username -ForegroundColor Yellow
Write-Host ""

$escapedUsername = $Username -replace "'", "''"

$pythonScript = @"
import keyring
try:
    keyring.delete_password('KBNetAppMCP', '$escapedUsername')
    print('DELETED')
except Exception as e:
    err = str(e)
    if 'not found' in err.lower() or 'no item' in err.lower() or 'PasswordDeleteError' in type(e).__name__:
        print('NOT_FOUND')
    else:
        print(f'ERROR:{err}')
"@

try {
    $result = python -c $pythonScript 2>&1

    if ($result -eq "DELETED") {
        Write-Host "✅ Cookies removed from Windows Credential Manager." -ForegroundColor Green
    } elseif ($result -eq "NOT_FOUND") {
        Write-Host "ℹ️  No stored cookies found for user '$Username'." -ForegroundColor Yellow
    } else {
        Write-Host "❌ Error: $result" -ForegroundColor Red
    }
} catch {
    Write-Host "❌ Error: $_" -ForegroundColor Red
}

Write-Host ""
