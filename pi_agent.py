"""SIFS Raspberry Pi bridge. Run as a service after configuring Firebase credentials.

The Pi owns all actuator decisions. The browser only changes settings in Firebase.
"""

import base64
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone

import cv2
import firebase_admin
from firebase_admin import credentials, db
import serial
from control_logic import light_should_run, parse_co2_packet, pump_should_run
from local_server import LocalState, create_app

# Match these to the actual wiring and calibration before enabling the service.
DHT_PIN = int(os.getenv("SIFS_DHT_PIN", "24"))
DHT_TYPE = os.getenv("SIFS_DHT_TYPE", "DHT11")
SOIL_MODE = os.getenv("SIFS_SOIL_MODE", "digital")  # or "ads1115"
SOIL_DIGITAL_PIN = int(os.getenv("SIFS_SOIL_DIGITAL_PIN", "23"))
SOIL_DIGITAL_WET_LEVEL = int(os.getenv("SIFS_SOIL_DIGITAL_WET_LEVEL", "0"))
SOIL_ADC_CHANNEL = int(os.getenv("SIFS_SOIL_ADC_CHANNEL", "0"))
SOIL_DRY_VOLTAGE = float(os.getenv("SIFS_SOIL_DRY_VOLTAGE", "2.80"))
SOIL_WET_VOLTAGE = float(os.getenv("SIFS_SOIL_WET_VOLTAGE", "1.20"))
ADS1115_ADDRESS = int(os.getenv("SIFS_ADS1115_ADDRESS", "0x48"), 0)
CAMERA_INDEX = int(os.environ["SIFS_CAMERA_INDEX"]) if os.getenv("SIFS_CAMERA_INDEX") else None
CAMERA_MAX_INDEX = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sifs-pi")

FARM_ID = os.getenv("SIFS_FARM_ID", "sifs")
DB_URL = os.getenv("FIREBASE_DATABASE_URL")
SERVICE_ACCOUNT = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
PUMP_PIN = int(os.getenv("SIFS_PUMP_PIN", "17"))
LIGHT_PIN = int(os.getenv("SIFS_LIGHT_PIN", "27"))
RELAY_ACTIVE_HIGH = os.getenv("SIFS_RELAY_ACTIVE_HIGH", "0") == "1"
CO2_PORT = os.getenv("SIFS_CO2_PORT", "/dev/serial0")
POLL_SECONDS = 2
MAX_PUMP_SECONDS = 30
MIN_PUMP_REST_SECONDS = 10
CO2_WARMUP_SECONDS = 180

