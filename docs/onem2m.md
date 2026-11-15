# oneM2M backend (proof of concept)

The Pi can send its readings to a **oneM2M CSE** instead of Supabase. For the proof of concept
the CSE is [ACME](https://acmecse.net) running **on the Pi itself**, so the Pi is both:

- a **CSE** (`acme-cse.service`, configured in `acme/acme.ini`), and
- an **AE** (`hydro-onem2m.service`, code in `pi/onem2m/`) that registers with that CSE and
  publishes readings.

The HydroOS app (the `homegrow_frontend_onem2m` copy) reads from the CSE, read-only. Later the
CSE moves to (or registers with) an IN-CSE on Amazon EC2; see "Moving to EC2" below.

`HYDRO_BACKEND` picks **one** backend per Pi: `supabase` (the original path, unchanged) or
`onem2m`. A oneM2M Pi never writes to Supabase. Bluetooth works the same either way.

```
 sensors → hydro-sensors (main.py) → SQLite hydro.db ──► hydro-onem2m (AE) ──localhost──► ACME CSE ◄── app (LAN, HTTP)
                                                     └──► hydro-ble ───────── Bluetooth ───────────────► app (nearby)
```

SQLite stays the source of truth. A row is marked `synced_onem2m` only after the CSE accepts
it, so an ACME restart or outage loses nothing; uploads resume where they stopped.

## Resources

```
cse-hydropi                         MN-CSE, CSE-ID /id-hydropi
└─ HydroPi-<hash>   AE              originator CHydroPi-<hash>, lbl=[type:hydropi, system:<name>]
   ├─ acp           ACP             this AE: everything; app (CHydroOS-app): retrieve + discover
   ├─ readings      timeSeries      one instance per 30 s cycle, mni 5760 (2 days),
   │   └─ <tsi>                     dgt = cycle time, con = config.remote_record(row)
   └─ vision        container       camera.py metrics, one contentInstance per capture
```

A reading's `con` is the same record the Supabase sync and the Bluetooth server send
(`recorded_at`, `ph`, `ec`, `temperature_c`, `humidity`, `light_intensity`, `co2` as an integer,
`water_temp`, `water_level` as true/false, `system_name`, `username`), so the app parses all
three sources with one function.

## Setup on the Pi

1. **Install ACME** in its own virtualenv (it brings many packages the sensor code doesn't need):
   ```bash
   python3 -m venv /home/pi/acme-venv
   /home/pi/acme-venv/bin/pip install acmecse
   ```
2. **Set the CSE secret.** Copy `acme/.env.example` to `acme/.env` and set
   `ACME_SECURITY_SECRET` to a long random value.
3. **Create tokens**, one for this Pi and one for the app:
   ```bash
   cd pi
   python3 -m onem2m.make_token --secret <ACME_SECURITY_SECRET>    # run twice
   ```
   Put both printed **hash** lines in `acme/certs/http_token_auth.txt` (see the `.example`).
   The Pi's **token** goes in `pi/.env` as `ONEM2M_TOKEN`; the app's token goes in the app.
4. **Configure the Pi** in `pi/.env` (see `pi/.env.example`):
   `HYDRO_BACKEND=onem2m`, `ONEM2M_TOKEN=...`. The defaults for `ONEM2M_CSE_URL`
   (`http://127.0.0.1:8080`) and `ONEM2M_CSE_RN` (`cse-hydropi`) match `acme/acme.ini`.
5. **Install the services** (adjust `User` and paths in the files if needed):
   ```bash
   cd pi
   sudo cp systemd/acme-cse.service systemd/hydro-onem2m.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now acme-cse hydro-onem2m
   sudo systemctl restart hydro-sensors     # picks up HYDRO_BACKEND
   ```
6. **Check it:**
   ```bash
   journalctl -u hydro-onem2m -f          # "[onem2m] provisioned ..." then "uploaded N cycle(s)"
   curl -s http://127.0.0.1:8080/cse-hydropi/HydroPi-<hash>/readings/la \
     -H "X-M2M-Origin: CHydroOS-app" -H "X-M2M-RI: 1" -H "X-M2M-RVI: 3" \
     -H "Accept: application/json" -H "Authorization: Bearer <app token>"
   ```
   The AE name is printed by `main.py` at startup (`device HydroPi-...`).

The app reaches the CSE at `http://<Pi's LAN IP>:8080`, so the phone must be on the same
network (or use Tailscale for off-site demos).

## Long-term data

The CSE keeps about two days of readings. On a oneM2M Pi the full history is the Pi's SQLite
database; export it with:

```bash
cd pi && python3 export_readings.py --since 2026-11-01 --out readings.csv
```

Back up `hydro.db` (or the CSV) off the Pi regularly: with no Supabase copy, a failed SD card
would lose history.

## ACME behaviour this code relies on (checked with ACME 2026.5.3)

- An **MN-CSE with no registrar** serves requests normally. `enableRemoteCSE=false` for now.
- Bearer tokens: the token file holds **HMAC-SHA256(token, secret)**, not tokens
  (`onem2m/make_token.py` computes it). A missing or unknown token gets HTTP 401.
- Registering an AE again returns **4117** ("already registered"); creating an existing name
  returns **4105**. Both are treated as success, so provisioning can run on every start.
- A second timeSeriesInstance with the same `dgt` is refused with **4105**, which makes uploads
  repeat-safe: a duplicate means "already there".
- `mdd=true` (missing-data detection) also needs `mdt`.
- In an update, `acpi` must be the **only** attribute.
- `ofst` is 1-based, and discovery results are sorted **alphabetically** by default, so "last N
  instances" by offset is unreliable. Clients ask for a time window instead:
  `?rcn=4&fu=2&ty=30&cra=<UTC yyyymmddThhmmss>`, then sort by `dgt`/`recorded_at`.
- Only originators matching `CHydroPi-*` may register (`[cse.registration]`); the app reads
  without registering. Others get 4107.
- TinyDB writes are batched (`writeDelay=30`) to spare the SD card.

## Limits of the proof of concept

- **One basin per Pi.** Basins in a cycle share a timestamp, and the TimeSeries allows one
  instance per `dgt`. `onem2m_sync.py` refuses to start with more than one basin.
- **Read-only app.** No commands, subscriptions or polling channels yet.
- **Plain HTTP on the LAN** with bearer tokens. Tokens are not bound to originators, so any
  valid token holder could claim another originator; fine on a lab network, not on the internet.
- `camera.py` reads the level sensor on GPIO 11 while `config.py` says GPIO 17; unchanged here.

## Moving to EC2 later

1. Run ACME on EC2 as an **IN-CSE** (PostgreSQL storage, HTTPS behind a reverse proxy).
2. On each Pi, set the `[basic.config] registrar*` values in `acme/acme.ini` to the EC2 CSE,
   set `enableRemoteCSE=true`, and add the Pi's CSE-ID to the IN-CSE's `allowedCSROriginators`.
   Before several Pis register, give each a unique `cseID`/`cseName` (and the same
   `ONEM2M_CSE_RN` in `pi/.env`).
3. The Pi can't accept incoming connections, so the IN-CSE reaches it by long-polling, which
   ACME supports for request-unreachable CSEs. Announce the AE's resources to the IN-CSE so the
   app can read them there.
4. Point the app at the EC2 address (HTTPS) and turn off cleartext HTTP in its build.

The AE code (`pi/onem2m/`) doesn't change: it always talks to its local CSE.
