"""Detection, lifecycle and rendering with data shaped like a real in-guest bridging loop."""
import html
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import monitor as m  # noqa: E402

LOOP_NET = "dvportgroup-200 (VLAN 200)"
MGMT_NET = "dvportgroup-100 (VLAN 100)"


@pytest.fixture
def cfg(monkeypatch):
    for name in ("TX_THRESHOLD_PPS", "RX_THRESHOLD_PPS", "RX_PORTS_MIN", "MIN_HOT_SAMPLES",
                 "WINDOW_SAMPLES", "REMIND_MINUTES", "RESOLVE_POLLS", "VC_NAME", "VC_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VC_NAME", "Lab vCenter")
    return m.Config()


def port(vm, nic, network, tx, rx, host="esxi05", same=1):
    return m.PortStat(vm_id=f"vm-{vm}", vm=vm, host=host, nic=nic, network=network,
                      tx=list(tx), rx=list(rx), same_net_nics=same)


def loop_ports():
    """fw01 bridges eth2/eth3 on VLAN 200; everyone else in that VLAN only receives."""
    ports = [
        port("fw01", "eth2", LOOP_NET, [3800, 3900, 4000], [3800, 3900, 4000], same=2),
        port("fw01", "eth3", LOOP_NET, [3700, 3850, 3950], [3700, 3850, 3950], same=2),
        port("fw01", "eth0", MGMT_NET, [1, 1, 1], [8, 9, 10], same=8),
    ]
    for vm in ("app01", "dc01", "dc02", "web01", "db01"):
        ports.append(port(vm, "Network adapter 1", LOOP_NET, [0, 1, 0], [3700, 3900, 4000],
                          host="esxi02"))
    return ports


def quiet_ports():
    return [port(f"vm{i}", "Network adapter 1", LOOP_NET, [0.3, 0.2, 0.4], [8, 12, 10]) for i in range(10)]


def test_baseline_has_no_findings(cfg):
    assert m.evaluate(quiet_ports(), cfg) == {}


def test_loop_source_and_storm(cfg):
    findings = m.evaluate(loop_ports(), cfg)
    assert set(findings) == {"src:vm-fw01", f"net:{LOOP_NET}"}

    src = findings["src:vm-fw01"]
    assert src.title == "Broadcast source: fw01 @ esxi05"
    assert any("2 vNICs flooding on the same network" in line for line in src.lines)
    assert src.peak == 4000

    storm = findings[f"net:{LOOP_NET}"]
    assert storm.lines[0].startswith("7 vNICs / 6 VMs receive > 1000 bcast pps")
    assert "Suspected source: fw01" in storm.lines


def test_victims_are_not_sources(cfg):
    findings = m.evaluate(loop_ports(), cfg)
    assert not any(k.startswith("src:") and "fw01" not in k for k in findings)


def test_storm_without_vm_source_points_outside(cfg):
    ports = [p for p in loop_ports() if p.vm != "fw01"]
    findings = m.evaluate(ports, cfg)
    assert list(findings) == [f"net:{LOOP_NET}"]
    assert "source is probably outside" in findings[f"net:{LOOP_NET}"].lines[-1]


def test_storm_needs_enough_ports(cfg):
    ports = [p for p in loop_ports() if p.vm != "fw01"][:4]
    assert m.evaluate(ports, cfg) == {}


def test_single_spike_is_not_hot(cfg):
    assert not m.hot([0, 5000, 0], cfg.tx_threshold, cfg.min_hot)
    assert m.hot([0, 5000, 600], cfg.tx_threshold, cfg.min_hot)


def test_tracker_lifecycle(cfg):
    tracker = m.Tracker(cfg)
    storm = m.evaluate(loop_ports(), cfg)

    new, remind, resolved = tracker.update(storm, 0)
    assert len(new) == 2 and not remind and not resolved

    new, remind, resolved = tracker.update(storm, 60)
    assert not new and not remind and not resolved

    new, remind, resolved = tracker.update(storm, cfg.remind_minutes * 60)
    assert not new and len(remind) == 2

    # RESOLVE_POLLS (2) quiet polls before "resolved"
    assert tracker.update({}, 1000) == ([], [], [])
    new, remind, resolved = tracker.update({}, 1060)
    assert len(resolved) == 2 and not tracker.active
    assert {peak for _, _, peak in resolved} == {4000}


def test_tracker_quiet_poll_resets(cfg):
    tracker = m.Tracker(cfg)
    storm = m.evaluate(loop_ports(), cfg)
    tracker.update(storm, 0)
    tracker.update({}, 60)
    tracker.update(storm, 120)  # back again: counter resets, no new alert
    assert tracker.update({}, 180) == ([], [], [])
    assert len(tracker.active) == 2


def test_render_escapes_and_truncates(cfg):
    alert = m.Alert("hdr <x>", [(f"VM <{i}>", ["a & b"] * 20) for i in range(200)])
    text = m._render(alert, lambda s: f"<b>{s}</b>", lambda s: html.escape(s, quote=False), 4096)
    assert len(text) <= 4096
    assert "<b>hdr &lt;x&gt;</b>" in text
    assert "a &amp; b" in text
    assert "more" in text.splitlines()[-1]


def test_build_alerts(cfg):
    f = m.Finding("k", "Broadcast storm on 'x'", ["line"], 1234)
    alerts = m.build_alerts(cfg, [f], [(f, 900)], [(f, 3700, 4000)])
    assert [a.header.split(" · ")[0] for a in alerts] == [
        "🔴 Broadcast storm detected", "🔁 Still active", "✅ Resolved"]
    assert alerts[1].blocks[0][1][-1] == "Active for 15m"
    assert alerts[2].blocks[0][1] == ["Lasted ~1h 1m, peak 4000 pps"]