DEFAULT_SETTINGS = {
    "pumpMode": "auto", "lightMode": "auto",
    "manualPump": False, "manualLight": False,
    "co2Threshold": 1000, "co2Hysteresis": 100,
    "photoIntervalMinutes": 5,
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def bounded_number(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


class Hardware:
    def __init__(self):
        from gpiozero import DigitalInputDevice, OutputDevice
        self.pump = OutputDevice(PUMP_PIN, active_high=RELAY_ACTIVE_HIGH, initial_value=False)
        self.light = OutputDevice(LIGHT_PIN, active_high=RELAY_ACTIVE_HIGH, initial_value=False)
        self.pump_lock = threading.RLock()
        self.pump_timer = None
        self.pump_started_at = None
        self.pump_last_off = 0
        self.soil = DigitalInputDevice(SOIL_DIGITAL_PIN) if SOIL_MODE == "digital" else None
        self.ads = None
        if SOIL_MODE == "ads1115":
            import board
            import busio
            import adafruit_ads1x15.ads1115 as ADS
            from adafruit_ads1x15.analog_in import AnalogIn
            ads = ADS.ADS1115(busio.I2C(board.SCL, board.SDA), address=ADS1115_ADDRESS)
            self.ads = AnalogIn(ads, SOIL_ADC_CHANNEL)
        import board
        import adafruit_dht
        dht_class = adafruit_dht.DHT22 if DHT_TYPE.upper() == "DHT22" else adafruit_dht.DHT11
        self.dht = dht_class(getattr(board, f"D{DHT_PIN}"))
        try:
            self.co2 = serial.Serial(CO2_PORT, baudrate=9600, timeout=1)
        except Exception as exc:
            self.co2 = None
            log.error("CO2 UART unavailable: %s", exc)
        self.started = time.monotonic()
        self.camera = None
        self.camera_index = None
        self.picam = None

    def read(self):
        result = {"temperature": None, "humidity": None, "soilState": None,
                  "soilMoisture": None, "co2": None, "errors": {}}
        try:
            result["temperature"] = self.dht.temperature
            result["humidity"] = self.dht.humidity
        except RuntimeError as exc:
            result["errors"]["dht"] = str(exc)
        except Exception as exc:
            result["errors"]["dht"] = str(exc)
        try:
            if self.soil:
                result["soilState"] = "wet" if self.soil.value == SOIL_DIGITAL_WET_LEVEL else "dry"
            else:
                voltage = self.ads.voltage
                percent = (SOIL_DRY_VOLTAGE - voltage) / (SOIL_DRY_VOLTAGE - SOIL_WET_VOLTAGE) * 100
                result["soilMoisture"] = round(max(0, min(100, percent)), 1)
                result["soilState"] = "wet" if percent >= 60 else "dry" if percent <= 35 else "moist"
        except Exception as exc:
            result["errors"]["soil"] = str(exc)
        try:
            if self.co2 is None:
                raise RuntimeError("CO2 UART unavailable")
            if time.monotonic() - self.started < CO2_WARMUP_SECONDS:
                result["errors"]["co2"] = "warming up"
            else:
                self.co2.reset_input_buffer()
                self.co2.write(bytes.fromhex("FF 01 86 00 00 00 00 00 79"))
                packet = self.co2.read(9)
                result["co2"] = parse_co2_packet(packet)
        except Exception as exc:
            result["errors"]["co2"] = str(exc)
        return result

    def capture(self):
        if self.camera is None and self.picam is None:
            indexes = [CAMERA_INDEX] if CAMERA_INDEX is not None else range(CAMERA_MAX_INDEX)
            for index in indexes:
                cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
                if cap.isOpened():
                    self.camera, self.camera_index = cap, index
                    break
                cap.release()
            if self.camera is None and self.picam is None:
                try:
                    from picamera2 import Picamera2
                    self.picam = Picamera2()
                    self.picam.configure(self.picam.create_still_configuration(
                        main={"size": (640, 480), "format": "RGB888"}))
                    self.picam.start()
                    self.camera_index = "Pi CSI"
                except Exception as exc:
                    self.picam = None
                    raise RuntimeError("No camera available") from exc
        if self.picam:
            frame = self.picam.capture_array()
            ok = frame is not None
        else:
            ok, frame = self.camera.read()
        if not ok:
            if self.camera:
                self.camera.release()
                self.camera = None
            raise RuntimeError("Camera did not deliver a frame")
        frame = cv2.resize(frame, (640, 480))
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return base64.b64encode(encoded).decode("ascii")

    def close(self):
        self.pump_off()
        self.light.off()
        self.dht.exit()
        if self.co2:
            self.co2.close()
        if self.camera:
            self.camera.release()
        if self.picam:
            self.picam.stop()
            self.picam.close()

    def pump_on(self):
        with self.pump_lock:
            if self.pump.is_active:
                return
            self.pump.on()
            self.pump_started_at = time.monotonic()
            self.pump_timer = threading.Timer(MAX_PUMP_SECONDS, self.pump_off)
            self.pump_timer.daemon = True
            self.pump_timer.start()

    def pump_off(self):
        with self.pump_lock:
            if self.pump_timer:
                self.pump_timer.cancel()
                self.pump_timer = None
            if self.pump.is_active:
                self.pump.off()
                self.pump_last_off = time.monotonic()
            self.pump_started_at = None


def cloud_sync(root, state, stop_event):
    """Keep network I/O away from the sensor and relay control loop."""
    last_analysis_sync = 0
    while not stop_event.is_set():
        try:
            remote_settings = root.child("settings").get() or {}
            commands = root.child("commands").get() or {}
            with state.lock:
                local_settings = dict(state.settings)
                state.remote_capture_request = commands.get("captureRequest")
            if (remote_settings.get("updatedAt") or 0) > (local_settings.get("updatedAt") or 0):
                state.apply_remote_settings(remote_settings)
            elif (local_settings.get("updatedAt") or 0) > (remote_settings.get("updatedAt") or 0):
                root.child("settings").update(local_settings)
            snapshot = state.snapshot()
            if snapshot["telemetry"]:
                root.child("telemetry").set(snapshot["telemetry"])
            for row_id, path, payload in state.pending():
                item = json.loads(payload)
                sent = root.child(path).push(item)
                if path == "history/photos":
                    root.child("photos/latest").set({"image": item["image"],
                                                       "capturedAt": item["capturedAt"]})
                state.mark_sent(row_id, sent.key if path == "history/analyses" else None)
            if time.monotonic() - last_analysis_sync >= 60:
                cloud_analyses = root.child("history/analyses").order_by_key().limit_to_last(100).get() or {}
                state.import_analyses(cloud_analyses)
                last_analysis_sync = time.monotonic()
            root.child("status").update({"onlineAt": utc_now(),
                                          "cameraError": snapshot["cameraError"]})
            state.cloud_online = True
        except Exception as exc:
            state.cloud_online = False
            log.warning("Firebase sync unavailable; local control continues: %s", exc)
        stop_event.wait(POLL_SECONDS)


def main():
    if len(os.getenv("SIFS_LOCAL_PIN", "")) < 6:
        raise RuntimeError("Set SIFS_LOCAL_PIN to at least 6 characters before starting")
    root = None
    if DB_URL and SERVICE_ACCOUNT:
        firebase_admin.initialize_app(credentials.Certificate(SERVICE_ACCOUNT), {"databaseURL": DB_URL})
        root = db.reference(f"farms/{FARM_ID}")
    state = LocalState()
    local_app = create_app(state)
    threading.Thread(target=lambda: local_app.run(host="0.0.0.0", port=5000,
                                                  threaded=True, use_reloader=False), daemon=True).start()
    stop_event = threading.Event()
    if root:
        threading.Thread(target=cloud_sync, args=(root, state, stop_event), daemon=True).start()
    hardware = Hardware()
    stopping = False
    last_photo = 0
    last_capture_request = None
    last_history = 0
    light_on = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopping:
            cycle = time.monotonic()
            try:
                with state.lock:
                    settings = {**DEFAULT_SETTINGS, **state.settings}
                readings = hardware.read()
                pump_mode = settings["pumpMode"] if settings["pumpMode"] in ("auto", "manual") else "auto"
                light_mode = settings["lightMode"] if settings["lightMode"] in ("auto", "manual") else "auto"
                desired_pump = pump_should_run(
                    pump_mode, settings["manualPump"], readings["soilState"],
                    hardware.pump.is_active, hardware.pump_started_at, hardware.pump_last_off, cycle,
                    MAX_PUMP_SECONDS, MIN_PUMP_REST_SECONDS)
                if desired_pump and not hardware.pump.is_active:
                    hardware.pump_on()
                elif not desired_pump and hardware.pump.is_active:
                    hardware.pump_off()

                threshold = bounded_number(settings["co2Threshold"], 1000, 400, 5000)
                hysteresis = bounded_number(settings["co2Hysteresis"], 100, 20, 500)
                light_on = light_should_run(light_mode, settings["manualLight"],
                                            readings["co2"], light_on, threshold, hysteresis)
                hardware.light.on() if light_on else hardware.light.off()

                telemetry = {**readings, "pumpOn": hardware.pump.is_active,
                             "lightOn": hardware.light.is_active, "pumpMode": pump_mode,
                             "lightMode": light_mode, "updatedAt": utc_now()}
                with state.lock:
                    state.telemetry = telemetry
                if cycle - last_history >= 60:
                    state.record_telemetry(telemetry, settings)
                    last_history = cycle
                photo_interval = bounded_number(settings["photoIntervalMinutes"], 5, 1, 60) * 60
                with state.lock:
                    local_request = state.capture_request
                    remote_request = state.remote_capture_request
                request_id = local_request or remote_request
                capture_requested = request_id and request_id != last_capture_request
                if cycle - last_photo >= photo_interval or capture_requested:
                    last_photo = cycle
                    if capture_requested:
                        last_capture_request = request_id
                        with state.lock:
                            if state.capture_request == request_id:
                                state.capture_request = None
                    try:
                        image = hardware.capture()
                        captured_at = utc_now()
                        state.set_photo(image, captured_at)
                        with state.lock:
                            state.camera_error = None
                    except Exception as exc:
                        log.warning("Camera capture failed: %s", exc)
                        with state.lock:
                            state.camera_error = str(exc)
            except Exception:
                log.exception("Control cycle failed; switching actuators off")
                hardware.pump_off()
                hardware.light.off()
            time.sleep(max(0.1, POLL_SECONDS - (time.monotonic() - cycle)))
    finally:
        stop_event.set()
        hardware.close()


if __name__ == "__main__":
    main()
