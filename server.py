import asyncio
import csv
from datetime import datetime, timezone
import os
import signal
import time
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.websockets import WebSocket, WebSocketDisconnect
import numpy as np

app = FastAPI()
CURRENT_DIR = Path(__file__).parent
OBS_DIR = CURRENT_DIR / "observations"
OBS_DIR.mkdir(exist_ok=True)

OFFSET_FREQ = 250000  # Décalage matériel en Hz (250 kHz)
FFT_SIZE = 512
CROP_BINS = 56

CONFIG = {
    "center_freq": 1420405750,
    "sample_rate": 2400000,
    "gain": "30"
}

sdr_proc = None
sdr_task = None
spectrum_queue = asyncio.Queue(maxsize=15)
audio_queue = asyncio.Queue(maxsize=20)
is_audio_client_active = False
current_mode = "am"
sdr_lock = asyncio.Lock()

ui_settings = {
    "integration_enabled": False,
    "duration_sec": 360,
    "remove_dc": True,
    "record_enabled": True
}

# Stockage de la référence (en dB) et du dernier spectre brut rogné
reference_spectrum: np.ndarray | None = None
latest_raw_spectrum: np.ndarray | None = None
current_csv_path: Path | None = None
saved_spectra_count = 0

ROTATION_VECTOR = None

def get_rotation_vector(n_samples: int, f_shift: float, fs: float):
    global ROTATION_VECTOR
    if ROTATION_VECTOR is None or len(ROTATION_VECTOR) != n_samples:
        t = np.arange(n_samples)
        ROTATION_VECTOR = np.exp(-2j * np.pi * f_shift * t / fs).astype(np.complex64)
    return ROTATION_VECTOR

def get_frequency_axis_mhz() -> list[float]:
    freqs = np.fft.fftshift(np.fft.fftfreq(FFT_SIZE, d=1.0 / CONFIG["sample_rate"])) + CONFIG["center_freq"]
    cropped_freqs = freqs[CROP_BINS : -CROP_BINS]
    return [round(float(f) / 1e6, 6) for f in cropped_freqs]

def init_new_csv_session() -> Path:
    """Crée un fichier CSV horodaté UTC pour les données brutes."""
    global current_csv_path, saved_spectra_count
    now_utc = datetime.now(timezone.utc)
    filename = f"HI_raw_{now_utc.strftime('%Y%m%d_%H%M%S')}_UTC.csv"
    filepath = OBS_DIR / filename

    freqs_mhz = get_frequency_axis_mhz()

    with open(filepath, mode="w", newline="", encoding="utf-8") as f:
        f.write(f"# Observation Raie HI 1420 MHz (DONNEES BRUTES NON LISSEES) - Debut UTC: {now_utc.isoformat()}\n")
        f.write(f"# Frequence Centrale (Hz): {CONFIG['center_freq']}\n")
        f.write(f"# Sample Rate (Hz): {CONFIG['sample_rate']} | FFT Size: {FFT_SIZE} | Bins utiles: {len(freqs_mhz)}\n")
        f.write(f"# Gain Manuel (dB): {CONFIG['gain']} | Integration (s): {ui_settings['duration_sec']}\n")

        writer = csv.writer(f)
        header = ["timestamp_utc", "unix_time", "integration_index"] + [f"{freq:.6f}_MHz" for freq in freqs_mhz]
        writer.writerow(header)

    current_csv_path = filepath
    saved_spectra_count = 0
    return filepath

def append_spectrum_to_csv(int_index: int, raw_data: list[float]):
    """Enregistre la ligne brute horodatée sur disque."""
    global current_csv_path, saved_spectra_count
    if not ui_settings["record_enabled"]:
        return

    if current_csv_path is None or not current_csv_path.exists():
        init_new_csv_session()

    now_utc = datetime.now(timezone.utc)
    row = [
        now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        f"{now_utc.timestamp():.2f}",
        int_index
    ] + [f"{val:.4f}" for val in raw_data]

    with open(current_csv_path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)

    saved_spectra_count += 1

