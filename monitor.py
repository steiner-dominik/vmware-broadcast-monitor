#!/usr/bin/env python3
"""
bcast-monitor - detect broadcast storms and in-guest bridging loops on vSphere
and alert via Telegram and/or Slack.

Data source: vCenter real-time performance counters (20 s samples) per vNIC,
reported from the VM's point of view:

  net.broadcastTx.summation  -> broadcasts the VM SENDS     (loop source)
  net.broadcastRx.summation  -> broadcasts the VM RECEIVES  (storm victims)

A bridging VM shows high TX on two or more vNICs (usually in the same network).
Every other VM in that VLAN only shows high RX. That is how source and victims
are told apart.
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import re
import signal
import ssl
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

import requests
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

__version__ = "1.0.0"

log = logging.getLogger("bcast-monitor")

RT_INTERVAL = 20  # vCenter real-time sampling interval in seconds


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def _env(name, default=None):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def _env_bool(name, default=False):
    v = _env(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


def _env_re(name):
    v = _env(name)
    return re.compile(v, re.I) if v else None


class Config:
    def __init__(self):
        self.vc_host = _env("VC_HOST")
        self.vc_port = int(_env("VC_PORT", "443"))
        self.vc_user = _env("VC_USER")
        self.vc_password = _env("VC_PASSWORD")
        pw_file = _env("VC_PASSWORD_FILE")
        if pw_file and not self.vc_password:
            with open(pw_file, encoding="utf-8") as fh:
                self.vc_password = fh.read().strip()
        self.vc_insecure = _env_bool("VC_INSECURE", False)
        self.vc_ca_file = _env("VC_CA_FILE")
        self.vc_name = _env("VC_NAME", self.vc_host or "vCenter")

        self.poll_interval = max(RT_INTERVAL, int(_env("POLL_INTERVAL", "60")))
        self.window = max(1, int(_env("WINDOW_SAMPLES", "3")))
        self.min_hot = max(1, min(self.window, int(_env("MIN_HOT_SAMPLES", "2"))))
        self.tx_threshold = float(_env("TX_THRESHOLD_PPS", "500"))
        self.rx_threshold = float(_env("RX_THRESHOLD_PPS", "1000"))
        self.rx_ports_min = int(_env("RX_PORTS_MIN", "5"))
        self.include_multicast = _env_bool("INCLUDE_MULTICAST", False)

        self.remind_minutes = int(_env("REMIND_MINUTES", "15"))
        self.resolve_polls = max(1, int(_env("RESOLVE_POLLS", "2")))
        self.inventory_refresh = int(_env("INVENTORY_REFRESH", "300"))
        self.fail_alert_polls = int(_env("FAIL_ALERT_POLLS", "5"))
        self.query_batch = int(_env("QUERY_BATCH", "64"))

        self.exclude_vms = _env_re("EXCLUDE_VMS")
        self.exclude_networks = _env_re("EXCLUDE_NETWORKS")

        self.telegram_token = _env("TELEGRAM_BOT_TOKEN")
        self.telegram_chat = _env("TELEGRAM_CHAT_ID")
        self.telegram_thread = _env("TELEGRAM_THREAD_ID")
        self.slack_webhook = _env("SLACK_WEBHOOK_URL")
        self.startup_message = _env_bool("STARTUP_MESSAGE", True)

    def validate_vcenter(self):
        missing = [n for n, v in (("VC_HOST", self.vc_host), ("VC_USER", self.vc_user),
                                  ("VC_PASSWORD", self.vc_password)) if not v]
        if missing:
            sys.exit(f"missing configuration: {', '.join(missing)}")


# --------------------------------------------------------------------------- #
# vCenter access
# --------------------------------------------------------------------------- #
def connect(cfg: Config):
    ctx = ssl.create_default_context()
    if cfg.vc_insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif cfg.vc_ca_file:
        ctx.load_verify_locations(cfg.vc_ca_file)
    si = SmartConnect(host=cfg.vc_host, port=cfg.vc_port, user=cfg.vc_user,
                      pwd=cfg.vc_password, sslContext=ctx)
    log.info("connected to %s (%s)", cfg.vc_host, si.content.about.fullName)
    return si


def collect(si, obj_type, props):
    """Fetch properties for all objects of a type via one PropertyCollector call."""
    content = si.content
    view = content.viewManager.CreateContainerView(content.rootFolder, [obj_type], True)
    try:
        pc = vmodl.query.PropertyCollector
        traversal = pc.TraversalSpec(name="view", path="view", skip=False,
                                     type=vim.view.ContainerView)
        obj_spec = pc.ObjectSpec(obj=view, skip=True, selectSet=[traversal])
        prop_spec = pc.PropertySpec(type=obj_type, pathSet=props, all=False)
        filter_spec = pc.FilterSpec(objectSet=[obj_spec], propSet=[prop_spec])
        res = content.propertyCollector.RetrievePropertiesEx(
            specSet=[filter_spec], options=pc.RetrieveOptions(maxObjects=500))
        out = []
        while res:
            for o in res.objects:
                d = {p.name: p.val for p in (o.propSet or [])}
                d["_obj"] = o.obj
                out.append(d)
            if not res.token:
                break
            res = content.propertyCollector.ContinueRetrievePropertiesEx(token=res.token)
        return out
    finally:
        view.Destroy()


@dataclass
class Nic:
    key: str
    label: str
    mac: str
    network: str


@dataclass
class VmInfo:
    moid: str
    name: str
    host: str
    obj: object
    nics: dict  # device key (str) -> Nic


class Inventory:
    def __init__(self, vms: dict):
        self.vms = vms
        self.loaded_at = time.time()


def _vlan_label(port_cfg):
    vlan = getattr(port_cfg, "vlan", None) if port_cfg else None
    if vlan is None:
        return ""
    if isinstance(vlan, vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec):
        return f"VLAN {vlan.vlanId}" if vlan.vlanId else ""
    if isinstance(vlan, vim.dvs.VmwareDistributedVirtualSwitch.TrunkVlanSpec):
        return "trunk"
    if isinstance(vlan, vim.dvs.VmwareDistributedVirtualSwitch.PvlanSpec):
        return f"PVLAN {vlan.pvlanId}"
    return ""


def load_inventory(si, cfg: Config) -> Inventory:
    hosts = {h["_obj"]._moId: h.get("name", "?") for h in collect(si, vim.HostSystem, ["name"])}

    pgs = {}
    for pg in collect(si, vim.dvs.DistributedVirtualPortgroup,
                      ["key", "name", "config.defaultPortConfig"]):
        vlan = _vlan_label(pg.get("config.defaultPortConfig"))
        pgs[pg.get("key")] = f"{pg.get('name')} ({vlan})" if vlan else pg.get("name")

    vms = {}
    for v in collect(si, vim.VirtualMachine,
                     ["name", "runtime.powerState", "runtime.host",
                      "config.template", "config.hardware.device"]):
        name = v.get("name", "?")
        if v.get("runtime.powerState") != "poweredOn" or v.get("config.template"):
            continue
        if cfg.exclude_vms and cfg.exclude_vms.search(name):
            continue
        host_ref = v.get("runtime.host")
        nics = {}
        for dev in v.get("config.hardware.device") or []:
            if not isinstance(dev, vim.vm.device.VirtualEthernetCard):
                continue
            nics[str(dev.key)] = Nic(
                key=str(dev.key),
                label=dev.deviceInfo.label if dev.deviceInfo else f"nic {dev.key}",
                mac=dev.macAddress or "",
                network=_nic_network(dev, pgs),
            )
        moid = v["_obj"]._moId
        vms[moid] = VmInfo(moid=moid, name=name,
                           host=hosts.get(host_ref._moId, "?") if host_ref else "?",
                           obj=v["_obj"], nics=nics)
    log.info("inventory: %d powered-on VMs, %d hosts, %d portgroups", len(vms), len(hosts), len(pgs))
    return Inventory(vms)


def _nic_network(dev, pgs):
    b = dev.backing
    if isinstance(b, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
        key = b.port.portgroupKey if b.port else None
        return pgs.get(key, key or "dvs-port")
    if isinstance(b, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
        return b.deviceName or "?"
    if isinstance(b, vim.vm.device.VirtualEthernetCard.OpaqueNetworkBackingInfo):
        return f"opaque:{b.opaqueNetworkId}"
    return "unknown"


class Perf:
    def __init__(self, si, include_multicast: bool):
        self.pm = si.content.perfManager
        by_name = {f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}": c.key
                   for c in self.pm.perfCounter}
        wanted = {"net.broadcastTx.summation": "tx", "net.broadcastRx.summation": "rx"}
        if include_multicast:
            wanted.update({"net.multicastTx.summation": "tx", "net.multicastRx.summation": "rx"})
        self.direction = {}
        for name, d in wanted.items():
            if name not in by_name:
                raise RuntimeError(f"perf counter {name} not available on this vCenter")
            self.direction[by_name[name]] = d
        self.metric_ids = [vim.PerformanceManager.MetricId(counterId=cid, instance="*")
                           for cid in self.direction]

    def _spec(self, vm, window):
        return vim.PerformanceManager.QuerySpec(entity=vm.obj, metricId=self.metric_ids,
                                                intervalId=RT_INTERVAL, maxSample=window)

    def query(self, vms, window, batch):
        """Return {(vm_moid, nic_key): {"tx": [pps...], "rx": [pps...]}}."""
        out = {}
        for i in range(0, len(vms), batch):
            chunk = vms[i:i + batch]
            try:
                results = self.pm.QueryPerf(querySpec=[self._spec(v, window) for v in chunk]) or []
            except vim.fault.NotAuthenticated:
                raise
            except Exception as exc:  # one bad VM fails the whole batch -> retry one by one
                log.debug("batch query failed (%s), retrying per VM", exc)
                results = []
                for v in chunk:
                    try:
                        results.extend(self.pm.QueryPerf(querySpec=[self._spec(v, window)]) or [])
                    except vim.fault.NotAuthenticated:
                        raise
                    except Exception as exc2:
                        log.debug("perf query for %s failed: %s", v.name, exc2)
            for em in results:
                intervals = [s.interval or RT_INTERVAL for s in (em.sampleInfo or [])]
                for series in em.value or []:
                    inst = series.id.instance
                    d = self.direction.get(series.id.counterId)
                    if not inst or d is None:  # "" = aggregate over all vNICs
                        continue
                    vals = list(series.value or [])
                    rec = out.setdefault((em.entity._moId, inst), {"tx": [], "rx": []})
                    pps = [max(val, 0) / (intervals[j] if j < len(intervals) else RT_INTERVAL)
                           for j, val in enumerate(vals)]
                    if rec[d]:
                        n = min(len(rec[d]), len(pps))
                        rec[d] = [rec[d][j] + pps[j] for j in range(n)]
                    else:
                        rec[d] = pps
        return out


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
@dataclass
class PortStat:
    vm_id: str
    vm: str
    host: str
    nic: str
    network: str
    tx: list
    rx: list
    same_net_nics: int = 1

    @staticmethod
    def _avg(v):
        return sum(v) / len(v) if v else 0.0

    @property
    def avg_tx(self):
        return self._avg(self.tx)

    @property
    def avg_rx(self):
        return self._avg(self.rx)

    @property
    def peak_tx(self):
        return max(self.tx, default=0.0)

    @property
    def peak_rx(self):
        return max(self.rx, default=0.0)


def hot(values, threshold, min_hot):
    return sum(1 for v in values if v >= threshold) >= min_hot


def build_ports(raw, inv: Inventory, cfg: Config):
    ports = []
    for (moid, key), d in raw.items():
        vm = inv.vms.get(moid)
        if vm is None:
            continue
        nic = vm.nics.get(key)
        network = nic.network if nic else "?"
        if cfg.exclude_networks and cfg.exclude_networks.search(network):
            continue
        same = sum(1 for n in vm.nics.values() if n.network == network) if nic else 1
        ports.append(PortStat(vm_id=moid, vm=vm.name, host=vm.host,
                              nic=nic.label if nic else f"nic key {key}",
                              network=network, tx=d["tx"], rx=d["rx"], same_net_nics=same))
    return ports


@dataclass
class Finding:
    key: str
    title: str
    lines: list
    peak: float


def evaluate(ports, cfg: Config) -> dict:
    findings = {}

    # 1) sources: vNICs that SEND broadcasts above threshold
    by_vm = defaultdict(list)
    for p in ports:
        if hot(p.tx, cfg.tx_threshold, cfg.min_hot):
            by_vm[p.vm_id].append(p)

    sources_by_net = defaultdict(set)
    for vm_id, plist in by_vm.items():
        plist.sort(key=lambda p: p.nic)
        per_net = defaultdict(list)
        for p in plist:
            per_net[p.network].append(p.nic)
            sources_by_net[p.network].add(p.vm)
        lines = [f"{p.nic} on {p.network}: TX {p.avg_tx:.0f} pps (peak {p.peak_tx:.0f})"
                 for p in plist]
        loop_nets = {n: nics for n, nics in per_net.items() if len(nics) >= 2}
        for net, nics in loop_nets.items():
            lines.append(f"⚠ {len(nics)} vNICs flooding on the same network '{net}' "
                         f"→ likely a bridge/loop inside the guest; disconnect one of them")
        if not loop_nets:
            multi = sorted({p.network for p in plist if p.same_net_nics >= 2})
            for net in multi:
                lines.append(f"Note: VM has several vNICs on '{net}'")
        findings[f"src:{vm_id}"] = Finding(
            key=f"src:{vm_id}",
            title=f"Broadcast source: {plist[0].vm} @ {plist[0].host}",
            lines=lines,
            peak=max(p.peak_tx for p in plist))

    # 2) storms: many vNICs in one network RECEIVING broadcasts above threshold
    rx_by_net = defaultdict(list)
    for p in ports:
        if hot(p.rx, cfg.rx_threshold, cfg.min_hot):
            rx_by_net[p.network].append(p)

    for net, plist in rx_by_net.items():
        if len(plist) < cfg.rx_ports_min:
            continue
        peak = max(p.peak_rx for p in plist)
        vms = {p.vm for p in plist}
        hosts = sorted({p.host for p in plist})
        host_txt = ", ".join(hosts[:6]) + (f" (+{len(hosts) - 6})" if len(hosts) > 6 else "")
        lines = [f"{len(plist)} vNICs / {len(vms)} VMs receive > {cfg.rx_threshold:.0f} bcast pps "
                 f"(peak {peak:.0f} pps)",
                 f"Hosts: {host_txt}"]
        if sources_by_net.get(net):
            lines.append("Suspected source: " + ", ".join(sorted(sources_by_net[net])))
        else:
            lines.append("No VM in this vCenter sends at that rate → source is probably "
                         "outside (physical network / other vCenter)")
        findings[f"net:{net}"] = Finding(key=f"net:{net}", title=f"Broadcast storm on '{net}'",
                                         lines=lines, peak=peak)
    return findings


class Tracker:
    """Turns per-poll findings into new / reminder / resolved events."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.active = {}

    def update(self, findings: dict, now: float):
        new, remind, resolved = [], [], []
        for key, f in findings.items():
            st = self.active.get(key)
            if st is None:
                self.active[key] = {"first": now, "notified": now, "missing": 0,
                                    "finding": f, "peak": f.peak}
                new.append(f)
                continue
            st.update(finding=f, missing=0, peak=max(st["peak"], f.peak))
            if self.cfg.remind_minutes > 0 and now - st["notified"] >= self.cfg.remind_minutes * 60:
                st["notified"] = now
                remind.append((f, now - st["first"]))
        for key in list(self.active):
            if key in findings:
                continue
            st = self.active[key]
            st["missing"] += 1
            if st["missing"] >= self.cfg.resolve_polls:
                resolved.append((st["finding"], now - st["first"], st["peak"]))
                del self.active[key]
        return new, remind, resolved


