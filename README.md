# SIFS Smart Farm

Mobile-first Firebase web dashboard and Raspberry Pi controller for an indoor farm.

## What is implemented

- Live DHT11 temperature and humidity, soil state, and MH-Z19B CO₂ readings every two seconds.
- Automatic pump: starts on dry soil, stops on wet soil; manual mode is available in the app. Wet or missing soil readings stop the pump in either mode. Each run is limited to 30 seconds with a 10 second rest.
- Automatic grow light: turns on when CO₂ reaches the app threshold; turns off 100 ppm below it to prevent relay chatter. Manual mode is available.
- Camera still photo every 1–60 minutes, plus an on-demand capture button. No live stream is published.
- History page with timestamped telemetry and a settings snapshot every minute, every settings change, saved plant photos, and saved AI analyses. The Pi keeps a local SQLite copy and forwards records to Firebase when online.
- Direct LAN dashboard at `http://<pi-ip>:5000`, protected with a local PIN. It reads and controls the Pi without depending on Firebase connectivity.
- User-triggered plant photo analysis for likely crop type, growth stage, and visible health concerns. The user's OpenAI API key is held only in browser `sessionStorage` and the image is sent to OpenAI only when Analyze is tapped. Do not enter a shared or unrestricted key on a shared device.
- Firebase Authentication, Realtime Database, and a separate Firebase Hosting site.

## Firebase deployment

The SIFS site is `https://sifs-smart-farm.web.app` in project `crop-moniotring-ggvmx4`. The app has its own Web App registration and Hosting target `sifs`; it does not deploy to the project's default Hosting site. Firebase Spark allowed only one Realtime Database instance, so SIFS uses the new default database under `farms/sifs`. Anyone can create an email account or continue anonymously. Both verified email users and anonymous visitors can view the dashboard and change settings, request photos, and save analyses. The Pi uses the Admin SDK to write sensor data and photos.

1. The project's **Email/Password** and **Anonymous** sign-in providers are enabled. At the SIFS site, select **Create account** with any email address and verify the link Firebase sends, or select **Continue anonymously**. Both paths can control the connected farm. Do not put passwords in this repository.
2. From this folder, deploy with `firebase deploy --only auth,hosting:sifs,database --project crop-moniotring-ggvmx4`. Use this exact Hosting target to avoid deploying to another site.
3. Sign in at `https://sifs-smart-farm.web.app` to configure the threshold and camera interval. The screen will show the Pi offline until the bridge below is running.

The `web/firebase-config.js` values are Firebase client identifiers, not service account secrets. Never commit a service account JSON or an OpenAI API key.

## Raspberry Pi setup

Use the Raspberry Pi OS Python version. In Firebase Console → Project settings → Service accounts, create a Firebase Admin service account key and copy it privately to the Pi. Keep it readable only by the service user. Install dependencies with:

```bash
sudo apt update
sudo apt install python3-picamera2 python3-opencv
python3 -m venv --system-site-packages venv
./venv/bin/pip install -r requirements.txt
```

Enable the Pi UART in `raspi-config`, disable the serial login shell, and connect the MH-Z19B to 5 V, GND, Pi TX (GPIO14) → sensor RX, and Pi RX (GPIO15) ← sensor TX. The sensor uses 3.3 V UART logic at 9600 baud and needs about three minutes to warm up. Confirm the module's pin labels against its board before powering it. The pump and LED must have suitable external power and relay ratings; do not power them from a Pi GPIO. Check whether your relay is active low or active high before connecting the load.

The defaults in `pi_agent.py` are DHT data GPIO24, digital soil GPIO23 (low = wet), pump relay GPIO17, and LED relay GPIO27 (active low). Set `SIFS_DHT_PIN`, `SIFS_SOIL_DIGITAL_PIN`, `SIFS_SOIL_DIGITAL_WET_LEVEL`, `SIFS_PUMP_PIN`, `SIFS_LIGHT_PIN`, and `SIFS_RELAY_ACTIVE_HIGH` in the service environment if the wiring differs. For an ADS1115 analog soil sensor, set `SIFS_SOIL_MODE=ads1115` and calibrate `SIFS_SOIL_DRY_VOLTAGE` and `SIFS_SOIL_WET_VOLTAGE`. A USB camera can be selected with `SIFS_CAMERA_INDEX`; leaving it unset scans available video devices. Start with the pump disconnected while confirming relay states.

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/etc/sifs/service-account.json
export FIREBASE_DATABASE_URL=https://crop-moniotring-ggvmx4-default-rtdb.europe-west1.firebasedatabase.app
export SIFS_FARM_ID=sifs
export SIFS_PUMP_PIN=17
export SIFS_LIGHT_PIN=27
export SIFS_RELAY_ACTIVE_HIGH=0
export SIFS_CO2_PORT=/dev/serial0
export SIFS_LOCAL_PIN='choose-a-long-private-pin'
./venv/bin/python pi_agent.py
```

Open `http://<pi-ip>:5000` on a phone connected to the same local network and enter the PIN. The local dashboard uses the same interface and controls. `SIFS_LOCAL_PIN` must be at least six characters. Use a trusted LAN; local HTTP does not encrypt the PIN in transit. The Pi stores recent history in `data/history.sqlite3` and settings in `data/settings.json`. Keep the `data/` folder when updating the code.

For startup after reboot, use [deploy/sifs.service](deploy/sifs.service). It assumes the repository is at `/home/farm/sifs-web-monitor`, its virtual environment is at `venv/`, and the service user is `farm`. Put the variables above in `/etc/sifs/sifs.env` using `KEY=value` lines; set `SIFS_LOCAL_PIN` to a private value of at least six characters. The Firebase variables and service account are optional for LAN-only operation. Keep `/etc/sifs` root-owned with the environment file and key readable only by `farm` where needed. Install and enable the unit with `sudo install -m 644 deploy/sifs.service /etc/systemd/system/sifs.service`, `sudo systemctl daemon-reload`, and `sudo systemctl enable sifs.service`. Start it only after confirming the GPIO mapping, relay polarity, and disconnected-load relay test. Then check `systemctl status sifs.service` and `journalctl -u sifs.service -n 100 --no-pager`, reboot, and verify it comes back online.

## Development

The old `app.py` Flask LAN monitor is retained for reference. Do not run it alongside `pi_agent.py` because both use port 5000 and the same sensors. `pi_agent.py` serves the new local dashboard itself. The Pi controller cannot be exercised on a non-Pi machine without hardware mocks.
