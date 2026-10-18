@echo off
chcp 65001 >nul
REM ============================================================
REM  upmine.bat -- upload local files/folders to My Documents
REM
REM  Usage: drag a file or folder onto this bat
REM         or:  upmine.bat "D:\xxx\folder"
REM
REM  Step 1  show what will be uploaded, and what the cloud has
REM  Step 2  ask Y/N
REM  Step 3  upload only what is missing
REM
REM  ASCII only: cmd.exe mis-parses non-ASCII inside REM lines.
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
    pause & exit /b 1
)

"%PY%" -c "import ast;ast.parse(open('wblib.py',encoding='utf-8').read())" 2>nul
if errorlevel 1 (
    echo [ERROR] wblib.py is corrupted.
    pause & exit /b 1
)

set "SRC=%~1"
if "%SRC%"=="" (
    echo.
    set /p "SRC=Folder or file to upload: "
)
if "%SRC%"=="" (
    echo   Nothing to do.
    pause & exit /b 0
)
if not exist "%SRC%" (
    echo   [ERROR] Not found: %SRC%
    pause & exit /b 1
)

set "TOKEN=%~2"

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
    echo     2^) Or pass it:  upmine.bat "D:\folder" op_xxxxxxxx
    echo.
    echo   Token lasts ~30 minutes, so this is expected now and then.
    echo.
    pause & exit /b 0
)

echo.
echo ============================================================
echo   Upload to My Documents
echo   Source: %SRC%
echo ============================================================
echo   Files land flat at the space root: the API has no
echo   "create folder" call, so subfolders are not recreated.
echo   Uploads cannot be undone - there is no delete API.
echo.

REM ---- Step 1: show the plan (never uploads) ----
"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$tok='!TOKEN!'; $src='!SRC!'; $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='%PY%'; $psi.Arguments=('wblib.py --push ' + [char]34 + $src + [char]34 + ' --push-plan-only --top 20'); $psi.RedirectStandardInput=$true; $psi.UseShellExecute=$false; $psi.WorkingDirectory='%SCRIPT_DIR%'; $p=[System.Diagnostics.Process]::Start($psi); $p.StandardInput.Write($tok); $p.StandardInput.Close(); $p.WaitForExit(); exit $p.ExitCode"

REM ---- Step 2: ask ----
echo.
echo ------------------------------------------------------------
set "ANS="
set /p "ANS=Upload these files? [Y/n]: "
if /i "%ANS%"=="n" goto :cancel
if /i "%ANS%"=="no" goto :cancel
if /i "%ANS%"=="q" goto :cancel

REM ---- Step 3: upload ----
echo.
echo   Uploading...
echo.
"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$tok='!TOKEN!'; $src='!SRC!'; $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='%PY%'; $psi.Arguments=('wblib.py --push ' + [char]34 + $src + [char]34 + ' --yes'); $psi.RedirectStandardInput=$true; $psi.UseShellExecute=$false; $psi.WorkingDirectory='%SCRIPT_DIR%'; $p=[System.Diagnostics.Process]::Start($psi); $p.StandardInput.Write($tok); $p.StandardInput.Close(); $p.WaitForExit(); exit $p.ExitCode"

echo.
echo   Done.  Files already in the cloud were skipped.
pause
endlocal
exit /b 0

:cancel
echo.
echo   Cancelled. Nothing was uploaded.
pause
endlocal
