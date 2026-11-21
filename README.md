# CineHaven

**Search free movies, press play, and watch while they download** – plus free live TV, documentaries, your own video collection, and where to stream anything else legally.

CineHaven is a movie streamer and player for Linux, with Windows, macOS and BSD support in preview. It's built on libtorrent (a faster, more modern engine than Transmission's) and mpv (every codec FFmpeg supports, plus hardware decoding).

## What it does

* **Search everywhere a film can be watched legally:** one search covers the Internet Archive's film library (classics, silent films, cartoons, film noir, open movies – these play right in CineHaven), official full-movie uploads on YouTube, and every streaming service in your countries (free services first, then subscription, rent and buy), and Kanopy and Hoopla for library-card holders. If a film isn't streaming where you live, CineHaven says so and links to where it can be found. Pirate sites are never searched.
* **Documentaries:** switch Discover to 🌍 *Documentaries*. Classic documentaries (*Man with a Movie Camera*, *The Battle of Midway*, *Why We Fight*, *Louisiana Story*…) plus about 27,000 US-Government and NASA films (Apollo missions…) and about 10,000 Prelinger Archive films all play right in the app. A shelf above them shows **documentaries streaming now in South Africa and the UAE**, with buttons that open Netflix, Prime Video and other services. A button jumps to the live documentary TV channels.
* **Watch now:** playback starts on its own after a few seconds of buffering. The download follows the playhead, so you can seek anywhere and those parts download first.
* **Checks that each title is authentic and legal to share:**
  * Rights check on every title. Films from 1930 or earlier, open movies and items the Archive lists as public domain or Creative Commons are shown. Modern commercial films (1978 and later) with no proof they're free to share are hidden. Titles with unclear rights are hidden unless you turn on *Menu → Show unverified titles*.
  * Every downloaded piece is SHA-1 verified, so the player never gets corrupt or altered data.
  * When a download finishes, the whole file is checked against the original checksum the Archive publishes. You'll see *✔ Checksum matches original* in the Library.
  * Fake-torrent scanner for magnet links and .torrent files you add yourself. It blocks programs (`.exe`, `.scr`, …), disguised names such as `movie.mp4.exe`, the "password-protected archive" scam and "codec required" bait files.
