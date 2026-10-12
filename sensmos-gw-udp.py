#!/usr/bin/env python3
"""
SENSMOS — passive LoRaWAN gateway listener (Semtech packet forwarder, no MQTT).

FOR WHOM
For gateways with NO ChirpStack Gateway Bridge — Helium, Crankk, bare boxes running a
packet forwarder pointed straight at a server. There is no local MQTT there, so the
`sensmos-gw-mqtt.py` agent has nothing to subscribe to.

WHY PASSIVE, NOT A TEE
The tee (`sensmos-gw-tee.py`) sits in the data path: the forwarder must point at it and it
relays onward. On a gateway earning on Helium or Crankk that is unacceptable — our outage
would become theirs. This listener only WATCHES the traffic already flowing over loopback.
It is nobody's target, relays nothing and delays nothing. Killing this process is invisible
to the gateway.

WHAT IT READS
The Semtech GWMP protocol over UDP. Recognised by header, not by port (guessing-free):
    byte 0    version (1 or 2)
    bytes 1-2 token
    byte 3    type:  0=PUSH_DATA (received frames)   3=PULL_RESP (gateway is to transmit)
    bytes 4-11 gateway EUI
    then      JSON
A multi-server forwarder sends THE SAME frame to every configured server, so it shows up
several times on loopback — that is why deduplication here is mandatory, not decorative.

NEEDS ROOT (AF_PACKET socket). Installs nothing and changes nothing in the gateway config.

USAGE
    sudo ./sensmos-gw-udp.py --pair 192.168.1.50 --pin 123456 --name "rooftop gateway"
    sudo ./sensmos-gw-udp.py --dry-run          # print what it WOULD send, without sending
    sudo ./sensmos-gw-udp.py
"""
import argparse, base64, hashlib, json, os, socket, struct, sys, threading, time
import urllib.request, urllib.error

STATE     = os.environ.get("SENSMOS_STATE", "/etc/sensmos-gw-udp.json")
BE_URL    = os.environ.get("SENSMOS_BE", "https://sensmos.com")
BATCH_MAX = 200          # limit paczki po stronie BE (ext_registry: max dla rxpk)
QUEUE_MAX = 2000
DEDUP_S   = 30           # the same frame to several servers can arrive with a delay
RENEW_H   = 12

PUSH_DATA, PULL_RESP = 0x00, 0x03
SCOPES = ["radio.frames"]


# ── State ────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception:
        return {}

def save_state(s):
    with open(STATE, "w") as f: json.dump(s, f, indent=2)
    os.chmod(STATE, 0o600)


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
# The node vouches for the hardware on its own LAN; the backend issues a token only on its request.
def pair(node_ip, pin, name, be):
    st, info = http_json(f"http://{node_ip}/info")
    if st != 200:
        # A bare "(0)" tells no one anything — http_json keeps the real reason in the error field.
        why = info.get("error") or f"HTTP {st}"
        me  = os.path.basename(sys.argv[0])
        sys.exit(f"node {node_ip} did not respond: {why}\n\n"
                 f"Most likely: no node at that address, the node is on a different\n"
                 f"subnet than the gateway, or the router isolates clients.\n"
                 f"See what this gateway can actually reach:  sudo {me} --find")
    print(f"node {info.get('device_id','?')[:8]}  fw {info.get('firmware','?')}  "
          f"WS {'yes' if info.get('ws_connected') else 'NO'}")
    if not info.get("ws_connected"):
        sys.exit("node has no link to the backend — nothing to ask for approval")

    hdr = {"Authorization": f"Bearer {pin}"}
    st, r = http_json(f"http://{node_ip}/ext/authorize", headers=hdr, data={
        # Firmware reads `scopes` as a comma-separated STRING
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


# ── Find a node on the LAN ───────────────────────────────────────────────────
# Pairing needs the node's address, which the gateway owner usually doesn't know.
# We don't ask anyone to paste multi-line scripts into a terminal — that breaks.
# We probe /info, the exact endpoint pairing uses: whatever answers is definitely a node.
# We also show ws_connected, because without a live node-to-backend link pairing won't
# work anyway, and it's better to know that up front.
def find_nodes():
    import concurrent.futures as cf
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))          # sends nothing, just picks the route
        me = s.getsockname()[0]
    finally:
        s.close()
    net = ".".join(me.split(".")[:3])
    print(f"this host: {me}   scanning {net}.0/24 …")

    def probe(i):
        ip = f"{net}.{i}"
        try:
            with urllib.request.urlopen(f"http://{ip}/info", timeout=1.5) as r:
                d = json.loads(r.read().decode())
                return (ip, d) if "device_id" in d else None
        except Exception:
            return None

    found = []
    with cf.ThreadPoolExecutor(64) as ex:
        for r in ex.map(probe, range(1, 255)):
            if not r:
                continue
            ip, d = r
            found.append((ip, d))
            print(f"  NODE {ip:15}  id={str(d.get('device_id','?'))[:8]}  "
                  f"fw={d.get('firmware','?'):12} WS={'yes' if d.get('ws_connected') else 'NO'}")

    if not found:
        print("\nno Sensmos node found on this subnet.")
        print("A node must be on the SAME network as the gateway — it is what vouches for it.")
        return
    ready = [f for f in found if f[1].get("ws_connected")]
    print(f"\nfound: {len(found)}, ready to pair: {len(ready)}")
    if ready:
        # NO angle brackets in the hint — someone will paste them literally and bash
        # treats them as redirects and throws "syntax error". Seen in the wild.
        print(f"\n  sudo {os.path.basename(sys.argv[0])} --pair {ready[0][0]} --pin YOUR_PIN")
    else:
        print("\nnone have a backend link — without it there is nothing to ask for approval.")


