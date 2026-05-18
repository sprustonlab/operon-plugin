@echo off
rem Stop hook wrapper (Windows companion to stop-wrapper).

setlocal EnableDelayedExpansion

set "PLUGIN_ROOT=%~dp0.."
for %%I in ("%PLUGIN_ROOT%") do set "PLUGIN_ROOT=%%~fI"

set "SCRIPT=%PLUGIN_ROOT%\hooks\stop.py"

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

1>&2 echo operon-plugin stop hook: no python with deps.
echo {}
exit /b 0
