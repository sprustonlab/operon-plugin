@echo off
rem PreToolUse hook wrapper (Windows companion to pretooluse-wrapper).
rem Same three-path resolution: uv -> pre-flight-import python -> loud-
rem error fail-open. Cross-platform per SPEC section 2.

setlocal EnableDelayedExpansion

set "PLUGIN_ROOT=%~dp0.."
for %%I in ("%PLUGIN_ROOT%") do set "PLUGIN_ROOT=%%~fI"

set "SCRIPT=%PLUGIN_ROOT%\hooks\pretooluse.py"

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

rem Fall-open allow per the bash wrapper's policy.
1>&2 echo operon-plugin pretooluse hook: no python with required deps.
1>&2 echo Install uv or pip install mcp watchdog PyYAML.
echo {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}
exit /b 0
