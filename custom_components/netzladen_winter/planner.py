"""Plan battery charging from local energy counters and forecasts."""

import datetime
import math

ETA = math.sqrt(0.90)
# Assumption from the original daily energy model, independent of grid power.
PV_STORAGE_KW = 5.0
BALANCE = (
    "pv_energy", "second_pv_energy", "grid_import", "grid_export",
    "battery_charge", "battery_discharge",
)


def finite(value, minimum=-1e12, maximum=1e12):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def counter_delta(current, previous, limit):
    if current is None or previous is None:
        return None
    difference = current - previous
    return max(0, difference) if -0.01 <= difference <= limit else None


def parse_date(value, now):
    if isinstance(value, datetime.datetime):
        return value.astimezone(now.tzinfo) if value.tzinfo is not None else None
    try:
        date = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return date.astimezone(now.tzinfo) if date.tzinfo is not None else None


def window_end(settings, now):
    end = datetime.time.fromisoformat(settings["end_time"])
    return datetime.datetime.combine(now.date(), end, now.tzinfo).timestamp()


def price_in_cents(value, unit):
    """Normalize supported euro-denominated electricity prices to ct/kWh."""
    if not isinstance(unit, str):
        return None
    factor = {
        "ct/kwh": 1, "¢/kwh": 1, "eur/kwh": 100, "€/kwh": 100,
        "eur/mwh": 0.1, "€/mwh": 0.1,
    }.get("".join(unit.lower().split()))
    price = finite(value)
    return round(price * factor, 9) if factor is not None and price is not None else None


def in_window(settings, now, price_ct=None, *, charging=False):
    if str(now.month) not in settings["winter_months"]:
        return False
    if settings.get("tariff_mode") == "price":
        limit = settings["price_limit_ct"]
        return price_ct is not None and (price_ct < limit or (charging and price_ct == limit))
    return now.timestamp() < window_end(settings, now)


def grid_charge_power(settings):
    """Limit grid charging to the configured battery charging power."""
    grid_w = settings["charge_w"]
    storage_w = settings.get("storage_charge_w")
    return min(grid_w, storage_w) if storage_w is not None else grid_w


def pv_forecast(entities, settings, now, midnight, day_hours):
    for key in ("pv_today", "pv_tomorrow"):
        entity = entities.get(settings.get(key))
        if entity is None or entity.state in ("unknown", "unavailable"):
            continue
        rows = entity.attributes.get("detailedHourly")
        if not isinstance(rows, (list, tuple)):
            continue
        hours = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            moment = parse_date(row.get("period_start"), now)
            if moment is None or moment.date() != now.date():
                continue
            elapsed = (moment.timestamp() - midnight.timestamp()) / 3600
            mid = finite(row.get("pv_estimate"), 0, 1000)
            if not elapsed.is_integer() or elapsed in hours or mid is None:
                break
            hours[int(elapsed)] = (moment.hour, mid, finite(row.get("pv_estimate10"), 0, mid))
        else:
            if hours.keys() != set(range(day_hours)):
                continue
            raw, low = [0.0] * 24, [0.0] * 24
            fallback = False
            for hour, mid, lower in hours.values():
                raw[hour] += mid
                low[hour] += mid * 0.25 if lower is None else lower
                fallback |= lower is None
            return raw, low, settings[key], fallback
    return None, [], "", False


def conservative_pv(raw, low, weather, previous_fog):
    def fog(row):
        return row.get("condition") == "fog" or (
            (row.get("cloud") or 0) >= 90 and (row.get("humidity") or 0) >= 95
        )

    morning_fog = previous_fog or any(fog(weather.get(h, {})) for h in range(6, 12))
    planned, modes = [], []
    for hour, (mid, lower) in enumerate(zip(raw, low, strict=True)):
        row = weather.get(hour, {})
        cloud = row.get("cloud")
        if morning_fog or fog(row):
            mode, weight, cap = "fog", 0.8, 0.5
        elif cloud is None:
            mode, weight, cap = "weather_missing", 0.6, 0.5
        elif cloud >= 90:
            mode, weight, cap = "overcast", 0.6, 1.0
        else:
            mode, weight, cap = "normal", 0.2, 1.0
        planned.append(round(min(mid * cap, (1 - weight) * mid + weight * lower), 4))
        modes.append(mode)
    return planned, modes, morning_fog


