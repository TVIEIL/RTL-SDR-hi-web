import sys
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.ndimage import gaussian_filter1d, median_filter

C_LIGHT_KMS = 299792.458
HI_REST_MHZ = 1420.40575177

def analyze_csv(csv_path: str):
    path = Path(csv_path)
    if not path.exists():
        print(f"Erreur : fichier introuvable -> {path}")
        sys.exit(1)

    df = pd.read_csv(path, comment="#")
    timestamps = pd.to_datetime(df["timestamp_utc"])
    freq_cols = df.columns[3:]
    freqs_mhz = np.array([float(col.replace("_MHz", "")) for col in freq_cols])
    raw_matrix = df[freq_cols].to_numpy(dtype=np.float64)

    # Estimation automatique du fond instrumental (médiane des 15 % intégrations les plus basses)
    total_power = np.mean(raw_matrix, axis=1)
    cold_idx = np.argsort(total_power)[: max(1, int(0.15 * len(raw_matrix)))]
    cold_baseline = np.median(raw_matrix[cold_idx], axis=0)

    # Calibration relative au ciel froid + lissage doux pour la visualisation
    cal_matrix = raw_matrix - cold_baseline
    cal_matrix = median_filter(cal_matrix, size=(1, 5))
    cal_matrix = gaussian_filter1d(cal_matrix, sigma=1.5, axis=1)

    # Conversion fréquence -> vitesse radiale Doppler (convention astronomique : + = éloignement)
    velocities_kms = C_LIGHT_KMS * (HI_REST_MHZ - freqs_mhz) / HI_REST_MHZ

    # Création de la planche de synthèse
    fig, (ax_wf, ax_prof) = plt.subplots(
        2, 1, figsize=(12, 8), gridspec_kw={"height_ratios": [2.2, 1]}, constrained_layout=True
    )

    # 1. Carte de transit 2D (Waterfall Temps UTC vs Vitesse Doppler / Fréquence)
    extent = [velocities_kms[0], velocities_kms[-1], len(timestamps), 0]
    im = ax_wf.imshow(
        cal_matrix,
        aspect="auto",
        cmap="turbo",
        extent=extent,
        vmin=np.percentile(cal_matrix, 5),
        vmax=np.percentile(cal_matrix, 99),
    )
    ax_wf.axvline(0, color="white", linestyle="--", linewidth=1, alpha=0.8, label="1420.406 MHz (0 km/s)")
    ax_wf.set_title(f"Transit de la Raie HI (21 cm) — {path.name} ({len(timestamps)} intégrations)")
    ax_wf.set_xlabel("Vitesse Radiale Doppler (km/s)")
    ax_wf.set_ylabel("Index d'intégration (6 min / ligne)")
    ax_wf.legend(loc="upper right")
    cbar = fig.colorbar(im, ax=ax_wf, pad=0.01)
    cbar.set_label("Excès Δ dB (vs Ciel Froid)")

    # 2. Spectre moyen et spectre maximal observé
    peak_row = np.argmax(np.max(cal_matrix, axis=1))
    ax_prof.plot(freqs_mhz, np.mean(cal_matrix, axis=0), color="#00bcd4", linewidth=1.5, label="Spectre moyen session")
    ax_prof.plot(
        freqs_mhz,
        cal_matrix[peak_row],
        color="#ff5252",
        linewidth=1.8,
        label=f"Pic maximal ({timestamps.iloc[peak_row].strftime('%H:%M UTC')})",
    )
    ax_prof.axvline(HI_REST_MHZ, color="#888", linestyle=":", linewidth=1.2)
    ax_prof.set_xlabel("Fréquence (MHz)")
    ax_prof.set_ylabel("Δ dB")
    ax_prof.grid(True, alpha=0.25)
    ax_prof.legend()

    out_png = path.with_suffix(".png")
    plt.savefig(out_png, dpi=150)
    print(f"Analyse terminée : graphique sauvegardé dans {out_png}")
    plt.show()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage : python analyze_transit.py observations/HI_raw_YYYYMMDD_HHMMSS_UTC.csv")
        sys.exit(1)
    analyze_csv(sys.argv[1])