# ── Packet parse ─────────────────────────────────────────────────────────────
# Linux loopback carries a fake Ethernet header (tcpdump: EN10MB), so the parse is the
# same as for a real NIC: 14 B frame header, then IPv4, then UDP.
def udp_payload(frame):
    if len(frame) < 14 + 20 + 8:
        return None
    if struct.unpack("!H", frame[12:14])[0] != 0x0800:      # IPv4 only
        return None
    ip = frame[14:]
    ihl = (ip[0] & 0x0F) * 4
    if ip[9] != 17 or len(ip) < ihl + 8:                    # UDP only
        return None
    return ip[ihl + 8:]


def gwmp(payload):
    """Returns (type, EUI, JSON object) or None. Recognised by header, not by port —
    forwarders show up on the oddest ports."""
    if len(payload) < 12 or payload[0] not in (1, 2):
        return None
    typ = payload[3]
    if typ not in (PUSH_DATA, PULL_RESP):
        return None
    body = payload[12:] if typ == PUSH_DATA else payload[4:]
    if not body:
        return None
    try:
        return typ, payload[4:12].hex(), json.loads(body.decode("utf-8", "strict"))
    except Exception:
        return None


class Dedup:
    """A multi-server forwarder sends the same frame to each server separately, and further
    fan-out stages can duplicate it again. Without this, one over-the-air transmission would
    be counted several times."""
    def __init__(self):
        self.seen = {}

    def fresh(self, pk):
        now = time.time()
        if len(self.seen) > 8000:
            self.seen = {k: t for k, t in self.seen.items() if now - t < DEDUP_S}
        key = hashlib.sha1(
            f"{pk.get('tmst')}|{pk.get('freq')}|{pk.get('datr')}|{pk.get('data')}".encode()
        ).digest()
        if now - self.seen.get(key, 0) < DEDUP_S:
            return False
        self.seen[key] = now
        return True


