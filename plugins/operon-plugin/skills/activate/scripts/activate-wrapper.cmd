@echo off
rem Activation-script launcher (Windows companion to activate-wrapper).
rem
rem The default !`...` injection in a SKILL.md runs under bash, which
rem executes the POSIX `activate-wrapper` directly via its shebang -- so
rem this .cmd is only reached if a SKILL.md opts into `shell: powershell`
rem or some context runs the wrapper under cmd.exe. Kept for parity with
rem the plugin's other paired launchers (bin/, hooks/).
rem
rem Same ladder as the bash twin: a bare interpreter first (the pre-flight
rem `-c ""` skips the Microsoft Store stub, which exits non-zero), then
rem `uv run --no-project python` as a fallback. activate.py is stdlib-only,
rem so no PYTHONPATH or project sync is needed.

setlocal EnableDelayedExpansion

set "HERE=%~dp0"
set "SCRIPT=%HERE%activate.py"

for %%P in (python3 python) do (
    where %%P >nul 2>nul
    if not errorlevel 1 (
        %%P -c "" >nul 2>nul
        if not errorlevel 1 (
            %%P "%SCRIPT%" %*
            exit /b !ERRORLEVEL!
        )
    )
)

where uv >nul 2>nul
if not errorlevel 1 (
    uv run --no-project python "%SCRIPT%" %*
    exit /b !ERRORLEVEL!
)

echo ERROR: /project_team could not find a working Python interpreter (tried python3, python, and 'uv run'). Install Python or uv, then retry.
exit /b 1