def remove_dc_spike(data: np.ndarray, offset_hz: float, fs: float, fft_size: int, crop: int) -> np.ndarray:
    """Calcule dynamiquement la position du pic DC matériel et l'interpole."""
    if offset_hz == 0:
        return data
    bin_hz = fs / fft_size
    center_bin = fft_size // 2
    dc_bin_full = int(round(center_bin - (offset_hz / bin_hz)))
    dc_idx = dc_bin_full - crop

    w = max(2, int(round(15000.0 / bin_hz)))
    if w < dc_idx < len(data) - w - 1:
        data[dc_idx - w : dc_idx + w + 1] = np.linspace(
            data[dc_idx - w - 1],
            data[dc_idx + w + 1],
            2 * w + 1
        )
    return data

def prepare_raw_spectrum(power_db: np.ndarray) -> list[float]:
    """Rogne les bords et retire optionnellement le pic DC matériel sans altérer la dynamique."""
    global latest_raw_spectrum
    data = power_db[CROP_BINS : -CROP_BINS].copy()

    if ui_settings["remove_dc"]:
        data = remove_dc_spike(data, OFFSET_FREQ, CONFIG["sample_rate"], FFT_SIZE, CROP_BINS)

    latest_raw_spectrum = data.copy()
    return data.tolist()

async def sdr_stream_worker():
    global sdr_proc
    chunk_size = 131072

    try:
        while True:
            if not sdr_proc or sdr_proc.stdout is None or sdr_proc.stdout.at_eof():
                await asyncio.sleep(0.05)
                continue

            try:
                raw_bytes = await sdr_proc.stdout.read(chunk_size)
            except Exception:
                await asyncio.sleep(0.05)
                continue

            if not raw_bytes:
                await asyncio.sleep(0.01)
                continue

            raw_data = np.frombuffer(raw_bytes, dtype=np.uint8)
            if len(raw_data) < 2:
                continue

            iq = (raw_data[0::2].astype(np.float32) - 127.5) + 1j * (raw_data[1::2].astype(np.float32) - 127.5)
            rot = get_rotation_vector(len(iq), OFFSET_FREQ, CONFIG["sample_rate"])
            iq_shifted = iq * rot

            if not spectrum_queue.full():
                await spectrum_queue.put(iq_shifted)

            if is_audio_client_active and not audio_queue.full():
                await audio_queue.put(iq_shifted)

    except asyncio.CancelledError:
        pass

async def restart_sdr(target_freq: int, target_gain: str = None):
    global sdr_proc, reference_spectrum, current_csv_path
    async with sdr_lock:
        CONFIG["center_freq"] = target_freq
        if target_gain:
            CONFIG["gain"] = target_gain
            reference_spectrum = None

        current_csv_path = None
        hardware_freq = target_freq - OFFSET_FREQ

        if sdr_proc:
            try:
                os.killpg(os.getpgid(sdr_proc.pid), signal.SIGTERM)
                await sdr_proc.wait()
            except Exception:
                pass
            sdr_proc = None
            await asyncio.sleep(0.1)

        cmd = [
            "rtl_sdr",
            "-f", str(hardware_freq),
            "-s", str(CONFIG["sample_rate"]),
            "-g", str(CONFIG["gain"]),
            "-"
        ]
        sdr_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=os.setsid
        )

@app.on_event("startup")
async def startup_event():
    global sdr_task
    await restart_sdr(CONFIG["center_freq"], CONFIG["gain"])
    sdr_task = asyncio.create_task(sdr_stream_worker())

@app.on_event("shutdown")
async def shutdown_event():
    global sdr_proc, sdr_task
    if sdr_task:
        sdr_task.cancel()
    if sdr_proc:
        try:
            os.killpg(os.getpgid(sdr_proc.pid), signal.SIGTERM)
        except Exception:
            pass

@app.get('/favicon.ico', include_in_schema=False)
async def favicon():
    return Response(content=b"", media_type="image/x-icon")

