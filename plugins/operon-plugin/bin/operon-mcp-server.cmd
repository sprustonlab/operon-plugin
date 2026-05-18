@echo off
rem operon-plugin MCP server entry shim, Windows companion to
rem `operon-mcp-server` (bash). Mirrors the same three-path
rem resolution: uv -> pre-flight-import python -> clear-error.
rem
rem Cross-platform per SPEC section 2. Tested manually against
rem `claude --bg` on Linux/macOS via the bash shim; Windows path
rem is implemented to spec but not empirically smoke-tested at
rem the time of writing (no Windows machine available to Boaz).
rem Issues should land on Carryover #4 follow-up.

setlocal EnableDelayedExpansion

rem Plugin root = parent dir of this script.
set "PLUGIN_ROOT=%~dp0.."
for %%I in ("%PLUGIN_ROOT%") do set "PLUGIN_ROOT=%%~fI"

rem Prepend src/ to PYTHONPATH so the operon_mcp_server package is
rem importable regardless of which python ends up running us. On
rem Windows the path separator is `;`.
if defined PYTHONPATH (
    set "PYTHONPATH=%PLUGIN_ROOT%\src;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%PLUGIN_ROOT%\src"
)

rem Path 1: uv run.
where uv >nul 2>nul
if not errorlevel 1 (
    uv run --project "%PLUGIN_ROOT%" python -m operon_mcp_server.server %*
    exit /b %ERRORLEVEL%
)

rem Path 2: pre-flight-import check on python then python3.
rem (Order swapped vs POSIX: on Windows, `python` is the standard
rem name; `python3` is rare unless the user has the launcher.)
for %%P in (python python3) do (
    where %%P >nul 2>nul
    if not errorlevel 1 (
        %%P -c "import mcp, watchdog, yaml" >nul 2>nul
        if not errorlevel 1 (
            %%P -m operon_mcp_server.server %*
            exit /b !ERRORLEVEL!
        )
    )
)

rem Path 3: clear-error fallback.
1>&2 echo operon-plugin: cannot launch MCP server -- no working python found.
1>&2 echo.
1>&2 echo Either install uv (recommended):
1>&2 echo   https://docs.astral.sh/uv/getting-started/installation/
1>&2 echo.
1>&2 echo Or pip install the three runtime deps into the python that
1>&2 echo Claude Code finds on PATH:
1>&2 echo.
1>&2 echo   pip install "mcp^>=1.0" "watchdog^>=4.0" "PyYAML^>=6.0"
exit /b 1
