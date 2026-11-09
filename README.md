# radio-relay — fix silent Internet Radio in Synology Audio Station

*Bahasa Indonesia: [README.id.md](README.id.md)*

If Internet Radio in **Audio Station** (web player, and **DS audio** on Android) shows a station as "playing" but you hear **nothing**, while your own music plays fine — this fixes it. Stations start in about 1–5 seconds and stay live.

Tested on a DS3622xs+ with **DSM 7.2.2**, **Audio Station 7.2.1-6006** (and 7.2.0-5516), Chrome/Edge/Brave and DS audio on Android.

---

## The problem (measured, not guessed)

Audio Station never plays radio directly. Every station goes through a small program on the NAS, `proxy.cgi`. That program **sends nothing to the player until it has received more than 4 MiB from the station**:

| Bytes the station sent | Bytes the player got |
|---|---|
| 4,194,304 (exactly 4 MiB) | 0 — nothing, not even HTTP headers |
| 4 MiB + 8 KiB | everything, at once |

At radio bitrates that means minutes of silence before the first sound:

| Station bitrate | Silence before first sound |
|---|---|
| 320 kbps | ~1.7 min |
| 192 kbps | ~2.9 min |
| 128 kbps (most stations) | ~4.4 min |
| 32 kbps | ~17.5 min |

Players give up long before that, so radio looks "dead". It is the same on Audio Station 7.2.0 and 7.2.1, so **downgrading does not help**, and neither does installing codecs (Advanced Media Extensions).

Two smaller issues make it worse:
- Some SHOUTcast servers send their **web page instead of audio** when the request looks like a browser.
- The old **SHOUTcast directory still lists dead stations** (shown as "(0 kbps)"), mostly former Radionomy stations that shut down in January 2020. Those cannot be fixed by anyone.

## How the fix works

```
 Player (browser / DS audio)
        │
        ▼
 Audio Station proxy.cgi   ← waits for >4 MiB before sending anything
        │
        ▼
 radio-relay (this project, Docker, port 18570)
   1. sends ~4 MiB of harmless padding immediately
      (66 small ID3 tags — players skip them)
   2. then streams the station live
   3. converts AAC/AAC+ stations to MP3
        │
        ▼
 The radio station on the internet
```

Because `proxy.cgi` gets its 4 MiB straight away, it starts sending right away, and you hear the station live.

How Audio Station is pointed at the relay:
- **SHOUTcast directory stations**: Audio Station looks up stations at `yp.shoutcast.com`. A line in `/etc/hosts` sends that name to your NAS, and a DSM Reverse Proxy rule passes it to the relay. The relay fetches the real playlist and rewrites it to go through itself.
- **User-defined and Favourite stations**: their URLs are rewritten to `http://<NAS_IP>:18570/s?u=<original URL>`. The original URL stays inside the link, so nothing is lost.

## Requirements

- Synology NAS with **DSM 7.2 or newer** and **Audio Station**
- **Container Manager** installed (Package Center)
- An admin account and **SSH** enabled (Control Panel → Terminal & SNMP → Enable SSH service)
- The NAS can reach the internet (it already can if radio "plays" silently)

## Installation

### 1. Copy the files to the NAS

Put this folder somewhere on the NAS, for example `/volume1/docker/radio-relay`. It must contain:

```
radio_relay.py   the relay
Dockerfile       container image (Python + ffmpeg)
compose.yml      container settings
.env.example     settings template
setup.sh         installer (safe to run again)
uninstall.sh     removes everything
LICENSE          MIT licence
```

You can upload them with File Station, or over SSH.

### 2. (Optional) check the settings

`setup.sh` creates `.env` for you and detects your NAS IP and DNS automatically. Only edit it if detection is wrong or you need a different port:

```bash
cp .env.example .env
vi .env
```

| Setting | Meaning | Default |
|---|---|---|
| `NAS_IP` | LAN IP of the NAS. **Must not be 127.0.0.1** — Audio Station refuses loopback addresses. | auto-detected |
| `RELAY_PORT` | Port of the relay | `18570` |
| `RELAY_DNS` | DNS server used to find the real `yp.shoutcast.com` (your router is fine) | first nameserver of the NAS |
| `RELAY_MAX_ACTIVE` | Max simultaneous streams; a new one closes the oldest | `3` |
| `RELAY_MP3_BITRATE` | Bitrate when converting AAC stations to MP3 | `192k` |

### 3. Run the installer

SSH into the NAS as your admin user, then:

```bash
sudo sh /volume1/docker/radio-relay/setup.sh
```

It does four things, each one printed as it runs:
1. builds and starts the `radio-relay` container (it restarts by itself after a reboot)
2. adds `<NAS_IP> yp.shoutcast.com  # radio-relay` to `/etc/hosts` (backup: `/etc/hosts.bak-radio-relay`)
3. creates a DSM Reverse Proxy rule `yp.shoutcast.com:80 → 127.0.0.1:18570` (visible in Control Panel → Login Portal → Advanced → Reverse Proxy)
4. points your existing User-defined and Favourite stations at the relay (backup: `/usr/syno/etc/radio/*.bak-radio-relay`)