@app.get("/download/latest")
async def download_latest_csv():
    if current_csv_path and current_csv_path.exists():
        return FileResponse(path=current_csv_path, filename=current_csv_path.name, media_type="text/csv")
    files = sorted(OBS_DIR.glob("HI_raw_*.csv"))
    if files:
        latest = files[-1]
        return FileResponse(path=latest, filename=latest.name, media_type="text/csv")
    return Response(content="Aucune observation enregistrée.", status_code=404)

@app.websocket("/ws/spectrum")
async def websocket_spectrum(websocket: WebSocket):
    global reference_spectrum, current_csv_path
    await websocket.accept()
    window = np.hanning(FFT_SIZE)

    power_acc = np.zeros(FFT_SIZE, dtype=np.float64)
    acc_count = 0
    session_int_idx = 0
    start_time = time.time()
    last_progress_send = time.time()

    async def send_record_status():
        await websocket.send_json({
            "type": "record_status",
            "filename": current_csv_path.name if current_csv_path else "En attente...",
            "count": saved_spectra_count
        })

    async def receive_client_commands():
        nonlocal power_acc, acc_count, session_int_idx, start_time
        global reference_spectrum
        try:
            while True:
                msg = await websocket.receive_json()

                if "integration" in msg:
                    was_enabled = ui_settings["integration_enabled"]
                    new_enabled = bool(msg["integration"])
                    new_dur = int(msg.get("duration", 360))

                    if new_enabled != was_enabled or new_dur != ui_settings["duration_sec"]:
                        ui_settings["integration_enabled"] = new_enabled
                        ui_settings["duration_sec"] = new_dur
                        power_acc.fill(0)
                        acc_count = 0
                        session_int_idx = 0
                        start_time = time.time()
                        if new_enabled and ui_settings["record_enabled"]:
                            init_new_csv_session()
                            await send_record_status()

                if "record" in msg:
                    new_rec = bool(msg["record"])
                    if new_rec and not ui_settings["record_enabled"]:
                        ui_settings["record_enabled"] = True
                        if ui_settings["integration_enabled"]:
                            init_new_csv_session()
                    else:
                        ui_settings["record_enabled"] = new_rec
                    await send_record_status()

                if "remove_dc" in msg:
                    ui_settings["remove_dc"] = bool(msg["remove_dc"])

                if msg.get("action") == "capture_ref":
                    if latest_raw_spectrum is not None:
                        reference_spectrum = latest_raw_spectrum.copy()
                        await websocket.send_json({
                            "type": "ref_data",
                            "ref": reference_spectrum.tolist()
                        })

                if msg.get("action") == "clear_ref":
                    reference_spectrum = None
                    await websocket.send_json({
                        "type": "ref_data",
                        "ref": None
                    })

                if "gain" in msg:
                    new_gain = str(msg["gain"])
                    if new_gain != CONFIG["gain"]:
                        await restart_sdr(CONFIG["center_freq"], new_gain)
                        power_acc.fill(0)
                        acc_count = 0
                        session_int_idx = 0
                        start_time = time.time()
                        if ui_settings["integration_enabled"] and ui_settings["record_enabled"]:
                            init_new_csv_session()
                        await send_record_status()
                        await websocket.send_json({
                            "type": "ref_data",
                            "ref": None
                        })
        except Exception:
            pass

    rx_task = asyncio.create_task(receive_client_commands())

    # Si une référence existe déjà lors d'une reconnexion navigateur, la renvoyer
    if reference_spectrum is not None:
        await websocket.send_json({"type": "ref_data", "ref": reference_spectrum.tolist()})

    try:
        while True:
            iq = await spectrum_queue.get()
            if len(iq) < FFT_SIZE:
                continue

            segment = iq[:FFT_SIZE] * window
            fft_vals = np.fft.fftshift(np.fft.fft(segment, n=FFT_SIZE))
            instant_power = np.abs(fft_vals)**2

            if not ui_settings["integration_enabled"]:
                power_db = 10 * np.log10(instant_power + 1e-6)
                raw_data = prepare_raw_spectrum(power_db)
                await websocket.send_json({
                    "type": "waterfall",
                    "data": raw_data
                })
                await asyncio.sleep(0.04)

            else:
                power_acc += instant_power
                acc_count += 1
                elapsed = time.time() - start_time
                target = ui_settings["duration_sec"]

                if time.time() - last_progress_send >= 0.5:
                    progress_pct = min(100.0, (elapsed / target) * 100.0)
                    cur_mean = power_acc / max(1, acc_count)
                    cur_db = 10 * np.log10(cur_mean + 1e-6)
                    raw_data = prepare_raw_spectrum(cur_db)

                    await websocket.send_json({
                        "type": "progress",
                        "percent": round(progress_pct, 1),
                        "elapsed": int(elapsed),
                        "total": target,
                        "current_data": raw_data
                    })
                    last_progress_send = time.time()

                if elapsed >= target:
                    if acc_count > 0:
                        session_int_idx += 1
                        mean_power = power_acc / acc_count
                        power_db = 10 * np.log10(mean_power + 1e-6)
                        raw_data = prepare_raw_spectrum(power_db)

                        if ui_settings["record_enabled"]:
                            append_spectrum_to_csv(session_int_idx, raw_data)
                            await send_record_status()

                        await websocket.send_json({
                            "type": "waterfall",
                            "data": raw_data
                        })
                    power_acc.fill(0)
                    acc_count = 0
                    start_time = time.time()

                await asyncio.sleep(0.01)

    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        rx_task.cancel()