# --------------------------------------------------------------------------- #
# notification
# --------------------------------------------------------------------------- #
@dataclass
class Alert:
    header: str
    blocks: list  # [(title, [lines])]


def _fmt_dur(seconds):
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{max(m, 1)}m"


def build_alerts(cfg: Config, new, remind, resolved):
    alerts = []
    if new:
        alerts.append(Alert(f"🔴 Broadcast storm detected · {cfg.vc_name}",
                            [(f.title, f.lines) for f in new]))
    if remind:
        alerts.append(Alert(f"🔁 Still active · {cfg.vc_name}",
                            [(f.title, f.lines + [f"Active for {_fmt_dur(d)}"]) for f, d in remind]))
    if resolved:
        alerts.append(Alert(f"✅ Resolved · {cfg.vc_name}",
                            [(f.title, [f"Lasted ~{_fmt_dur(d)}, peak {peak:.0f} pps"])
                             for f, d, peak in resolved]))
    return alerts


def _render(alert: Alert, bold, esc, limit):
    out = bold(esc(alert.header))
    for i, (title, lines) in enumerate(alert.blocks):
        part = "\n\n" + bold(esc(title)) + "\n" + "\n".join("• " + esc(line) for line in lines)
        if len(out) + len(part) > limit - 40:
            out += f"\n\n… and {len(alert.blocks) - i} more"
            break
        out += part
    return out


