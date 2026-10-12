#!/usr/bin/env python3
"""
SENSMOS — LoRaWAN gateway listener via ChirpStack Gateway Bridge.

RECEIVE ONLY. The agent does not transmit and cannot be made to — a deliberate choice, not
a missing feature. The gateway's value is that it HEARS our nodes: a node is independent of
the internet and will one day report a fault while offline, and the gateway is there to hear it.

HOW IT DIFFERS FROM THE TEE (sensmos-gw-tee.py)
The tee sits in the data path: the gateway sends packets to it and it relays them onward to
the real network server. This agent relays nothing — where Gateway Bridge runs, the gateway's
traffic is already on the local MQTT and we are simply a second subscriber. If this process
dies, the gateway doesn't even notice.

REQUIREMENTS
    pip install paho-mqtt
    pip install chirpstack-api      # optional, see ENCODING

ENCODING
Gateway Bridge publishes protobuf by default (`marshaler = "protobuf"`). With `chirpstack-api`
present, we read protobuf and NOTHING has to change on someone else's gateway. Without that
library we read JSON, but then the owner must set `marshaler = "json"` and restart the service.

USAGE
    sudo ./sensmos-gw-mqtt.py --pair 192.168.1.50 --pin 123456 --name "rooftop gateway"
    ./sensmos-gw-mqtt.py --broker localhost
"""
import argparse, base64, binascii, json, os, re, sys, threading, time
import urllib.request, urllib.error

# Separate path from the tee. A shared one wiped the token when both ran on one gateway.
STATE     = os.environ.get("SENSMOS_STATE", "/etc/sensmos-gw-mqtt.json")
BE_URL    = os.environ.get("SENSMOS_BE", "https://sensmos.com")
MAX_FRAME = 128          # this many payload bytes forwarded — BE trims anyway
BATCH_MAX = 200          # limit paczki po stronie BE (ext_registry: max)
QUEUE_MAX = 2000         # frames buffered while Sensmos is unreachable
DEDUP_S   = 10           # window to collapse the same transmission from several gateways
RENEW_H   = 12           # how often (hours) we renew the token

SCOPES = ["radio.frames"]        # nadawania nie prosimy — agent go nie ma


# ── State ────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception:
        return {}

def save_state(s):
    with open(STATE, "w") as f: json.dump(s, f, indent=2)
    os.chmod(STATE, 0o600)          # the token is a secret


# ── HTTP ─────────────────────────────────────────────────────────────────────
def http_json(url, data=None, headers=None, method=None, timeout=15):
    body = json.dumps(data).encode() if data is not None else None
    req  = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"))
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items(): req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:    return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


# ── Pairing ──────────────────────────────────────────────────────────────────
# The node vouches for the hardware on its own LAN; the backend issues a token only on its
# request. The agent never asks the backend directly.
def pair(node_ip, pin, name, be):
    st, info = http_json(f"http://{node_ip}/info")
    if st != 200:
        me = os.path.basename(sys.argv[0])
        why = info.get("error") or f"HTTP {st}"
        sys.exit(f"node {node_ip} did not respond: {why}")
    print(f"node {info.get('device_id','?')[:8]}  fw {info.get('firmware','?')}  "
          f"WS {'yes' if info.get('ws_connected') else 'NO'}")
    if not info.get("ws_connected"):
        sys.exit("node has no link to the backend — nothing to ask for approval")

    hdr = {"Authorization": f"Bearer {pin}"}
    st, r = http_json(f"http://{node_ip}/ext/authorize", headers=hdr, data={
        # Firmware czyta `scopes` jako STRING rozdzielony przecinkami
        # (http_node.cpp: doc["scopes"] | ""). A JSON array yields an empty string there,
        # so the node answers 400 "kind and scopes required".
        "kind": "lora-gw", "name": name, "scopes": ",".join(SCOPES),
    })
    if st == 403: sys.exit("wrong PIN")
    if st != 202: sys.exit(f"node refused: {r}")

    req = r["req"]
    print(f"request sent (req {req}), waiting for backend approval", end="", flush=True)
    for _ in range(30):
        time.sleep(1); print(".", end="", flush=True)
        st, r = http_json(f"http://{node_ip}/ext/authorize?req={req}", headers=hdr)
        if st != 200 or r.get("status") == "pending":
            continue
        print()
        if r.get("status") != "granted":
            sys.exit(f"denied: {r.get('reason')}")
        s = load_state()
        s.update(token=r["token"], id=r["id"], exp=r["exp"], be=be, node=node_ip)
        save_state(s)
        print(f"OK — attachment {r['id']}, token saved to {STATE}")
        return
    sys.exit("\nno answer within 30 s")