def calculate(now, settings, read, entities, weather_rows, old, owned, *, price_ct=None, charging=False):
    """Return a plan, diagnostics and the data needed for the next calculation."""
    low_pv_kwh = settings['low_pv_kwh']
    buffer_kwh = settings['buffer_kwh']
    charge_w = grid_charge_power(settings)
    storage_charge_w = settings.get('storage_charge_w')
    pv_charge_kw = storage_charge_w / 1000 if storage_charge_w is not None else PV_STORAGE_KW
    ts = now.timestamp()
    today = now.date().isoformat()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    saved_hours = old.get('hours', [])
    history = [x for x in saved_hours if ts - 15 * 86400 < x[0] <= ts]
    snapshot = old.get('snapshot', {})
    # Zählerdifferenzen werden nur bei zwei aufeinanderfolgenden Stunden gebildet.
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_ts = hour_start.timestamp()
    if not snapshot or (now.minute == 0 and hour_ts > snapshot['hour_ts']):
        direct = settings['load_source'] == 'direct'
        keys = ('house_energy',) if direct else BALANCE
        current = [read(key, 'energy') for key in keys]
        if not direct:
            current[1] = 0.0 if not settings.get('second_pv_energy') else current[1]
        ev_now = read('wallbox_energy', 'energy') if settings['has_wallbox'] else None
        wp_now = read('heat_pump_energy', 'energy') if settings['has_heat_pump'] else None
        temp_now = read('outdoor_temperature', 'temperature')
        if snapshot and hour_ts - snapshot['hour_ts'] == 3600 and 3540 <= ts - snapshot['ts'] <= 3660:
            diffs = [counter_delta(value, previous, 100)
                     for value, previous in zip(current, snapshot['values'], strict=True)]
            ev_delta = counter_delta(ev_now, snapshot['ev'], 50) if settings['has_wallbox'] else 0
            if all(value is not None for value in diffs) and ev_delta is not None:
                load = diffs[0] if direct else sum(diffs[:3]) - sum(diffs[3:5]) + diffs[5]
                load -= ev_delta
                wp_delta = counter_delta(wp_now, snapshot['wp'], 50)
                temp_mean = None
                if temp_now is not None and snapshot['temperature'] is not None:
                    temp_mean = (temp_now + snapshot['temperature']) / 2
                if -0.3 <= load <= 50:
                    history.append([snapshot['hour_ts'], snapshot['day'], snapshot['hour'],
                                    round(max(0, load), 3), wp_delta, temp_mean])
        snapshot = {'ts': ts, 'hour_ts': hour_ts, 'day': today, 'hour': now.hour,
                    'values': current, 'ev': ev_now, 'wp': wp_now, 'temperature': temp_now}

    profile = []
    counts = []
    cutoff = (midnight - datetime.timedelta(days=14)).timestamp()
    for hour in range(24):
        samples = [x[3] for x in history if cutoff <= x[0] < midnight.timestamp() and x[2] == hour]
        counts.append(len(samples))
        # Drei gueltige Stunden mindestens; bis dahin jahreszeitlicher Startwert.
        profile.append(round(sum(samples) / len(samples), 3) if len(samples) >= 3 else settings['fallback_load_kw'])

    temperatures = {}
    pv_weather = {}
    for row in weather_rows:
        moment = parse_date(row.get('datetime'), now)
        if moment is None or moment.date() != now.date():
            continue
        temperature = finite(row.get('temperature'), -40, 60)
        if temperature is not None:
            temperatures[moment.hour] = temperature
        pv_weather[moment.hour] = {
            'cloud': finite(row.get('cloud_coverage'), 0, 100),
            'humidity': finite(row.get('humidity'), 0, 100),
            'condition': row.get('condition', ''),
        }
    current_temperature = read('outdoor_temperature', 'temperature')
    if current_temperature is not None:
        temperatures.setdefault(now.hour, current_temperature)
    weather_valid = all(h in temperatures for h in range(now.hour, 24))

    # Neue Messpaare ersetzen schrittweise die historischen Startwerte.
    paired = [x for x in history if len(x) >= 6 and x[4] is not None and x[5] is not None and cutoff <= x[0] < midnight.timestamp()]
    daily_pairs = {}
    for row in paired:
        key = row[1]
        if key not in daily_pairs:
            daily_pairs[key] = []
        daily_pairs[key].append(row)
    points = []
    for key in daily_pairs:
        dayrows = daily_pairs[key]
        if len(dayrows) >= 20:
            points.append([sum(max(0, 18 - x[5]) for x in dayrows) / len(dayrows), sum(x[4] for x in dayrows) / len(dayrows)])
    wp_slope = 0.025 if settings['has_heat_pump'] else 0.0
    model_source = 'Lernt' if settings['has_heat_pump'] else 'Aus'
    if len(points) >= 7:
        mx = sum(x[0] for x in points) / len(points)
        my = sum(x[1] for x in points) / len(points)
        variance = sum((x[0] - mx) ** 2 for x in points)
        if variance / len(points) >= 1:
            candidate = sum((x[0] - mx) * (x[1] - my) for x in points) / variance
            if 0 <= candidate <= 0.15:
                wp_slope = candidate
                model_source = 'Gelernte Temperaturabhaengigkeit'
    base_profile = [x for x in profile]
    wp_profile = []
    weather_adjustment = []
    for h in range(24):
        samples = [x for x in paired if x[2] == h]
        if not settings['has_heat_pump'] or len(samples) < 3:
            wp_profile.append(0.0)
            weather_adjustment.append(0.0)
            continue
        baseline_wp = sum(x[4] for x in samples) / len(samples)
        baseline_degree = sum(max(0, 18 - x[5]) for x in samples) / len(samples)
        # Nur WP-Anteil anpassen, nicht nochmals den gesamten Hausverbrauch addieren.
        baseline_wp = min(profile[h], baseline_wp)
        adjustment = 0
        if weather_valid and h in temperatures:
            adjustment = wp_slope * (max(0, 18 - temperatures[h]) - baseline_degree)
            adjustment = max(-baseline_wp, min(2.0, adjustment))
        elif not weather_valid:
            # Ausfall: gelerntes Profil plus 20 % WP-Puffer; keine Nachtfreigabe.
            adjustment = baseline_wp * 0.2
        profile[h] = round(max(0, profile[h] + adjustment), 3)
        wp_profile.append(round(max(0, baseline_wp + adjustment), 3))
        weather_adjustment.append(round(adjustment, 3))

    tomorrow_start = midnight + datetime.timedelta(days=1)
    day_hours = round((tomorrow_start.timestamp() - midnight.timestamp()) / 3600)
    day_slots = [
        (midnight.timestamp() + offset * 3600,
         datetime.datetime.fromtimestamp(midnight.timestamp() + offset * 3600, now.tzinfo).hour)
        for offset in range(day_hours)
    ]
    raw_pv, low_pv, forecast_source, low_fallback = pv_forecast(
        entities, settings, now, midnight, day_hours
    )
    previous_night = old.get('daily_forecasts', {}).get(today, {}).get('pv_night', {})
    pv, pv_modes, fog_risk = (None, [], False)
    if raw_pv is not None:
        pv, pv_modes, fog_risk = conservative_pv(
            raw_pv, low_pv, pv_weather, previous_night.get('fog_risk', False)
        )
    repeats = {hour for hour in range(24) if sum(slot == hour for _, slot in day_slots) > 1}
    solar_per_slot = [value / 2 if hour in repeats else value
                      for hour, value in enumerate(pv or [0] * 24)]

    forecast_valid = pv is not None
    pv_total = round(sum(pv), 2) if forecast_valid else None
    capacity = read('capacity_sensor', 'energy') if settings.get('capacity_sensor') else settings['capacity_kwh']
    minimum = read('min_soc', 'percent')
    maximum = read('max_soc', 'percent')
    backup = read('backup_soc', 'percent') if settings.get('backup_soc') else settings['backup_percent']
    soc = read('battery_soc', 'percent')
    hardware_valid = (capacity is not None and 1 <= capacity <= 1000
                      and minimum is not None and 0 <= minimum <= 50
                      and maximum is not None and minimum < maximum <= 100
                      and soc is not None and 0 <= soc <= 100
                      and backup is not None and 0 <= backup <= 100)
    if hardware_valid:
        if settings['usable_soc']:
            soc = soc * (maximum - minimum) / (100 - minimum)
        else:
            soc = max(0, (soc - minimum) * 100 / (100 - minimum))
        capacity *= (100 - minimum) / 100
    full_date = old.get('full_date', '')
    price_based = settings.get('tariff_mode') == 'price'
    deadline = ts if price_based else window_end(settings, now)
    charging_allowed = in_window(settings, now, price_ct, charging=charging)
    night = charging_allowed and not price_based

    if night and forecast_valid and pv_total <= low_pv_kwh:
        full_date = today
    force_full = not price_based and full_date == today
    target = 100.0
    reason = 'Stündliche PV-Prognose fehlt'
    if forecast_valid:
        if force_full or pv_total <= low_pv_kwh:
            target = 100.0
            reason = 'Sehr wenig PV: 100 % bis Ladeende'
        elif hardware_valid:
            floor = capacity * backup / 100
            needed = floor
            for start, hour in reversed(day_slots):
                if start + 3600 <= deadline:
                    break
                fraction = (start + 3600 - max(start, deadline)) / 3600
                net = (profile[hour] - solar_per_slot[hour]) * fraction
                if net >= 0:
                    needed = min(capacity, needed + min(PV_STORAGE_KW * fraction, net) / ETA)
                else:
                    needed = max(floor, needed - min(pv_charge_kw * fraction, -net) * ETA)
            target = min(100.0, max(backup, (needed + buffer_kwh) / capacity * 100))
            reason = 'Stundenprofil, PV-Prognose und Energiepuffer'
    target = round(target, 1)
    # Deckung fuer REST der Nacht plus Tagesziel: nur dann Eigenverbrauch zulassen.
    night_need = 0.0
    if hardware_valid and night:
        for start, hour in day_slots:
            remaining = max(0, min(deadline, start + 3600) - max(ts, start)) / 3600
            night_need += max(0, profile[hour] - solar_per_slot[hour]) * remaining / ETA
    night_required = target * capacity / 100 + night_need if hardware_valid else None
    previous_free = old.get('night_self_consumption', False) and old.get('date') == today
    # Hysterese verhindert staendiges Umschalten nahe der Grenze.
    margin = 0.2 if previous_free else 0.5
    night_free = (hardware_valid and forecast_valid and weather_valid and not force_full
                  and pv_total > low_pv_kwh and night
                  and soc * capacity / 100 >= night_required + margin)

    # Auch nach einem Neustart ausserhalb des Fensters: zuerst freigeben.
    command = 'release' if owned else 'idle'
    if charging_allowed:
        if not hardware_valid:
            reason = 'Ladestand, Kapazität oder Reserve ungültig'
        elif not forecast_valid:
            reason = 'Stündliche PV-Prognose fehlt'
        elif price_based:
            if soc < target:
                command = 'charge'
        elif night_free:
            reason = 'Akku deckt Restnacht und Tagesziel'
        elif soc >= target:
            command = 'hold'
        elif target == 100:
            command = 'charge'
        else:
            remaining = max(0, deadline - 20 * 60 - ts) / 3600
            need_hours = (target - soc) * capacity / 100 / ETA / (charge_w / 1000)
            was_charging = old.get('command') == 'charge' and old.get('date') == today
            command = 'charge' if was_charging or remaining <= need_hours + 10 / 60 else 'hold'

    # Tagesvergleich ohne Einfluss auf die Ladesteuerung.
    daily_forecasts = old.get('daily_forecasts', {})
    keep_dates = [(now.date() - datetime.timedelta(days=d)).isoformat() for d in range(16)]
    daily_forecasts = {k: dict(v) for k, v in daily_forecasts.items() if k in keep_dates}
    forecast_total = sum(profile[hour] for _, hour in day_slots)
    # Nachtraegliche Tagesprognosen nicht als Mitternachtsprognose ausgeben.
    if now.hour == 0 and now.minute < 3 and today not in daily_forecasts:
        daily_forecasts[today] = {'kwh': round(forecast_total, 3),
                                  'captured_at': now.isoformat(),
                                  'weather_valid': weather_valid}
    # Letzte Nachtentscheidung samt Stundenwerten fuer spaetere Rueckvergleiche.
    if charging_allowed:
        if today not in daily_forecasts:
            daily_forecasts[today] = {}
        daily_forecasts[today]['pv_night'] = {
            'at': now.isoformat(), 'raw': raw_pv, 'low': low_pv, 'planned': pv,
            'weather': [[h, pv_weather[h]['cloud'], pv_weather[h]['humidity'],
                         pv_weather[h]['condition']] for h in sorted(pv_weather)],
            'fog_risk': fog_risk, 'target': target, 'command': command}
    # Stundenarchive nur sieben Tage behalten; Verbrauchsvergleich bleibt 16 Tage.
    archive_dates = [(now.date() - datetime.timedelta(days=d)).isoformat() for d in range(7)]
    for day in daily_forecasts:
        if day not in archive_dates and 'pv_night' in daily_forecasts[day]:
            daily_forecasts[day] = {k: v for k, v in daily_forecasts[day].items() if k != 'pv_night'}

    yesterday = (now.date() - datetime.timedelta(days=1)).isoformat()
    yesterday_start = midnight - datetime.timedelta(days=1)
    expected_hours = int(round((midnight.timestamp() - yesterday_start.timestamp()) / 3600))
    today_rows = [x for x in history if x[1] == today]
    yesterday_rows = [x for x in history if x[1] == yesterday]
    complete = len(yesterday_rows) == expected_hours
    if complete:
        complete = all(3540 <= yesterday_rows[i][0] - yesterday_rows[i-1][0] <= 3660
                       for i in range(1, len(yesterday_rows)))
    actual_yesterday = round(sum(x[3] for x in yesterday_rows), 3) if complete else None
    forecast_today = daily_forecasts.get(today, {}).get('kwh')
    forecast_yesterday = daily_forecasts.get(yesterday, {}).get('kwh')
    result = {}
    result.update({'daily_forecasts': daily_forecasts,
                   'forecast_today': forecast_today,
                   'forecast_captured_at': daily_forecasts.get(today, {}).get('captured_at'),
                   'actual_today': round(sum(x[3] for x in today_rows), 3),
                   'actual_today_hours': len(today_rows),
                   'evaluation_date': yesterday, 'actual_yesterday': actual_yesterday,
                   'actual_yesterday_hours': len(yesterday_rows),
                   'expected_yesterday_hours': expected_hours,
                   'forecast_yesterday': forecast_yesterday,
                   'deviation_yesterday': round(actual_yesterday - forecast_yesterday, 3)
                       if actual_yesterday is not None and forecast_yesterday is not None else None})

    if forecast_valid:
        reason = reason + (' · Nebelreserve' if fog_risk else ' · PV-Sicherheitsabschlag')
    result.update({'date': today, 'calculated_at': now.isoformat(), 'command': command,
                   'tariff_mode': 'price' if price_based else 'fixed',
                   'charging_allowed': charging_allowed,
                   'electricity_price_ct': price_ct if price_based else None,
                   'price_limit_ct': settings['price_limit_ct'] if price_based else None,
                   'target': target if hardware_valid and forecast_valid else None,
                   'reason': reason, 'hardware_valid': hardware_valid,
                   'night_self_consumption': night_free and night,
                   'night_required_kwh': round(night_required, 3) if night_required is not None else None,
                   'night_load_kwh': round(night_need, 3),
                   'weather_valid': weather_valid, 'wp_model_source': model_source,
                   'wp_slope': round(wp_slope, 5), 'wp_profile': wp_profile,
                   'weather_adjustment_kwh': round(sum(weather_adjustment), 3),
                   'base_load_profile': base_profile, 'pv_kwh': pv_total,
                   'forecast_valid': forecast_valid, 'forecast_source': forecast_source,
                   'low_pv_kwh': low_pv_kwh, 'max_charge_w': charge_w,
                   'storage_charge_w': storage_charge_w,
                   'load_profile': profile, 'profile_counts': counts,
                   'full_date': full_date, 'snapshot': snapshot, 'hours': history,
                   'profile_source': '14 Tage mit einstellbarem Startwert'})


    result['pv_diagnostics'] = {
        'raw_pv_kwh': round(sum(raw_pv), 2) if raw_pv is not None else None,
        'low_pv_kwh': round(sum(low_pv), 2) if low_pv else None,
        'fog_risk': fog_risk, 'low_forecast_fallback': low_fallback,
        'hourly_modes': pv_modes,
    }
    return result
