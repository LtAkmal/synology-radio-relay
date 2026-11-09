# radio-relay — memperbaiki Internet Radio Audio Station yang bisu

*English: [README.md](README.md)*

Kalau Internet Radio di **Audio Station** (player web, dan **DS audio** di Android) terlihat sedang memutar tapi **tidak ada suaranya**, sementara lagu milikmu sendiri normal, ini perbaikannya. Station mulai berbunyi dalam sekitar 1–5 detik dan tetap live.

Diuji di DS3622xs+ dengan **DSM 7.2.2**, **Audio Station 7.2.1-6006** (dan 7.2.0-5516), Chrome/Edge/Brave, serta DS audio di Android.

---

## Masalahnya (hasil pengukuran, bukan tebakan)

Audio Station tidak pernah memutar radio langsung. Setiap station lewat program kecil di NAS bernama `proxy.cgi`. Program itu **tidak mengirim apa pun ke player sebelum menerima lebih dari 4 MiB dari station**:

| Byte yang dikirim station | Byte yang sampai ke player |
|---|---|
| 4.194.304 (tepat 4 MiB) | 0, bahkan header HTTP pun tidak |
| 4 MiB + 8 KiB | semuanya, sekaligus |

Untuk bitrate radio, artinya diam bermenit-menit sebelum suara pertama:

| Bitrate station | Diam sebelum suara pertama |
|---|---|
| 320 kbps | ~1,7 menit |
| 192 kbps | ~2,9 menit |
| 128 kbps (kebanyakan station) | ~4,4 menit |
| 32 kbps | ~17,5 menit |

Player sudah menyerah jauh sebelum itu, jadi radio terlihat "mati". Perilakunya sama di Audio Station 7.2.0 dan 7.2.1, jadi **downgrade tidak membantu**, begitu juga memasang codec (Advanced Media Extensions).

Dua masalah kecil memperparah:
- Beberapa server SHOUTcast mengirim **halaman web, bukan audio**, kalau request-nya terlihat seperti browser.
- Direktori SHOUTcast lama **masih memuat station yang sudah mati** (tertulis "(0 kbps)"), kebanyakan bekas Radionomy yang tutup Januari 2020. Station seperti itu tidak bisa diperbaiki oleh siapa pun.

## Cara kerja perbaikannya

```
 Player (browser / DS audio)
        │
        ▼
 proxy.cgi Audio Station   ← menunggu >4 MiB sebelum mengirim apa pun
        │
        ▼
 radio-relay (proyek ini, Docker, port 18570)
   1. langsung mengirim ~4 MiB bantalan yang tidak berbahaya
      (66 tag ID3 kecil, yang dilewati oleh player)
   2. lalu meneruskan siaran live
   3. mengubah station AAC/AAC+ menjadi MP3
        │
        ▼
 Station radio di internet
```

Karena `proxy.cgi` langsung mendapat 4 MiB-nya, ia langsung mulai mengirim, dan kamu mendengar siarannya secara live.

Cara Audio Station diarahkan ke relay:
- **Station dari direktori SHOUTcast**: Audio Station mencari station di `yp.shoutcast.com`. Satu baris di `/etc/hosts` mengarahkan nama itu ke NAS, lalu aturan Reverse Proxy DSM meneruskannya ke relay. Relay mengambil playlist asli dan menulis ulang supaya lewat relay.
- **Station User-defined dan Favorit**: URL-nya ditulis ulang menjadi `http://<NAS_IP>:18570/s?u=<URL asli>`. URL asli tetap tersimpan di dalam link, jadi tidak ada yang hilang.

## Kebutuhan

- NAS Synology dengan **DSM 7.2 atau lebih baru** dan **Audio Station**
- **Container Manager** terpasang (Package Center)
- Akun admin dan **SSH** aktif (Control Panel → Terminal & SNMP → Enable SSH service)
- NAS bisa mengakses internet (kalau radionya "jalan tapi bisu", berarti sudah bisa)

## Instalasi

### 1. Salin file ke NAS

Taruh folder ini di NAS, misalnya `/volume1/docker/radio-relay`. Isinya:

```
radio_relay.py   relay-nya
Dockerfile       image container (Python + ffmpeg)
compose.yml      pengaturan container
.env.example     contoh pengaturan
setup.sh         installer (aman dijalankan ulang)
uninstall.sh     menghapus semuanya
LICENSE          lisensi MIT
```