def _slack_esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token, chat_id, thread_id=None):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.thread_id = thread_id

    def send(self, alert: Alert):
        payload = {"chat_id": self.chat_id, "parse_mode": "HTML",
                   "disable_web_page_preview": True,
                   "text": _render(alert, lambda s: f"<b>{s}</b>",
                                   lambda s: html.escape(s, quote=False), 4096)}
        if self.thread_id:
            payload["message_thread_id"] = int(self.thread_id)
        r = requests.post(self.url, json=payload, timeout=15)
        if r.status_code != 200:  # never log the URL, it contains the bot token
            raise RuntimeError(f"Telegram API returned {r.status_code}: {r.text[:200]}")


class SlackNotifier:
    name = "slack"

    def __init__(self, webhook):
        self.webhook = webhook

    def send(self, alert: Alert):
        text = _render(alert, lambda s: f"*{s}*", _slack_esc, 3500)
        r = requests.post(self.webhook, json={"text": text}, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"Slack webhook returned {r.status_code}: {r.text[:200]}")


def build_notifiers(cfg: Config):
    notifiers = []
    if cfg.telegram_token and cfg.telegram_chat:
        notifiers.append(TelegramNotifier(cfg.telegram_token, cfg.telegram_chat, cfg.telegram_thread))
    if cfg.slack_webhook:
        notifiers.append(SlackNotifier(cfg.slack_webhook))
    return notifiers