# ── Decode Gateway Bridge events ─────────────────────────────────────────────
# Protobuf's JSON mapping allows both name conventions, and different bridge versions
# emit different ones. We reach for both instead of guessing.
def pick(d, *names, default=None):
    for n in names:
        if isinstance(d, dict) and n in d and d[n] is not None:
            return d[n]
    return default


try:
    from chirpstack_api import gw as _gw
    from google.protobuf.json_format import MessageToDict
    HAVE_PB = True
except Exception:
    HAVE_PB = False


def decode_uplink(payload):
    """Zwraca dict UplinkFrame albo None. Najpierw JSON (tani), potem protobuf."""
    try:
        o = json.loads(payload.decode())
        if isinstance(o, dict):
            return o
    except Exception:
        pass
    if not HAVE_PB:
        return None
    try:
        m = _gw.UplinkFrame()
        m.ParseFromString(payload)
        return MessageToDict(m, preserving_proto_field_name=False)
    except Exception:
        return None


# Timestamp z protojson bywa z nanosekundami ("…00.123456789Z"), a fromisoformat przyjmuje
# at most 6 digits — without this trim the timestamp never parsed.
_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?$")

def parse_ts(v):
    m = _TS.match(str(v or "").strip())
    if not m:
        return None
    frac = (m.group(2) or "")[:6]
    zone = (m.group(3) or "Z").replace("Z", "+00:00")
    if len(zone) == 5:                       # "+0200" → "+02:00"
        zone = zone[:3] + ":" + zone[3:]
    txt = m.group(1) + ("." + frac if frac else "") + zone
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(txt).timestamp())
    except Exception:
        return None


# CRCStatus in gw.proto: NO_CRC = 0, BAD_CRC = 1, CRC_OK = 2. Numeric 1 means BAD CRC —
# I used to take it as good. We accept only an explicit CRC_OK: BE uses this field to decide
# whether to extract `smos` from the frame, so a mistake produces a fake beacon ID.
def crc_ok(v):
    return str(v).strip().upper() in ("CRC_OK", "2")


# UplinkFrame → our `frames` format. We normalize HERE, not on BE: the integration registry
# has a `frames` format meant for attachments we write ourselves.
def to_frame(up):
    rx  = pick(up, "rxInfo", "rx_info", default={}) or {}
    tx  = pick(up, "txInfo", "tx_info", default={}) or {}
    phy = pick(up, "phyPayload", "phy_payload", default="") or ""
    try:
        raw = base64.b64decode(phy)
    except Exception:
        return None
    if not raw:
        return None

    mod  = pick(tx, "modulation", default={}) or {}
    lora = mod.get("lora") or {}
    fsk  = mod.get("fsk")  or {}
    if not lora and not fsk:
        return None                      # we don't know how it arrived — no guessing

    return {
        "ts":   parse_ts(pick(rx, "gwTime", "gw_time", "time")),
        # Our canonical format keeps frequency in MHz (like Semtech rxpk),
        # a Gateway Bridge podaje w Hz.
        "freq": round((pick(tx, "frequency", default=0) or 0) / 1e6, 4),
        "sf":   int(pick(lora, "spreadingFactor", "spreading_factor", default=0) or 0),
        "mode": 1 if fsk else 0,
        "rssi": int(pick(rx, "rssi", default=0) or 0),
        "snr":  float(pick(rx, "snr", default=0) or 0),
        "len":  len(raw),
        "crc":  crc_ok(pick(rx, "crcStatus", "crc_status", default="")),
        "hex":  binascii.hexlify(raw[:MAX_FRAME]).decode(),
    }


# The same transmission heard by two gateways on one broker is ONE over-the-air event —
# without this it would count as two samples on the same edge.
class Dedup:
    def __init__(self):
        self.seen = {}

    def fresh(self, fr):
        now = time.time()
        if len(self.seen) > 4000:
            self.seen = {k: t for k, t in self.seen.items() if now - t < DEDUP_S}
        key = (fr["hex"], fr["freq"], fr["sf"])
        if now - self.seen.get(key, 0) < DEDUP_S:
            return False
        self.seen[key] = now
        return True


