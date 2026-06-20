import os
import time
import logging
from threading import Thread

import tinytuya
from prometheus_client import start_http_server, Gauge, Info
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config from .env ────────────────────────────────────────────────────────
DEVICE_NAME    = os.environ["DEVICE_NAME"]
DEVICE_ID      = os.environ["DEVICE_ID"]
DEVICE_KEY     = os.environ["DEVICE_KEY"]
DEVICE_IP      = os.environ["DEVICE_IP"]
DEVICE_VERSION = float(os.getenv("DEVICE_VERSION", "3.3"))
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL", "10"))
METRICS_PORT   = int(os.getenv("METRICS_PORT", "8000"))

# ── Prometheus metrics ───────────────────────────────────────────────────────
LABELS = ["device", "outlet"]

switch_state   = Gauge("tuya_outlet_switch",         "Outlet on/off state (1=on)",               LABELS)
countdown_secs = Gauge("tuya_outlet_countdown_secs", "Countdown timer in seconds",                LABELS)
voltage_v      = Gauge("tuya_power_voltage_v",       "Measured voltage (V)",                      ["device"])
current_ma     = Gauge("tuya_power_current_ma",      "Measured current (mA)",                     ["device"])
power_w        = Gauge("tuya_power_watts",           "Measured power (W)",                        ["device"])
energy_kwh     = Gauge("tuya_energy_kwh",            "Cumulative energy (kWh)",                   ["device"])
child_lock     = Gauge("tuya_child_lock",            "Child lock enabled (1=locked)",              ["device"])
poll_errors    = Gauge("tuya_poll_errors_total",     "Cumulative poll error count",               ["device"])
device_info    = Info("tuya_device",                 "Static device metadata")

device_info.info({
    "name":    DEVICE_NAME,
    "id":      DEVICE_ID,
    "ip":      DEVICE_IP,
    "version": str(DEVICE_VERSION),
})

_error_count = 0


def get_device() -> tinytuya.OutletDevice:
    d = tinytuya.OutletDevice(
        dev_id=DEVICE_ID,
        address=DEVICE_IP,
        local_key=DEVICE_KEY,
        version=DEVICE_VERSION,
    )
    d.set_socketTimeout(5)
    return d


def apply_scale(raw: int, scale: int) -> float:
    """Divide raw integer value by 10^scale as Tuya specifies."""
    return raw / (10 ** scale)


def update_metrics(dps: dict) -> None:
    """Parse a DPS dict and push values to Prometheus gauges."""
    dev = DEVICE_NAME

    # Per-outlet switch + countdown
    for outlet_num, sw_code, cd_code in [
        (1, "1", "9"),
        (2, "2", "10"),
    ]:
        label = str(outlet_num)
        if sw_code in dps:
            switch_state.labels(device=dev, outlet=label).set(1 if dps[sw_code] else 0)
        if cd_code in dps:
            countdown_secs.labels(device=dev, outlet=label).set(int(dps[cd_code]))

    # Whole-device power metrics (apply Tuya scale factors)
    if "20" in dps:   # cur_voltage: scale=1 → divide by 10
        voltage_v.labels(device=dev).set(apply_scale(int(dps["20"]), 1))
    if "18" in dps:   # cur_current: scale=0
        current_ma.labels(device=dev).set(int(dps["18"]))
    if "19" in dps:   # cur_power: scale=1 → divide by 10
        power_w.labels(device=dev).set(apply_scale(int(dps["19"]), 1))
    if "17" in dps:   # add_ele (energy): scale=3 → divide by 1000  → kWh
        energy_kwh.labels(device=dev).set(apply_scale(int(dps["17"]), 3))
    if "41" in dps:
        child_lock.labels(device=dev).set(1 if dps["41"] else 0)


def poll_loop() -> None:
    global _error_count
    device = get_device()
    log.info("Starting poll loop — interval=%ds port=%d", POLL_INTERVAL, METRICS_PORT)

    while True:
        try:
            data = device.status()
            dps  = data.get("dps", {})
            if not dps:
                raise ValueError(f"Empty DPS response: {data}")

            update_metrics(dps)
            log.info("DPS: %s", dps)

        except Exception as exc:
            _error_count += 1
            poll_errors.labels(device=DEVICE_NAME).set(_error_count)
            log.warning("Poll error #%d: %s — reconnecting…", _error_count, exc)
            time.sleep(2)
            try:
                device = get_device()   # fresh socket
            except Exception:
                pass

        time.sleep(POLL_INTERVAL)


def main() -> None:
    start_http_server(METRICS_PORT)
    log.info("Prometheus metrics server started on :%d", METRICS_PORT)

    t = Thread(target=poll_loop, daemon=True)
    t.start()
    t.join()


if __name__ == "__main__":
    main()
    