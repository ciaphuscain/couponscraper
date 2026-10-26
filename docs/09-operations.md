# Operations: Windows install, configuration, backup, troubleshooting

Everything below runs in **PowerShell** as your normal user (no admin rights needed, except to
install Python/Git if they're missing). Nothing here deletes existing data.

## 1. Prerequisites

```powershell
# Check what you already have
python --version          # need 3.11 or newer
git --version

# Install if missing (winget ships with Windows 10 21H2+/11)
winget install --id Python.Python.3.13 -e
winget install --id Git.Git -e
# Close and reopen PowerShell afterwards so PATH updates.
```

## 2. Install CouponHunter

**One command** (recommended; idempotent, never deletes data):

```powershell
git clone https://github.com/ciaphuscain/couponscraper.git; cd couponscraper
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -AutoStart     # add -Demo for synthetic sample data
```

Or step by step:

```powershell
cd $HOME
git clone https://github.com/ciaphuscain/couponscraper.git
cd couponscraper

python -m venv .venv
# If activation is blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\Activate.ps1
python -m pip install -e .          # no third-party dependencies are downloaded

couponhunter init                   # creates %LOCALAPPDATA%\CouponHunter (db, token, backups, logs)
# Optional, to explore with clearly-labelled SYNTHETIC data first:
# couponhunter init --demo
```

**Validate:**
```powershell
python -m unittest discover -s tests -t .      # expect "OK"
couponhunter sources                           # extension + manual sources listed
```

## 3. Run it

```powershell
couponhunter serve        # dashboard + API + scheduler on http://127.0.0.1:8765
```
Open http://127.0.0.1:8765 and sign in with the output of `couponhunter token`.

**Validate (second PowerShell window):**
```powershell
Invoke-RestMethod http://127.0.0.1:8765/healthz        # -> ok : True
```

### Start automatically at logon (always-on laptop)

```powershell
$py = (Resolve-Path .\.venv\Scripts\python.exe).Path   # python.exe directly: one process to stop, no locked launcher
$action   = New-ScheduledTaskAction -Execute $py -Argument "-m couponhunter serve" -WorkingDirectory (Get-Location).Path
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName "CouponHunter" -Action $action -Trigger $trigger -Settings $settings
# Re-running this fails with "already exists"; to update: Unregister-ScheduledTask CouponHunter -Confirm:$false, then re-register.

Start-ScheduledTask CouponHunter
Get-ScheduledTask CouponHunter | Get-ScheduledTaskInfo     # LastTaskResult 267009 = running
```

**Updating later:** `git pull`, then re-run `.\scripts\setup.ps1`. It stops the task, updates, and
restarts it. If you started the server by hand, stop it first (Ctrl+C).

To keep the laptop awake for alerts, set *Settings → System → Power → Sleep* to "Never" when
plugged in.

## 4. Telegram alerts (optional, free)

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Send any message to your new bot, then find your chat id:

```powershell
$token = Read-Host "Bot token"         # not saved in shell history
(Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates").result[-1].message.chat.id
```

3. Store both as *user* environment variables (never in the database or repository):

```powershell
[Environment]::SetEnvironmentVariable("COUPONHUNTER_TELEGRAM_TOKEN", $token, "User")
[Environment]::SetEnvironmentVariable("COUPONHUNTER_TELEGRAM_CHAT_ID", "<chat id from step 2>", "User")
Remove-Variable token
```

4. Restart `couponhunter serve` (or the scheduled task). The bot starts automatically (disable with
   `serve --no-bot`); send it `/help`. Then test alerts with
   `couponhunter alerts --dry-run` (prints, sends nothing) and `couponhunter alerts --channel telegram`.

Without Telegram, alerts go to `%LOCALAPPDATA%\CouponHunter\alerts.jsonl`.

## 5. Configuration reference

| Setting | Where | Default | Meaning |
|---|---|---|---|
| `COUPONHUNTER_HOME` | env | `%LOCALAPPDATA%\CouponHunter` | Data folder |
| `COUPONHUNTER_PORT` / `--port` | env / CLI | 8765 | Loopback port (update the extension's options too) |
| `COUPONHUNTER_HOST` | env | 127.0.0.1 | Only loopback values are accepted |
| `COUPONHUNTER_TELEGRAM_TOKEN`, `_CHAT_ID` | env | — | Telegram channel |
| `profile` | Settings page / `couponhunter settings profile '{"is_prime":true,"is_new_customer":false}'` | unknown | Unknown makes Prime/new-customer offers "unconfirmed" |
| `wallet` | Settings page | `[]` | Payment methods: `id,label,type,bank,network,cost_ratio_bp`. Types: credit_card, debit_card, upi, netbanking, emi, amazon_pay_balance |
| `min_savings_paise`, `min_savings_pct_bp` | Settings | ₹200, 3% | Alert only above both |
| `cooldown_hours` | Settings | 12 | Per-product alert cooldown |
| `quiet_hours` | Settings | 22:30–07:30 IST | Deferred delivery |
| `price_fresh_hours` | Settings | 12 | Older prices are stale: no alerts, warning shown |
| `stacking_defaults` | Settings | `{}` | Owner-confirmed pair rules, e.g. `{"bank_instant+product_coupon":"yes"}` |
| `couponhunter import-email PATH [--max-age-days N]` | CLI | 30 days | Import offers from saved emails |
| `extra_redemption_domains` | `couponhunter settings extra_redemption_domains '["www.examplebank.com"]'` | `[]` | Allow official links on more https domains |
| `include_hypothetical_instruments` | Settings | off | Also show offers for cards you don't hold |
| Per-source `min_interval_s`, `stale_after_h` | `sources` table (set at registration) | 3600 s, 48 h | Fetch spacing; freshness window |

## 6. Backup and recovery

* Automatic: the nightly job writes `backups\couponhunter-<UTC time>.db` and keeps the newest 14.
  Each backup is integrity-checked.
* Manual: `couponhunter backup` (or `--dest D:\Backups`). It is safe while the server runs.
* Restore:

```powershell
Stop-ScheduledTask CouponHunter            # or Ctrl+C the server
couponhunter restore "$env:LOCALAPPDATA\CouponHunter\backups\couponhunter-20261025T000000Z.db"   # dry check
couponhunter restore "<same path>" --yes   # copies your current DB aside first, then restores
Start-ScheduledTask CouponHunter
```

Copy the `backups` folder to another disk or cloud folder occasionally. It contains your purchase
history, so treat it as private.

## 7. Troubleshooting

Start with **`couponhunter doctor`**. It checks Python, the data folder, database integrity, token
permissions, whether the server runs (or another program holds the port), extension contact,
wallet/profile, stale watchlist prices, sources, scheduler, backups, alert delivery and Telegram
(`--offline` skips the network check), and prints the fix for each problem.

| Symptom | Cause | Fix |
|---|---|---|
| Panel says "backend not reachable" | Server not running / wrong port | `Invoke-RestMethod http://127.0.0.1:8765/healthz`; check the extension options URL |
| Panel says "Set your API token" or 401 | Token missing/rotated | `couponhunter token` → paste in extension options |
| Dashboard shows "unexpected Host header" (421) | Opened via another hostname | Use `http://127.0.0.1:8765` or `http://localhost:8765` |
| "Couldn't read the price on this page" | Amazon changed its layout | Enter the price on the product's dashboard page; report the selector in `extension/lib.js` |
| Offer text "not understood" | Phrasing the parser doesn't know | Add it via Offers → "Add an offer you've seen" with the fields; parser improvements go in `normalize.py` |
| Source disabled on Health page | Auth refusal, bad config, or 5 failures | Read the reason. Fix config and `UPDATE sources SET enabled=1, consecutive_failures=0, disabled_reason=NULL WHERE id='…'` (sqlite3), or re-register |
| No alerts arriving | Quiet hours, threshold, stale price, cooldown | `couponhunter alerts --dry-run`; Health → Alert outbox shows suppressed/deferred/failed with reasons |
| `database is locked` | Another long-running process holds a write lock | Close other tools using the DB; writes retry for 5 s |
| `₹` shows as `?` in old consoles | Legacy code page | Use Windows Terminal; the CLI already forces UTF-8 output |
| Port in use | Another app on 8765 | `couponhunter --port 8766 serve` and update the extension options |

## 8. Runbook (weekly, 5 minutes)

1. Health page: all sources green? Any `failed` alerts?
2. History page: move cashback records along (pending → confirmed → paid).
3. Look at offers you marked "Didn't apply" and flag them invalid if they're dead.
4. Check that `backups` has recent files.

## 9. Cost model (₹0 recurring)

| Watched products | CPU | Disk growth | Network | Notes |
|---|---|---|---|---|
| 10 | Negligible (alert job: tens of ms every 15 min) | ~1–2 MB/year (prices + alerts) | Telegram messages only | |
| 100 | Alert job ≈ 1.4 s cold per run on the benchmark VM (realistic set) | ~10–20 MB/year | Same | Fine on a 2015 laptop |
| 1,000 | Alert job ≈ 15 s cold per 15-min run (extrapolated, not measured) | ~150 MB/year | Same | First bottleneck: per-product optimisation in `alerts.decide`; fix by evaluating only products whose data changed |

The binding constraint is not compute but **data access**: prices only arrive when you view a page
or enter them, so more watched products don't mean more fresh data. Telegram, SQLite and Python
are free. Optional paid upgrades (none needed): none are recommended.