def dispatch(notifiers, alert: Alert):
    log.warning("%s | %s", alert.header,
                " | ".join(f"{title}: {'; '.join(lines)}" for title, lines in alert.blocks))
    for n in notifiers:
        try:
            n.send(alert)
        except Exception as exc:
            log.error("%s notification failed: %s", n.name, exc)


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #
def _err(exc):
    """One-line error text; str() of a vmodl fault is a multi-line object dump."""
    msg = getattr(exc, "msg", None)
    return f"{type(exc).__name__}: {msg}" if msg else str(exc)


def print_table(ports, top=25):
    rows = sorted(ports, key=lambda p: max(p.avg_tx, p.avg_rx), reverse=True)[:top]
    print(f"{'VM':<38} {'vNIC':<18} {'Network':<34} {'Host':<20} {'TX pps':>8} {'RX pps':>8}")
    for p in rows:
        # one decimal: normal vNICs send well below 1 bcast pps
        print(f"{p.vm[:38]:<38} {p.nic[:18]:<18} {p.network[:34]:<34} {p.host[:20]:<20} "
              f"{p.avg_tx:>8.1f} {p.avg_rx:>8.1f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--once", action="store_true",
                    help="print the current top broadcast talkers and exit (no alerts)")
    ap.add_argument("--test-notify", action="store_true",
                    help="send a test message to all configured notifiers and exit")
    args = ap.parse_args()

    logging.basicConfig(level=_env("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s %(message)s")
    cfg = Config()
    notifiers = build_notifiers(cfg)

    if args.test_notify:
        if not notifiers:
            sys.exit("no notifier configured (TELEGRAM_* or SLACK_WEBHOOK_URL)")
        dispatch(notifiers, Alert(f"🧪 bcast-monitor test · {cfg.vc_name}",
                                  [("Notification channel works", ["No action needed."])]))
        return

    cfg.validate_vcenter()
    if not notifiers and not args.once:
        log.warning("no notifier configured - alerts will only be logged")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    si = perf = inv = None
    tracker = Tracker(cfg)
    failures = 0
    degraded_alerted = False

    try:
        while True:
            started = time.time()
            try:
                if si is None:
                    si = connect(cfg)
                    perf = Perf(si, cfg.include_multicast)
                    inv = None
                if inv is None or started - inv.loaded_at >= cfg.inventory_refresh:
                    inv = load_inventory(si, cfg)

                raw = perf.query(list(inv.vms.values()), cfg.window, cfg.query_batch)
                ports = build_ports(raw, inv, cfg)

                if args.once:
                    print_table(ports)
                    return

                for alert in build_alerts(cfg, *tracker.update(evaluate(ports, cfg), started)):
                    dispatch(notifiers, alert)

                if degraded_alerted:
                    dispatch(notifiers, Alert(f"✅ bcast-monitor polling again · {cfg.vc_name}", []))
                failures, degraded_alerted = 0, False
                log.info("poll ok: %d vNICs, %d active incident(s)", len(ports), len(tracker.active))

                if cfg.startup_message and notifiers:
                    cfg.startup_message = False
                    dispatch(notifiers, Alert(
                        f"▶️ bcast-monitor started · {cfg.vc_name}",
                        [("Watching", [f"{len(inv.vms)} VMs / {len(ports)} vNICs",
                                       f"TX ≥ {cfg.tx_threshold:.0f} pps or RX ≥ "
                                       f"{cfg.rx_threshold:.0f} pps on ≥ {cfg.rx_ports_min} vNICs"])]))
            except Exception as exc:
                failures += 1
                log.error("poll failed (%d in a row): %s", failures, _err(exc))
                if args.once:
                    raise
                if si is not None:
                    try:
                        Disconnect(si)
                    except Exception:
                        pass
                si = None
                if cfg.fail_alert_polls and failures >= cfg.fail_alert_polls and not degraded_alerted:
                    degraded_alerted = True
                    dispatch(notifiers, Alert(f"⚠️ bcast-monitor cannot poll {cfg.vc_name}",
                                              [("Last error", [_err(exc)[:300]])]))
            time.sleep(max(1.0, cfg.poll_interval - (time.time() - started)))
    finally:
        if si is not None:
            try:
                Disconnect(si)
            except Exception:
                pass


if __name__ == "__main__":
    main()
