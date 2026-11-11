# TrimReaper installer for Windows:  irm https://github.com/michael-berardi/TrimReaper/releases/latest/download/install.ps1 | iex
# TrimReaper runs on macOS only: its scheduler needs launchd and its dedupe and compression need APFS, which Windows
# (and Linux) do not have. This script therefore refuses and installs nothing, on Windows and under PowerShell 7 on
# Linux alike. On a Mac, run install.sh instead.
$ErrorActionPreference = 'Stop'
throw 'TrimReaper supports macOS only (its scheduler needs launchd and its dedupe and compression need APFS), so nothing was installed on this system; on a Mac run install.sh instead.'