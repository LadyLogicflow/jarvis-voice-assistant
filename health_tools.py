"""
Apple Health data integration via Health Auto Export webhook.

Health Auto Export (iOS App) sendet regelmaessig per HTTP POST an /health
einen JSON-Body mit Metriken aus Apple Health / Apple Watch.

Dieses Modul parst den Body, normalisiert die Felder und liefert ein
einheitliches dict das in S.HEALTH_INFO gespeichert wird.

Unterstuetzte Metriken (werden ignoriert wenn nicht im Export):
  sleep_analysis          Schlaf (Stunden)
  active_energy           Kalorien Bewegungsring (kcal)
  apple_exercise_time     Trainingsring (Minuten, Ziel 30)
  apple_stand_hour        Stehring (Stunden, Ziel 12)
  resting_heart_rate      Ruheherzfrequenz (bpm)
  heart_rate_variability_sdnn  HRV (ms)
  workouts                Liste der letzten Trainingseinheiten
"""

from __future__ import annotations

import datetime
import re
from typing import Optional


# Workout-Name-Mapping EN -> DE
_WORKOUT_NAMES_DE = {
    "Running": "Laufen",
    "Walking": "Spaziergang",
    "Cycling": "Radfahren",
    "Swimming": "Schwimmen",
    "Hiking": "Wandern",
    "Yoga": "Yoga",
    "Strength Training": "Krafttraining",
    "High Intensity Interval Training": "HIIT",
    "Elliptical": "Crosstrainer",
    "Stair Climbing": "Treppensteigen",
    "Pilates": "Pilates",
    "Dance": "Tanzen",
    "Tennis": "Tennis",
    "Soccer": "Fussball",
    "Basketball": "Basketball",
    "Rowing": "Rudern",
    "Other": "Sport",
    "Functional Strength Training": "Krafttraining",
    "Core Training": "Core-Training",
    "Flexibility": "Dehnen",
    "Cooldown": "Cool-down",
    "Mind And Body": "Entspannung",
}


def _latest_qty(metric_data: list[dict]) -> Optional[float]:
    """Gibt den qty-Wert des juengsten Eintrags zurueck, oder None."""
    if not metric_data:
        return None
    try:
        return float(metric_data[-1].get("qty", 0) or 0)
    except (TypeError, ValueError):
        return None


def _sum_qty(metric_data: list[dict]) -> Optional[float]:
    """Summiert alle qty-Werte (z.B. Schlaf-Segmente)."""
    if not metric_data:
        return None
    try:
        return sum(float(e.get("qty", 0) or 0) for e in metric_data)
    except (TypeError, ValueError):
        return None


def parse_health_export(payload: dict) -> dict:
    """Parst den JSON-Body von Health Auto Export und gibt ein normalisiertes
    dict zurueck.

    Unbekannte Felder werden ignoriert; fehlende Felder bleiben None.
    Robust gegen beide bekannten Export-Formate der App.
    """
    # Health Auto Export verpackt manchmal in {"data": {...}}
    if "data" in payload and isinstance(payload["data"], dict):
        payload = payload["data"]

    metrics_list: list[dict] = payload.get("metrics", [])
    workouts_list: list[dict] = payload.get("workouts", [])

    # Index metrics by name for O(1) lookup
    metrics: dict[str, list[dict]] = {}
    for m in metrics_list:
        name = m.get("name", "")
        if name:
            metrics[name] = m.get("data", [])

    # --- Sleep ---
    sleep_h: Optional[float] = None
    for key in ("sleep_analysis", "sleepAnalysis", "sleep"):
        if key in metrics:
            # sleep_analysis may have multiple segments — sum them
            sleep_h = _sum_qty(metrics[key])
            if sleep_h is not None:
                break

    # --- Activity rings ---
    move_kcal: Optional[float] = None
    for key in ("active_energy", "activeEnergy", "basalEnergyBurned"):
        if key in metrics:
            move_kcal = _latest_qty(metrics[key])
            break

    exercise_min: Optional[float] = None
    for key in ("apple_exercise_time", "appleExerciseTime", "exercise_time"):
        if key in metrics:
            exercise_min = _latest_qty(metrics[key])
            break

    stand_h: Optional[float] = None
    for key in ("apple_stand_hour", "appleStandHour", "stand_hour"):
        if key in metrics:
            stand_h = _latest_qty(metrics[key])
            break

    # --- Steps ---
    steps: Optional[float] = None
    for key in ("step_count", "stepCount"):
        if key in metrics:
            steps = _sum_qty(metrics[key])
            break

    # --- Heart ---
    resting_hr: Optional[float] = None
    for key in ("resting_heart_rate", "restingHeartRate"):
        if key in metrics:
            resting_hr = _latest_qty(metrics[key])
            break

    hrv: Optional[float] = None
    for key in ("heart_rate_variability_sdnn", "heartRateVariabilitySDNN",
                "heart_rate_variability", "heartRateVariability", "hrv"):
        if key in metrics:
            hrv = _latest_qty(metrics[key])
            break

    # --- SpO2 ---
    spo2: Optional[float] = None
    for key in ("blood_oxygen_saturation", "bloodOxygenSaturation", "oxygenSaturation"):
        if key in metrics:
            spo2 = _latest_qty(metrics[key])
            break

    # --- Most recent workout ---
    last_workout: Optional[dict] = None
    if workouts_list:
        w = workouts_list[-1]
        raw_name = w.get("name", w.get("workoutActivityType", "Sport"))
        # Strip "HKWorkoutActivityType" prefix if present
        raw_name = re.sub(r"^HKWorkoutActivityType", "", raw_name)
        name_de = _WORKOUT_NAMES_DE.get(raw_name, raw_name)
        duration_min = None
        try:
            duration_min = round(float(w.get("duration", 0) or 0))
        except (TypeError, ValueError):
            pass
        last_workout = {
            "name": name_de,
            "duration_min": duration_min,
            "kcal": w.get("totalEnergyBurned"),
            "distance_km": w.get("totalDistance"),
            "date": w.get("startDate", ""),
        }

    return {
        "date": datetime.date.today().isoformat(),
        "sleep_h": round(sleep_h, 1) if sleep_h is not None else None,
        "move_kcal": round(move_kcal) if move_kcal is not None else None,
        "exercise_min": round(exercise_min) if exercise_min is not None else None,
        "stand_h": round(stand_h) if stand_h is not None else None,
        "steps": round(steps) if steps is not None else None,
        "resting_hr": round(resting_hr) if resting_hr is not None else None,
        "hrv": round(hrv) if hrv is not None else None,
        "spo2": round(spo2, 1) if spo2 is not None else None,
        "last_workout": last_workout,
    }


