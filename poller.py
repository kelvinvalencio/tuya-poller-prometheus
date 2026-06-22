import json
import logging
import os
import time
from threading import Thread

import tinytuya
from dotenv import load_dotenv
from prometheus_client import Gauge, Info, start_http_server

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Global config ────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
METRICS_PORT  = int(os.getenv("METRICS_PORT",  "8000"))

# DEVICES is a JSON array in .env, e.g.:
# DEVICES='[{"name":"outlet","type":"outlet","id":"...","ip":"...","key":"...","version":"3.4"}]'
DEVICES: list[dict] = json.loads(os.environ["DEVICES"])

# ── Prometheus metrics — outlet ──────────────────────────────────────────────
outlet_switch    = Gauge("tuya_outlet_switch",         "Outlet on/off state (1=on)",      ["device", "outlet"])
outlet_countdown = Gauge("tuya_outlet_countdown_secs", "Countdown timer in seconds",       ["device", "outlet"])
outlet_voltage   = Gauge("tuya_power_voltage_v",       "Measured voltage (V)",             ["device"])
outlet_current   = Gauge("tuya_power_current_ma",      "Measured current (mA)",            ["device"])
outlet_power     = Gauge("tuya_power_watts",           "Measured power (W)",               ["device"])
outlet_energy    = Gauge("tuya_energy_kwh",            "Cumulative energy (kWh)",          ["device"])
outlet_childlock = Gauge("tuya_child_lock",            "Child lock enabled (1=locked)",    ["device"])

# ── Prometheus metrics — sensor ──────────────────────────────────────────────
sensor_temp      = Gauge("sensor_temperature_celsius", "Temperature in Celsius",           ["device_id", "device_ip"])
sensor_humidity  = Gauge("sensor_humidity_percent",    "Relative humidity percentage",     ["device_id", "device_ip"])
sensor_ts        = Gauge("sensor_last_poll_timestamp_seconds",
                         "Unix timestamp of the last successful poll",                     ["device_id", "device_ip"])

# ── Prometheus metrics — shared ──────────────────────────────────────────────
poll_errors      = Gauge("tuya_poll_errors_total",     "Cumulative poll error count",      ["device"])
device_info      = Info("tuya_device",                 "Static device metadata",           ["device"])


# ── Helpers ──────────────────────────────────────────────────────────────────
def apply_scale(raw: int, scale: int) -> float:
    return raw / (10 ** scale)


def make_tinytuya_device(cfg: dict) -> tinytuya.Device:
    cls = tinytuya.OutletDevice if cfg["type"] == "outlet" else tinytuya.Device
    d = cls(
        dev_id=cfg["id"],
        address=cfg["ip"],
        local_key=cfg["key"],
        version=float(cfg.get("version", "3.4")),
    )
    d.set_socketTimeout(5)
    return d


# ── Per-type poll & metric update ────────────────────────────────────────────
def poll_outlet(cfg: dict, d: tinytuya.OutletDevice) -> None:
    data = d.status()
    dps  = {str(k): v for k, v in data.get("dps", {}).items()}
    if not dps:
        raise ValueError(f"Empty DPS: {data}")

    name = cfg["name"]
    for outlet_num, sw, cd in [("1", "1", "9"), ("2", "2", "10")]:
        if sw in dps:
            outlet_switch.labels(device=name, outlet=outlet_num).set(1 if dps[sw] else 0)
        if cd in dps:
            outlet_countdown.labels(device=name, outlet=outlet_num).set(int(dps[cd]))

    if "20" in dps: outlet_voltage.labels(device=name).set(apply_scale(int(dps["20"]), 1))
    if "18" in dps: outlet_current.labels(device=name).set(int(dps["18"]))
    if "19" in dps: outlet_power.labels(device=name).set(apply_scale(int(dps["19"]), 1))
    if "17" in dps: outlet_energy.labels(device=name).set(apply_scale(int(dps["17"]), 3))
    if "41" in dps: outlet_childlock.labels(device=name).set(1 if dps["41"] else 0)

    log.info("[%s] DPS: %s", name, dps)


def poll_sensor(cfg: dict, d: tinytuya.Device) -> None:
    data = d.status()
    if "Error" in data:
        raise RuntimeError(data["Error"])

    dps = {str(k): v for k, v in data.get("dps", {}).items()}
    lbl = [cfg["id"], cfg["ip"]]

    raw_temp = dps.get("101")
    raw_hum  = dps.get("102")

    if raw_temp is not None:
        sensor_temp.labels(*lbl).set(raw_temp / 10.0)
    if raw_hum is not None:
        sensor_humidity.labels(*lbl).set(raw_hum)

    sensor_ts.labels(*lbl).set(time.time())
    log.info("[%s] temp=%.1f°C  hum=%s%%", cfg["name"], raw_temp / 10.0 if raw_temp else 0, raw_hum)


POLL_FN = {
    "outlet": poll_outlet,
    "sensor": poll_sensor,
}


# ── Per-device poll loop (runs in its own thread) ────────────────────────────
def poll_loop(cfg: dict) -> None:
    name      = cfg["name"]
    logger    = logging.getLogger(name)
    error_count = 0
    poll_errors.labels(device=name).set(0)
    device_info.labels(device=name).info({
        "name":    name,
        "type":    cfg["type"],
        "id":      cfg["id"],
        "ip":      cfg["ip"],
        "version": str(cfg.get("version", "3.4")),
    })

    poll_fn = POLL_FN.get(cfg["type"])
    if poll_fn is None:
        logger.error("Unknown device type '%s' — skipping", cfg["type"])
        return

    d = make_tinytuya_device(cfg)
    logger.info("Poll loop started")

    while True:
        try:
            poll_fn(cfg, d)
        except Exception as exc:
            error_count += 1
            poll_errors.labels(device=name).set(error_count)
            logger.warning("Error #%d: %s — reconnecting…", error_count, exc)
            time.sleep(2)
            try:
                d = make_tinytuya_device(cfg)
            except Exception:
                pass

        time.sleep(POLL_INTERVAL)


# ── Entry point ──────────────────────────────────────────────────────────────
def main() -> None:
    start_http_server(METRICS_PORT)
    log.info("Prometheus metrics on :%d — polling %d device(s)", METRICS_PORT, len(DEVICES))

    threads = []
    for cfg in DEVICES:
        t = Thread(target=poll_loop, args=(cfg,), name=cfg["name"], daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
    