* **Player like VLC:** the controls are play/pause, seek (it shows how much has downloaded), volume, subtitles and audio tracks, fullscreen and speed.
* **English first:** if a film has an English soundtrack, CineHaven uses it. If the audio is in another language, it turns on English subtitles. If there are only foreign-language subtitles, it **translates them to English live** while you watch. You can also pick *English – translated from …* in the **CC** menu. *Menu → English movies only* hides titles the Archive lists in other languages.
* **My Videos – everything on your drives:** finds every video in your home folder and on all connected disks, USB sticks and SD cards. It turns messy file names into clean titles and years, and sorts them into Movies, TV episodes and Home & other. Each video gets a preview picture. Damaged or unfinished downloads are flagged. **New videos appear within seconds**, wherever they're saved or copied to (any folder, any depth, any drive), and plugging in a drive scans it automatically. You can add extra folders. Videos **resume where you stopped**; right-click a video for *Play from the start* or *Show in folder*.
* **Live TV:** free TV channels by name, in sections: South Africa (SABC News, SABC Lehae, Cape Town TV, Soweto TV, WildEarth…), UAE (MBC 1/4/5, MBC Drama, Dubai TV, Sharjah TV, Sky News Arabia…), plus English Series, Movies, Entertainment, Kids, Documentary and News channels. Each channel is checked first, so only ones on air right now are shown. Pay-TV channels are excluded.
* **YouTube:** free full-length movies posted by official studio and distributor channels (FilmRise, Popcornflix, Paramount Movies, Movie Central, Maverick Movies), with a Heartfelt section for family and love dramas. Save your own finds by pasting a YouTube link; films from channels that aren't known rights holders are marked Unverified. Films play on youtube.com in your browser.
* **My List – where to watch legally:** search any movie or TV series (including new releases) and add it to your list. For each film it shows where it can be watched in **South Africa** and the **UAE**: free, included in a subscription (Netflix, Apple TV, Prime Video…) or rent/buy with prices. Click a service to open it. A **📚 Library card** line shows films that are free on **Kanopy** or **Hoopla**, with flags for the countries where they're offered. You need a card from a participating public library (mainly in the US and Canada, plus some in the UK, Australia and New Zealand). Availability data comes from JustWatch and is re-checked every 12 hours.
* **Library – save and play many movies:** up to 6 movies download at the same time, and you can watch any of them while the rest keep downloading. Pause, resume or remove downloads, or open the folder. Finished movies play straight from disk. Files are saved in `~/Videos/CineHaven`.
* **✨ AI subtitles for any video:** in the **CC** menu, *AI subtitles* has speech recognition (Whisper) running on your computer write subtitles for videos that have none, in the spoken language or translated to English. The speech engine downloads once (about 1 GB), and nothing is uploaded.
* **Picture and sound** (**CC** menu): *Enhance picture* upscales and sharpens older or low-resolution films and reduces noise. It eases off by itself if the computer can't keep up. *Dialogue boost* makes voices clearer, and *Night mode* evens out sudden loud bangs.
* **✨ Suggest films:** describe what you feel like watching ("a quiet, heartfelt film about a father and son") and press *Suggest films*. AI running on your own computer ([Ollama](https://ollama.com) with `nomic-embed-text` and `qwen2.5:3b`) picks real, well-rated films whose stories match and shows where to watch them. Your description never leaves the computer.
* **Skip intro and credits:** TV episodes on your computer skip their intro and recap automatically (*Watch it* undoes a skip). CineHaven finds them from chapter names or by matching the theme tune's audio across episodes. When the credits roll, the next episode starts after a 10-second countdown. Both can be turned off in the **CC** menu.
* **Cast to a TV:** the TV button in the player sends what you're watching to an **Apple TV, a Mac or an AirPlay 2 TV** (2018+ Samsung, LG, Sony, Vizio), a **Chromecast / Google TV**, or a **DLNA smart TV** (Samsung Smart View, LG, Sony, Philips…). The controls stay on your computer and your phone. CineHaven has its own AirPlay sender: it pairs the way Apple devices do, and if the TV shows a code you type it in once. Videos the TV can't play directly (MKV, AVI, unusual sound formats…) are repackaged on the fly, starting from where you are. AirPlay needs the `cryptography` and `zeroconf` Python packages; Chromecast needs `pychromecast`. If an Apple TV or Mac only allows its owner's devices, CineHaven shows which setting to change.
* **Phone remote:** *Menu → Phone remote* (or the phone button in the player) shows an address to open on your phone for big play/pause, seek, volume, skip intro and next episode buttons. The address changes every time CineHaven starts.
* **Keeps playing when the internet drops:** a large read-ahead buffer covers short dropouts. During longer ones CineHaven shows *Connection lost – reconnecting…*, waits for the connection to come back and carries on from the same spot.
* **Open anything:** use **+** to paste a magnet link, open a `.torrent` or play a local video. You can also drag and drop files onto the window.

## Fixes itself

* **Playback doctor:** if a video errors or freezes, CineHaven works through a series of fixes on its own:
  1. switch to software video decoding
  2. stream straight from the Internet Archive
  3. look for more peers
  4. use a live channel's backup stream or reconnect
  5. work around or skip a damaged part of a file
  6. **any format, any codec:** if a video can't be decoded, CineHaven converts it on the fly and starts playing within seconds while the rest converts. If this computer's FFmpeg lacks the codec, it downloads the full FFmpeg build once (checksum-verified). Damaged files are rebuilt. Only copy-protected (DRM) files can't be played; CineHaven tells you which ones.

  If nothing works, it offers *Try again* or *Open in VLC*.
* **Download doctor:** restarts downloads that hit errors and wakes up stalled ones. An Archive download stalled for 15 minutes is finished straight from archive.org and checked against the original's checksum. A full disk pauses downloads and tells you.
* **Startup self-check:** a damaged settings or library file is restored from its automatic backup. It also checks that folders can be written to and warns when space is low.
* **Crash guard:** if CineHaven ever crashes it restarts itself. After a second crash it restarts in safe mode, with hardware decoding off.
* **Help & diagnostics** (menu) shows the repair log and lets you copy a report.

## YouTube and Recently played

* **Any YouTube video plays inside CineHaven.** Paste a link into the YouTube tab or Discover search, press Ctrl+V anywhere, drag a link in from the browser, or use Open › Play a YouTube link…. A `?t=` start time is honoured, English captions are added when available, and the format your graphics chip decodes is preferred.
* **Easy YouTube search:** one box with suggestions as you type. One click plays a result.
* **Keeps working when YouTube changes:** CineHaven keeps its own copy of yt-dlp up to date. It checks weekly and whenever a video fails, and uses a download only if its SHA-256 matches yt-dlp's published checksum. If a stream address expires mid-film, it fetches a fresh one and carries on from the same spot. *Open on YouTube* is always there too.
* **Recently played:** the top of Discover shows the last 10 films and videos you played and where you left off in each. Click one to carry on from there.

## Your security cameras

The **Cameras** tab shows your own IP cameras, DVRs and NVRs in a live grid. Click a tile to watch it full screen with as little delay as possible.

* **Any format:** RTSP/RTSPS (nearly every IP camera and recorder), HTTP MJPEG (older cameras and phone camera apps), HLS, RTMP/HTTP-FLV and SRT.
* **Search my network:** finds cameras and recorders that speak ONVIF, and anything with a camera port open.
* **Pick the make:** Hikvision, Annke, HiLook, Dahua, Amcrest, Lorex, Reolink, Uniview, Swann, TP-Link Tapo and VIGI, Axis, Foscam, Hanwha/Wisenet, Vivotek, Eufy, Wyze and phone camera apps. Enter the address, user name and password. A DVR or NVR adds one tile per channel.
* **"I don't know – find it for me":** asks the camera over ONVIF for its stream address, or tries each make's address format until one plays.
* **Or paste any stream address,** for example a UniFi Protect RTSPS link.
* The grid uses each camera's smaller stream and the full-screen view uses the main stream. The doctor falls back to the smaller stream if the main one fails, and snapshots are saved to Pictures › CineHaven Cameras.
* Logins stay on this computer in a file only you can read, and are sent only to the cameras themselves.

## Tunes itself to your computer

* **Knows what your hardware can do:** a few seconds after starting, CineHaven tests which video formats (H.264, HEVC, HEVC 10-bit/HDR, VP9, AV1) your graphics chip really decodes. It does this with tiny clips in a separate process, so a faulty driver can't crash it. If a hardware-decoding driver is missing it tells you, and on Linux you can install it with one click. It tests again by itself when the graphics chip, its driver or the player changes.
* **Learns the best picture for each kind of video:** with *Enhance picture* on, every film starts at the highest quality this computer has kept smooth for that kind of video (format × resolution × hardware/software decoding). Quality steps down at once if frames drop, and that is remembered. Levels that stuttered are tried again after a month, or straight away after a driver, hardware or player upgrade.
* **Keeps up with video technology:** once a week it checks for new mpv and FFmpeg releases (the engines that decode and draw the picture) and shows what's newer than what you have. Menu → **Video technology** shows all of this.
* For the project, a weekly GitHub job opens an issue whenever mpv, FFmpeg, libplacebo or dav1d release a new version, so new capabilities get into the next CineHaven release.

## Install

**Ubuntu 24.04+ / Debian 13+ / Linux Mint 22+** (recommended – keeps itself up to date):

```bash
wget https://github.com/pi5ck/cinehaven/releases/latest/download/cinehaven-1.0.9.tar.gz
tar xzf cinehaven-1.0.9.tar.gz && cd cinehaven-1.0.9
./install.sh
```

The installer asks for your password once to add the system libraries (GTK, libtorrent, mpv). Then it installs CineHaven for your user and adds it to your app menu. It also works on **Fedora, Arch and openSUSE**.

**Or use the .deb package** (Ubuntu/Debian):

```bash
wget https://github.com/pi5ck/cinehaven/releases/latest/download/cinehaven_1.0.9_all.deb
sudo apt install ./cinehaven_1.0.9_all.deb
```

The .deb version tells you when an update is out, but you install the update yourself. The install.sh version updates itself.

Every download's checksum is listed in `SHA256SUMS` on the [releases page](https://github.com/pi5ck/cinehaven/releases).

### Other platforms (preview – not yet tested)

CineHaven is written in Python with GTK 4, libtorrent and mpv, which all run on these systems too. Native installers aren't ready yet; for now, install the libraries, download the source tarball, and run `python3 cinehaven-launcher` from its folder.

* **Other Linux distributions:** install GTK 4, libadwaita, PyGObject, the libtorrent Python bindings and libmpv from your package manager, then `pip install --user python-mpv`.
* **macOS** (with [Homebrew](https://brew.sh)): `brew install gtk4 libadwaita pygobject3 mpv libtorrent-rasterbar`, then `pip3 install python-mpv`.
* **Windows** (with [MSYS2](https://www.msys2.org), UCRT64 shell): `pacman -S mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-libadwaita mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-mpv mingw-w64-ucrt-x86_64-libtorrent-rasterbar`, then `pip install python-mpv`.
* **FreeBSD and other Unix:** install `gtk4`, `libadwaita`, PyGObject, `mpv` and the libtorrent Python bindings from ports or packages, then `pip install python-mpv`.

Data is kept in the usual place for each system: `~/.local/share/cinehaven` (Linux/BSD), `~/Library/Application Support/cinehaven` (macOS), `%LOCALAPPDATA%\cinehaven` (Windows). Please report what works and what doesn't in an [issue](https://github.com/pi5ck/cinehaven/issues).

### Requirements
GTK 4, libadwaita ≥ 1.5, python3-libtorrent ≥ 2.0, python3-mpv and libmpv. `ffmpeg` is optional; it makes preview pictures in *My Videos*.

### Problems?
Open *Menu → Help & diagnostics → Copy report* and paste the report into a [new issue](https://github.com/pi5ck/cinehaven/issues).

## Keyboard shortcuts (in the player)

| Key | Action |
|---|---|
| Space / K | Play / pause |
| F / F11 / double-click | Fullscreen |
| ← / → | Back 10 s / forward 30 s |
| ↑ / ↓ / mouse wheel | Volume |
| M | Mute |
| S / A | Cycle subtitles / audio track |
| [ / ] / Backspace | Slower / faster / normal speed |
| Esc | Leave fullscreen / back to browsing |

Ctrl+L pastes a magnet link, Ctrl+O opens a video, Ctrl+F jumps to search and Ctrl+Q quits.

## A note on content

CineHaven's catalogue only offers films that are verifiably free to share. When you add your own magnet links or torrents, make sure you have the right to download that content.