# ── Upload to Sensmos ────────────────────────────────────────────────────────
class Sender(threading.Thread):
    """Separate thread — HTTP to Sensmos must not block the MQTT loop.

    A failed batch goes back to the FRONT of the queue: it used to be dropped on the first
    error, so one network blip lost frames for good. The queue is bounded and on overflow
    drops the OLDEST — growing without limit is worse."""
    def __init__(self, be, token):
        super().__init__(daemon=True)
        self.be, self.token = be, token
        self.url = f"{be}/v1/ext/radio/frames"
        self.q, self.lock = [], threading.Lock()
        self.sent = self.failed = self.dropped = 0
        self.last_renew = time.time()
        self.backoff = 2

    def push(self, fr):
        with self.lock:
            self.q.append(fr)
            while len(self.q) > QUEUE_MAX:
                self.q.pop(0); self.dropped += 1

    def hdr(self):
        return {"Authorization": f"Bearer {self.token}"}

    # The token has a limited lifetime. Without this the agent would just start getting 401
    # and never recover.
    def renew(self):
        st, r = http_json(f"{self.be}/v1/ext/renew", data={}, headers=self.hdr())
        if st == 200:
            self.last_renew = time.time()
            s = load_state(); s["exp"] = r.get("exp", s.get("exp")); save_state(s)
            print(f"[sensmos] token renewed until {r.get('exp','?')}")
        else:
            print(f"[sensmos] token renewal failed ({st})", file=sys.stderr)
        return st == 200

    def run(self):
        while True:
            time.sleep(self.backoff)
            if time.time() - self.last_renew > RENEW_H * 3600:
                self.renew()

            with self.lock:
                batch, self.q = self.q[:BATCH_MAX], self.q[BATCH_MAX:]
            if not batch:
                self.backoff = 2
                continue

            st, r = http_json(self.url, data={"frames": batch}, headers=self.hdr())
            if st == 200:
                self.sent += len(batch)
                self.backoff = 2
                continue

            if st == 401 and self.renew():
                st, r = http_json(self.url, data={"frames": batch}, headers=self.hdr())
                if st == 200:
                    self.sent += len(batch); continue

            # A 4xx other than 401 means this batch will never go through — retrying it
            # forever would jam the queue for good.
            if 400 <= st < 500 and st != 401:
                self.failed += len(batch)
                print(f"[sensmos] batch permanently rejected ({st}) {r}", file=sys.stderr)
                continue

            with self.lock:
                self.q[:0] = batch
                while len(self.q) > QUEUE_MAX:
                    self.q.pop(); self.dropped += 1
            self.backoff = min(self.backoff * 2, 60)
            print(f"[sensmos] send failed ({st}), retrying in {self.backoff}s", file=sys.stderr)


# ── Main loop ────────────────────────────────────────────────────────────────
def run(args, state):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("paho-mqtt missing — install: pip install paho-mqtt")

    be    = args.be or state.get("be", BE_URL)
    token = state["token"]

    print(f"sensmos-gw-mqtt (receive only)  broker {args.broker}:{args.port}  "
          f"protobuf {'yes' if HAVE_PB else 'NO — needs marshaler=json on the gateway'}")

    sender = Sender(be, token); sender.start()
    dedup  = Dedup()

    def on_connect(cli, _u, _f, rc, *a):
        if rc != 0:
            print(f"[mqtt] connection refused (rc={rc})", file=sys.stderr); return
        # '+' na prefiksie: bywa regionem (eu868), bywa pusty — nie zgadujemy.
        cli.subscribe("+/gateway/+/event/up", qos=0)
        print("[mqtt] connected, subscribed to event/up")

    def on_message(_c, _u, msg):
        parts = msg.topic.split("/")
        try:
            gw_id = parts[parts.index("gateway") + 1]
        except ValueError:
            return
        if args.gateway and gw_id.lower() != args.gateway.lower():
            return
        up = decode_uplink(msg.payload)
        if up is None:
            print("[mqtt] cannot read uplink — set marshaler=\"json\" on the gateway "
                  "or install chirpstack-api", file=sys.stderr)
            return
        fr = to_frame(up)
        if fr and dedup.fresh(fr):
            sender.push(fr)

    # paho-mqtt 2.0 made CallbackAPIVersion a REQUIRED argument — without it the agent
    # crashed at startup on every fresh install (pip gives 2.x today).
    try:
        cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except AttributeError:
        cli = mqtt.Client()
    if args.user:
        cli.username_pw_set(args.user, args.password or "")
    cli.on_connect, cli.on_message = on_connect, on_message
    cli.reconnect_delay_set(min_delay=2, max_delay=60)

    # The broker often comes up later than we do (service start order at boot) —
    # the process used to just die.
    delay = 2
    while True:
        try:
            cli.connect(args.broker, args.port, 60)
            break
        except Exception as e:
            print(f"[mqtt] {args.broker}:{args.port} unreachable ({e}), retrying in {delay}s",
                  file=sys.stderr)
            time.sleep(delay); delay = min(delay * 2, 60)

    cli.loop_start()
    last = time.time()
    try:
        while True:
            time.sleep(30)
            if time.time() - last >= 300:
                last = time.time()
                print(f"[status] sent {sender.sent}  rejected {sender.failed}  "
                      f"dropped {sender.dropped}  queued {len(sender.q)}")
    except KeyboardInterrupt:
        cli.loop_stop()


