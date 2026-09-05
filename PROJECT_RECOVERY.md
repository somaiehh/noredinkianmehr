# TABDEAL RADAR — PROJECT RECOVERY

Last recovery checkpoint: 2026-09-05

## 1. Project Identity

Project:
TABDEAL PRE-PUMP RADAR / Early Movement Hunting System

Main working directory:
~/tabdeal_cloud

GitHub repository:
somaiehh/noredinkianmehr

Main branch:
main

Primary goal:
Detect potentially strong movements in Tabdeal IRT spot markets as early as possible,
preferably before the main pump.

IMPORTANT:
This system is an analytical radar. It does not place orders automatically.

---

## 2. Core Market Rules

- Monitor Tabdeal IRT spot markets.
- Focus on early movement rather than coins that already completed a large pump.
- Major coins are generally excluded from hunting:
  BTCIRT
  ETHIRT
  SOLIRT
  XRPIRT
  ADAIRT
  BNBIRT
  TRXIRT
  DOGEIRT
- USDTIRT is also excluded from Early Watch.
- Main signals include:
  price movement
  abnormal/rising volume
  volume acceleration
  buy pressure
  Buy/Sell strength
  order-book information
  breakout
  persistence
  Hunt Score

Important analysis horizons:
15M
1H
4H
12H

---

## 3. Main Radar

Main engine:
radar_live.py

Main historical/data file:
tabdeal_radar_v21_data.json

Persistence:
persistence_state.json

Important statuses include:
SCANNED
WATCH_ACCUMULATION
PRE_EARLY
EARLY

The radar scans Tabdeal markets and stores historical observations.

---

## 4. Green V2.1

Green V2.1 is the more selective/confirmed hunting layer.

Important files:
green_logger.py
green_v21_log.json

Green V2.1 must NOT be casually replaced or weakened.

Early Watch was added as a SEPARATE experimental layer so that earlier opportunities
can be studied without damaging the Green V2.1 logic.

---

## 5. Early Watch

Early Watch was added on 2026-09-05 as an experimental early-warning layer.

Main file:
early_watch_logger.py

Log:
early_watch_log.json

Purpose:
Catch promising coins earlier than Green V2.1 and measure what happens afterward.

Levels:
WATCH
STRONG

STRONG is the higher-priority early signal.

Current experimental logic was selected after historical backtesting.
Hunt Score, EARLY/PRE_EARLY status, P15 and other radar metrics were tested.

A 2-hour per-symbol deduplication rule is used to reduce repeated alerts/events.

DO NOT assume Early Watch is a direct BUY signal.
It is an experimental watch/early-warning system.

---

## 6. Early Watch Outcome Tracking

Early Watch tracks future performance after an event at:

15 minutes
1 hour
4 hours
12 hours

The logger gradually fills outcome fields when enough future data exists.

This is used to determine which Early Watch conditions actually work prospectively.

Historical tests showed that 4H and 12H horizons were more informative than 15M
for this early-warning concept.

Do not optimize the rules based on only a few new observations.
Collect more forward/live data before major rule changes.

---

## 7. GitHub Actions Automation

Workflow:
.github/workflows/radar_schedule.yml

Workflow name:
Tabdeal Radar Scan

The cloud workflow runs automatically on GitHub Actions.

Important workflow stages include:

- Checkout repository
- Setup Python
- Install dependencies
- Run Tabdeal radar
- Update Green V2.1 log
- Update Early Watch log
- Save new radar data

Early Watch command:
python -u early_watch_logger.py

Files committed by the automated workflow include:
tabdeal_radar_v21_data.json
persistence_state.json
green_v21_log.json
early_watch_log.json

A verified GitHub Actions run successfully completed:
Run ID 33920750347

It showed:
Run Tabdeal radar          SUCCESS
Update Green V2.1 log      SUCCESS
Update Early Watch log     SUCCESS
Save new radar data        SUCCESS

Therefore Early Watch is running in the cloud and does not depend on Termux
remaining open.

---

## 8. Failure Monitoring

A ChatGPT scheduled/conditional task named:

هشدار شکست اسکن رادار

was created.

Purpose:
Periodically check GitHub Actions for new failures of Tabdeal Radar Scan.

Expected behavior:
- Successful runs: no alert needed.
- New failed run: notify user and identify failed step when possible.

---

## 9. Important Files

Core:
radar_live.py
index.html
requirements.txt

Radar data:
tabdeal_radar_v21_data.json
persistence_state.json

Green:
green_logger.py
green_v21_log.json

Early Watch:
early_watch_logger.py
early_watch_log.json

Automation:
.github/workflows/radar_schedule.yml

Recovery documentation:
PROJECT_RECOVERY.md

---

## 10. Backup Status

A full Termux project backup was created:

tabdeal_cloud_backup_20260905_0702.tar.gz

Approximate size:
87 MB

The archive was tested with:

tar -tzf ~/tabdeal_cloud_backup_20260905_0702.tar.gz > /dev/null

Result:
BACKUP OK

Important files confirmed inside the archive:

tabdeal_cloud/index.html
tabdeal_cloud/radar_live.py
tabdeal_cloud/tabdeal_radar_v21_data.json
tabdeal_cloud/.github/workflows/radar_schedule.yml
tabdeal_cloud/green_logger.py
tabdeal_cloud/early_watch_logger.py

A copy was placed in Android Downloads and uploaded to Google Drive.

An older tabdeal_dashboard backup also exists in Google Drive.

GitHub is another independent recovery source.

---

## 11. Recovery If Phone Is Lost

If the phone is lost or destroyed:

1. Sign into the same GitHub account.
2. Sign into Google Drive.
3. Recover the repository from:
   somaiehh/noredinkianmehr
4. If local/experimental files are required, download:
   tabdeal_cloud_backup_20260905_0702.tar.gz
5. Extract the backup.
6. Read PROJECT_RECOVERY.md before changing radar logic.
7. Verify GitHub Actions.
8. Verify Tabdeal Radar Scan is active.
9. Verify Green V2.1 and Early Watch are both running.
10. Continue collecting live Early Watch results before changing thresholds.

---

## 12. Instructions To A Future ChatGPT

If this file is provided to ChatGPT in a future conversation:

Treat this document as the recovery checkpoint for an existing project.

Do NOT rebuild the radar from scratch unless explicitly requested.

First inspect the current GitHub repository and current files.

Preserve:
- Tabdeal IRT-only hunting focus
- Green V2.1
- Early Watch as a separate experimental layer
- 2-hour Early Watch deduplication
- 15M / 1H / 4H / 12H outcome tracking
- cloud GitHub Actions automation

Before modifying thresholds:
compare new forward/live Early Watch results against existing results.

Primary objective:
Improve detection BEFORE the main pump while controlling false signals
and avoiding signals that arrive after a large move.

END OF RECOVERY CHECKPOINT
