# bcast-monitor: context for Claude Code

## Background (incident)
- Environment: ESXi 8 hosts on blade servers, vDS with NSX prepared (vdr port present).
  Lab cluster used by many people, who deploy their own appliances.
- Incident: a virtual firewall appliance **fw01** (10 vmxnet3 vNICs) on host **esxi05**.
  - eth0, eth1 and eth4–9 are on dvportgroup-100.
  - **eth2 and eth3 are on dvportgroup-200.** The guest bridged them (transparent/inline
    interface pair), which caused a broadcast loop through the vDS.
  - Effect: broadcast storm, and an old physical switch's CPU was overwhelmed (no ARP replies).
    The vNICs have since been disconnected.
- Measured with vsish per-port counters (TX+RX broadcast summed):
  - normal baseline: about 5–15 bcast pps per vNIC
  - during the loop: about 3700–4000 pps on every vNIC in VLAN 200. fw01 was the source;
    app01, dc01/dc02, web01 and others were only receiving the flood.
- Key insight: separate **TX (VM sends)** from **RX (VM receives)**. The loop source shows high TX;
  victims show only high RX.

## Useful ESXi one-liners
VM/vmk → physical uplink pinning:
```sh
net-stats -l | awk '$2==5||$2==3{c=$6;for(i=7;i<=NF;i++)c=c" "$i;print $1,$4,c}' | while read p s n; do printf "%-45s %-14s %s\n" "$n" "$s" "$(vsish -e get /net/portsets/$s/ports/$p/teamUplink 2>/dev/null)"; done
```
Top broadcast talkers over 10 s (TX+RX combined):
```sh
s(){ net-stats -l | awk '$2==5{print $1,$4,$6}' | while read p sw n; do vsish -e get /net/portsets/$sw/ports/$p/stats 2>/dev/null | awk -v n="$n" -F: '/pktsTxBroadcast|pktsRxBroadcast/{t+=$2}END{print n,t+0}'; done; }; s >/tmp/bc1; sleep 10; s >/tmp/bc2; awk 'NR==FNR{a[$1]=$2;next}{printf "%-45s %10d bcast pps\n",$1,($2-a[$1])/10}' /tmp/bc1 /tmp/bc2 | sort -k2 -nr | head -15
```

## Prevention measures discussed (not part of this repo)
- Forged Transmits = **Reject** on shared portgroups; the affected portgroups allowed it.
- Dedicated portgroups/VLANs for appliance data interfaces, with each side of a bridge in a
  different VLAN.
- Physical side: storm-control on the fabric / top-of-rack and upstream switch ports,
  plus CoPP / ARP rate-limit on older switches.
- NSX segment security profile **rate limits** (Rx/Tx broadcast/multicast pps) for lab segments.
- vDS traffic shaping is only a weak backstop (it limits bandwidth, not pps).

## This repo: bcast-monitor
A Docker container that polls **vCenter real-time perf counters** (20 s samples, per vNIC instance =
device key such as "4000") via pyVmomi, using a read-only account. No SSH to the ESXi hosts.
- Counters used: `net.broadcastTx.summation`, `net.broadcastRx.summation`
  (optionally multicast with `INCLUDE_MULTICAST`). pps = value / sample interval.
- Findings:
  - **source**: a vNIC with TX ≥ `TX_THRESHOLD_PPS` (default 500). If ≥2 vNICs of one VM do this on
    the same network, the alert flags it as a likely in-guest bridge/loop.
  - **storm**: ≥ `RX_PORTS_MIN` (default 5) vNICs in one portgroup with RX ≥ `RX_THRESHOLD_PPS`
    (default 1000). The alert names the suspected source VM, or says the source is "outside" if no
    VM is sending at that rate.
  - A port is hot if ≥ `MIN_HOT_SAMPLES` (2) of the last `WINDOW_SAMPLES` (3) samples exceed the
    threshold. Poll interval is 60 s.
- Lifecycle (`Tracker`): new → reminder every `REMIND_MINUTES` → resolved after `RESOLVE_POLLS`
  quiet polls. There is a "cannot poll vCenter" alert after `FAIL_ALERT_POLLS` failures, and an
  optional startup message.
- Notifiers: Telegram (Bot API, HTML parse mode, optional forum thread id) and Slack (incoming
  webhook). Both are optional and can run at the same time.
- Inventory (PropertyCollector, refreshed every `INVENTORY_REFRESH` s): powered-on VMs, vNIC
  label/MAC/network, dvportgroup names with VLAN id, host names. `EXCLUDE_VMS` and
  `EXCLUDE_NETWORKS` are regexes.
- CLI: `--once` prints the top-25 TX/RX table; `--test-notify` sends a test message.

Files: `monitor.py` (all logic), `Dockerfile` (python:3.12-slim, non-root), `docker-compose.yml`,
`.env.example` (all settings documented; comments on their own lines), `README.md`, `requirements.txt`.

## Status
- Image: `ghcr.io/steiner-dominik/vmware-broadcast-monitor` (amd64+arm64), published by
  `.github/workflows/publish.yml` on `v*` tags (tag must match `__version__` in `monitor.py`).
  `ci.yml` runs `ruff check`, `pytest` (tests/test_detection.py, incident-shaped data) and a build.
- Tested live (2026-10-20) with the container against two lab vCenters (8.0.3 with ~180 VMs /
  ~370 vNICs, and 9.1 with ~60 VMs): `--once`, the polling loop
  with lowered thresholds (findings + lifecycle), SIGTERM shutdown, and the "cannot poll" alert.
  Quiet-day baseline: RX 4–42 bcast pps per vNIC (trunks/NSX edge uplinks highest), TX < 1.5 pps.
  TX/RX direction confirmed: TX = what the VM sends.
- Telegram/Slack sending was not tested against the real APIs (no bot token/webhook available).
- The code keeps its compact hand formatting; CI runs `ruff check`, not `ruff format`.

## Possible next steps
- Watch a few normal days with `--once` and tune thresholds / EXCLUDE_* per lab.
- Optionally add host-level vmnic broadcast counters (to catch storms that come from outside).
- Optionally expose Prometheus `/metrics` for Grafana.
- Optionally add an SSH/vsish collector mode for faster (sub-20 s) detection.
