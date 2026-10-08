"""
SIFS - Smart Indoor Farming System
Real-time web monitor: camera stream + soil moisture + DHT temperature/humidity.

Target: Raspberry Pi, Python 3.12 / 3.13, Flask.
Run:    python app.py   then open http://<pi-ip>:5000
"""

# =====================================================================
#  PIN / HARDWARE CONFIGURATION  (edit these to match your wiring)
#  All GPIO numbers are BCM numbers (GPIO4 = physical pin 7, etc.)
# =====================================================================

# --- DHT temperature & humidity sensor ---
DHT_PIN = 4                  # BCM GPIO connected to the DHT data line
DHT_TYPE = "DHT11"           # "DHT11" or "DHT22"

# --- Soil moisture sensor ---
# "ads1115" : analog sensor (capacitive v1.2 etc.) through an ADS1115 ADC on I2C
#             (SDA = GPIO2, SCL = GPIO3) -> gives a moisture percentage
# "digital" : sensor module DO pin wired straight to a GPIO -> gives wet/dry only
SOIL_MODE = "ads1115"
SOIL_ADC_CHANNEL = 0         # ADS1115 input channel (0..3 = A0..A3)
ADS1115_ADDRESS = 0x48       # I2C address of the ADS1115
SOIL_DRY_VOLTAGE = 2.80      # sensor voltage in dry air   (calibrate)
SOIL_WET_VOLTAGE = 1.20      # sensor voltage in water     (calibrate)
SOIL_DIGITAL_PIN = 17        # BCM GPIO for the DO pin (only used in "digital" mode)
SOIL_DIGITAL_WET_LEVEL = 0   # logic level the DO pin outputs when the soil is wet

# --- Camera ---
CAMERA_INDEX = None          # None = auto-select the first working camera, or set 0, 1, ...
CAMERA_MAX_INDEX = 10        # how many /dev/video indexes to try when auto-selecting
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
JPEG_QUALITY = 80

# --- General ---
SENSOR_INTERVAL = 2.0        # seconds between sensor reads (DHT11 needs >= 1 s)
HOST = "0.0.0.0"
PORT = 5000

# =====================================================================

import logging
import threading
import time
from datetime import datetime

import cv2
from flask import Flask, Response, jsonify, render_template

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sifs")

app = Flask(__name__)


