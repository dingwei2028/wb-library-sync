@echo off
chcp 65001 >nul
REM ============================================================
REM  dlmine.bat -- Export MY OWN library spaces
REM
REM  Step 1  compare remote vs local, show what is missing
REM  Step 2  ask  Y/N
REM  Step 3  download only what is missing ^(never re-download^)
REM
REM  Output: %USERPROFILE%\Downloads\WbLib.cn\<account-uid>\
REM ============================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PY="
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
) else (
    where python >nul 2>nul && set "PY=python"
)

if "%PY%"=="" (
    echo [ERROR] Python not found.
    echo         Install Python 3.9+ and check "Add Python to PATH".
    pause & exit /b 1
)

"%PY%" -c "import ast;ast.parse(open('wblib.py',encoding='utf-8').read())" 2>nul
if errorlevel 1 (
    echo [ERROR] wblib.py is corrupted.
    pause & exit /b 1
)

set "TOKEN=%~1"

REM ---- No token argument? fall back to token.txt sitting next to this bat ----
if "%TOKEN%"=="" (
    if exist "%SCRIPT_DIR%token.txt" (
        for /f "usebackq delims= tokens=* eol=#" %%T in ("%SCRIPT_DIR%token.txt") do (
            if "!TOKEN!"=="" set "TOKEN=%%T"
        )
        if not "!TOKEN!"=="" echo   [token] loaded from token.txt
    )
)

if "%TOKEN%"=="" (
    echo.
    echo   No token supplied.
    echo.
    echo   Two ways to fix:
    echo     1^) Ask WorkBuddy to refresh it:  "refresh my library token"
    echo        ^(it writes token.txt next to this bat^)
    echo     2^) Or right now, tell WorkBuddy:  "export my library to local"
    echo.
    echo   Token lasts ~30 minutes, so this is expected now and then.
    echo.
    pause & exit /b 0
)

echo.
echo ============================================================
echo   My library  -  check what is new
echo   Scope: own spaces only      dlall.bat = also team spaces
echo ============================================================
echo.
echo   Sort the download list by:
echo     [1] File size        ^(biggest first, recommended^)
echo     [2] Last modified
echo     [3] Name
echo     [4] Type
echo.
set "SORTIN="
set /p "SORTIN=Pick 1-4 (Enter=1): "
set "SORT=size"
if "%SORTIN%"=="2" set "SORT=time"
if "%SORTIN%"=="3" set "SORT=name"
if "%SORTIN%"=="4" set "SORT=type"

echo.
echo   Comparing remote vs local... ^(no download yet^)
echo.

REM ---- Step 1: show plan only (never downloads) ----
set "PSARGS=wblib.py --plan-only --sort %SORT%"
"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$tok='!TOKEN!'; $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='%PY%'; $psi.Arguments='%PSARGS%'; $psi.RedirectStandardInput=$true; $psi.UseShellExecute=$false; $psi.WorkingDirectory='%SCRIPT_DIR%'; $p=[System.Diagnostics.Process]::Start($psi); $p.StandardInput.Write($tok); $p.StandardInput.Close(); $p.WaitForExit(); exit $p.ExitCode"

REM ---- Step 2: ask ----
echo.
echo ------------------------------------------------------------
set "ANS="
set /p "ANS=Download the missing items? [Y/n]: "
if /i "%ANS%"=="n" goto :cancel
if /i "%ANS%"=="no" goto :cancel
if /i "%ANS%"=="q" goto :cancel

REM ---- Step 3: download (idempotent) ----
echo.
echo   Downloading missing items only...
echo.
set "PARGS=wblib.py"
if not "%~2"=="" set "PARGS=wblib.py --max-total %~2"
"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$tok='!TOKEN!'; $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='%PY%'; $psi.Arguments='%PARGS%'; $psi.RedirectStandardInput=$true; $psi.UseShellExecute=$false; $psi.WorkingDirectory='%SCRIPT_DIR%'; $p=[System.Diagnostics.Process]::Start($psi); $p.StandardInput.Write($tok); $p.StandardInput.Close(); $p.WaitForExit(); exit $p.ExitCode"

echo.
echo   Done.  Already-synced files were skipped, never overwritten.
pause
endlocal
exit /b 0

:cancel
echo.
echo   Cancelled. Nothing was downloaded.
echo   Tip: try another sort to review the list:  [2] by time  [3] by name
pause
endlocal