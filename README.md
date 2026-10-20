# bcast-monitor

Watches vSphere for broadcast storms and VM-internal bridging loops and alerts via
Telegram and/or Slack.

## How it works

Every `POLL_INTERVAL` seconds the container reads the vCenter **real-time**
performance counters (20 s samples) for every vNIC of every powered-on VM:

| Counter | Meaning (VM perspective) | Used for |
|---|---|---|
| `net.broadcastTx.summation` | broadcasts the VM **sends** | finding the **source** |
| `net.broadcastRx.summation` | broadcasts the VM **receives** | detecting the **storm** |

Two kinds of findings are raised:

* **Broadcast source**: a vNIC sends more than `TX_THRESHOLD_PPS`. If two or more
  vNICs of the same VM do this on the same network, the alert says it is most likely
  a bridge or loop inside the guest. Typical case: a virtual firewall
  with an inline/transparent interface pair whose two vNICs sit on the same portgroup.
* **Broadcast storm**: at least `RX_PORTS_MIN` vNICs in one portgroup receive more
  than `RX_THRESHOLD_PPS`. The alert names the suspected source, or says that no VM
  in this vCenter is sending at that rate, which points to a source outside it.

Victims of the storm only show high RX, so they are not reported as sources. Each
finding is notified once, with a reminder every `REMIND_MINUTES` while it lasts and
a "resolved" message after `RESOLVE_POLLS` quiet polls.

No SSH to the ESXi hosts is needed. One read-only vCenter account covers all hosts.

## Setup

1. **vCenter account**: create a user with the built-in **Read-only** role on the
   vCenter root (propagate).
2. **Telegram bot**: create a bot with @BotFather and note the token. Add the bot to
   your group or send it a message, then read the chat ID from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (group IDs are negative).
3. **Slack (optional)**: create an incoming webhook and set `SLACK_WEBHOOK_URL`.
4. Configure and start. The image is published for amd64 and arm64 as
   `ghcr.io/steiner-dominik/vmware-broadcast-monitor` (tags `latest` and the release
   version, e.g. `1.0.0`), so the monitoring host only needs `docker-compose.yml` and `.env`:

```sh
cp .env.example .env      # fill in VC_* and TELEGRAM_* / SLACK_*
docker compose pull
docker compose run --rm bcast-monitor --test-notify   # test message to all channels
docker compose run --rm bcast-monitor --once          # top-25 table, no alerts
docker compose up -d
docker compose logs -f
```

Without compose:

```sh
docker run -d --name bcast-monitor --restart unless-stopped --env-file .env \
  ghcr.io/steiner-dominik/vmware-broadcast-monitor:latest
```

To build from source instead, run `docker compose build`.

## Tuning

Values seen in a lab incident: the normal baseline is about 5–15 bcast pps per
vNIC. During an in-guest bridging loop it was about 3700–4000 pps on every vNIC in the VLAN.
The defaults (TX 500 / RX 1000 pps) sit well clear of both.

* Lab-measured with this container (vCenter 8.0 and 9.1, quiet day): RX 4–42 bcast pps per vNIC (highest on trunk / NSX edge uplink portgroups), TX below
  1.5 pps everywhere. One poll of ~370 vNICs takes about 0.5 s.
* Run `--once` a few times on a normal day. If anything legitimately sits near the
  thresholds (nested ESXi, sniffers, some appliances), raise the threshold or add it
  to `EXCLUDE_VMS` / `EXCLUDE_NETWORKS`.
* Detection delay: real-time samples are 20 s, vCenter delivers them with some lag,
  and `MIN_HOT_SAMPLES=2` needs about 40 s of storm. Expect an alert within roughly
  1–2 minutes. For faster detection, use `POLL_INTERVAL=20` with `WINDOW_SAMPLES=2`
  and `MIN_HOT_SAMPLES=2`.
* `INCLUDE_MULTICAST=true` adds multicast to both counters (mDNS/SSDP loops).

## Limits

* Only VMs registered in this vCenter are seen. Host vmkernel ports and storms that
  originate in the physical network show up only as an RX storm with no VM source.
* vCenter must reach the hosts. If the storm also takes down the management VLAN,
  polling fails and you get a "cannot poll" alert after `FAIL_ALERT_POLLS` failures
  instead.
* This only detects storms. Prevention still needs Forged Transmits = Reject on
  shared portgroups, plus storm-control / CoPP on the physical switches.

## Development

```sh
pip install -r requirements.txt -r requirements-dev.txt
ruff check . && pytest -q
```

Releases: bump `__version__` in `monitor.py`, then push a matching tag (`git tag v1.0.1 && git push --tags`).
The *Publish image* workflow runs the tests and pushes `ghcr.io/steiner-dominik/vmware-broadcast-monitor:<version>`
and `:latest`.
