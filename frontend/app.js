(() => {
  "use strict";

  const diagnostic = (level, event, details = {}) => {
    const write = console[level] || console.debug;
    write.call(console, `[dashboard] ${event}`, details);
  };

  const form = document.querySelector("#range-form");
  const startInput = document.querySelector("#start-date");
  const endInput = document.querySelector("#end-date");
  const rangeEyebrow = document.querySelector("#range-eyebrow");
  const rangeHeading = document.querySelector("#range-heading");
  const rangeHelp = document.querySelector("#range-help");
  const zoneLabel = document.querySelector("#zone-name");
  const startLabel = document.querySelector("#start-label");
  const endLabel = document.querySelector("#end-label");
  const excludedHourHeading = document.querySelector("#excluded-hour-heading");
  const excludedPointHeading = document.querySelector("#excluded-point-heading");
  const status = document.querySelector("#status");
  const content = document.querySelector("#content");
  const details = document.querySelector("#details");
  const tooltip = document.querySelector("#point-tooltip");
  const badge = document.querySelector("#scenario-badge");
  const heading = document.querySelector("#chart-heading");
  const eyebrow = document.querySelector("#series-eyebrow");
  const interpretation = document.querySelector("#interpretation-text");
  const efficiencySummary = document.querySelector("#efficiency-summary");
  const efficiencyMetrics = document.querySelector("#efficiency-metrics");
  const chartNote = document.querySelector("#chart-note");
  const excludedContent = document.querySelector("#excluded-content");
  const excludedSources = document.querySelector("#excluded-sources");
  const excludedSummary = document.querySelector("#excluded-summary");
  const excludedEmpty = document.querySelector("#excluded-empty");
  const excludedTable = document.querySelector("#excluded-table");
  const excludedRows = document.querySelector("#excluded-rows");
  let scenario = "actual";
  // The ids of the series the operator hid through a legend. It lives for the
  // page session only, so it survives a range reload and a tab switch and is
  // reset by a page reload.
  const hiddenSeries = new Set();

  const pad = (value) => String(value).padStart(2, "0");
  const hourMilliseconds = 60 * 60 * 1000;
  const dayMilliseconds = 24 * hourMilliseconds;
  const inputPattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/;

  // Every time the dashboard shows or accepts belongs to the time zone the
  // service is configured with, never to the browser's zone. Only the data API
  // speaks UTC. A "wall time" below is a local date and time encoded as
  // milliseconds as if it were UTC, so arithmetic on it never meets a
  // daylight-saving change.
  let timeZone = null;
  let timeZoneProblem = "The dashboard time zone has not been loaded.";
  let zoneFormat = null;
  const offsetCache = new Map();

  const useTimeZone = (name) => {
    // The constructor throws a RangeError for a zone this browser does not know.
    zoneFormat = new Intl.DateTimeFormat("en-US", {
      timeZone: name,
      hourCycle: "h23",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    timeZone = name;
    offsetCache.clear();
  };
  const wallTimeAt = (instant) => {
    const parts = {};
    zoneFormat.formatToParts(instant).forEach(({ type, value }) => { parts[type] = Number(value); });
    return Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second);
  };
  // Supported zones have whole-hour offsets that change on UTC hour boundaries,
  // so one lookup per UTC hour is exact and keeps labelling long ranges cheap.
  const offsetAt = (instant) => {
    const hourStart = Math.floor(instant / hourMilliseconds) * hourMilliseconds;
    if (!offsetCache.has(hourStart)) offsetCache.set(hourStart, wallTimeAt(hourStart) - hourStart);
    return offsetCache.get(hourStart);
  };
  // The instants at which the zone's clock shows this wall time: none when a
  // clock change skips it, two when a clock change repeats it.
  const instantsForWall = (wall) => [...new Set([wall - dayMilliseconds, wall + dayMilliseconds].map(offsetAt))]
    .map((offset) => wall - offset)
    .filter((instant) => offsetAt(instant) === wall - instant)
    .sort((first, second) => first - second);
  // A repeated time resolves to its first occurrence. A skipped time resolves
  // forward by the length of the gap, which is what reading it with the offset
  // in force before the change yields (02:30 becomes 03:30).
  const wallToInstant = (wall) => instantsForWall(wall)[0] ?? wall - offsetAt(wall - dayMilliseconds);
  const isRepeatedTime = (instant) => instantsForWall(instant + offsetAt(instant)).length > 1;

  const wallText = (wall) => {
    const date = new Date(wall);
    return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`
      + `T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
  };
  const dateTimeInputValue = (value) => {
    const instant = new Date(value).getTime();
    if (Number.isNaN(instant)) return "";
    return wallText(instant + offsetAt(instant));
  };
  const parseInputTimestamp = (value) => {
    const match = inputPattern.exec(value);
    if (!match) return Number.NaN;
    const [year, month, day, hour, minute] = match.slice(1).map(Number);
    return wallToInstant(Date.UTC(year, month - 1, day, hour, minute));
  };
  const utcTimestamp = (value) => new Date(parseInputTimestamp(value)).toISOString().replace(".000Z", "Z");
  const isValidRange = (start, end) => {
    const startInstant = parseInputTimestamp(start);
    const endInstant = parseInputTimestamp(end);
    return Number.isFinite(startInstant) && Number.isFinite(endInstant) && endInstant > startInstant;
  };
  const formatOffset = (offset) => {
    const minutes = Math.abs(offset) / 60000;
    return `${offset < 0 ? "-" : "+"}${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}`;
  };
  // A time that the clock shows twice also carries its UTC offset, so the two
  // occurrences stay distinguishable.
  const formatTimestamp = (value) => {
    const instant = new Date(value).getTime();
    const text = wallText(instant + offsetAt(instant)).replace("T", " ");
    return isRepeatedTime(instant) ? `${text}${formatOffset(offsetAt(instant))}` : text;
  };
  const localDayRange = (instant) => {
    const wall = instant + offsetAt(instant);
    const start = Math.floor(wall / dayMilliseconds) * dayMilliseconds;
    return { start: wallText(start), end: wallText(start + dayMilliseconds) };
  };
  const zoneName = () => timeZone || "the configured time zone";
  const rangeRules = () => "The end time is exclusive and must be later than the start. "
    + "When the clocks go back, a time in the repeated hour means its first occurrence; "
    + "when they go forward, a skipped time moves ahead by the length of the gap.";

  const setStatus = (message, kind = "") => {
    status.className = `status ${kind}`;
    status.textContent = message;
  };

  const addDetail = (label, value) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    details.append(term, description);
  };

  const selectedSeries = (data) => data.series || [];
  const axisDomainForValues = (values) => {
    const finiteValues = values.filter((value) => Number.isFinite(value));
    if (!finiteValues.length) return { min: 0, max: 1 };
    const dataMin = Math.min(...finiteValues);
    const dataMax = Math.max(...finiteValues);
    const dataSpan = dataMax - dataMin;
    const padding = dataSpan === 0
      ? Math.max(Math.abs(dataMin) * 0.1, 0.05)
      : dataSpan * 0.05;
    return { min: dataMin - padding, max: dataMax + padding };
  };
  const axisPrecisionForSpan = (span) => {
    const tickSpan = span / 4;
    if (!Number.isFinite(tickSpan) || tickSpan <= 0) return 1;
    return Math.min(6, Math.max(1, Math.ceil(-Math.log10(tickSpan))));
  };
  const unitForSeries = (item) => item.id === "import_price_forecast" || item.id === "export_price_forecast"
    ? "EUR/kWh"
    : item.id === "pv_generation_forecast" || item.id === "household_load_actual"
      ? "kW"
      : item.unit;
  const seriesLabel = (item) => ({
    household_load_actual: "Household load",
    grid_import_actual: "Grid import",
    grid_export_actual: "Grid export",
    import_price_actual: "Import price",
    export_price_actual: "Export price",
    battery_state_of_charge_actual: "Battery state of charge",
    pv_generation_forecast: "PV generation",
    import_price_forecast: "Import price",
    export_price_forecast: "Export price",
    inverter_charge_efficiency_actual: "Inverter charge efficiency",
    inverter_discharge_efficiency_actual: "Inverter discharge efficiency",
    battery_efficiency_actual: "Battery round-trip efficiency",
    round_trip_efficiency_actual: "Complete round-trip efficiency",
  }[item.id] || item.id);
  const assetLabel = (asset) => ({
    household_load: "Household load",
    pv_generation: "PV generation",
    grid_flow: "Grid import and export",
    electricity_prices: "Electricity prices",
    battery: "Battery state",
    electric_vehicle: "Electric vehicle",
    heat_pump: "Heat pump",
  }[asset] || asset);
  const availabilityLabel = (status) => ({
    available: "Available",
    empty: "No data in range",
    stale: "Stale",
    not_configured: "Not configured",
    unavailable: "Unavailable",
    invalid: "Invalid data withheld",
  }[status] || status);
  const efficiencyStatusLabel = (status) => ({
    calculated: "calculated",
    calculated_with_defaults: "calculated with defaults",
    defaulted: "default",
    unavailable: "unavailable",
    invalid: "invalid",
  }[status] || "status unknown");
  const efficiencyFallbackStatuses = new Set(["defaulted", "unavailable", "invalid"]);
  // Energy metrics always show two decimals, including for zero and whole
  // numbers; cycle counts are whole numbers. Other units render unchanged.
  const metricValueFormatters = {
    kWh: (value) => value.toFixed(2),
    cycles: (value) => String(Math.round(value)),
  };
  const formatMetricValue = (metric) => (metricValueFormatters[metric.unit] || String)(metric.value);
  const chartDefinitions = {
    power: {
      panel: document.querySelector("#power-panel"),
      element: document.querySelector("#power-chart"),
      legend: document.querySelector("#power-legend"),
      grid: document.querySelector("#power-grid-lines"),
      labels: document.querySelector("#power-labels"),
      points: document.querySelector("#power-points"),
      seriesPaths: document.querySelector("#power-series-paths"),
      axisUnit: document.querySelector("#power-axis-unit"),
      title: document.querySelector("#power-chart-title"),
      description: document.querySelector("#power-chart-description"),
      series: [],
    },
    price: {
      panel: document.querySelector("#price-panel"),
      element: document.querySelector("#price-chart"),
      legend: document.querySelector("#price-legend"),
      grid: document.querySelector("#price-grid-lines"),
      labels: document.querySelector("#price-labels"),
      points: document.querySelector("#price-points"),
      seriesPaths: document.querySelector("#price-series-paths"),
      axisUnit: document.querySelector("#price-axis-unit"),
      title: document.querySelector("#price-chart-title"),
      description: document.querySelector("#price-chart-description"),
      series: [],
    },
    battery: {
      panel: document.querySelector("#battery-panel"),
      element: document.querySelector("#battery-chart"),
      legend: document.querySelector("#battery-legend"),
      grid: document.querySelector("#battery-grid-lines"),
      labels: document.querySelector("#battery-labels"),
      points: document.querySelector("#battery-points"),
      seriesPaths: document.querySelector("#battery-series-paths"),
      axisUnit: document.querySelector("#battery-axis-unit"),
      title: document.querySelector("#battery-chart-title"),
      description: document.querySelector("#battery-chart-description"),
      series: [],
    },
  };
  const chartSeries = (series) => ({
    power: series.filter((item) => [
      "household_load_actual", "grid_import_actual", "grid_export_actual", "pv_generation_forecast",
    ].includes(item.id)),
    price: series.filter((item) => [
      "import_price_forecast", "export_price_forecast", "import_price_actual", "export_price_actual",
    ].includes(item.id)),
    battery: series.filter((item) => item.id === "battery_state_of_charge_actual"),
  });
  const coverageRange = (data) => {
    const ranges = selectedSeries(data)
      .filter((item) => item.available_start_time && item.available_end_time)
      .map((item) => ({
        start: new Date(item.available_start_time).getTime(),
        end: new Date(item.available_end_time).getTime(),
      }))
      .filter(({ start, end }) => Number.isFinite(start) && Number.isFinite(end) && end > start);
    if (!ranges.length) return null;
    // Keep the controls wide enough to show each provider's valid slice.
    // Missing intervals remain explicit gaps in the individual charts.
    const start = Math.min(...ranges.map((range) => range.start));
    const end = Math.max(...ranges.map((range) => range.end));
    if (end <= start) return null;
    return {
      start: dateTimeInputValue(new Date(start)),
      end: dateTimeInputValue(new Date(end)),
    };
  };
  const alignRangeToCoverage = (data, start, end) => {
    const coverage = coverageRange(data);
    if (!coverage || (isValidRange(start, end)
      && parseInputTimestamp(start) >= parseInputTimestamp(coverage.start)
      && parseInputTimestamp(end) <= parseInputTimestamp(coverage.end))) return null;
    return coverage;
  };
  // The tooltip sits above its point, or below it when the chart has no room
  // above, and is clamped to the box of the chart the point belongs to. The
  // legend is outside that box, so the tooltip never covers it.
  const showPoint = (label, timestamp, value, unit, point, chart) => {
    const margin = 4;
    const gap = 8;
    tooltip.textContent = `${label} · ${formatTimestamp(timestamp)} · ${value} ${unit}`;
    tooltip.style.maxWidth = "";
    tooltip.hidden = false;
    const origin = tooltip.parentElement.getBoundingClientRect();
    const bounds = chart.getBoundingClientRect();
    const pointBox = point.getBoundingClientRect();
    const available = bounds.width - 2 * margin;
    if (tooltip.offsetWidth > available) tooltip.style.maxWidth = `${available}px`;
    const { offsetWidth: width, offsetHeight: height } = tooltip;
    const left = Math.min(Math.max(pointBox.left, bounds.left + margin), bounds.right - width - margin);
    const above = pointBox.top - height - gap;
    const top = above >= bounds.top + margin ? above : pointBox.bottom + gap;
    tooltip.style.left = `${left - origin.left}px`;
    tooltip.style.top = `${Math.min(top, bounds.bottom - height - margin) - origin.top}px`;
  };
  const hidePoint = () => { tooltip.hidden = true; };
  const hasSeriesData = (series, id) => series.some(
    (item) => item.id === id && item.values.some((value) => Number.isFinite(value)),
  );
  const seriesClass = (item, index) => ({
    household_load_actual: "series-0",
    grid_import_actual: "series-1",
    grid_export_actual: "series-2",
    battery_state_of_charge_actual: "series-0",
    pv_generation_forecast: "series-0",
    import_price_forecast: "series-1",
    export_price_forecast: "series-2",
    import_price_actual: "series-1",
    export_price_actual: "series-2",
  }[item.id] || `series-${index}`);

  const renderDetails = (data) => {
    details.replaceChildren();
    const series = selectedSeries(data);
    const first = series[0];
    addDetail("Status", data.status[0].toUpperCase() + data.status.slice(1));
    if (scenario === "actual") {
      // Every asset has its own source, coverage, and freshness, so each series
      // is described separately instead of by the first one.
      series.forEach((item) => {
        const coverage = item.available_start_time
          ? `${formatTimestamp(item.available_start_time)} to ${formatTimestamp(item.available_end_time)}`
          : "no points";
        const parts = [
          `${item.source?.provider || "Unavailable"} / ${item.source?.entity_id || "default"}`,
          unitForSeries(item),
          `freshness ${item.freshness}`,
          `coverage ${coverage}`,
          `retrieved ${item.retrieved_at ? formatTimestamp(item.retrieved_at) : "not available"}`,
        ];
        if (item.validation_status !== "valid") parts.push(`validation ${item.validation_status}`);
        addDetail(seriesLabel(item), parts.join(" · "));
      });
      (data.assets || [])
        .filter((asset) => asset.status !== "available")
        .forEach((asset) => addDetail(
          assetLabel(asset.asset),
          asset.reason ? `${availabilityLabel(asset.status)}: ${asset.reason}` : availabilityLabel(asset.status),
        ));
      (data.diagnostics || []).forEach((diagnostic) => addDetail("Diagnostic", diagnostic));
      if (!first) addDetail("Coverage", "No points in range");
      return;
    }
    if (!first) {
      addDetail("Coverage", "No points in range");
      return;
    }
    addDetail("Source", `${first.source?.provider || "Unavailable"} / ${first.source?.entity_id || "default"}`);
    const units = [...new Set(series.filter((item) => hasSeriesData([item], item.id)).map(unitForSeries))];
    addDetail(units.length === 1 ? "Unit" : "Units", units.join(", "));
    addDetail("Coverage", first.available_start_time ? `${formatTimestamp(first.available_start_time)} to ${formatTimestamp(first.available_end_time)}` : "No points in range");
    if (scenario !== "efficiency") addDetail("Freshness", first.freshness);
    addDetail("Retrieved", first.retrieved_at ? formatTimestamp(first.retrieved_at) : "Not available");
    (data.diagnostics || []).forEach((diagnostic) => addDetail("Diagnostic", diagnostic));
    if (first.generated_at) addDetail("Generated", formatTimestamp(first.generated_at));
    if (first.published_at) addDetail("Published", formatTimestamp(first.published_at));
  };

  // Draws every series that is not hidden. The title, the description, and the
  // time axis describe the whole chart, so hiding a series changes only the
  // lines, the points, and the value axis, which is rescaled to what remains.
  const renderGraph = (definition) => {
    const { panel, element, grid, labels, points, seriesPaths, axisUnit, title, description, series } = definition;
    grid.replaceChildren(); labels.replaceChildren(); points.replaceChildren(); seriesPaths.replaceChildren();
    panel.removeAttribute("hidden");
    const visibleSeries = series.filter((item) => !hiddenSeries.has(item.id));
    const unit = unitForSeries(series[0]);
    axisUnit.textContent = unit;
    title.textContent = `${series.map(seriesLabel).join(" and ")} (${unit})`;
    description.textContent = `${series.map((item) => `${seriesLabel(item)} in ${unitForSeries(item)}`).join("; ")}. Missing intervals remain gaps.`;
    const valueList = visibleSeries.flatMap((item) => item.values);
    const left = 52; const right = 785; const top = 18; const bottom = 276;
    const { min, max } = axisDomainForValues(valueList);
    const axisPrecision = axisPrecisionForSpan(max - min);
    const x = (index, count) => left + (index / Math.max(count - 1, 1)) * (right - left);
    const y = (value) => bottom - ((value - min) / (max - min)) * (bottom - top);
    for (let index = 0; index <= 4; index += 1) {
      const value = min + (max - min) * (index / 4);
      const lineElement = document.createElementNS("http://www.w3.org/2000/svg", "line");
      lineElement.setAttribute("x1", left); lineElement.setAttribute("x2", right);
      lineElement.setAttribute("y1", y(value)); lineElement.setAttribute("y2", y(value));
      lineElement.setAttribute("class", "grid-line"); grid.append(lineElement);
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", 4); label.setAttribute("y", y(value) + 4); label.setAttribute("class", "axis-label");
      label.textContent = value.toFixed(axisPrecision); labels.append(label);
    }
    const timestamps = series.find((item) => item.timestamps.length)?.timestamps || [];
    const tickCount = Math.min(5, timestamps.length);
    for (let tick = 0; tick < tickCount; tick += 1) {
      const index = tickCount === 1 ? 0 : Math.round(tick * (timestamps.length - 1) / (tickCount - 1));
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", x(index, timestamps.length));
      label.setAttribute("y", "304");
      label.setAttribute("class", "axis-label x-axis-label");
      label.setAttribute("text-anchor", tick === 0 ? "start" : tick === tickCount - 1 ? "end" : "middle");
      label.textContent = formatTimestamp(timestamps[index]);
      labels.append(label);
    }
    series.forEach((item, seriesIndex) => {
      if (hiddenSeries.has(item.id)) return;
      const className = seriesClass(item, seriesIndex);
      let path = "";
      item.values.forEach((value, index) => {
        if (!Number.isFinite(value)) return;
        const command = index && Number.isFinite(item.values[index - 1]) ? "L" : "M";
        path += `${command}${x(index, item.values.length).toFixed(2)},${y(value).toFixed(2)} `;
        const point = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        point.setAttribute("cx", x(index, item.values.length)); point.setAttribute("cy", y(value)); point.setAttribute("r", item.values.length > 48 ? 2.5 : 4); point.setAttribute("class", `point point-${className.replace("series-", "")}`);
        point.setAttribute("tabindex", "0");
        point.setAttribute("aria-label", `${seriesLabel(item)}, ${formatTimestamp(item.timestamps[index])}: ${value} ${unitForSeries(item)}`);
        const show = () => showPoint(seriesLabel(item), item.timestamps[index], value, unitForSeries(item), point, element);
        point.addEventListener("pointerenter", show);
        point.addEventListener("pointerleave", hidePoint);
        point.addEventListener("focus", show);
        point.addEventListener("blur", hidePoint);
        points.append(point);
      });
      const seriesLine = document.createElementNS("http://www.w3.org/2000/svg", "path");
      seriesLine.setAttribute("d", path.trim());
      seriesLine.setAttribute("class", `series-line ${className}`);
      seriesLine.dataset.seriesId = item.id;
      seriesPaths.append(seriesLine);
    });
  };

  // One button per series of the chart. Toggling updates the entry in place
  // instead of rebuilding the legend, so keyboard focus stays on the entry.
  const renderLegend = (definition) => {
    definition.legend.replaceChildren();
    definition.series.forEach((item, seriesIndex) => {
      const entry = document.createElement("button");
      entry.type = "button";
      entry.className = `legend-item ${seriesClass(item, seriesIndex).replace("series-", "legend-")}`;
      entry.dataset.seriesId = item.id;
      entry.setAttribute("aria-pressed", String(!hiddenSeries.has(item.id)));
      const swatch = document.createElement("span");
      swatch.className = "legend-swatch";
      swatch.setAttribute("aria-hidden", "true");
      entry.append(swatch, `${seriesLabel(item)} (${unitForSeries(item)})`);
      entry.addEventListener("click", () => {
        const hide = !hiddenSeries.has(item.id);
        if (hide) hiddenSeries.add(item.id); else hiddenSeries.delete(item.id);
        entry.setAttribute("aria-pressed", String(!hide));
        hidePoint();
        renderGraph(definition);
        diagnostic("debug", "series_toggled", { series: item.id, visible: !hide });
      });
      definition.legend.append(entry);
    });
  };

  const renderChart = (data) => {
    Object.values(chartDefinitions).forEach((definition) => {
      const { panel, grid, labels, points, seriesPaths, legend } = definition;
      panel.setAttribute("hidden", "");
      grid.replaceChildren(); labels.replaceChildren(); points.replaceChildren(); seriesPaths.replaceChildren();
      legend.replaceChildren();
      definition.series = [];
    });
    hidePoint();
    const series = selectedSeries(data);
    efficiencySummary.replaceChildren();
    efficiencySummary.setAttribute("hidden", "");
    efficiencyMetrics.replaceChildren();
    efficiencyMetrics.setAttribute("hidden", "");
    if (scenario === "efficiency") {
      series
        .filter((item) => item.data_type === "battery_efficiency")
        .forEach((item) => {
          const value = item.values.find((candidate) => Number.isFinite(candidate));
          if (value === undefined) return;
          const label = document.createElement("dt");
          label.textContent = seriesLabel(item);
          const valueCell = document.createElement("dd");
          const numericValue = document.createElement("data");
          numericValue.className = "efficiency-value";
          numericValue.value = String(value);
          numericValue.textContent = value.toFixed(4);
          valueCell.append(numericValue, ` ${unitForSeries(item)}`);
          // calculation_status is the only source for the row annotation. The API's
          // legacy default flag describes the same fallback state, so rendering it
          // as well would show one condition twice.
          if (item.calculation_status) {
            const status = document.createElement("span");
            status.className = `efficiency-status efficiency-status-${item.calculation_status}`;
            status.textContent = ` (${efficiencyStatusLabel(item.calculation_status)})`;
            if (efficiencyFallbackStatuses.has(item.calculation_status)) {
              status.title = "Fallback ratio used because this component was not calculable.";
            }
            valueCell.append(status);
          }
          efficiencySummary.append(label, valueCell);
      });
      if (efficiencySummary.childElementCount) efficiencySummary.removeAttribute("hidden");
      (data.metrics || []).forEach((metric) => {
        const label = document.createElement("dt");
        label.textContent = metric.label;
        const value = document.createElement("dd");
        value.textContent = `${formatMetricValue(metric)} ${metric.unit}`;
        efficiencyMetrics.append(label, value);
      });
      if (efficiencyMetrics.childElementCount) efficiencyMetrics.removeAttribute("hidden");
      chartNote.textContent = "Each value is a calculated ratio over the retained battery and inverter history, not an hourly observation.";
      return;
    }
    chartNote.textContent = `Each point represents one hourly interval. Times are shown in ${zoneName()}. Charts are separated by unit. Focus a point to inspect it, or use a legend entry to show or hide its line.`;
    Object.entries(chartSeries(series)).forEach(([kind, groupedSeries]) => {
      const definition = chartDefinitions[kind];
      definition.series = groupedSeries.filter((item) => hasSeriesData([item], item.id));
      if (!definition.series.length) return;
      renderLegend(definition);
      renderGraph(definition);
    });
  };

  const excludedSourceLabel = (source) => ({
    household_load: "Household load",
    grid_flow: "Grid import and export",
    battery_efficiency: "Battery efficiency",
  }[source] || source);
  const excludedNumber = (value) => String(Number(Number(value).toFixed(6)));
  const excludedPointDetail = (point) => {
    const parts = [];
    if (point.previous_timestamp !== null && point.previous_value !== null) {
      parts.push(`previous ${excludedNumber(point.previous_value)}${point.unit ? ` ${point.unit}` : ""} at ${formatTimestamp(point.previous_timestamp)}`);
    }
    if (point.step_kwh !== null) parts.push(`${point.previous_timestamp === null ? "energy" : "step"} ${excludedNumber(point.step_kwh)} kWh`);
    if (point.maximum_kwh !== null) parts.push(`maximum ${excludedNumber(point.maximum_kwh)} kWh`);
    return parts.join(", ");
  };
  const appendCell = (row, text, className = "") => {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.textContent = text;
    row.append(cell);
    return cell;
  };
  const renderExcluded = (data) => {
    excludedSources.replaceChildren();
    excludedSummary.replaceChildren();
    excludedRows.replaceChildren();
    data.sources.forEach((source) => {
      const item = document.createElement("li");
      const label = availabilityLabel(source.status);
      item.textContent = `${excludedSourceLabel(source.source)}: ${label}${source.reason ? ` (${source.reason})` : ""}`;
      item.dataset.source = source.source;
      item.dataset.status = source.status;
      excludedSources.append(item);
    });
    data.summary.forEach((entry) => {
      const item = document.createElement("li");
      item.textContent = `${excludedSourceLabel(entry.source)} · ${entry.reason}: ${entry.excluded_hour_count} hour${entry.excluded_hour_count === 1 ? "" : "s"}`;
      item.dataset.source = entry.source;
      item.dataset.reason = entry.reason;
      excludedSummary.append(item);
    });
    data.hours.forEach((hour) => {
      hour.causes.forEach((cause) => {
        const points = cause.data_points.length ? cause.data_points : [null];
        points.forEach((point, pointIndex) => {
          const row = document.createElement("tr");
          row.dataset.source = hour.source;
          row.dataset.reason = cause.reason;
          const repeated = pointIndex > 0 ? "repeated" : "";
          appendCell(row, formatTimestamp(hour.hour_start), repeated);
          appendCell(row, excludedSourceLabel(hour.source), repeated);
          appendCell(row, (point && point.entity_id) || cause.entity_id || "-", repeated);
          const reason = document.createElement("td");
          if (repeated) reason.className = repeated;
          const code = document.createElement("code");
          code.className = "excluded-reason";
          code.textContent = cause.reason;
          reason.append(code);
          if (!repeated) {
            const message = document.createElement("span");
            message.className = "excluded-message";
            message.textContent = cause.data_point_count > cause.data_points.length
              ? `${cause.message} Showing ${cause.data_points.length} of ${cause.data_point_count} data points.`
              : cause.message;
            reason.append(message);
          }
          row.append(reason);
          appendCell(row, point ? formatTimestamp(point.timestamp) : "-");
          appendCell(row, point && point.state !== null ? point.state : "-");
          appendCell(row, point ? excludedPointDetail(point) : "");
          excludedRows.append(row);
        });
      });
    });
    const available = data.sources.some((source) => source.status === "available");
    excludedTable.hidden = data.hours.length === 0;
    excludedEmpty.hidden = data.hours.length !== 0 || !available;
  };

  const renderHeader = (data = {}) => {
    if (scenario === "excluded") {
      badge.innerHTML = "<span></span> Excluded hours";
      rangeEyebrow.textContent = "Time window";
      rangeHeading.textContent = `Choose a time window in ${zoneName()}`;
      rangeHelp.textContent = `Lists every hour in the window that was left out of the imported history. ${rangeRules()}`;
      form.hidden = false;
      return;
    }
    const forecast = scenario === "forecast";
    const efficiency = scenario === "efficiency";
    const series = selectedSeries(data);
    const hasPv = hasSeriesData(series, "pv_generation_forecast");
    const hasImportPrice = hasSeriesData(series, "import_price_forecast");
    const hasExportPrice = hasSeriesData(series, "export_price_forecast");
    const efficiencyHistory = efficiency && series.find(
      (item) => item.available_start_time && item.available_end_time,
    );
    badge.innerHTML = `<span></span> ${forecast ? "Forecast inputs" : efficiency ? "Measured diagnostics" : "Historic actuals"}`;
    eyebrow.textContent = forecast ? "Planning inputs" : efficiency ? "Efficiency components" : "Imported series";
    rangeEyebrow.textContent = efficiency ? "Calculation period" : "Time window";
    rangeHeading.textContent = efficiency ? "Complete retained history" : `Choose a time window in ${zoneName()}`;
    rangeHelp.textContent = efficiency
      ? "Ratios use the complete retained battery and inverter history. Coverage and retrieval time are shown beside the results."
      : `Use ${zoneName()} date and time boundaries. ${rangeRules()}`;
    form.hidden = efficiency;
    if (forecast) {
      const forecastTypes = [];
      if (hasPv) forecastTypes.push("PV");
      if (hasImportPrice || hasExportPrice) forecastTypes.push("price");
      heading.textContent = forecastTypes.length
        ? `${forecastTypes.join(" and ")} forecast`
        : "Forecast";
    } else if (efficiency) {
      heading.textContent = "Battery and inverter efficiency";
    } else {
      heading.textContent = "Historic energy data";
    }
    interpretation.textContent = forecast
      ? "Forecasts are predictions, not measured actuals. Missing intervals remain gaps and are never treated as zero."
      : efficiency
        ? `These ratios are calculated from measured battery and inverter energy over the complete retained history${efficiencyHistory ? ` (${formatTimestamp(efficiencyHistory.available_start_time)} to ${formatTimestamp(efficiencyHistory.available_end_time)})` : ""}. Battery efficiency is one full-cycle value; inverter charge and discharge are separate conversion values.`
        : "These are imported actuals from the configured providers. Prices are those that applied in completed hours. They are not forecasts and do not describe an optimization plan; battery state of charge is the sample at the start of each hour.";
  };

  const loadExcluded = async (start, end) => {
    setStatus("Loading excluded hours...");
    diagnostic("debug", "data_load_started", { scenario, start, end });
    let requestId = "none";
    let responseStatus = "none";
    const params = new URLSearchParams({ start_time: utcTimestamp(start), end_time: utcTimestamp(end) });
    try {
      const response = await fetch(`/api/v1/dashboard/excluded-hours?${params}`);
      requestId = response.headers.get("X-Request-ID") || "none";
      responseStatus = response.status;
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "The excluded hours could not be loaded.");
      // A slower response must not show its panel after the user left this tab.
      if (scenario !== "excluded") return;
      renderExcluded(data);
      excludedContent.hidden = false;
      diagnostic("debug", "data_load_completed", { scenario, hours: data.excluded_hour_count, requestId });
      const withheld = data.sources.filter((source) => source.status === "invalid").map((source) => excludedSourceLabel(source.source));
      const count = `${data.excluded_hour_count} excluded hour${data.excluded_hour_count === 1 ? "" : "s"} listed.`;
      if (withheld.length) setStatus(`${count} Invalid data withheld: ${withheld.join(", ")}.`, "warning");
      else setStatus(count);
    } catch (error) {
      diagnostic("error", "data_load_failed", { scenario, status: responseStatus, message: error instanceof Error ? error.message : String(error), requestId });
      if (scenario === "excluded") setStatus(error instanceof Error ? error.message : "The excluded hours could not be loaded.", "error");
    }
  };

  const load = async (start, end, correctionAttempted = false) => {
    content.hidden = true;
    excludedContent.hidden = true;
    if (scenario === "excluded") {
      await loadExcluded(start, end);
      return;
    }
    const label = scenario === "forecast"
      ? "forecasts"
      : scenario === "efficiency"
        ? "efficiency diagnostics"
        : "imported actuals";
    setStatus(`Loading ${label}...`);
    diagnostic("debug", "data_load_started", { scenario, start, end });
    let requestId = "none";
    let responseStatus = "none";
    const requestedScenario = scenario;
    const params = new URLSearchParams({
      start_time: utcTimestamp(start), end_time: utcTimestamp(end), scenario_kind: scenario,
    });
    try {
      const response = await fetch(`/api/v1/dashboard/data?${params}`);
      requestId = response.headers.get("X-Request-ID") || "none";
      responseStatus = response.status;
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || "The dashboard data could not be loaded.");
      }
      // A slower response must not draw its view after the user left this tab.
      if (requestedScenario !== scenario) return;
      const alignedRange = alignRangeToCoverage(data, start, end);
      if (alignedRange && !correctionAttempted) {
        startInput.value = alignedRange.start;
        endInput.value = alignedRange.end;
        diagnostic("info", "range_aligned_to_coverage", { scenario, start: alignedRange.start, end: alignedRange.end, requestId });
        await load(alignedRange.start, alignedRange.end, true);
        return;
      }
      renderHeader(data); renderDetails(data); renderChart(data); content.hidden = false;
      const count = selectedSeries(data).reduce((total, item) => total + item.values.filter((value) => value !== null).length, 0);
      diagnostic("debug", "data_load_completed", { scenario, status: data.status, series: selectedSeries(data).length, points: count, requestId });
      if (data.status === "unavailable") {
        diagnostic("warn", "data_unavailable", { scenario, diagnostics: data.diagnostics, requestId });
      }
      const withheldAssets = (data.assets || [])
        .filter((asset) => asset.status === "invalid")
        .map((asset) => assetLabel(asset.asset));
      if (data.status === "unavailable") setStatus(data.diagnostics.join(" ") || "The selected data is unavailable.", "error");
      else if (data.status === "empty") setStatus("No data points are available in this range.", "warning");
      else if (withheldAssets.length) setStatus(`${count} data point${count === 1 ? "" : "s"} loaded. Invalid data withheld: ${withheldAssets.join(", ")}.`, "warning");
      else if (data.status === "stale") setStatus("Data is available, but its freshness window has expired.", "warning");
      else if (data.status === "partial") setStatus("Partial coverage is available. Missing intervals are shown as gaps.", "warning");
      else if (scenario === "efficiency") setStatus(`${count} efficiency value${count === 1 ? "" : "s"} loaded.`);
      else setStatus(`${count} data point${count === 1 ? "" : "s"} loaded.`);
    } catch (error) {
      diagnostic("error", "data_load_failed", { scenario, status: responseStatus, message: error instanceof Error ? error.message : String(error), requestId });
      setStatus(error instanceof Error ? error.message : "The dashboard data could not be loaded.", "error");
    }
  };

  const loadSelectedRange = () => {
    if (!timeZone) { setStatus(timeZoneProblem, "error"); return; }
    if (!isValidRange(startInput.value, endInput.value)) {
      setStatus("End time must be later than the start time.", "error");
      return;
    }
    load(startInput.value, endInput.value);
  };

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    scenario = { "forecast-tab": "forecast", "efficiency-tab": "efficiency", "excluded-tab": "excluded" }[tab.id] || "actual";
    diagnostic("info", "tab_clicked", { tab: tab.id, scenario });
    document.querySelectorAll(".tab").forEach((item) => {
      const active = item === tab;
      item.classList.toggle("is-active", active); item.setAttribute("aria-selected", String(active));
    });
    renderHeader();
    content.hidden = true;
    excludedContent.hidden = true;
    loadSelectedRange();
  }));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    loadSelectedRange();
  });

  const renderZoneLabels = () => {
    zoneLabel.textContent = timeZone;
    startLabel.textContent = `Start (${timeZone})`;
    endLabel.textContent = `End (${timeZone}, exclusive)`;
    excludedHourHeading.textContent = `Hour (${timeZone})`;
    excludedPointHeading.textContent = `Data point (${timeZone})`;
  };
  // The zone decides the default range, so no data is requested until it is
  // known and usable, and the range controls stay disabled until then. A failure
  // is reported instead of guessing a zone.
  const initialize = async () => {
    renderHeader();
    setStatus("Loading dashboard settings...");
    let requestId = "none";
    try {
      let response;
      try {
        response = await fetch("/api/v1/dashboard/settings");
      } catch {
        throw new Error("The dashboard settings could not be loaded; check the connection and reload the page.");
      }
      requestId = response.headers.get("X-Request-ID") || "none";
      const settings = response.ok ? await response.json().catch(() => null) : null;
      if (!settings || typeof settings.timezone !== "string" || !settings.timezone) {
        throw new Error(`The dashboard settings could not be loaded (HTTP ${response.status}); reload the page to try again.`);
      }
      try {
        useTimeZone(settings.timezone);
      } catch {
        throw new Error(`This browser does not support the configured time zone ${settings.timezone}, so no times can be shown or requested.`);
      }
    } catch (error) {
      timeZoneProblem = error instanceof Error ? error.message : String(error);
      diagnostic("error", "settings_load_failed", { message: timeZoneProblem, requestId });
      setStatus(timeZoneProblem, "error");
      return;
    }
    renderZoneLabels();
    renderHeader();
    const range = localDayRange(Date.now());
    startInput.value = range.start;
    endInput.value = range.end;
    form.querySelectorAll("input, button").forEach((control) => { control.disabled = false; });
    diagnostic("debug", "initialized", { scenario, timeZone });
    load(range.start, range.end);
  };
  initialize();
})();