The last line should read `OK`. You can run `setup.sh` again at any time; it only fixes what is missing.

### 4. Keep the hosts line after reboots / DSM updates (recommended)

DSM may rewrite `/etc/hosts`. Create a boot task so the line is always re-added:

1. Control Panel → **Task Scheduler** → Create → **Triggered Task** → **User-defined script**
2. General: name `radio-relay hosts`, User **root**, Event **Boot-up**
3. Task Settings → Run command:
   ```
   sh /volume1/docker/radio-relay/setup.sh --boot
   ```
4. OK

### 5. Test

Open Audio Station → Internet Radio → SHOUTcast, pick a station that shows a format such as **MP3 (128 kbps)**, and press play. It should start within a few seconds.

You can also check the relay itself:

```bash
curl http://127.0.0.1:18570/health    # "ok", version, active streams
curl http://127.0.0.1:18570/log       # last 400 requests and stream results
```

## Adding new User-defined stations

Stations you add later go straight to the station again and will be silent. Either:
- run `sudo sh setup.sh` again — it rewrites new User-defined/Favourite stations, or
- enter the URL in this form when adding the station:
  ```
  http://<NAS_IP>:18570/s?u=<station URL, URL-encoded>
  ```
  Example: `http://192.168.1.10:18570/s?u=https%3A%2F%2Fstream.example.com%2Flive`

SHOUTcast directory stations always go through the relay automatically.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Station shows **"(0 kbps)"** and never plays | The station is dead at its source (usually old Radionomy). Pick another one. |
| A single station fails, others work | Station offline. `curl http://127.0.0.1:18570/log` shows e.g. `Connection refused` or `Host is unreachable`. |
| All SHOUTcast stations fail, User-defined work | Check `/etc/hosts` contains `<NAS_IP> yp.shoutcast.com` with the **LAN IP, not 127.0.0.1**, and that the Reverse Proxy rule exists. Run `setup.sh` again. |
| Nothing works, `/health` does not answer | Container not running: `sudo docker logs radio-relay`, then `sudo sh setup.sh`. |
| DSM Firewall enabled and it stopped working | Allow TCP port `18570` from the NAS's own LAN IP / your LAN. |
| Android DS audio says "failed to connect" | Make sure you run the current `radio_relay.py` (default padding `id3multi`). One big padding tag is rejected by DS audio; many small ones work. |
| Song titles missing | Some stations send no titles. The relay forwards the titles the station provides. |
| Port 18570 already used | Change `RELAY_PORT` in `.env`, then `sudo sh setup.sh`. |

## Updating

The container reads `radio_relay.py` directly from this folder and **restarts itself when the file changes** (only while nothing is playing; a file that does not compile is ignored). To update, replace `radio_relay.py` — no rebuild needed. After changing `compose.yml`, `Dockerfile` or `.env`, run `sudo sh setup.sh` again.

## Uninstall

```bash
sudo sh /volume1/docker/radio-relay/uninstall.sh
```

This removes the container, the `/etc/hosts` line and the Reverse Proxy rule, and restores your User-defined/Favourite stations from the backup. Delete the boot task (step 4) by hand if you created it.

## Technical notes

- **Padding format matters.** One single 4 MiB ID3 tag plays in Chrome, but DS audio on Android rejects it and keeps reconnecting. 66 consecutive 64 KiB ID3 tags play everywhere. You can compare variants per station by adding `&m=id3`, `&m=id3multi` or `&m=id3v23` to a relay URL.
- **AAC/AAC+** is converted to MP3 because Chrome will not start an AAC stream that begins with padding.
- **Format probes**: Audio Station checks each station several times before playing. The relay answers those checks instantly with a short silent MP3, so stations open fast.
- **User-Agent**: the relay identifies itself without "Mozilla", because many SHOUTcast servers answer browser-like clients on `/` with an HTML status page instead of audio.
- **Loopback**: Audio Station refuses playlist URLs that resolve to `127.0.0.1` (error 101), which is why `/etc/hosts` must use the LAN IP.
- **Leftover streams**: Audio Station sometimes keeps reading a station after the player stopped. The relay closes the oldest stream when more than `RELAY_MAX_ACTIVE` are open.
- The relay changes nothing inside Audio Station itself, and Audio Station updates do not remove it.

## Disclaimer

Not affiliated with or endorsed by Synology. Use at your own risk. The installer changes `/etc/hosts`, adds a DSM Reverse Proxy rule and edits Audio Station's radio station files; `uninstall.sh` reverts all of it.

## License

[MIT](LICENSE): free to use, change and share, including commercially, as long as the licence text stays with the code. No warranty.