# Service: we show the unit before writing it and require a token, to avoid a restart loop.
# Absolute path so ExecStart doesn't depend on the launch directory.
SERVICE_PATH = "/etc/systemd/system/sensmos-gw-mqtt.service"

def install_service(args):
    import shutil, subprocess
    if os.geteuid() != 0:
        sys.exit("root required: run with sudo")
    if not load_state().get("token"):
        me = os.path.basename(sys.argv[0])
        sys.exit(f"pair first — the service needs a token:\n"
                 f"  sudo {me} --pair NODE_IP --pin YOUR_PIN")

    exe  = os.path.abspath(sys.argv[0])
    dest = "/usr/local/bin/sensmos-gw-mqtt.py"
    if exe != dest:
        shutil.copyfile(exe, dest); os.chmod(dest, 0o755)
        print(f"installed script to {dest}")

    py    = shutil.which("python3") or sys.executable
    extra = f" --broker {args.broker}" if args.broker != "localhost" else ""
    unit  = (
        "[Unit]\n"
        "Description=Sensmos LoRaWAN gateway listener (MQTT)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={py} {dest}{extra}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "User=root\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    print("\n--- unit file ---\n" + unit + "-----------------")
    with open(SERVICE_PATH, "w") as f:
        f.write(unit)
    print(f"written: {SERVICE_PATH}")

    for cmd in (["systemctl", "daemon-reload"],
                ["systemctl", "enable", "--now", "sensmos-gw-mqtt"]):
        if subprocess.run(cmd).returncode != 0:
            sys.exit(f"command failed: {' '.join(cmd)}")

    print("\nservice is running. Follow it with:")
    print("  journalctl -u sensmos-gw-mqtt -f")
    print("Stop/remove with:")
    print("  sudo systemctl disable --now sensmos-gw-mqtt")
    print(f"  sudo rm {SERVICE_PATH} {dest}")


def main():
    p = argparse.ArgumentParser(
        description="SENSMOS — LoRaWAN gateway listener via ChirpStack Gateway Bridge")
    p.add_argument("--pair", metavar="NODE_IP", help="one-time pairing with a node on the LAN")
    p.add_argument("--pin",  help="node PIN (for pairing)")
    p.add_argument("--name", default=os.uname().nodename if hasattr(os, "uname") else "gateway")
    p.add_argument("--be",   help=f"backend URL (default: saved at pairing / {BE_URL})")
    p.add_argument("--broker",  default="localhost")
    p.add_argument("--port",    type=int, default=1883)
    p.add_argument("--user"); p.add_argument("--password")
    p.add_argument("--gateway", help="restrict to one gateway (EUI)")
    p.add_argument("--install-service", action="store_true",
                   help="install and start a systemd service so it runs on boot")
    a = p.parse_args()

    if a.install_service:
        return install_service(a)

    if a.pair:
        if not a.pin: sys.exit("--pair requires --pin")
        return pair(a.pair, a.pin, a.name, a.be or BE_URL)

    s = load_state()
    if not s.get("token"):
        sys.exit(f"no token in {STATE} — pair first: sudo {os.path.basename(sys.argv[0])} --pair NODE_IP --pin YOUR_PIN")
    run(a, s)


if __name__ == "__main__":
    main()
