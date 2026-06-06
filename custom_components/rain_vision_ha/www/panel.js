// Rain Vision HA — Irrigation Advisor Panel

// ─── Option tables ────────────────────────────────────────────────────────────

const EXPOSURE = {
  options: ["north", "north_east", "east", "south_east", "south", "south_west", "west", "north_west", "full_sun", "shade"],
  labels:  { north:"North", north_east:"North-East", east:"East", south_east:"South-East", south:"South", south_west:"South-West", west:"West", north_west:"North-West", full_sun:"Full Sun", shade:"Shade/Covered" },
  factors: { north:0.70, north_east:0.80, east:0.95, south_east:1.10, south:1.20, south_west:1.10, west:0.95, north_west:0.80, full_sun:1.25, shade:0.60 },
};

const SOIL = {
  options: ["sandy", "sandy_loam", "loam", "clay_loam", "clay"],
  labels:  { sandy:"Sandy", sandy_loam:"Sandy Loam", loam:"Loam", clay_loam:"Clay Loam", clay:"Clay" },
  factors: { sandy:1.25, sandy_loam:1.10, loam:1.00, clay_loam:0.90, clay:0.80 },
};

const GRASS = {
  options: ["lawn", "shrubs", "vegetables", "flowers"],
  labels:  { lawn:"Lawn (auto season)", shrubs:"Shrubs", vegetables:"Vegetables", flowers:"Flowers" },
  kc:      { lawn: null, shrubs:0.50, vegetables:0.90, flowers:0.80 },
  kc_cool: 0.80,
  kc_warm: 0.65,
};

const IRRIGATION_TYPE = {
  options: ["sprinkler", "rotor", "drip"],
  labels:  { sprinkler:"Sprinkler", rotor:"Rotor", drip:"Drip" },
  efficiency: { sprinkler:0.75, rotor:0.82, drip:0.92 },
};

const WEATHER_KEYS = [
  { key: "temperature",     label: "Temperature (°C)",       deviceClass: "temperature" },
  { key: "humidity",        label: "Relative Humidity (%)",  deviceClass: "humidity" },
  { key: "solar_radiation", label: "Solar Radiation (W/m²)", deviceClass: "irradiance" },
  { key: "wind_speed",      label: "Wind Speed (m/s)",       deviceClass: "wind_speed" },
  { key: "rain_today",      label: "Rain Today (mm)",        deviceClass: "precipitation" },
];

// Estimated W/m² by weather condition (for forecast days without direct sensor)
const CONDITION_SOLAR = {
  sunny: 700, "clear-night": 0, partlycloudy: 400, cloudy: 180,
  rainy: 100, pouring: 80, snowy: 120, "lightning-rainy": 90,
  windy: 500, fog: 150, hail: 100, exceptional: 250,
};

const CONDITION_ICON = {
  sunny:            "mdi:weather-sunny",
  "clear-night":    "mdi:weather-night",
  partlycloudy:     "mdi:weather-partly-cloudy",
  cloudy:           "mdi:weather-cloudy",
  rainy:            "mdi:weather-rainy",
  pouring:          "mdi:weather-pouring",
  snowy:            "mdi:weather-snowy",
  "lightning-rainy":"mdi:weather-lightning-rainy",
  windy:            "mdi:weather-windy",
  fog:              "mdi:weather-fog",
  hail:             "mdi:weather-hail",
  exceptional:      "mdi:weather-partly-lightning",
};

// ─── Season helpers ───────────────────────────────────────────────────────────

function _isWarmSeason(lat) {
  const month = new Date().getMonth();
  const northern = (lat ?? 45) >= 0;
  const warmMonths = northern ? [3,4,5,6,7,8] : [9,10,11,0,1,2];
  return warmMonths.includes(month);
}

function _lawnKc(lat) {
  return _isWarmSeason(lat) ? GRASS.kc_warm : GRASS.kc_cool;
}

// ─── Date helpers ─────────────────────────────────────────────────────────────

