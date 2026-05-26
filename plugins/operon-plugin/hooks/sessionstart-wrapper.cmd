@echo off
rem SessionStart hook wrapper (Windows companion to sessionstart-wrapper).
rem Same three-path resolution: uv -> pre-flight-import python -> loud-
rem error then exit 0 (best-effort marker). Cross-platform per SPEC
rem section 2.

setlocal EnableDelayedExpansion

set "PLUGIN_ROOT=%~dp0.."
for %%I in ("%PLUGIN_ROOT%") do set "PLUGIN_ROOT=%%~fI"

set "SCRIPT=%PLUGIN_ROOT%\hooks\sessionstart.py"

if defined PYTHONPATH (
    set "PYTHONPATH=%PLUGIN_ROOT%\src;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%PLUGIN_ROOT%\src"
)

where uv >nul 2>nul
if not errorlevel 1 (
    uv run --project "%PLUGIN_ROOT%" python "%SCRIPT%" %*
    exit /b %ERRORLEVEL%
)

for %%P in (python python3) do (
    where %%P >nul 2>nul
    if not errorlevel 1 (
        %%P -c "import mcp, watchdog, yaml" >nul 2>nul
        if not errorlevel 1 (
            %%P "%SCRIPT%" %*
            exit /b !ERRORLEVEL!
        )
    )
)

rem Best-effort marker: loud error, then exit 0 so the session starts.
1>&2 echo operon-plugin sessionstart hook: no python with required deps.
1>&2 echo Install uv or pip install mcp watchdog PyYAML.
exit /b 0