Bisa diupload lewat File Station, atau lewat SSH.

### 2. (Opsional) cek pengaturan

`setup.sh` membuat `.env` sendiri dan mendeteksi IP NAS serta DNS secara otomatis. Edit hanya kalau deteksinya salah atau kamu butuh port lain:

```bash
cp .env.example .env
vi .env
```

| Pengaturan | Arti | Default |
|---|---|---|
| `NAS_IP` | IP LAN NAS. **Tidak boleh 127.0.0.1**, karena Audio Station menolak alamat loopback. | dideteksi otomatis |
| `RELAY_PORT` | Port relay | `18570` |
| `RELAY_DNS` | Server DNS untuk mencari alamat asli `yp.shoutcast.com` (router rumah sudah cukup) | nameserver pertama NAS |
| `RELAY_MAX_ACTIVE` | Batas stream bersamaan; stream baru menutup yang terlama | `3` |
| `RELAY_MP3_BITRATE` | Bitrate saat mengubah station AAC menjadi MP3 | `192k` |

### 3. Jalankan installer

Masuk SSH ke NAS dengan user admin, lalu:

```bash
sudo sh /volume1/docker/radio-relay/setup.sh
```

Installer melakukan lima hal, dan setiap langkah ditampilkan saat berjalan:
1. membangun dan menyalakan container `radio-relay` (otomatis hidup lagi setelah reboot)
2. menambahkan `<NAS_IP> yp.shoutcast.com  # radio-relay` ke `/etc/hosts` (backup: `/etc/hosts.bak-radio-relay`)
3. membuat aturan Reverse Proxy DSM `yp.shoutcast.com:80 → 127.0.0.1:18570` (terlihat di Control Panel → Login Portal → Advanced → Reverse Proxy)
4. mengarahkan station User-defined dan Favorit yang sudah ada ke relay (backup: `/usr/syno/etc/radio/*.bak-radio-relay`)
5. membuat task boot DSM **`radio-relay hosts`** yang menjalankan `setup.sh --boot` sebagai root (lihat langkah 4)

Baris terakhir seharusnya `OK`. `setup.sh` boleh dijalankan ulang kapan saja; ia hanya memperbaiki bagian yang kurang.

### 4. Supaya baris hosts tetap ada setelah reboot / update DSM (otomatis)

DSM bisa saja menulis ulang `/etc/hosts`, misalnya setelah update. Karena itu `setup.sh` membuat task boot **`radio-relay hosts`** yang memasang lagi baris itu setiap NAS menyala. Task-nya terlihat di Control Panel → **Task Scheduler** (Owner `root`, Event `Boot-up`). Kalau folder ini dipindah, jalankan lagi `setup.sh`, dan task-nya ikut diperbarui ke lokasi baru.

Kalau installer bilang task tidak bisa dibuat, tambahkan manual:

1. Control Panel → **Task Scheduler** → Create → **Triggered Task** → **User-defined script**
2. General: nama `radio-relay hosts`, User **root**, Event **Boot-up**
3. Task Settings → Run command:
   ```
   sh /volume1/docker/radio-relay/setup.sh --boot
   ```
4. OK

### 5. Tes

Buka Audio Station → Internet Radio → SHOUTcast, pilih station yang menampilkan format seperti **MP3 (128 kbps)**, lalu putar. Seharusnya berbunyi dalam beberapa detik.

Relay-nya sendiri juga bisa dicek:

```bash
curl http://127.0.0.1:18570/health    # "ok", versi, jumlah stream aktif
curl http://127.0.0.1:18570/log       # 400 request terakhir dan hasil tiap stream
```

## Menambah station User-defined baru

Station yang ditambahkan belakangan akan langsung ke station lagi, jadi bisu lagi. Pilih salah satu:
- jalankan lagi `sudo sh setup.sh`, yang akan menulis ulang station User-defined/Favorit yang baru, atau
- masukkan URL dengan format ini saat menambah station:
  ```
  http://<NAS_IP>:18570/s?u=<URL station, di-URL-encode>
  ```
  Contoh: `http://192.168.1.10:18570/s?u=https%3A%2F%2Fstream.example.com%2Flive`