function _dateISO(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function _todayISO() { return _dateISO(new Date()); }

function _shiftDate(isoDate, days) {
  const d = new Date(isoDate + "T12:00:00");
  d.setDate(d.getDate() + days);
  return _dateISO(d);
}

function _dayLabel(isoDate) {
  const d = new Date(isoDate + "T12:00:00");
  return d.toLocaleDateString(undefined, { weekday: "short" });
}

function _dayNum(isoDate) { return parseInt(isoDate.split("-")[2], 10); }

function _monthShort(isoDate) {
  const d = new Date(isoDate + "T12:00:00");
  return d.toLocaleDateString(undefined, { month: "short" });
}

function _calendarWindow() {
  const today = _todayISO();
  const days = [];
  for (let i = -5; i <= 5; i++) days.push(_shiftDate(today, i));
  return days; // 11 dates: -5 … +5
}

// ─── ET Calculation ───────────────────────────────────────────────────────────

function _dayOfYear(date) {
  return Math.floor((date - new Date(date.getFullYear(), 0, 0)) / 86400000);
}

function _extraterrestrialRadiation(latDeg, doy) {
  const Gsc = 0.082;
  const dr   = 1 + 0.033 * Math.cos((2 * Math.PI / 365) * doy);
  const decl = 0.409 * Math.sin((2 * Math.PI / 365) * doy - 1.39);
  const lr   = (Math.PI / 180) * latDeg;
  const ws   = Math.acos(-Math.tan(lr) * Math.tan(decl));
  return (24 * 60 / Math.PI) * Gsc * dr * (
    ws * Math.sin(lr) * Math.sin(decl) + Math.cos(lr) * Math.cos(decl) * Math.sin(ws)
  );
}

function calculateETo(params) {
  const { lat, tMean, tMin, tMax, humidity, solarRadWm2, windSpeedMs, elevation } = params;
  const elev = elevation || 0;
  const doy  = _dayOfYear(new Date());
  const Ra   = _extraterrestrialRadiation(lat, doy);

  const Tmin = tMin ?? (tMean - 3);
  const Tmax = tMax ?? (tMean + 3);

  if (humidity != null && solarRadWm2 != null) {
    const T   = tMean;
    const u2  = windSpeedMs ?? 2.0;
    const Rs  = solarRadWm2 * 0.0864;
    const P     = 101.3 * Math.pow((293 - 0.0065 * elev) / 293, 5.26);
    const gamma = 0.000665 * P;
    const delta = 4098 * (0.6108 * Math.exp(17.27 * T / (T + 237.3))) / Math.pow(T + 237.3, 2);
    const eTmin = 0.6108 * Math.exp(17.27 * Tmin / (Tmin + 237.3));
    const eTmax = 0.6108 * Math.exp(17.27 * Tmax / (Tmax + 237.3));
    const es    = (eTmin + eTmax) / 2;
    const ea    = es * humidity / 100;
    const Rso   = (0.75 + 2e-5 * elev) * Ra;
    const Rns   = (1 - 0.23) * Rs;
    const sigma = 4.903e-9;
    const Rnl   = sigma * ((Math.pow(Tmax+273.16,4) + Math.pow(Tmin+273.16,4)) / 2)
                  * (0.34 - 0.14 * Math.sqrt(Math.max(ea, 0)))
                  * (1.35 * Math.min(Rs / Math.max(Rso, 0.001), 1.0) - 0.35);
    const Rn  = Rns - Rnl;
    const ETo = (0.408 * delta * Rn + gamma * (900 / (T + 273)) * u2 * (es - ea))
                / (delta + gamma * (1 + 0.34 * u2));
    return { value: Math.max(0, ETo), method: "Penman-Monteith (FAO-56)" };
  }

  const ETo = 0.0023 * Ra * Math.sqrt(Math.max(0, Tmax - Tmin)) * (tMean + 17.8);
  return { value: Math.max(0, ETo), method: "Hargreaves-Samani" };
}

function calculateZoneDuration(ETo, zoneConfig, rainToday, lat) {
  const { area, flow_rate, exposure, soil, grass, irrigation_type } = zoneConfig;
  if (!area || !flow_rate) return null;
  const Kc         = grass === "lawn" ? _lawnKc(lat) : (GRASS.kc[grass] ?? 0.80);
  const expFactor  = EXPOSURE.factors[exposure] ?? 1.00;
  const soilFactor = SOIL.factors[soil] ?? 1.00;
  const efficiency = IRRIGATION_TYPE.efficiency[irrigation_type] ?? 0.75;
  const ETc         = ETo * Kc * expFactor * soilFactor;
  const effectiveET = Math.max(0, ETc - (rainToday || 0));
  const volumeL     = effectiveET * area;
  const minutes     = volumeL / (flow_rate * efficiency);
  return Math.max(0, Math.round(minutes));
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function _stateNums(hass, entityIds) {
  if (!hass) return null;
  const ids = Array.isArray(entityIds) ? entityIds : (entityIds ? [entityIds] : []);
  const vals = ids.map(id => {
    if (!id) return null;
    const s = hass.states[id];
    if (!s) return null;
    const v = parseFloat(s.state);
    return isNaN(v) ? null : v;
  }).filter(v => v !== null);
  if (!vals.length) return null;
  return vals.reduce((a, b) => a + b, 0) / vals.length;
}

function _formatTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { weekday:"short", month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" });
}

function _formatDuration(minutes) {
  if (minutes == null || minutes < 0) return "—";
  if (minutes === 0) return "0 min";
  const h = Math.floor(minutes / 60), m = minutes % 60;
  return h > 0 ? `${h}h ${m}min` : `${m} min`;
}

function _formatZoneDurations(zoneDurations) {
  if (!Array.isArray(zoneDurations)) return "—";
  const parts = [];
  zoneDurations.forEach((d, i) => { if (d > 0) parts.push(`Z${i+1}: ${Math.round(d/60)}min`); });
  return parts.length ? parts.join(" · ") : "—";
}

function _weekdayLabel(days) {
  const names = ["","Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
  if (!days || !days.length) return "—";
  if (days.length === 7) return "Every day";
  return days.map(d => names[d] || `D${d}`).join(", ");
}

function _scheduleTypeLabel(s) {
  switch (s.schedule_type) {
    case "weekday":  return _weekdayLabel(s.weekdays);
    case "even_odd": return s.even_mode === 0 ? "Even days" : "Odd days";
    case "hourly":   return "Hourly";
    case "calendar": return "Calendar";
    default:         return s.schedule_type;
  }
}

function _selectOptions(optObj, currentValue) {
  const placeholder = currentValue
    ? `<option value="" disabled>— select —</option>`
    : `<option value="" selected disabled>— select —</option>`;
  return placeholder + optObj.options
    .map(o => `<option value="${o}"${o === currentValue ? " selected" : ""}>${optObj.labels[o]}</option>`)
    .join("");
}

// ─── Calendar helpers ─────────────────────────────────────────────────────────

function _alertStatus(actual, suggested) {
  if (actual == null || suggested == null || suggested === 0) return "unknown";
  if (actual < suggested * 0.80) return "under";
  if (actual > suggested * 1.20) return "over";
  return "ok";
}

function _trendIcon(pastDays, enabledZones) {
  let totalDelta = 0, count = 0;
  pastDays.forEach(day => {
    if (!day || !day.zones) return;
    enabledZones.forEach(z => {
      const zd = day.zones[String(z)];
      if (!zd || zd.actual == null || zd.suggested == null) return;
      totalDelta += zd.actual - zd.suggested;
      count++;
    });
  });
  if (count === 0) return { icon: "mdi:help-circle-outline", label: "Not enough data", color: "var(--secondary-text-color)" };
  const avgDelta = totalDelta / count;
  if (avgDelta < -2)  return { icon: "mdi:arrow-up-bold-circle", label: "Increase irrigation", color: "#e53935" };
  if (avgDelta > 2)   return { icon: "mdi:arrow-down-bold-circle", label: "Decrease irrigation", color: "#1976d2" };
  return { icon: "mdi:check-circle", label: "On track", color: "#43a047" };
}

function _conditionIcon(condition) {
  return CONDITION_ICON[condition] || "mdi:weather-partly-cloudy";
}

// ─── Suggestion rendering helper ─────────────────────────────────────────────

function _renderSuggestionContent(sug, enabledZones) {
  if (!sug) return "";
  const zoneCards = enabledZones.map(z => {
    const r = sug.zones.find(x => x.zone === z);
    if (!r || r.duration == null) {
      return `<div class="zone-suggestion not-configured">
        <div class="zs-label">Zone ${z}</div>
        <div class="zs-duration">—</div>
        <div class="zs-sub">Not configured</div>
      </div>`;
    }
    return `<div class="zone-suggestion">
      <div class="zs-label">Zone ${z}</div>
      <div class="zs-duration">${_formatDuration(r.duration)}</div>
      <div class="zs-sub">${r.etcMm.toFixed(1)} mm ETc</div>
    </div>`;
  }).join("");

  return `
    <div class="suggestion-header">
      <span class="eto-info">
        ETo <span class="eto-value">${sug.eto.toFixed(2)} mm/day</span>
        &nbsp;·&nbsp; ${sug.method}
        ${sug.rainToday ? `&nbsp;·&nbsp; Rain today: ${sug.rainToday.toFixed(1)} mm` : ""}
      </span>
    </div>
    <div class="suggestion-grid">${zoneCards}</div>`;
}

// ─── Styles ───────────────────────────────────────────────────────────────────

const STYLES = `
  :host { display: block; }
  .panel-wrap {
    display: flex; flex-direction: column; height: 100%;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
    font-size: 14px;
  }

  /* header */
  .panel-header {
    display: flex; align-items: center; gap: 12px;
    padding: 0 16px; height: 56px;
    background: var(--app-header-background-color, var(--primary-color));
    color: var(--app-header-text-color, #fff);
    flex-shrink: 0; box-shadow: 0 2px 4px rgba(0,0,0,.2);
  }
  .panel-title { font-size: 20px; font-weight: 400; flex: 1; }
  .header-btn {
    background: none; border: none; cursor: pointer; color: inherit;
    padding: 6px; border-radius: 50%; display: flex; align-items: center; opacity: .85;
  }
  .header-btn:hover { opacity: 1; background: rgba(255,255,255,.12); }

  /* scroll body */
  .panel-body { flex: 1; overflow-y: auto; padding: 16px; box-sizing: border-box; }

  /* device tabs */
  .tabs {
    display: flex; border-bottom: 2px solid var(--divider-color);
    margin-bottom: 16px; overflow-x: auto;
  }
  .tab-btn {
    padding: 10px 20px; border: none; border-bottom: 3px solid transparent;
    background: none; color: var(--secondary-text-color); font-size: 14px;
    font-weight: 500; cursor: pointer; white-space: nowrap; margin-bottom: -2px;
    transition: color .15s, border-color .15s;
  }
  .tab-btn:hover { color: var(--primary-text-color); }
  .tab-btn.active { color: var(--primary-color); border-bottom-color: var(--primary-color); }

  /* inner tabs */
  .inner-tabs {
    display: flex; margin-bottom: 16px;
    background: var(--secondary-background-color); border-radius: 8px; padding: 4px;
  }
  .inner-tab-btn {
    flex: 1; padding: 8px 12px; border: none; border-radius: 6px;
    background: none; color: var(--secondary-text-color);
    font-size: 13px; font-weight: 500; cursor: pointer; transition: background .15s, color .15s;
  }
  .inner-tab-btn:hover { color: var(--primary-text-color); }
  .inner-tab-btn.active {
    background: var(--primary-background-color); color: var(--primary-color);
    box-shadow: 0 1px 3px rgba(0,0,0,.15);
  }

  /* cards */
  ha-card { display: block; margin-bottom: 16px; }
  .card-content { padding: 16px; }
  .card-title {
    font-size: 13px; font-weight: 600; letter-spacing: .06em;
    text-transform: uppercase; color: var(--secondary-text-color); margin: 0 0 12px;
  }
  .card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
  }
  .card-head .card-title { margin: 0; }
  .icon-btn {
    width: 32px;
    height: 32px;
    border: none;
    border-radius: 50%;
    background: var(--secondary-background-color);
    color: var(--primary-color);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: background .15s, transform .15s;
  }
  .icon-btn:hover { background: color-mix(in srgb, var(--primary-color) 16%, var(--secondary-background-color)); }
  .icon-btn:active { transform: scale(.96); }
  .icon-btn ha-icon { --mdc-icon-size: 18px; }

  /* status chips */
  .status-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px,1fr)); gap: 12px; }
  .status-chip { background: var(--secondary-background-color); border-radius: 8px; padding: 10px 14px; }
  .status-chip .chip-label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--secondary-text-color); margin-bottom: 4px; }
  .status-chip .chip-value { font-size: 18px; font-weight: 500; }

  /* schedules table */
  .sched-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .sched-table th {
    text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--divider-color);
    color: var(--secondary-text-color); font-weight: 600; font-size: 12px;
    letter-spacing: .04em; text-transform: uppercase;
  }
  .sched-table td { padding: 8px; border-bottom: 1px solid var(--divider-color); vertical-align: middle; }
  .sched-table tr:last-child td { border-bottom: none; }
  .sched-status-cell {
    display: flex;
    align-items: center;
    min-height: 28px;
  }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 11px; font-weight: 600; background: var(--primary-color); color: #fff;
  }
  .badge.prog-b { background: #43a047; }
  .badge.prog-c { background: #fb8c00; }
  .badge.prog-d { background: #8e24aa; }

  /* suggestion */
  .suggestion-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .eto-info { font-size: 13px; color: var(--secondary-text-color); }
  .eto-value { font-weight: 600; color: var(--primary-text-color); }
  .suggestion-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px,1fr)); gap: 10px; margin-bottom: 14px; }
  .zone-suggestion { background: var(--secondary-background-color); border-radius: 8px; padding: 12px; text-align: center; }
  .zone-suggestion .zs-label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--secondary-text-color); margin-bottom: 6px; }
  .zone-suggestion .zs-duration { font-size: 22px; font-weight: 600; color: var(--primary-color); }
  .zone-suggestion .zs-sub { font-size: 11px; color: var(--secondary-text-color); margin-top: 3px; }
  .zone-suggestion.not-configured { opacity: .45; }

  /* zone config */
  .zone-config-section { margin-bottom: 20px; }
  .zone-fields-grid {
    display: grid;
    grid-template-columns: min-content repeat(auto-fill, minmax(160px,1fr));
    gap: 12px; align-items: end;
  }
  .field-group { display: flex; flex-direction: column; gap: 4px; }
  .field-label { font-size: 12px; color: var(--secondary-text-color); letter-spacing: .03em; height: 18px; display: flex; align-items: center; }
  .field-input, .field-select {
    padding: 8px 10px; border: 1px solid var(--divider-color); border-radius: 6px;
    background: var(--primary-background-color); color: var(--primary-text-color);
    font-size: 14px; width: 100%; box-sizing: border-box;
    transition: border-color .15s; height: 38px;
  }
  .field-input:focus, .field-select:focus { outline: none; border-color: var(--primary-color); }
  .zone-number-badge {
    padding: 0 14px; border-radius: 6px; background: var(--primary-color); color: #fff;
    font-weight: 600; font-size: 13px; display: flex; align-items: center;
    justify-content: center; white-space: nowrap; box-sizing: border-box; height: 38px;
  }

  /* entity pickers */
  .entity-pickers { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px,1fr)); gap: 14px; margin-bottom: 14px; }
  .entity-select {
    padding: 4px 6px; border: 1px solid var(--divider-color); border-radius: 6px;
    background: var(--primary-background-color); color: var(--primary-text-color);
    font-size: 13px; width: 100%; box-sizing: border-box; cursor: pointer;
    transition: border-color .15s;
  }
  .entity-select:focus { outline: none; border-color: var(--primary-color); }

  /* buttons */
  .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
  .btn { padding: 8px 18px; border: none; border-radius: 6px; font-size: 14px; font-weight: 500; cursor: pointer; transition: opacity .15s; }
  .btn-primary { background: var(--primary-color); color: #fff; }
  .btn-primary:hover { opacity: .88; }

  /* misc */
  .divider { border: none; border-top: 1px solid var(--divider-color); margin: 16px 0; }
  .empty-state { text-align: center; padding: 48px 16px; color: var(--secondary-text-color); font-size: 15px; }
  .empty-state ha-icon { --mdc-icon-size: 48px; opacity: .4; display: block; margin: 0 auto 12px; }
  .info-row { font-size: 12px; color: var(--secondary-text-color); margin-top: 8px; }
  .spinner-wrap { display: flex; justify-content: center; padding: 48px; }
  .error-state { background: var(--error-color,#db4437); color:#fff; border-radius:8px; padding:12px; font-size:13px; margin-bottom:12px; }
  .config-save-bar { position: sticky; bottom: 0; padding: 12px 0 4px; background: var(--primary-background-color); }

  /* ── Calendar ── */
  .trend-bar {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px; border-radius: 8px; margin-bottom: 16px;
    background: var(--secondary-background-color);
  }
  .trend-bar .trend-label { font-size: 14px; font-weight: 500; }

  /* horizontal strip (desktop) */
  .day-strip-wrap { margin-bottom: 16px; }
  .day-strip { display: flex; gap: 6px; }
  .day-card {
    display: flex; flex-direction: column; align-items: center;
    flex: 1; min-width: 0; padding: 8px 4px; border-radius: 8px; cursor: pointer;
    background: var(--secondary-background-color);
    border: 2px solid transparent; transition: border-color .15s, background .15s;
    gap: 3px; user-select: none;
  }
  .day-card:hover { background: var(--primary-background-color); }
  .day-card.selected { border-color: var(--primary-color); }
  .day-card.today { background: color-mix(in srgb, var(--primary-color) 10%, var(--secondary-background-color)); }
  .day-card.future { opacity: .85; }
  .day-card.missing { opacity: .5; }
  .dc-weekday { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--secondary-text-color); }
  .dc-date { font-size: 18px; font-weight: 600; line-height: 1; }
  .dc-month { font-size: 10px; color: var(--secondary-text-color); }
  .dc-weather { display: flex; align-items: center; gap: 3px; font-size: 12px; }
  .dc-rain { font-size: 11px; color: #1976d2; min-height: 14px; }
  .dc-alert { font-size: 13px; margin-top: 2px; }

  /* day detail panel */
  .day-detail {
    border-radius: 10px; padding: 14px 16px;
    background: var(--secondary-background-color); margin-bottom: 16px;
  }
  .dd-header {
    display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
  }
  .dd-date { font-size: 15px; font-weight: 600; flex: 1; }
  .dd-source {
    font-size: 11px; padding: 2px 8px; border-radius: 12px;
    background: var(--primary-color); color: #fff;
  }
  .dd-source.recorded   { background: #43a047; }
  .dd-source.background { background: #388e3c; }
  .dd-source.backfill   { background: #78909c; }
  .dd-source.live       { background: #7b1fa2; }
  .dd-source.forecast   { background: #1976d2; }
  .dd-weather { font-size: 13px; color: var(--secondary-text-color); margin-bottom: 12px; }
  .dd-zones { display: flex; flex-direction: column; gap: 8px; }
  .dd-zone-row { display: grid; grid-template-columns: 60px 1fr auto; align-items: center; gap: 10px; font-size: 13px; }
  .dd-zone-label { font-weight: 600; }
  .dd-zone-bar-wrap { position: relative; height: 8px; border-radius: 4px; background: var(--divider-color); overflow: hidden; }
  .dd-zone-bar-sug { position: absolute; top: 0; height: 100%; background: rgba(var(--rgb-primary-color,3,169,244),.25); border-radius: 4px; }
  .dd-zone-bar-actual { position: absolute; top: 0; left: 0; height: 100%; border-radius: 4px; }
  .dd-zone-info { font-size: 12px; white-space: nowrap; text-align: right; }

  /* vertical list (mobile) */
  .day-list { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; }
  .day-list-item {
    border-radius: 8px; padding: 10px 12px;
    background: var(--secondary-background-color);
    border-left: 4px solid transparent;
  }
  .day-list-item.today { border-left-color: var(--primary-color); }
  .day-list-item.future { opacity: .85; }
  .dli-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .dli-date { font-weight: 600; font-size: 13px; flex: 1; }
  .dli-weather { font-size: 12px; color: var(--secondary-text-color); }
  .dli-zones { display: flex; flex-direction: column; gap: 5px; }
  .dli-zone { display: grid; grid-template-columns: 50px 1fr auto; align-items: center; gap: 8px; font-size: 12px; }
`;

// ─── Panel Component ──────────────────────────────────────────────────────────

class RainVisionHaPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass   = null;
    this._panel  = null;
    this._tab    = 0;
    this._innerTab = 0; // 0=Overview 1=Calendar 2=Configuration
    this._devices        = [];
    this._advisorConfigs = {};
    this._suggestions    = {};
    this._suggestionLoading = {};
    this._calendarData   = {}; // entry_id → { days: { "YYYY-MM-DD": snapshot } }
    this._selectedDay    = _todayISO();
    this._loading  = true;
    this._inited   = false;
    this._narrow   = false;
    this._currentDev    = null;
    this._currentConfig = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._initialize();
  }

  set panel(panel) { this._panel = panel; }
  set narrow(narrow) {
    const was = this._narrow;
    this._narrow = !!narrow;
    if (this._narrow !== was && this._inited) this._render();
  }

  connectedCallback() { if (!this._hass) this._renderLoading(); }

  // ── Init ────────────────────────────────────────────────────────────────────

  async _initialize() {
    this._renderLoading();
    try {
      const res = await this._ws({ type: "rain_vision_ha/get_devices" });
      this._devices = res.devices || [];
      for (const dev of this._devices) await this._fetchAdvisorConfig(dev.entry_id);
    } catch (e) { console.error("[RainVision] init error", e); }
    this._loading = false;
    this._inited  = true;
    this._render();
  }

  async _fetchAdvisorConfig(entryId) {
    try {
      const res = await this._ws({ type: "rain_vision_ha/get_advisor_config", entry_id: entryId });
      this._advisorConfigs[entryId] = res;
    } catch (_) {
      this._advisorConfigs[entryId] = { zones: {}, weather_entities: {}, forecast_entity: null };
    }
  }

  async _refresh() {
    const res = await this._ws({ type: "rain_vision_ha/get_devices" });
    this._devices = res.devices || [];
    this._render();
  }

  // ── Rendering ────────────────────────────────────────────────────────────────

  _renderLoading() {
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="panel-wrap">
        ${this._renderHeader()}
        <div class="panel-body"><div class="spinner-wrap"><ha-circular-progress active></ha-circular-progress></div></div>
      </div>`;
  }

  _render() {
    try {
      const devs = Array.isArray(this._devices) ? this._devices : [];
      if (this._tab >= devs.length && devs.length > 0) this._tab = 0;

      const dev    = devs[this._tab] || null;
      const config = dev ? this._normalizeConfig(this._advisorConfigs[dev.entry_id]) : null;
      this._currentDev    = dev;
      this._currentConfig = config;

      this.shadowRoot.innerHTML = `
        <style>${STYLES}</style>
        <div class="panel-wrap">
          ${this._renderHeader()}
          <div class="panel-body">
            ${devs.length === 0 ? this._renderEmpty() : ""}
            ${devs.length > 0  ? this._renderTabs(devs) : ""}
            ${dev ? this._renderDevice(dev, config) : ""}
          </div>
        </div>`;

      this._attachListeners();
      this._populateForms(dev, config);
    } catch (e) {
      console.error("[RainVision] render error", e);
      this.shadowRoot.innerHTML = `
        <style>${STYLES}</style>
        <div class="panel-wrap">
          ${this._renderHeader()}
          <div class="panel-body">${this._renderError(e)}</div>
        </div>`;
      this._attachListeners();
    }
  }

  _renderHeader() {
    const menuBtn = this._narrow
      ? `<button class="header-btn" id="btn-menu" aria-label="Open menu">
           <ha-icon icon="mdi:menu"></ha-icon>
         </button>`
      : "";
    return `
      <div class="panel-header">
        ${menuBtn}
        <div class="panel-title">Rain Vision</div>
      </div>`;
  }

  _renderError(err) {
    return `<div class="error-state">Panel error: ${err && err.message ? err.message : err}</div>`;
  }

  _renderEmpty() {
    return `<div class="empty-state"><ha-icon icon="mdi:water-off"></ha-icon>No Rain Vision devices configured.<br>Add one via Settings → Integrations.</div>`;
  }

  _renderTabs(devs) {
    return `
      <div class="tabs">
        ${devs.map((d, i) => `
          <button class="tab-btn${i === this._tab ? " active" : ""}" data-tab="${i}">
            ${d.title || d.entry_id || `Device ${i+1}`}
          </button>`).join("")}
      </div>`;
  }

  _renderDevice(dev, config) {
    const t = this._innerTab;
    return `
      <div class="inner-tabs">
        <button class="inner-tab-btn${t===0?" active":""}" data-inner-tab="0">Overview</button>
        <button class="inner-tab-btn${t===1?" active":""}" data-inner-tab="1">Calendar</button>
        <button class="inner-tab-btn${t===2?" active":""}" data-inner-tab="2">Configuration</button>
      </div>
      ${t===0 ? this._renderOverviewTab(dev, config) : ""}
      ${t===1 ? this._renderCalendarTab(dev) : ""}
      ${t===2 ? this._renderConfigTab(dev, config) : ""}
    `;
  }

  // ── Overview tab ─────────────────────────────────────────────────────────────

  _renderOverviewTab(dev, config) {
    return `
      ${this._renderStatusCard(dev)}
      ${this._renderSchedulesCard(dev)}
      ${this._renderAdvisorCard(dev, config)}
    `;
  }

  // ── Calendar tab ─────────────────────────────────────────────────────────────

  _renderCalendarTab(dev) {
    return `
      <div id="calendar-content-${dev.entry_id}">
        <div class="spinner-wrap"><ha-circular-progress active></ha-circular-progress></div>
      </div>`;
  }

  async _loadAndRenderCalendar(dev, config) {
    const eid  = dev.entry_id;
    const container = this.shadowRoot.getElementById(`calendar-content-${eid}`);
    if (!container) return;

    try {
      const lat  = this._hass.config.latitude;
      const elev = this._hass.config.elevation || 0;
      const today = _todayISO();

      // 1. Fetch snapshots saved by the background service
      const calRes   = await this._ws({ type: "rain_vision_ha/get_calendar", entry_id: eid });
      const storedRaw = calRes.days || {};

      // 2. Normalise: convert the background format (irrigation + raw weather) to the
      //    rendering format (zones.{actual,suggested} + weather.eto).
      //    Old frontend-saved snapshots already have that shape and pass through unchanged.
      const storedDays = {};
      for (const [d, data] of Object.entries(storedRaw)) {
        storedDays[d] = this._normalizeStoredDay(data, config, dev, lat, elev);
      }

      // 3. Compute today's data live from sensor history (not saved — the background
      //    service will persist the authoritative end-of-day snapshot at 23:58).
      const days = { ...storedDays };
      const todayLive = await this._computeTodayLive(dev, config, lat, elev);
      if (todayLive) days[today] = todayLive;

      // 4. Fetch forecast for future days
      let forecastDays = {};
      if (config.forecast_entity) {
        try {
          const fcRes = await this._ws({ type: "rain_vision_ha/get_forecast", entity_id: config.forecast_entity });
          (fcRes.forecast || []).forEach(fc => {
            const d = fc.datetime ? fc.datetime.split("T")[0] : null;
            if (d && d >= today) forecastDays[d] = this._mapForecastDay(fc, lat);
          });
        } catch (e) { console.warn("[RainVision] forecast fetch failed", e); }
      }

      this._calendarData[eid] = { days, forecastDays };
      container.innerHTML = this._renderCalendarContent(dev, config, days, forecastDays);
      this._attachCalendarListeners(dev, config, days, forecastDays);
    } catch (e) {
      console.error("[RainVision] calendar load error", e);
      if (container) container.innerHTML = `<div class="error-state">Calendar error: ${e.message || e}</div>`;
    }
  }

  _mapForecastDay(fc, lat) {
    const tMax  = fc.temperature ?? null;
    const tMin  = fc.templow ?? null;
    const tMean = (tMax != null && tMin != null) ? (tMax + tMin) / 2 : (tMax ?? tMin);
    const condition  = fc.condition || "partlycloudy";
    const solarEst = CONDITION_SOLAR[condition] ?? 300;
    const windKmh  = fc.wind_speed ?? null;
    const windMs   = windKmh != null ? windKmh / 3.6 : null;
    const humidity = fc.humidity ?? null;
    const rain     = fc.precipitation ?? 0;

    const etoResult = calculateETo({ lat, tMean, tMin, tMax, humidity, solarRadWm2: solarEst, windSpeedMs: windMs });

    return { tMean, tMin, tMax, humidity, rain, condition, eto: etoResult.value, source: "forecast" };
  }

  // Compute today's snapshot from live sensor history without saving — the background
  // service (advisor.py) persists the authoritative end-of-day record at 23:58.
  async _computeTodayLive(dev, config, lat, elev) {
    const we = config.weather_entities || {};
    if (!we.temperature?.length) return null;

    const [tempStats, humStats, solarStats, windStats] = await Promise.all([
      this._fetchDailyStats(we.temperature),
      this._fetchDailyStats(we.humidity),
      this._fetchDailyStats(we.solar_radiation),
      this._fetchDailyStats(we.wind_speed),
    ]);

    const tMeanCur = _stateNums(this._hass, we.temperature);
    const tMean    = tempStats.mean ?? tMeanCur;
    if (tMean == null) return null;

    const rainToday = _stateNums(this._hass, we.rain_today) ?? 0;

    const etoResult = calculateETo({
      lat, elevation: elev,
      tMean, tMin: tempStats.min, tMax: tempStats.max,
      humidity:    humStats.mean  ?? _stateNums(this._hass, we.humidity),
      solarRadWm2: solarStats.mean ?? _stateNums(this._hass, we.solar_radiation),
      windSpeedMs: windStats.mean  ?? _stateNums(this._hass, we.wind_speed),
    });

    const zones = {};
    dev.enabled_zones.forEach(z => {
      const zk   = String(z);
      const zc   = (config.zones || {})[zk];
      const grass = zc?.grass === "cool_lawn" || zc?.grass === "warm_lawn" ? "lawn" : zc?.grass;
      let actual  = 0;
      dev.schedules.forEach(s => {
        if (s.active && Array.isArray(s.zone_durations) && s.zone_durations[z - 1] > 0) {
          actual += Math.round(s.zone_durations[z - 1] / 60);
        }
      });
      const suggested = zc?.area && zc?.flow_rate
        ? calculateZoneDuration(etoResult.value, { ...zc, grass }, rainToday, lat)
        : null;
      zones[zk] = { actual, suggested };
    });

    return {
      weather: {
        tMean, tMin: tempStats.min, tMax: tempStats.max,
        humidity: humStats.mean, rain: rainToday, eto: etoResult.value,
      },
      zones,
      source: "live",
    };
  }

  // Convert a background-saved snapshot (irrigation + raw weather stats) to the
  // rendering format (zones.{actual,suggested} + weather.eto).
  // Old frontend-saved snapshots already have the rendering format and pass through.
  _normalizeStoredDay(data, config, dev, lat, elev) {
    if (!data) return null;

    // New background format: has 'irrigation' instead of 'zones'
    if (data.irrigation !== undefined) {
      const w = data.weather || {};
      const etoResult = calculateETo({
        lat, elevation: elev,
        tMean: w.tMean, tMin: w.tMin, tMax: w.tMax,
        humidity:    w.humidity,
        solarRadWm2: w.solar,
        windSpeedMs: w.wind,
      });
      const eto  = etoResult.value;
      const rain = w.rain ?? 0;
      const zones = {};
      dev.enabled_zones.forEach(z => {
        const zk    = String(z);
        const zc    = (config.zones || {})[zk];
        const grass = zc?.grass === "cool_lawn" || zc?.grass === "warm_lawn" ? "lawn" : zc?.grass;
        const actual = data.irrigation[zk] != null ? Number(data.irrigation[zk]) : 0;
        const suggested = zc?.area && zc?.flow_rate
          ? calculateZoneDuration(eto, { ...zc, grass }, rain, lat)
          : null;
        zones[zk] = { actual, suggested };
      });
      return { weather: { ...w, eto }, zones, source: data.source || "background" };
    }

    // Old recorded format: already has zones + weather.eto — pass through unchanged
    return data;
  }

  _renderCalendarContent(dev, config, storedDays, forecastDays) {
    const today = _todayISO();
    const window = _calendarWindow();
    const enabledZones = dev.enabled_zones;

    // Build unified day data for the window
    const dayData = window.map(date => {
      const stored   = storedDays[date];
      const forecast = forecastDays[date];
      const isPast   = date < today;
      const isToday  = date === today;
      const data     = stored || (forecast ? { weather: forecast, zones: this._computeSuggestedZones(forecast, config, dev, this._hass.config.latitude), source: "forecast" } : null);
      return { date, isPast, isToday, isFuture: date > today, data };
    });

    // Trend from past 7 days
    const pastData = dayData.filter(d => d.isPast && d.data).map(d => d.data);
    const trend = _trendIcon(pastData, enabledZones);

    const selectedDay = this._selectedDay || today;

    // Trend bar
    const trendHtml = `
      <div class="trend-bar">
        <ha-icon icon="${trend.icon}" style="--mdc-icon-size:22px;color:${trend.color}"></ha-icon>
        <span class="trend-label" style="color:${trend.color}">${trend.label}</span>
        <span style="font-size:12px;color:var(--secondary-text-color);margin-left:auto">past 7 days</span>
      </div>`;

    let calHtml;
    if (this._narrow) {
      calHtml = this._renderDayList(dayData, enabledZones, selectedDay);
    } else {
      calHtml = this._renderDayStrip(dayData, enabledZones, selectedDay);
      const selected = dayData.find(d => d.date === selectedDay);
      calHtml += this._renderDayDetail(selected, enabledZones);
    }

    const noForecastWarning = !config.forecast_entity
      ? `<div class="info-row" style="margin-bottom:12px">⚠ No weather forecast entity configured — future suggestions unavailable. Add one in Configuration.</div>`
      : "";

    return trendHtml + noForecastWarning + calHtml;
  }

  _computeSuggestedZones(weather, config, dev, lat) {
    const zones = {};
    dev.enabled_zones.forEach(z => {
      const zk = String(z);
      const zc = (config.zones || {})[zk];
      const grass = zc?.grass === "cool_lawn" || zc?.grass === "warm_lawn" ? "lawn" : zc?.grass;
      const suggested = zc?.area && zc?.flow_rate
        ? calculateZoneDuration(weather.eto, { ...zc, grass }, weather.rain ?? 0, lat)
        : null;
      zones[zk] = { actual: null, suggested };
    });
    return zones;
  }

  _renderDayStrip(dayData, enabledZones, selectedDay) {
    const today = _todayISO();
    const cards = dayData.map(({ date, isToday, isFuture, data }) => {
      const w       = data?.weather;
      const icon    = w?.condition ? _conditionIcon(w.condition) : (isFuture ? "mdi:calendar-clock" : "mdi:calendar-minus");
      const tMaxStr = w?.tMax != null ? `${Math.round(w.tMax)}°` : "—";
      const rainStr = (w?.rain != null && w.rain > 0) ? `${w.rain.toFixed(0)}mm` : "";

      // Zone alert summary for the day
      const zones   = data?.zones || {};
      const alerts  = enabledZones.map(z => {
        const zd = zones[String(z)];
        if (!zd) return "unknown";
        return _alertStatus(zd.actual, zd.suggested);
      });
      const hasOver  = alerts.includes("over");
      const hasUnder = alerts.includes("under");
      const alertIcon = hasOver ? "🔴" : hasUnder ? "🟡" : alerts.every(a => a === "ok") ? "🟢" : "⚪";

      const classes = [
        "day-card",
        isToday  ? "today"   : "",
        isFuture ? "future"  : "",
        !data    ? "missing" : "",
        date === selectedDay ? "selected" : "",
      ].filter(Boolean).join(" ");

      return `
        <div class="${classes}" data-cal-day="${date}">
          <div class="dc-weekday">${_dayLabel(date)}</div>
          <div class="dc-date">${_dayNum(date)}</div>
          <div class="dc-month">${_monthShort(date)}</div>
          <ha-icon icon="${icon}" style="--mdc-icon-size:18px;color:var(--secondary-text-color)"></ha-icon>
          <div class="dc-weather">${tMaxStr}</div>
          ${rainStr ? `<div class="dc-rain">${rainStr}</div>` : `<div class="dc-rain"></div>`}
          <div class="dc-alert">${alertIcon}</div>
        </div>`;
    }).join("");

    return `<div class="day-strip-wrap"><div class="day-strip">${cards}</div></div>`;
  }

  _renderDayDetail(dayEntry, enabledZones) {
    if (!dayEntry) return "";
    const { date, isToday, isFuture, data } = dayEntry;

    if (!data) {
      // Past days with no data: no detail panel (empty state)
      if (!isFuture) return "";
      // Future days: hint about forecast entity
      return `<div class="day-detail"><div style="color:var(--secondary-text-color);font-size:13px">No forecast data for ${date} — configure a weather forecast entity in Configuration.</div></div>`;
    }

    const w  = data.weather || {};
    const d  = new Date(date + "T12:00:00");
    const dateLabel = d.toLocaleDateString(undefined, { weekday:"long", month:"long", day:"numeric" });
    const iconName  = _conditionIcon(w.condition);
    const sourceLabel = data.source || "recorded";

    const tempStr = [
      w.tMax != null ? `${Math.round(w.tMax)}°↑` : null,
      w.tMin != null ? `${Math.round(w.tMin)}°↓` : null,
    ].filter(Boolean).join(" ");
    const rainStr    = (w.rain != null && w.rain > 0) ? `💧 ${w.rain.toFixed(1)} mm` : "";
    const etoStr     = w.eto != null ? `ETo ${w.eto.toFixed(2)} mm/day` : "";

    const zones = data.zones || {};
    const zoneRows = enabledZones.map(z => {
      const zd = zones[String(z)];
      if (!zd) return "";
      const { actual, suggested } = zd;
      const alert      = _alertStatus(actual, suggested);
      const alertEmoji = alert === "over" ? "🔴" : alert === "under" ? "🟡" : alert === "ok" ? "🟢" : "⚪";
      const max        = Math.max(actual || 0, suggested || 0, 1);
      const actPct     = actual    != null ? Math.min(100, (actual    / max) * 100) : 0;
      const sugPct     = suggested != null ? Math.min(100, (suggested / max) * 100) : 0;
      const barColor   = alert === "over" ? "#e53935" : alert === "under" ? "#ff9800" : "#43a047";
      const actualStr  = actual    != null ? `${actual} min` : (isFuture ? "—" : "—");
      const sugStr     = suggested != null ? `suggest ${suggested} min` : "";

      return `
        <div class="dd-zone-row">
          <span class="dd-zone-label">Zone ${z} ${alertEmoji}</span>
          <div class="dd-zone-bar-wrap">
            <div class="dd-zone-bar-sug" style="left:0%;width:${sugPct}%"></div>
            <div class="dd-zone-bar-actual" style="width:${actPct}%;background:${barColor}"></div>
          </div>
          <span class="dd-zone-info">${[actualStr, sugStr].filter(Boolean).join(" · ")}</span>
        </div>`;
    }).join("");

    return `
      <div class="day-detail">
        <div class="dd-header">
          <ha-icon icon="${iconName}" style="--mdc-icon-size:20px"></ha-icon>
          <span class="dd-date">${dateLabel}</span>
          <span class="dd-source ${sourceLabel}">${sourceLabel}</span>
        </div>
        <div class="dd-weather">${[tempStr, rainStr, etoStr].filter(Boolean).join(" &nbsp;·&nbsp; ")}</div>
        <div class="dd-zones">${zoneRows || `<span style="color:var(--secondary-text-color);font-size:12px">No zones configured</span>`}</div>
      </div>`;
  }

  _renderDayList(dayData, enabledZones, selectedDay) {
    const today = _todayISO();
    return `<div class="day-list">` + dayData.map(({ date, isToday, isFuture, data }) => {
      const w = data?.weather;
      const icon = _conditionIcon(w?.condition);
      const classes = ["day-list-item", isToday?"today":"", isFuture?"future":""].filter(Boolean).join(" ");
      const d = new Date(date + "T12:00:00");
      const dateLabel = d.toLocaleDateString(undefined, { weekday:"short", month:"short", day:"numeric" });
      const tempStr = [w?.tMax != null ? `${Math.round(w.tMax)}°↑` : null, w?.tMin != null ? `${Math.round(w.tMin)}°↓` : null].filter(Boolean).join(" ");
      const rainStr = (w?.rain != null && w.rain > 0) ? `💧${w.rain.toFixed(0)}mm` : "";

      const zones = data?.zones || {};
      const zoneRows = enabledZones.map(z => {
        const zd = zones[String(z)];
        if (!zd) return "";
        const alert    = _alertStatus(zd.actual, zd.suggested);
        const emoji    = alert === "over" ? "🔴" : alert === "under" ? "🟡" : alert === "ok" ? "🟢" : "⚪";
        const max      = Math.max(zd.actual||0, zd.suggested||0, 1);
        const actPct   = zd.actual    != null ? Math.min(100,(zd.actual    /max)*100) : 0;
        const sugPct   = zd.suggested != null ? Math.min(100,(zd.suggested /max)*100) : 0;
        const barColor = alert==="over"?"#e53935":alert==="under"?"#ff9800":"#43a047";
        return `
          <div class="dli-zone">
            <span>Z${z} ${emoji}</span>
            <div class="dd-zone-bar-wrap">
              <div class="dd-zone-bar-sug" style="left:0%;width:${sugPct}%"></div>
              <div class="dd-zone-bar-actual" style="width:${actPct}%;background:${barColor}"></div>
            </div>
            <span>${zd.actual!=null?zd.actual+" min":"—"}</span>
          </div>`;
      }).join("");

      return `
        <div class="${classes}">
          <div class="dli-header">
            <ha-icon icon="${icon}" style="--mdc-icon-size:16px"></ha-icon>
            <span class="dli-date">${dateLabel}${isToday?" · Today":""}</span>
            <span class="dli-weather">${[tempStr,rainStr].filter(Boolean).join(" ")}</span>
          </div>
          <div class="dli-zones">${zoneRows || `<span style="color:var(--secondary-text-color);font-size:12px">No data</span>`}</div>
        </div>`;
    }).join("") + `</div>`;
  }

  _attachCalendarListeners(dev, config, storedDays, forecastDays) {
    const root = this.shadowRoot;
    const today = _todayISO();
    const window = _calendarWindow();
    const enabledZones = dev.enabled_zones;

    root.querySelectorAll(".day-card[data-cal-day]").forEach(card => {
      card.addEventListener("click", () => {
        this._selectedDay = card.dataset.calDay;
        // Update selection highlight
        root.querySelectorAll(".day-card").forEach(c => c.classList.remove("selected"));
        card.classList.add("selected");
        // Update detail panel
        const container = root.getElementById(`calendar-content-${dev.entry_id}`);
        if (!container) return;
        const dayData = window.map(date => {
          const stored   = storedDays[date];
          const forecast = forecastDays[date];
          const data = stored || (forecast ? { weather: forecast, zones: this._computeSuggestedZones(forecast, config, dev, this._hass.config.latitude), source: "forecast" } : null);
          return { date, isPast: date<today, isToday: date===today, isFuture: date>today, data };
        });
        const selected = dayData.find(d => d.date === this._selectedDay);
        const detailEl = container.querySelector(".day-detail");
        if (detailEl) detailEl.outerHTML = this._renderDayDetail(selected, enabledZones);
        else container.insertAdjacentHTML("beforeend", this._renderDayDetail(selected, enabledZones));
      });
    });
  }

  // ── Configuration tab ─────────────────────────────────────────────────────────

  _renderConfigTab(dev, config) {
    const enabledZones = Array.isArray(dev.enabled_zones) ? dev.enabled_zones : [];

    const zones = enabledZones.map(z => {
      const zk = String(z);
      const zc = config.zones[zk] || {};
      return `
        <div class="zone-config-section">
          <div class="zone-fields-grid">
            <div class="field-group">
              <span class="field-label">&nbsp;</span>
              <div class="zone-number-badge">Zone ${z}</div>
            </div>
            <div class="field-group">
              <label class="field-label">Area (m²)</label>
              <input class="field-input" type="number" min="0.1" step="0.1" data-zone="${zk}" data-field="area">
            </div>
            <div class="field-group">
              <label class="field-label">Flow rate (L/min)</label>
              <input class="field-input" type="number" min="0.1" step="0.1" data-zone="${zk}" data-field="flow_rate">
            </div>
            <div class="field-group">
              <label class="field-label">Exposure</label>
              <select class="field-select" data-zone="${zk}" data-field="exposure">
                ${_selectOptions(EXPOSURE, zc.exposure || "")}
              </select>
            </div>
            <div class="field-group">
              <label class="field-label">Soil type</label>
              <select class="field-select" data-zone="${zk}" data-field="soil">
                ${_selectOptions(SOIL, zc.soil || "")}
              </select>
            </div>
            <div class="field-group">
              <label class="field-label">Plant / Grass type</label>
              <select class="field-select" data-zone="${zk}" data-field="grass">
                ${_selectOptions(GRASS, zc.grass || "")}
              </select>
            </div>
            <div class="field-group">
              <label class="field-label">Irrigation type</label>
              <select class="field-select" data-zone="${zk}" data-field="irrigation_type">
                ${_selectOptions(IRRIGATION_TYPE, zc.irrigation_type || "")}
              </select>
            </div>
          </div>
        </div>`;
    }).join(`<hr class="divider">`);

    const weatherPickers = WEATHER_KEYS.map(({ key, label, deviceClass }) => {
      const selected = config.weather_entities[key] || [];
      return `
        <div class="field-group">
          <label class="field-label">${label}</label>
          <select class="entity-select" multiple size="4" data-we-key="${key}" data-entry="${dev.entry_id}">
            ${this._entityPickerOptions(deviceClass, selected)}
          </select>
        </div>`;
    }).join("");

    const forecastOpts = this._weatherEntityOptions(config.forecast_entity);

    return `
      <ha-card>
        <div class="card-content">
          <div class="card-title">Zone Configuration</div>
          ${zones}
        </div>
      </ha-card>
      <ha-card>
        <div class="card-content">
          <div class="card-title">Weather Data Sources</div>
          <div class="entity-pickers">${weatherPickers}</div>
          <div class="info-row">Click to select one sensor, or hold Ctrl/Cmd for multiple. Values are averaged.</div>
        </div>
      </ha-card>
      <ha-card>
        <div class="card-content">
          <div class="card-title">Weather Forecast</div>
          <div class="field-group">
            <label class="field-label">Forecast entity (weather.*)</label>
            <select class="field-select" id="forecast-entity-${dev.entry_id}" style="height:auto">
              ${forecastOpts}
            </select>
          </div>
          <div class="info-row">Used in the Calendar tab to compute suggested irrigation for the upcoming 7 days.</div>
        </div>
      </ha-card>
      <div class="config-save-bar">
        <button class="btn btn-primary" id="btn-save-${dev.entry_id}" style="width:100%">
          <ha-icon icon="mdi:content-save" style="--mdc-icon-size:16px;vertical-align:middle;margin-right:6px"></ha-icon>
          Save Configuration
        </button>
      </div>
    `;
  }

  // ── Entity pickers ────────────────────────────────────────────────────────────

  _entityPickerOptions(deviceClass, selectedValues) {
    if (!this._hass) return `<option disabled>Loading…</option>`;
    const selected = Array.isArray(selectedValues) ? selectedValues : (selectedValues ? [selectedValues] : []);
    const entities = Object.values(this._hass.states)
      .filter(s => s.entity_id.startsWith("sensor.") && (deviceClass ? s.attributes.device_class === deviceClass : !isNaN(parseFloat(s.state))))
      .sort((a, b) => (a.attributes.friendly_name || a.entity_id).localeCompare(b.attributes.friendly_name || b.entity_id));
    if (!entities.length) return `<option disabled>No matching sensors</option>`;
    return entities.map(s => {
      const name = s.attributes.friendly_name || s.entity_id;
      return `<option value="${s.entity_id}"${selected.includes(s.entity_id) ? " selected" : ""}>${name}</option>`;
    }).join("");
  }

  _weatherEntityOptions(selectedValue) {
    if (!this._hass) return "";
    const placeholder = selectedValue
      ? `<option value="" disabled>— select —</option>`
      : `<option value="" selected disabled>— select a weather entity —</option>`;
    const entities = Object.values(this._hass.states)
      .filter(s => s.entity_id.startsWith("weather."))
      .sort((a, b) => (a.attributes.friendly_name || a.entity_id).localeCompare(b.attributes.friendly_name || b.entity_id));
    return placeholder + entities.map(s => {
      const name = s.attributes.friendly_name || s.entity_id;
      return `<option value="${s.entity_id}"${s.entity_id === selectedValue ? " selected" : ""}>${name}</option>`;
    }).join("");
  }

  // ── Status Card ─────────────────────────────────────────────────────────────

  _renderStatusCard(dev) {
    const s = dev.status;
    const statusIcon = { manual_watering:"mdi:water", scheduled_watering:"mdi:calendar-clock", idle:"mdi:check-circle", ready:"mdi:power", unknown:"mdi:help-circle" }[s.status_name] || "mdi:help-circle";
    const battPct  = s.battery != null ? `${s.battery}%` : "—";
    const battIcon = s.battery == null ? "mdi:battery-unknown" : s.battery>80 ? "mdi:battery" : s.battery>50 ? "mdi:battery-60" : s.battery>20 ? "mdi:battery-30" : "mdi:battery-alert";
    return `
      <ha-card><div class="card-content">
        <div class="card-title">Device Status</div>
        <div class="status-grid">
          <div class="status-chip"><div class="chip-label">Status</div><div class="chip-value"><ha-icon icon="${statusIcon}" style="--mdc-icon-size:18px;vertical-align:middle"></ha-icon> ${s.status_name.replace("_"," ")}</div></div>
          <div class="status-chip"><div class="chip-label">Battery</div><div class="chip-value"><ha-icon icon="${battIcon}" style="--mdc-icon-size:18px;vertical-align:middle"></ha-icon> ${battPct}</div></div>
          <div class="status-chip"><div class="chip-label">Next irrigation</div><div class="chip-value" style="font-size:14px">${_formatTime(s.next_irrigation)}</div></div>
          <div class="status-chip"><div class="chip-label">Enabled zones</div><div class="chip-value" style="font-size:14px">${dev.enabled_zones.join(", ")||"—"}</div></div>
          <div class="status-chip"><div class="chip-label">Firmware</div><div class="chip-value" style="font-size:14px">${s.firmware||"—"}</div></div>
          <div class="status-chip"><div class="chip-label">Connection</div><div class="chip-value" style="font-size:14px">${s.connection_state} / ${s.polling_mode}</div></div>
        </div>
      </div></ha-card>`;
  }

  // ── Schedules Card ───────────────────────────────────────────────────────────

  _renderSchedulesCard(dev) {
    const rows = dev.schedules.length === 0
      ? `<tr><td colspan="5" style="text-align:center;color:var(--secondary-text-color);padding:16px">No schedules configured</td></tr>`
      : dev.schedules.map(s => {
          const badgeCls  = `badge prog-${s.program_name.toLowerCase()}`;
          const rowStyle  = s.active ? "" : ' style="opacity:0.5"';
          const statusBadge = s.active
            ? `<span class="badge" style="background:#43a047;font-size:10px">Active</span>`
            : `<span class="badge" style="background:var(--secondary-text-color);font-size:10px">Disabled</span>`;
          return `<tr${rowStyle}>
            <td><span class="${badgeCls}">${s.program_name}</span></td>
            <td>${s.start_time}</td>
            <td>${_scheduleTypeLabel(s)}</td>
            <td style="font-size:12px">${_formatZoneDurations(s.zone_durations)}</td>
            <td><div class="sched-status-cell">${statusBadge}</div></td>
          </tr>`;
        }).join("");
    return `
      <ha-card><div class="card-content">
        <div class="card-title">Current Schedules</div>
        <table class="sched-table">
          <thead><tr><th>Prog</th><th>Start</th><th>Days</th><th>Zone Durations</th><th>Status</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div></ha-card>`;
  }

  // ── Advisor Card ─────────────────────────────────────────────────────────────

  _renderAdvisorCard(dev, config) {
    const sug = this._suggestions[dev.entry_id];
    const loading = !!this._suggestionLoading[dev.entry_id];
    const suggestionHtml = sug
      ? _renderSuggestionContent(sug, dev.enabled_zones)
      : `<div style="text-align:center;padding:16px;color:var(--secondary-text-color)">${loading ? "Computing..." : "No suggestion yet."}</div>`;
    return `
      <ha-card><div class="card-content">
        <div class="card-head">
          <div class="card-title">Irrigation Suggestion</div>
          <button class="icon-btn" id="btn-calculate-${dev.entry_id}" title="Update suggestion" aria-label="Update suggestion">
            <ha-icon icon="mdi:update"></ha-icon>
          </button>
        </div>
        <div id="suggestion-content-${dev.entry_id}">${suggestionHtml}</div>
        <div class="info-row">Uses today's sensor history. Configure zones and data sources in Configuration.</div>
      </div></ha-card>`;
  }

  // ── Post-render population ───────────────────────────────────────────────────

  _populateForms(dev, config) {
    if (!dev || !config) return;
    const root = this.shadowRoot;
    (dev.enabled_zones || []).forEach(z => {
      const zk = String(z);
      const zc = config.zones[zk] || {};
      ["area", "flow_rate"].forEach(field => {
        const el = root.querySelector(`[data-zone="${zk}"][data-field="${field}"]`);
        if (el && zc[field] != null) el.value = zc[field];
      });
    });
  }

  // ── Event Listeners ──────────────────────────────────────────────────────────

  _attachListeners() {
    const root = this.shadowRoot;

    root.querySelectorAll(".tab-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        this._tab = parseInt(btn.dataset.tab, 10);
        this._innerTab = 0;
        this._render();
      });
    });

    root.querySelectorAll(".inner-tab-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        this._innerTab = parseInt(btn.dataset.innerTab, 10);
        this._render();
      });
    });

    const menuBtn = root.getElementById("btn-menu");
    if (menuBtn) menuBtn.addEventListener("click", () => {
      this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true }));
    });

    this._devices.forEach(dev => {
      const eid = dev.entry_id;
      const calcBtn = root.getElementById(`btn-calculate-${eid}`);
      if (calcBtn) calcBtn.addEventListener("click", () => this._calculate(dev));

      const saveBtn = root.getElementById(`btn-save-${eid}`);
      if (saveBtn) saveBtn.addEventListener("click", () => this._saveConfig(dev));
    });

    // Load calendar data after listeners are attached
    if (this._innerTab === 0 && this._currentDev) {
      this._ensureInitialSuggestion(this._currentDev);
    }

    if (this._innerTab === 1 && this._currentDev) {
      this._loadAndRenderCalendar(this._currentDev, this._currentConfig);
    }
  }

  // ── Calculate suggestion ─────────────────────────────────────────────────────

  async _buildSuggestion(dev) {
    const config = this._advisorConfigs[dev.entry_id] || {};
    const we     = config.weather_entities || {};
    if (!we.temperature?.length) return null;

    const lat  = this._hass.config.latitude;
    const elev = this._hass.config.elevation || 0;

    const [tempStats, humStats, solarStats, windStats] = await Promise.all([
      this._fetchDailyStats(we.temperature),
      this._fetchDailyStats(we.humidity),
      this._fetchDailyStats(we.solar_radiation),
      this._fetchDailyStats(we.wind_speed),
    ]);

    const tMeanCur = _stateNums(this._hass, we.temperature);
    if ((tempStats.mean ?? tMeanCur) == null) return null;

    const rainToday = _stateNums(this._hass, we.rain_today);

    // Single ETo from daily means — this is how FAO-56 PM is designed to be used
    const etoResult = calculateETo({
      lat, elevation: elev,
      tMean: tempStats.mean ?? tMeanCur, tMin: tempStats.min, tMax: tempStats.max,
      humidity:    humStats.mean  ?? _stateNums(this._hass, we.humidity),
      solarRadWm2: solarStats.mean ?? _stateNums(this._hass, we.solar_radiation),
      windSpeedMs: windStats.mean  ?? _stateNums(this._hass, we.wind_speed),
    });

    const zones = dev.enabled_zones.map(z => {
      const zc = (config.zones || {})[String(z)];
      if (!zc || !zc.area || !zc.flow_rate) return { zone: z, duration: null, etcMm: 0 };
      const grass = zc.grass === "cool_lawn" || zc.grass === "warm_lawn" ? "lawn" : zc.grass;
      const Kc    = grass === "lawn" ? _lawnKc(lat) : (GRASS.kc[grass] ?? 0.80);
      const expF  = EXPOSURE.factors[zc.exposure] ?? 1.0;
      const soilF = SOIL.factors[zc.soil] ?? 1.0;
      const etcMm   = etoResult.value * Kc * expF * soilF;
      const duration = calculateZoneDuration(etoResult.value, { ...zc, grass }, rainToday, lat);
      return { zone: z, duration, etcMm };
    });

    return { eto: etoResult.value, method: etoResult.method, rainToday, zones };
  }

  async _ensureInitialSuggestion(dev) {
    const eid = dev.entry_id;
    if (this._suggestions[eid] || this._suggestionLoading[eid]) return;

    this._suggestionLoading[eid] = true;
    const block = this.shadowRoot.getElementById(`suggestion-content-${eid}`);
    if (block) block.innerHTML = `<div style="text-align:center;padding:16px;color:var(--secondary-text-color)">Computing...</div>`;

    try {
      const suggestion = await this._buildSuggestion(dev);
      if (!suggestion) return;
      this._suggestions[eid] = suggestion;
      const refreshed = this.shadowRoot.getElementById(`suggestion-content-${eid}`);
      if (refreshed) refreshed.innerHTML = _renderSuggestionContent(suggestion, dev.enabled_zones);
    } catch (e) {
      console.warn("[RainVision] initial suggestion failed", e);
    } finally {
      this._suggestionLoading[eid] = false;
    }
  }

  async _calculate(dev) {
    const config = this._advisorConfigs[dev.entry_id] || {};
    const we = config.weather_entities || {};
    if (!we.temperature?.length) {
      alert("Temperature sensor not configured. Please configure it in the Configuration tab.");
      return;
    }

    this._suggestionLoading[dev.entry_id] = true;
    const block = this.shadowRoot.getElementById(`suggestion-content-${dev.entry_id}`);
    if (block) block.innerHTML = `<div style="text-align:center;padding:16px;color:var(--secondary-text-color)">Computing…</div>`;
    try {
      const suggestion = await this._buildSuggestion(dev);
      if (!suggestion) {
        if (block) block.innerHTML = "";
        alert("Temperature sensor unavailable.");
        return;
      }
      this._suggestions[dev.entry_id] = suggestion;
      if (block) block.innerHTML = _renderSuggestionContent(suggestion, dev.enabled_zones);
    } finally {
      this._suggestionLoading[dev.entry_id] = false;
    }
  }

  // ── History fetching ─────────────────────────────────────────────────────────

  async _fetchDailyStats(entityIds) {
    const ids = Array.isArray(entityIds) ? entityIds.filter(Boolean) : (entityIds ? [entityIds] : []);
    if (!ids.length) return { mean: null, min: null, max: null };
    const startOfDay = new Date();
    startOfDay.setHours(0, 0, 0, 0);
    try {
      const res = await this._ws({
        type: "history/history_during_period",
        start_time: startOfDay.toISOString(),
        entity_ids: ids,
        include_start_time_state: true,
        significant_changes_only: false,
        minimal_response: true,
        no_attributes: true,
      });
      const vals = [];
      for (const states of Object.values(res)) {
        for (const s of states) {
          const v = parseFloat(s.s ?? s.state);
          if (!isNaN(v)) vals.push(v);
        }
      }
      if (!vals.length) return { mean: null, min: null, max: null };
      const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
      return { mean, min: Math.min(...vals), max: Math.max(...vals) };
    } catch (e) {
      console.warn("[RainVision] history fetch failed for", ids, e);
      return { mean: null, min: null, max: null };
    }
  }

  // ── Normalise & save config ───────────────────────────────────────────────────

  _normalizeConfig(rawConfig) {
    const cfg = rawConfig && typeof rawConfig === "object" ? rawConfig : {};

    const rawZones = cfg.zones && typeof cfg.zones === "object" ? cfg.zones : {};
    const zones = {};
    for (const [zk, zv] of Object.entries(rawZones)) {
      const z = { ...zv };
      if (z.grass === "cool_lawn" || z.grass === "warm_lawn") z.grass = "lawn";
      zones[zk] = z;
    }

    const rawWe = cfg.weather_entities && typeof cfg.weather_entities === "object" ? cfg.weather_entities : {};
    const weatherEntities = {};
    for (const [k, v] of Object.entries(rawWe)) {
      if (Array.isArray(v)) weatherEntities[k] = v.filter(Boolean);
      else if (v) weatherEntities[k] = [v];
      else weatherEntities[k] = [];
    }

    return { zones, weather_entities: weatherEntities, forecast_entity: cfg.forecast_entity || null };
  }

  async _saveConfig(dev) {
    const root   = this.shadowRoot;
    const config = this._advisorConfigs[dev.entry_id] || { zones: {}, weather_entities: {}, forecast_entity: null };

    const zones = { ...config.zones };
    dev.enabled_zones.forEach(z => {
      const zk  = String(z);
      const cur = zones[zk] || {};
      const get = f => root.querySelector(`[data-zone="${zk}"][data-field="${f}"]`);
      zones[zk] = {
        ...cur,
        area:            get("area")       ? parseFloat(get("area").value)      || null : cur.area,
        flow_rate:       get("flow_rate")  ? parseFloat(get("flow_rate").value) || null : cur.flow_rate,
        exposure:        get("exposure")   ? (get("exposure").value   || null)  : cur.exposure,
        soil:            get("soil")       ? (get("soil").value       || null)  : cur.soil,
        grass:           get("grass")      ? (get("grass").value      || null)  : cur.grass,
        irrigation_type: get("irrigation_type") ? (get("irrigation_type").value || null) : cur.irrigation_type,
      };
    });

    const weatherEntities = {};
    WEATHER_KEYS.forEach(({ key }) => {
      const el = root.querySelector(`[data-we-key="${key}"][data-entry="${dev.entry_id}"]`);
      if (el) weatherEntities[key] = Array.from(el.selectedOptions).map(o => o.value).filter(Boolean);
    });

    const fcEl = root.getElementById(`forecast-entity-${dev.entry_id}`);
    const forecastEntity = fcEl ? (fcEl.value || null) : config.forecast_entity;

    const newConfig = { zones, weather_entities: weatherEntities, forecast_entity: forecastEntity };
    this._advisorConfigs[dev.entry_id] = newConfig;

    await this._ws({ type: "rain_vision_ha/save_advisor_config", entry_id: dev.entry_id, config: newConfig });
    this._showToast("Configuration saved.");
  }

  // ── Utilities ────────────────────────────────────────────────────────────────

  _ws(msg) { return this._hass.connection.sendMessagePromise(msg); }

  _showToast(message) {
    this.dispatchEvent(new CustomEvent("hass-notification", { bubbles: true, composed: true, detail: { message } }));
  }
}

customElements.define("rain-vision-ha-panel", RainVisionHaPanel);