# ── Upload ───────────────────────────────────────────────────────────────────
class Sender(threading.Thread):
    """A failed batch goes back to the front of the queue. The queue is bounded — on overflow
    we drop the OLDEST, because growing without limit is worse than losing metadata."""
    def __init__(self, be, token, dry=False):
        super().__init__(daemon=True)
        self.be, self.token, self.dry = be, token, dry
        self.url = f"{be}/v1/ext/radio/rxpk"
        self.q, self.lock = [], threading.Lock()
        self.txq = []              # gateway transmits (dir=tx) → /radio/frames, separate path
        self.sent = self.failed = self.dropped = 0
        self.last_renew = time.time()
        self.backoff = 2
        self.first = True          # the first successful send should be VISIBLE

    def push(self, pk):
        with self.lock:
            self.q.append(pk)
            while len(self.q) > QUEUE_MAX:
                self.q.pop(0); self.dropped += 1

    def push_tx(self, fr):
        with self.lock:
            self.txq.append(fr)
            while len(self.txq) > QUEUE_MAX:
                self.txq.pop(0)

    # Gateway transmits go to /radio/frames with dir=tx — the proof that this box transmits
    # (a node hearing the same payload confirms the gateway). Rare and non-critical, so no
    # retry: on error we drop, we don't jam the receive path over a lost proof.
    def _flush_tx(self):
        with self.lock:
            tx, self.txq = self.txq[:BATCH_MAX], self.txq[BATCH_MAX:]
        if not tx:
            return
        if self.dry:
            print(f"[dry-run] WOULD SEND {len(tx)} gateway-transmit frame(s) "
                  f"to {self.be}/v1/ext/radio/frames (dir=tx)")
            return
        http_json(f"{self.be}/v1/ext/radio/frames", data={"frames": tx}, headers=self.hdr())

    def hdr(self):
        return {"Authorization": f"Bearer {self.token}"}

    def renew(self):
        st, r = http_json(f"{self.be}/v1/ext/renew", data={}, headers=self.hdr())
        if st == 200:
            self.last_renew = time.time()
            s = load_state(); s["exp"] = r.get("exp", s.get("exp")); save_state(s)
            print(f"[sensmos] token renewed until {r.get('exp','?')}")
        return st == 200

    def run(self):
        while True:
            time.sleep(self.backoff)
            if not self.dry and time.time() - self.last_renew > RENEW_H * 3600:
                self.renew()

            self._flush_tx()      # gateway transmits, independent of whether any rx is queued

            with self.lock:
                batch, self.q = self.q[:BATCH_MAX], self.q[BATCH_MAX:]
            if not batch:
                self.backoff = 2
                continue

            if self.dry:
                print(f"[dry-run] WOULD SEND {len(batch)} frame(s) to {self.url}:")
                for pk in batch[:5]:
                    print("   ", json.dumps(pk))
                if len(batch) > 5:
                    print(f"    … and {len(batch)-5} more")
                self.sent += len(batch)
                continue

            st, r = http_json(self.url, data={"rxpk": batch}, headers=self.hdr())
            if st == 200:
                self.sent += len(batch); self.backoff = 2
                # Without this, normal mode stays silent for the first 5 minutes and looks hung —
                # especially after --dry-run, where frames scroll past on screen.
                if self.first:
                    self.first = False
                    print(f"[sensmos] connection works — first batch accepted "
                          f"({len(batch)} frames). Status now printed every minute.")
                continue

            if st == 401 and self.renew():
                st, _ = http_json(self.url, data={"rxpk": batch}, headers=self.hdr())
                if st == 200:
                    self.sent += len(batch); continue

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


# ── Install as a service ─────────────────────────────────────────────────────
# We show the unit file BEFORE writing it and require prior pairing — without a token the
# service would restart-loop. The script path is absolute so ExecStart doesn't depend on
# where it was launched from.
SERVICE_PATH = "/etc/systemd/system/sensmos-gw-udp.service"

def install_service():
    import shutil, subprocess
    if os.geteuid() != 0:
        sys.exit("root required: run with sudo")
    if not load_state().get("token"):
        me = os.path.basename(sys.argv[0])
        sys.exit(f"pair first — the service needs a token:\n"
                 f"  sudo {me} --find\n"
                 f"  sudo {me} --pair NODE_IP --pin YOUR_PIN")

    exe = os.path.abspath(sys.argv[0])
    # Install canonically to /usr/local/bin if launched from elsewhere — otherwise ExecStart
    # would point at a file someone may move or delete.
    dest = "/usr/local/bin/sensmos-gw-udp.py"
    if exe != dest:
        shutil.copyfile(exe, dest)
        os.chmod(dest, 0o755)
        print(f"installed script to {dest}")

    py   = shutil.which("python3") or sys.executable
    unit = (
        "[Unit]\n"
        "Description=Sensmos passive gateway listener\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={py} {dest}\n"
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
                ["systemctl", "enable", "--now", "sensmos-gw-udp"]):
        r = subprocess.run(cmd)
        if r.returncode != 0:
            sys.exit(f"command failed: {' '.join(cmd)}")

    print("\nservice is running. Follow it with:")
    print("  journalctl -u sensmos-gw-udp -f")
    print("Stop/remove with:")
    print("  sudo systemctl disable --now sensmos-gw-udp")
    print(f"  sudo rm {SERVICE_PATH} {dest}")


# A gateway transmit (PULL_RESP txpk) → our `frames` shape with dir=tx. rssi carries the TX
# power; the payload hex is what matters — a node hearing the same hex confirms the gateway.
def txpk_to_frame(txpk):
    datr = str(txpk.get("datr", ""))
    sf = 0
    if "SF" in datr and "BW" in datr:
        try: sf = int(datr.split("SF")[1].split("BW")[0])
        except Exception: pass
    try:
        raw = base64.b64decode(txpk.get("data", ""))
    except Exception:
        return None
    if not raw:
        return None
    return {"ts": int(time.time()), "freq": round(float(txpk.get("freq", 0) or 0), 4),
            "sf": sf, "mode": 0, "rssi": int(txpk.get("powe", 0) or 0),
            "len": len(raw), "crc": True, "hex": raw[:128].hex(), "dir": "tx"}