Station dari direktori SHOUTcast selalu otomatis lewat relay.

## Troubleshooting

| Gejala | Penyebab / solusi |
|---|---|
| Station tertulis **"(0 kbps)"** dan tidak pernah bunyi | Station sudah mati di sumbernya (biasanya bekas Radionomy). Pilih station lain. |
| Satu station gagal, yang lain jalan | Station sedang offline. `curl http://127.0.0.1:18570/log` akan menunjukkan misalnya `Connection refused` atau `Host is unreachable`. |
| Semua station SHOUTcast gagal, User-defined jalan | Pastikan `/etc/hosts` berisi `<NAS_IP> yp.shoutcast.com` dengan **IP LAN, bukan 127.0.0.1**, dan aturan Reverse Proxy-nya ada. Jalankan ulang `setup.sh`. |
| Tidak ada yang jalan, `/health` tidak menjawab | Container tidak jalan: `sudo docker logs radio-relay`, lalu `sudo sh setup.sh`. |
| Firewall DSM aktif dan radio berhenti jalan | Izinkan TCP port `18570` dari IP LAN NAS sendiri / jaringan LAN. |
| DS audio di Android bilang "gagal terhubung" | Pastikan `radio_relay.py` yang dipakai versi terbaru (bantalan default `id3multi`). DS audio menolak satu tag bantalan besar, tapi menerima banyak tag kecil. |
| Judul lagu tidak muncul | Ada station yang memang tidak mengirim judul. Relay meneruskan judul yang dikirim station. |
| Port 18570 sudah dipakai | Ganti `RELAY_PORT` di `.env`, lalu `sudo sh setup.sh`. |

## Update

Container membaca `radio_relay.py` langsung dari folder ini dan **restart sendiri saat file itu berubah** (hanya ketika tidak ada yang diputar; file yang error tidak dipakai). Untuk update, cukup ganti `radio_relay.py` tanpa rebuild. Setelah mengubah `compose.yml`, `Dockerfile` atau `.env`, jalankan lagi `sudo sh setup.sh`.

## Uninstall

```bash
sudo sh /volume1/docker/radio-relay/uninstall.sh
```

Ini menghapus container, baris `/etc/hosts`, aturan Reverse Proxy, dan task boot `radio-relay hosts`, lalu mengembalikan station User-defined/Favorit dari backup.

## Catatan teknis

- **Bentuk bantalan berpengaruh.** Satu tag ID3 4 MiB bisa diputar Chrome, tapi DS audio di Android menolaknya dan terus menyambung ulang. 66 tag ID3 berurutan @64 KiB bisa diputar di semua player. Varian bisa dibandingkan per station dengan menambahkan `&m=id3`, `&m=id3multi` atau `&m=id3v23` ke URL relay.
- **AAC/AAC+** diubah menjadi MP3 karena Chrome tidak mau memulai stream AAC yang diawali bantalan.
- **Probe format**: Audio Station mengecek setiap station beberapa kali sebelum memutar. Relay langsung menjawab pengecekan itu dengan MP3 hening yang pendek, supaya station cepat terbuka.
- **User-Agent**: relay memperkenalkan diri tanpa kata "Mozilla", karena banyak server SHOUTcast membalas klien mirip browser di `/` dengan halaman status HTML, bukan audio.
- **Loopback**: Audio Station menolak URL playlist yang resolve ke `127.0.0.1` (error 101). Itu sebabnya `/etc/hosts` harus memakai IP LAN.
- **Stream sisa**: Audio Station kadang tetap membaca station setelah player berhenti. Relay menutup stream terlama kalau jumlahnya melebihi `RELAY_MAX_ACTIVE`.
- Relay tidak mengubah apa pun di dalam Audio Station, dan update Audio Station tidak menghapusnya.

## Disclaimer

Tidak berafiliasi dengan atau didukung oleh Synology. Risiko pemakaian ditanggung sendiri. Installer mengubah `/etc/hosts`, menambah aturan Reverse Proxy DSM, dan mengedit file station radio Audio Station; `uninstall.sh` mengembalikan semuanya.

## Lisensi

[MIT](LICENSE): bebas dipakai, diubah, dan dibagikan, termasuk untuk komersial, selama teks lisensinya tetap disertakan. Tanpa garansi.
