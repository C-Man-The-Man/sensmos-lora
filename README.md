# Sensmos LoRa Docker

A community-maintained Docker packaging of the UDP and MQTT listener scripts from the original [Sensmos Gateway repository](https://github.com/sensmos-network/sensmos-gateway).

This project makes the Sensmos LoRa integration easier to deploy by providing prebuilt Docker images. Instead of installing Python and its dependencies or building an image locally, users can download a Compose file and pull the ready-to-use image.

Docker also keeps the application isolated from the host operating system. This is particularly useful for CrankkOS gateways, where the root filesystem is read-only and persistent files must be stored under `/data`.

**This project packages the existing Sensmos scripts without intentionally modifying their code.** The original pairing process and integration behavior remain unchanged.

## Original Sensmos project

The integration scripts and their functionality belong to the Sensmos project. This repository provides Docker packaging and deployment instructions.

- **Original repository:** [https://github.com/sensmos-network/sensmos-gateway](https://github.com/sensmos-network/sensmos-gateway)
- **UDP listener:** [https://github.com/sensmos-network/sensmos-gateway/blob/main/sensmos-gw-udp.py](https://github.com/sensmos-network/sensmos-gateway/blob/main/sensmos-gw-udp.py)
- **MQTT listener:** [https://github.com/sensmos-network/sensmos-gateway/blob/main/sensmos-gw-mqtt.py](https://github.com/sensmos-network/sensmos-gateway/blob/main/sensmos-gw-mqtt.py)
- **MQTT integration guide:** [https://github.com/sensmos-network/sensmos-gateway/blob/main/sensmos-gw-mqtt.md](https://github.com/sensmos-network/sensmos-gateway/blob/main/sensmos-gw-mqtt.md)
- **Sensmos gateway documentation:** [https://sensmos.com/docs/gateway/](https://sensmos.com/docs/gateway/)

This Docker package covers the UDP and MQTT listener workflow. It does not implement the separate direct gateway configuration method described as “Way 1” in the official documentation.

## What are the UDP and MQTT listeners?

### UDP / GWMP listener

LoRaWAN gateways commonly use the Semtech packet-forwarder protocol, known as GWMP, to communicate received radio packets.

The UDP listener monitors this traffic on the gateway's loopback interface (`lo`). It observes existing gateway traffic rather than replacing the packet forwarder or changing its configuration.

The container uses host networking so it can access the relevant traffic on the host. The listener will only see frames if the gateway exposes the expected traffic on that interface.

### MQTT listener

Some LoRaWAN deployments use ChirpStack Gateway Bridge to make gateway events available through an MQTT broker.

The MQTT listener connects to that broker and listens for uplink events. This makes it useful for gateways and future projects that use a compatible ChirpStack Gateway Bridge setup.

The MQTT listener does not install or configure ChirpStack or the MQTT broker. Those services must already be available. The default broker address is `localhost:1883`; because the container uses host networking, this means port 1883 on the gateway itself.

### Why are there two listeners?

UDP and MQTT are two different ways to access gateway traffic. Which one is appropriate depends on the software already running on the gateway.

The two integrations are paired separately through a Sensmos node on the same local network. You can use UDP alone or pair MQTT as well if your gateway setup supports it.

## Requirements

Before starting, make sure you have:

- A supported Linux gateway with Docker and Docker Compose installed.
- SSH access to the gateway.
- An ESP32, ESP32-S3 or ESP32-C3 Sensmos node running on the same local network as the gateway.
- The Sensmos node online and connected to the Sensmos backend.
- The pairing PIN for the Sensmos node.
- For UDP listening, a gateway whose Semtech GWMP traffic is visible on the host's loopback interface.
- For MQTT listening, a working ChirpStack Gateway Bridge and MQTT broker.

The Sensmos node is used to authorize the gateway attachment. It does not replace the gateway's LoRa radio hardware.

---

## 1. CrankkIMG / Raspberry Pi OS

This guide is intended for Raspberry Pi-based gateways and hotspots running CrankkIMG or Raspberry Pi OS, such as:

- **Controllino V1**
- **COTX X3**
- **Finestra**
- **Helium OG**
- **Midas 926**
- **MNTD/RAKv2**
- **Nebra Indoor / Outdoor**
- **Pisces P100**
- **SenseCAP M1**
- **Wisgate / RAKv1.5**
- any other Raspberry Pi hardware with a LoRa hat.

These instructions use the modern Docker Compose command: `docker compose`.

### Step 1 — Create a working directory

Connect to your gateway over SSH and run:

```sh
mkdir -p sensmos-lora && cd sensmos-lora
```

This creates a working directory where you will keep the Compose file.

### Step 2 — Download the Compose file

Download the Compose file directly from this repository:

```sh
curl -fL -O https://raw.githubusercontent.com/C-Man-The-Man/sensmos-lora/main/docker-compose.yml
```

Create the directory where pairing information will be stored:

```sh
sudo mkdir -p /var/lib/sensmos
```

This directory keeps the listener's state across container restarts and updates. Its `JSON` files contain authorization tokens, so do not share or publish them.

### Step 3 — Download the Docker image

Pull the prebuilt image:

```sh
sudo docker compose pull
```

Docker downloads the image for your gateway's supported architecture. You do not need to install Python or build the image yourself.

### Step 4 — Test the container

Before pairing anything, run the UDP listener in dry-run mode:

```sh
sudo docker compose run --rm sensmos-lora --dry-run
```

You should see output similar to:

`sensmos-gw-udp listening on lo [DRY-RUN — nothing leaves this box]`

This checks that the container starts and the listener's dry-run mode works. Press `Ctrl+C` to exit if the command remains running.

### Step 5 — Find your Sensmos node

Run:

```sh
sudo docker compose run --rm sensmos-lora --find
```

The listener scans the local subnet for Sensmos nodes. If your node is reachable, the output should resemble:

```text
this host: 192.168.x.xx scanning 192.168.x.0/24 …
  NODE 192.168.x.xx id=xxxxxxxx fw=0.88 WS=yes

found: 1, ready to pair: 1
```

The addresses above are examples. Use the actual IP address shown for your node in the next step.

**If no node is found:** check that the node is powered on, online and connected to the same local network. Guest Wi-Fi isolation, VLANs or firewall rules may prevent discovery.

### Step 6 — Pair the UDP listener

Replace `NODE_IP` with your node's IP address, `YOUR_PIN` with its pairing PIN, and `GATEWAY_NAME` with a name of your choice.

```sh
sudo docker compose run --rm sensmos-lora --pair NODE_IP --pin YOUR_PIN --name "GATEWAY_NAME"
```

Wait for the pairing command to complete successfully. The listener saves its pairing state in `/var/lib/sensmos`.

### Step 7 — Pair the MQTT listener (optional)

Skip this step if you only want to use UDP.

MQTT requires a compatible ChirpStack Gateway Bridge and MQTT broker to be running and accessible from the gateway.

Run:

```sh
sudo docker compose run --rm sensmos-lora --mqtt --pair NODE_IP --pin YOUR_PIN --name "GATEWAY_NAME"
```

Use the same node IP and pairing PIN. MQTT pairing is separate from UDP pairing, so this step must be completed independently.

### Step 8 — Start the container

Once you have paired the listener or listeners you intend to use, start the service in the background:

```sh
sudo docker compose up -d
```

The UDP listener starts automatically. If MQTT has been paired and its state file exists, the MQTT listener starts as well.

### Step 9 — Check the logs

To see what the listeners are doing, run:

```sh
sudo docker compose logs -f
```

This displays live logs. Press `Ctrl+C` to stop viewing them; the container continues running.

### Step 10 — Stop the container

To stop and remove the container, run:

```sh
sudo docker compose down
```

Your pairing state remains stored in `/var/lib/sensmos`. You do not normally need to pair again when restarting the container.

---

## 2. CrankkOS

CrankkOS is based on a read-only root filesystem. Its writable directory is `/data`, so the project files and persistent listener state must be stored there.

This guide is intended for RockPi-based gateways and hotspots running CrankkOS, such as:

- **Bobcat 300 G280 / G285 / G290 / G295**
- **Browan MerryIOT / PantherX2**
- **Linxdot CM4 / Syncrobit**
- **Linxdot RK3566**
- **Nebra RockPi**

This guide uses the legacy `docker-compose` command available on older CrankkOS installations.

### Step 1 — Create the working directories

Connect to your gateway over SSH and run each command individually:

```sh
mkdir -p /data/sensmos
```
```sh
cd /data/sensmos
```
```sh
mkdir -p /data/sensmos/lora
```

The first directory holds the Compose file. The `/lora` directory stores persistent pairing information.

### Step 2 — Download the CrankkOS Compose file

Download the CrankkOS-specific configuration:

```sh
curl -fL -O https://raw.githubusercontent.com/C-Man-The-Man/sensmos-lora/main/docker-compose.crankkos.yml
```

Rename the file to the standard Compose filename:

```sh
mv docker-compose.crankkos.yml docker-compose.yml
```

**This rename is intentional.** From this point onward, you can use the usual `docker-compose` commands without specifying another filename.

### Step 3 — Download the Docker image

Pull the prebuilt image:

```sh
docker-compose pull
```

This downloads the image instead of building it on the CrankkOS gateway.

### Step 4 — Test the container

Run the UDP listener in dry-run mode:

```sh
docker-compose run --rm sensmos-lora --dry-run
```

Expected output:

`sensmos-gw-udp listening on lo [DRY-RUN — nothing leaves this box]`

This checks that the container starts and the listener's dry-run mode works. Press `Ctrl+C` to exit if necessary.

### Step 5 — Find your Sensmos node

Run:

```sh
docker-compose run --rm sensmos-lora --find
```

If the node is online and reachable on the same local network, its IP address should appear in the output.

If the listener cannot find it, check the node's network connection and whether client isolation or firewall rules are blocking discovery.

### Step 6 — Pair the UDP listener

Replace the example values with your node's IP address, pairing PIN and chosen gateway name:

```sh
docker-compose run --rm sensmos-lora --pair NODE_IP --pin YOUR_PIN --name "GATEWAY_NAME"
```

Wait for the pairing command to complete successfully.

### Step 7 — Pair the MQTT listener (optional)

If your gateway has a compatible ChirpStack Gateway Bridge and MQTT broker, run:

```sh
docker-compose run --rm sensmos-lora --mqtt --pair NODE_IP --pin YOUR_PIN --name "GATEWAY_NAME"
```

This pairs MQTT separately from UDP. Skip this step if you only intend to use UDP.

### Step 8 — Start the container

Run:

```sh
docker-compose up -d
```

The UDP listener starts automatically. MQTT starts automatically if its pairing state exists.

### Step 9 — Check the logs

```sh
docker-compose logs -f
```

Press `Ctrl+C` to stop viewing the logs. This does not stop the container.

### Step 10 — Stop the container

```sh
docker-compose down
```

The persistent pairing state remains under `/data/sensmos/lora`, so stopping the container does not remove it.

---

## Troubleshooting

### The Sensmos node is not found

- Make sure the node is powered on and connected to the same local network as the gateway.
- Check whether guest Wi-Fi isolation, VLANs or firewall rules prevent communication between devices.

### The UDP listener starts but does not see frames

- Confirm that the gateway's Semtech GWMP traffic is visible on the host's loopback interface (`lo`).
- The Compose configuration uses host networking and the `NET_RAW` capability for the upstream listener's packet-capture behaviour.
- A running listener does not necessarily mean that LoRaWAN packets are being received.

### The MQTT listener cannot connect

- Confirm that the MQTT broker is running and reachable.
- The default address is `localhost:1883`, which means the gateway host because host networking is enabled.
- Confirm that ChirpStack Gateway Bridge is publishing the expected gateway uplink events.

### Pairing state is missing after a restart

Check that the persistent directory exists:

- CrankkIMG / Raspberry Pi OS: `/var/lib/sensmos`
- CrankkOS: `/data/sensmos/lora`

Do not delete the JSON state files unless you intend to pair again.

---

## Docker image

The GitHub Actions workflow is configured to publish the image to:

```text
ghcr.io/c-man-the-man/sensmos-lora:latest
```

The configured target architectures are `linux/arm64` and `linux/amd64`. The image can be pulled after the repository's publishing workflow has successfully completed.

## Attribution and disclaimer

This repository packages the existing listener scripts from the [Sensmos gateway project](https://github.com/sensmos-network/sensmos-gateway) for Docker deployment. The scripts are included without intentional code changes. Please check the upstream repository for its current licence and retain all applicable notices.

This is an unofficial, community-maintained Docker packaging project and is not endorsed by Sensmos. Review the upstream source and documentation before deployment.

---

## Donations

**Bitcoin wallet address**
```text
bc1qpcfex53u7mqx4dc25gw7j7446amw9vn6743cn5
```

**EVM / Metamask  (ETH, ETC, OCTA, POL, PEAQ, MONAD, BASE etc.)**
```text
0xbE4879888d95B02B2FCaed2FcAeBbcf36829BDC9
```

**Solana wallet address**
```text
7EHWvShXfjLJ2HhzTf4CsHgjKckivfMQMjnEoUAEqau
```

**Sui wallet address**
```text
0x421a5a462f99c2d675d035d0c741ba5765a47c1e28f95d33ad770cd34a36a6ea
```

**Thank you for your support!**