# ── Listen ───────────────────────────────────────────────────────────────────
def sniff(args, state):
    be    = args.be or state.get("be", BE_URL)
    token = state.get("token", "")

    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        s.bind((args.iface, 0))
    except AttributeError:
        sys.exit("AF_PACKET is Linux only — this listener runs on the gateway, not on Windows")
    except PermissionError:
        sys.exit("root required: run with sudo (or grant CAP_NET_RAW)")
    except OSError as e:
        sys.exit(f"cannot listen on {args.iface}: {e}")

    sender = Sender(be, token, dry=args.dry_run); sender.start()
    dedup  = Dedup()
    stat   = {"push": 0, "dup": 0, "tx": 0, "eui": set()}
    last   = time.time()

    print(f"sensmos-gw-udp  listening on {args.iface}"
          + ("  [DRY-RUN — nothing leaves this box]" if args.dry_run else f"  → {be}"))

    while True:
        try:
            frame = s.recv(65535)
        except KeyboardInterrupt:
            break
        pay = udp_payload(frame)
        if not pay:
            continue
        g = gwmp(pay)
        if not g:
            continue
        typ, eui, obj = g
        stat["eui"].add(eui)

        if typ == PULL_RESP:
            # The gateway is about to TRANSMIT. We report only transmits others can HEAR —
            # ipol=false (non-inverted IQ, e.g. Helium PoC beacons). A normal LoRaWAN downlink
            # (ipol=true) is inverted and inaudible to our nodes, so it proves nothing — skip it.
            # Reported ones become proof the box transmits: a node hearing the same payload
            # confirms the gateway is real (see /radio/frames dir=tx on the backend).
            stat["tx"] += 1
            t = obj.get("txpk", {})
            if not t.get("ipol", False):
                fr = txpk_to_frame(t)
                if fr:
                    sender.push_tx(fr)
            if args.log_tx:
                print(f"[transmit] {eui[:8]} {t.get('freq')} MHz {t.get('datr')} "
                      f"{t.get('powe')} dBm ipol={t.get('ipol')} {t.get('size')} B")
            continue

        for pk in obj.get("rxpk") or []:
            if not isinstance(pk, dict):
                continue
            if dedup.fresh(pk):
                stat["push"] += 1
                sender.push(pk)
            else:
                stat["dup"] += 1

        # Every minute, not every five: the gateway owner must see it's alive.
        if time.time() - last >= 60:
            last = time.time()
            print(f"[status] gateways {len(stat['eui'])}  frames {stat['push']}  "
                  f"duplicates dropped {stat['dup']}  gateway transmits {stat['tx']}  "
                  f"sent {sender.sent}  queued {len(sender.q)}")


def main():
    p = argparse.ArgumentParser(
        description="SENSMOS — passive LoRaWAN gateway listener (packet forwarder, no MQTT)")
    p.add_argument("--find", action="store_true",
                   help="find Sensmos nodes on the local network (and whether they are ready to pair)")
    p.add_argument("--install-service", action="store_true",
                   help="install and start a systemd service so it runs on boot")
    p.add_argument("--pair", metavar="NODE_IP", help="one-time pairing with a node on the LAN")
    p.add_argument("--pin",  help="node PIN (for pairing)")
    p.add_argument("--name", default=os.uname().nodename if hasattr(os, "uname") else "gateway")
    p.add_argument("--be",   help=f"backend URL (default: saved at pairing / {BE_URL})")
    p.add_argument("--iface", default="lo",
                   help="interface to watch (default lo — forwarder and servers talk over loopback)")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be sent, and send NOTHING")
    p.add_argument("--log-tx", action="store_true",
                   help="also print gateway transmits (PULL_RESP)")
    a = p.parse_args()

    if a.install_service:
        return install_service()

    if a.find:
        return find_nodes()

    if a.pair:
        if not a.pin: sys.exit("--pair requires --pin")
        return pair(a.pair, a.pin, a.name, a.be or BE_URL)

    s = load_state()
    if not s.get("token") and not a.dry_run:
        me = os.path.basename(sys.argv[0])
        sys.exit(f"no token in {STATE} — pair first:  sudo {me} --pair NODE_IP --pin YOUR_PIN\n"
                 f"(or look without sending:  sudo {me} --dry-run)")
    sniff(a, s)


if __name__ == "__main__":
    main()