@app.get("/audio")
async def stream_audio(freq: int = 1420405750, mode: str = "am"):
    global is_audio_client_active, current_mode
    current_mode = mode.lower()

    if freq != CONFIG["center_freq"]:
        await restart_sdr(freq)

    is_audio_client_active = True

    async def generate_audio():
        global is_audio_client_active
        prev_sample = 0.0 + 0.0j
        deemph_state = 0.0
        alpha = np.exp(-1.0 / (24000.0 * 50e-6))

        try:
            while True:
                iq = await audio_queue.get()
                if len(iq) == 0:
                    continue

                if current_mode == "wfm":
                    trim = len(iq) - (len(iq) % 10)
                    iq_240k = iq[:trim].reshape(-1, 10).mean(axis=1)
                    if len(iq_240k) < 2:
                        continue

                    delayed = np.empty_like(iq_240k)
                    delayed[0] = prev_sample
                    delayed[1:] = iq_240k[:-1]
                    prev_sample = iq_240k[-1]

                    dphase = np.angle(iq_240k * np.conj(delayed))
                    trim_audio = len(dphase) - (len(dphase) % 10)
                    audio_24k = dphase[:trim_audio].reshape(-1, 10).mean(axis=1)

                    audio_filt = np.empty_like(audio_24k)
                    s = deemph_state
                    for i in range(len(audio_24k)):
                        s = (1.0 - alpha) * audio_24k[i] + alpha * s
                        audio_filt[i] = s
                    deemph_state = s

                    audio_out = audio_filt
                    gain_factor = 28000.0

                elif current_mode == "usb":
                    decim = int(CONFIG["sample_rate"] / 24000)
                    iq_sub = iq[::decim]
                    audio_out = np.real(iq_sub) + np.imag(iq_sub)
                    gain_factor = 250.0

                else:
                    decim = int(CONFIG["sample_rate"] / 24000)
                    iq_sub = iq[::decim]
                    audio_out = np.abs(iq_sub)
                    gain_factor = 120.0

                audio_ac = audio_out - np.mean(audio_out)
                pcm_data = (audio_ac * gain_factor).clip(-32768, 32767).astype(np.int16)
                yield pcm_data.tobytes()

        except asyncio.CancelledError:
            pass
        finally:
            is_audio_client_active = False

    return StreamingResponse(generate_audio(), media_type="audio/x-raw")

@app.get("/")
def get_gui():
    return FileResponse(CURRENT_DIR / "index.html")