def _delta(current: Optional[float], prev: Optional[float]) -> Optional[str]:
    """Gibt einen Vergleichs-Hinweis zurueck ('besser', 'schlechter', 'gleich')
    oder None wenn kein Vergleich moeglich."""
    if current is None or prev is None or prev == 0:
        return None
    diff = current - prev
    if abs(diff) < 0.05 * prev:  # <5% Aenderung = gleich
        return "gleich"
    return "besser" if diff > 0 else "schlechter"


def format_for_brief(
    info: dict,
    move_goal_kcal: int = 500,
    prev: Optional[dict] = None,
) -> str:
    """Formatiert die Gesundheitsdaten als kompakten Text-Block fuer den
    Morgenbriefing-Prompt inkl. Vortagsvergleich wenn prev vorhanden.
    Gibt leeren String zurueck wenn keine Daten."""
    if not info:
        return ""
    parts: list[str] = []

    # Schlaf
    if info.get("sleep_h") is not None:
        h = info["sleep_h"]
        sleep_line = f"Schlaf heute Nacht: {h:.1f} Stunden"
        d = _delta(h, prev.get("sleep_h") if prev else None)
        if d == "besser":
            sleep_line += " (mehr als gestern)"
        elif d == "schlechter":
            sleep_line += " (weniger als gestern)"
        parts.append(sleep_line)

    # Aktivitaetsringe
    ring_parts: list[str] = []
    if info.get("move_kcal") is not None:
        pct = min(100, round(info["move_kcal"] / move_goal_kcal * 100))
        move_line = f"Bewegung {pct} Prozent ({info['move_kcal']} kcal)"
        d = _delta(info["move_kcal"], (prev or {}).get("move_kcal"))
        if d and d != "gleich":
            move_line += f" ({d} als gestern)"
        ring_parts.append(move_line)
    if info.get("exercise_min") is not None:
        ex_pct = min(100, round(info["exercise_min"] / 30 * 100))
        ex_line = f"Training {ex_pct} Prozent ({info['exercise_min']} min)"
        d = _delta(info["exercise_min"], (prev or {}).get("exercise_min"))
        if d and d != "gleich":
            ex_line += f" ({d} als gestern)"
        ring_parts.append(ex_line)
    if info.get("stand_h") is not None:
        st_pct = min(100, round(info["stand_h"] / 12 * 100))
        ring_parts.append(f"Stehen {st_pct} Prozent ({info['stand_h']} Std.)")
    if ring_parts:
        parts.append("Aktivitaetsringe: " + ", ".join(ring_parts))

    # Schritte
    if info.get("steps") is not None:
        step_line = f"Schritte: {info['steps']:,}".replace(",", ".")
        d = _delta(info["steps"], (prev or {}).get("steps"))
        if d and d != "gleich":
            step_line += f" ({d} als gestern)"
        parts.append(step_line)

    # Herzwerte
    hr_parts: list[str] = []
    if info.get("resting_hr") is not None:
        hr_parts.append(f"Ruhepuls {info['resting_hr']} bpm")
    if info.get("hrv") is not None:
        hrv_line = f"HRV {info['hrv']} ms"
        d = _delta(info["hrv"], (prev or {}).get("hrv"))
        if d and d != "gleich":
            hrv_line += f" ({d} als gestern)"
        hr_parts.append(hrv_line)
    if info.get("spo2") is not None:
        hr_parts.append(f"SpO2 {info['spo2']} Prozent")
    if hr_parts:
        parts.append(", ".join(hr_parts))

    # Letztes Workout
    w = info.get("last_workout")
    if w and w.get("duration_min"):
        w_line = f"Letztes Training: {w['name']}, {w['duration_min']} Minuten"
        if w.get("distance_km"):
            try:
                w_line += f", {float(w['distance_km']):.1f} km"
            except (TypeError, ValueError):
                pass
        parts.append(w_line)

    return "\n".join(parts)