# ---------------------------------------------------------------------
#  Camera
# ---------------------------------------------------------------------
class Camera:
    """Grabs frames in a background thread and keeps the latest one as JPEG."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None           # latest JPEG bytes
        self.source = "none"        # description of the selected camera
        self.cap = None
        self.picam = None
        threading.Thread(target=self._run, daemon=True).start()

    def _open_opencv(self, index):
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        # Some /dev/video nodes open fine but never deliver frames, so test a read.
        for _ in range(5):
            ok, frame = cap.read()
            if ok and frame is not None:
                return cap
            time.sleep(0.1)
        cap.release()
        return None

    def _open_picamera2(self):
        try:
            from picamera2 import Picamera2  # installed with apt (python3-picamera2)
        except ImportError:
            return None
        try:
            picam = Picamera2()
            picam.configure(picam.create_video_configuration(
                main={"size": (FRAME_WIDTH, FRAME_HEIGHT), "format": "RGB888"}))
            picam.start()
            return picam
        except Exception as e:
            log.warning("Picamera2 failed: %s", e)
            return None

    def _select(self):
        """Pick a camera: fixed index, else first working USB index, else Pi camera."""
        indexes = [CAMERA_INDEX] if CAMERA_INDEX is not None else range(CAMERA_MAX_INDEX)
        for i in indexes:
            cap = self._open_opencv(i)
            if cap:
                self.cap, self.source = cap, f"OpenCV index {i}"
                log.info("Camera selected: %s", self.source)
                return True
        if CAMERA_INDEX is None:
            picam = self._open_picamera2()
            if picam:
                self.picam, self.source = picam, "Raspberry Pi camera (Picamera2)"
                log.info("Camera selected: %s", self.source)
                return True
        return False

    def _read(self):
        if self.cap:
            ok, frame = self.cap.read()
            return frame if ok else None
        if self.picam:
            # Picamera2 "RGB888" is actually BGR order, which is what OpenCV expects.
            return self.picam.capture_array()
        return None

    def _release(self):
        if self.cap:
            self.cap.release()
        if self.picam:
            self.picam.stop()
            self.picam.close()
        self.cap = self.picam = None
        self.source = "none"

    def _run(self):
        while True:
            if not self.cap and not self.picam and not self._select():
                log.warning("No camera found, retrying in 5 s")
                time.sleep(5)
                continue
            frame = self._read()
            if frame is None:
                log.warning("Camera stopped delivering frames, re-selecting")
                self._release()
                with self.lock:
                    self.frame = None
                time.sleep(1)
                continue
            ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                with self.lock:
                    self.frame = jpg.tobytes()

    def get_frame(self):
        with self.lock:
            return self.frame


# ---------------------------------------------------------------------
#  Sensors
# ---------------------------------------------------------------------
class Sensors:
    """Reads the DHT and soil moisture sensors in a background thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {
            "temperature": None,
            "humidity": None,
            "soil_moisture": None,     # percent (ads1115 mode)
            "soil_voltage": None,      # volts   (ads1115 mode)
            "soil_state": None,        # "wet"/"dry" (digital mode)
            "soil_mode": SOIL_MODE,
            "dht_error": None,
            "soil_error": None,
            "updated": None,
        }
        self.dht = self._init_dht()
        self.soil = self._init_soil()
        threading.Thread(target=self._run, daemon=True).start()

    def _init_dht(self):
        try:
            import adafruit_dht
            import board
            pin = getattr(board, f"D{DHT_PIN}")
            cls = adafruit_dht.DHT22 if DHT_TYPE.upper() == "DHT22" else adafruit_dht.DHT11
            return cls(pin)
        except Exception as e:
            log.error("DHT init failed: %s", e)
            self.data["dht_error"] = f"init failed: {e}"
            return None

    def _init_soil(self):
        try:
            if SOIL_MODE == "ads1115":
                import board
                import busio
                import adafruit_ads1x15.ads1115 as ADS
                from adafruit_ads1x15.analog_in import AnalogIn
                i2c = busio.I2C(board.SCL, board.SDA)
                ads = ADS.ADS1115(i2c, address=ADS1115_ADDRESS)
                return AnalogIn(ads, SOIL_ADC_CHANNEL)
            if SOIL_MODE == "digital":
                from gpiozero import DigitalInputDevice
                return DigitalInputDevice(SOIL_DIGITAL_PIN)
            raise ValueError(f"unknown SOIL_MODE '{SOIL_MODE}'")
        except Exception as e:
            log.error("Soil sensor init failed: %s", e)
            self.data["soil_error"] = f"init failed: {e}"
            return None

    def _read_dht(self):
        if not self.dht:
            return {}
        try:
            t, h = self.dht.temperature, self.dht.humidity
            if t is None or h is None:
                return {}
            return {"temperature": round(t, 1), "humidity": round(h, 1), "dht_error": None}
        except RuntimeError as e:
            # DHT sensors miss reads often; keep the last good value.
            return {"dht_error": str(e)}

    def _read_soil(self):
        if not self.soil:
            return {}
        try:
            if SOIL_MODE == "ads1115":
                v = self.soil.voltage
                pct = (SOIL_DRY_VOLTAGE - v) / (SOIL_DRY_VOLTAGE - SOIL_WET_VOLTAGE) * 100
                pct = max(0.0, min(100.0, pct))
                return {"soil_voltage": round(v, 3), "soil_moisture": round(pct, 1),
                        "soil_error": None}
            wet = self.soil.value == SOIL_DIGITAL_WET_LEVEL
            return {"soil_state": "wet" if wet else "dry", "soil_error": None}
        except Exception as e:
            return {"soil_error": str(e)}

    def _run(self):
        while True:
            update = {**self._read_dht(), **self._read_soil()}
            update["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self.lock:
                self.data.update(update)
            time.sleep(SENSOR_INTERVAL)

    def get(self):
        with self.lock:
            return dict(self.data)


camera = Camera()
sensors = Sensors()


# ---------------------------------------------------------------------
#  Web routes
# ---------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", interval_ms=int(SENSOR_INTERVAL * 1000))


@app.route("/api/data")
def api_data():
    data = sensors.get()
    data["camera"] = camera.source
    return jsonify(data)


def mjpeg_stream():
    last = None
    while True:
        frame = camera.get_frame()
        if frame is None or frame is last:
            time.sleep(0.03)
            continue
        last = frame
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"


@app.route("/video_feed")
def video_feed():
    return Response(mjpeg_stream(), mimetype="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
