# Telemetry

Telemetry is a local dashboard for live CPU, memory, NVIDIA GPU, disk, network,
and running-process activity. It runs on your computer and serves the dashboard
at `http://127.0.0.1:6767`.

## Quick start

Install Python, then run these commands from the project folder:

```powershell
python -m pip install -r requirements.txt
python dashboard.py
```

Open [http://127.0.0.1:6767](http://127.0.0.1:6767) in your browser. Press
**Ctrl+C** in the terminal to stop the dashboard.

The server listens on localhost by default. To use another port, run
`python dashboard.py --port 9000`, then open `http://127.0.0.1:9000`.

## Windows app window (optional)

The Windows launcher opens the dashboard in an app-style browser window. It
requires an existing Conda environment named `telemetry` with the project
dependencies installed.

1. Build the launcher once:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build-app.ps1
   ```

2. Double-click `launch-dashboard.cmd`.

Close the app window to stop monitoring. To stop it from a terminal, run
`.\launch-dashboard.cmd -Stop`.

## What it shows

- Live hardware readings and history charts.
- Running processes, their resource use, and Conda environment.
- NVIDIA GPU details when `nvidia-smi` is installed and available.
- Optional GitHub Copilot usage.

Use the dashboard controls to pause updates, change chart history, simplify the
layout, or clear chart history. Telemetry history is kept in memory and resets
when the server restarts. Display preferences are saved in the browser.

## GitHub Copilot usage (optional)

To view Copilot usage, expand **Sign-in setup** in the dashboard. Create a
[GitHub OAuth app](https://github.com/settings/applications/new), enable
**Device Flow**, set its homepage and callback URL to
`http://127.0.0.1:6767`, then enter its public **Client ID**. If you chose a
different port, use that dashboard address instead. Do not enter a client
secret or access token. Sign in through GitHub and approve the app.

The app requests the `read:user` scope and sends usage requests to GitHub. The
sign-in token stays in server memory; usage availability depends on GitHub and
your Copilot account. Usage refreshes every 30 seconds by default. The app also
supports existing `COPILOT_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN` environment
variables and GitHub CLI sign-in.

## Privacy and data

Telemetry is collected from your computer and served locally by default. The
dashboard may show process names and IDs, hardware details, and resource usage.
It does not save telemetry history or sign-in tokens to disk. Browser display
preferences and the app's browser profile may persist locally.

## Troubleshooting

- If the page does not open, check the terminal for errors and confirm the
  dashboard is running on the displayed port.
- NVIDIA readings require `nvidia-smi` on `PATH`; other hardware readings remain
  available without it.
- Some Windows temperature and fan sensors require
  [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  or OpenHardwareMonitor running as administrator.
