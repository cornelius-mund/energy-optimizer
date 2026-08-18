(() => {
  "use strict";

  const diagnostic = (level, event, details = {}) => {
    const write = console[level] || console.debug;
    write.call(console, `[dashboard] ${event}`, details);
  };

  const form = document.querySelector("#range-form");
  const startInput = document.querySelector("#start-date");
  const endInput = document.querySelector("#end-date");
  const status = document.querySelector("#status");
  const content = document.querySelector("#content");
  const details = document.querySelector("#details");
  const tooltip = document.querySelector("#point-tooltip");
  const badge = document.querySelector("#scenario-badge");
  const heading = document.querySelector("#chart-heading");
  const eyebrow = document.querySelector("#series-eyebrow");
  const legend = document.querySelector("#legend");
  const interpretation = document.querySelector("#interpretation-text");
  let scenario = "actual";

  const pad = (value) => String(value).padStart(2, "0");
  const isoDate = (date) => `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
  const dateTimeInputValue = (value) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return `${isoDate(date)}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
  };
  const utcTimestamp = (value) => `${value}:00Z`;
  const parseInputTimestamp = (value) => new Date(utcTimestamp(value));
  const isValidRange = (start, end) => {
    const startTimestamp = parseInputTimestamp(start).getTime();
    const endTimestamp = parseInputTimestamp(end).getTime();
    return Number.isFinite(startTimestamp) && Number.isFinite(endTimestamp) && endTimestamp > startTimestamp;
  };
  const today = new Date();
  today.setUTCHours(0, 0, 0, 0);
  const todayValue = dateTimeInputValue(today);
  const tomorrowValue = dateTimeInputValue(new Date(today.getTime() + 24 * 60 * 60 * 1000));
  startInput.value = todayValue;
  endInput.value = tomorrowValue;

  const setStatus = (message, kind = "") => {
    status.className = `status ${kind}`;
    status.textContent = message;
  };

  const formatTimestamp = (value) => new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
  }).format(new Date(value));
  const formatAxisTimestamp = (value) => {
    const date = new Date(value);
    return `${isoDate(date)} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}Z`;
  };

  const addDetail = (label, value) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    details.append(term, description);
  };

  const selectedSeries = (data) => data.series || [];
  const unitForSeries = (item) => item.id === "import_price_forecast" || item.id === "export_price_forecast"
    ? "EUR/kWh"
    : item.id === "pv_generation_forecast" || item.id === "household_load_actual"
      ? "kW"
      : item.unit;
  const seriesLabel = (item) => ({
    household_load_actual: "Household load",
    pv_generation_forecast: "PV generation",
    import_price_forecast: "Import price",
    export_price_forecast: "Export price",
  }[item.id] || item.id);
  const chartDefinitions = {
    power: {
      element: document.querySelector("#power-chart"),
      grid: document.querySelector("#power-grid-lines"),
      labels: document.querySelector("#power-labels"),
      points: document.querySelector("#power-points"),
      seriesPaths: document.querySelector("#power-series-paths"),
      axisUnit: document.querySelector("#power-axis-unit"),
      title: document.querySelector("#power-chart-title"),
      description: document.querySelector("#power-chart-description"),
    },
    price: {
      element: document.querySelector("#price-chart"),
      grid: document.querySelector("#price-grid-lines"),
      labels: document.querySelector("#price-labels"),
      points: document.querySelector("#price-points"),
      seriesPaths: document.querySelector("#price-series-paths"),
      axisUnit: document.querySelector("#price-axis-unit"),
      title: document.querySelector("#price-chart-title"),
      description: document.querySelector("#price-chart-description"),
    },
  };
  const chartSeries = (series) => ({
    power: series.filter((item) => ["household_load_actual", "pv_generation_forecast"].includes(item.id)),
    price: series.filter((item) => ["import_price_forecast", "export_price_forecast"].includes(item.id)),
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
  const showPoint = (timestamp, value, unit, point) => {
    tooltip.textContent = `${formatTimestamp(timestamp)} · ${value} ${unit}`;
    tooltip.hidden = false;
    const chart = document.querySelector("#chart").getBoundingClientRect();
    const pointBox = point.getBoundingClientRect();
    const left = Math.min(Math.max(pointBox.left - chart.left, 8), chart.width - 180);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${Math.max(pointBox.top - chart.top - 42, 4)}px`;
  };
  const hidePoint = () => { tooltip.hidden = true; };
  const hasSeriesData = (series, id) => series.some(
    (item) => item.id === id && item.values.some((value) => value !== null),
  );
  const seriesClass = (item, index) => ({
    household_load_actual: "series-0",
    pv_generation_forecast: "series-0",
    import_price_forecast: "series-1",
    export_price_forecast: "series-2",
  }[item.id] || `series-${index}`);

  const renderDetails = (data) => {
    details.replaceChildren();
    const series = selectedSeries(data);
    const first = series[0];
    addDetail("Status", data.status[0].toUpperCase() + data.status.slice(1));
    if (!first) {
      addDetail("Coverage", "No points in range");
      return;
    }
    addDetail("Source", `${first.source?.provider || "Unavailable"} / ${first.source?.entity_id || "default"}`);
    const units = [...new Set(series.filter((item) => hasSeriesData([item], item.id)).map(unitForSeries))];
    addDetail(units.length === 1 ? "Unit" : "Units", units.join(", "));
    addDetail("Coverage", first.available_start_time ? `${formatTimestamp(first.available_start_time)} to ${formatTimestamp(first.available_end_time)}` : "No points in range");
    addDetail("Freshness", first.freshness);
    addDetail("Retrieved", first.retrieved_at ? formatTimestamp(first.retrieved_at) : "Not available");
    if (first.generated_at) addDetail("Generated", formatTimestamp(first.generated_at));
    if (first.published_at) addDetail("Published", formatTimestamp(first.published_at));
  };

  const renderGraph = (definition, series) => {
    const { element, grid, labels, points, seriesPaths, axisUnit, title, description } = definition;
    grid.replaceChildren(); labels.replaceChildren(); points.replaceChildren(); seriesPaths.replaceChildren();
    element.hidden = false;
    const unit = unitForSeries(series[0]);
    axisUnit.textContent = unit;
    title.textContent = `${series.map(seriesLabel).join(" and ")} (${unit})`;
    description.textContent = `${series.map((item) => `${seriesLabel(item)} in ${unitForSeries(item)}`).join("; ")}. Missing intervals remain gaps.`;
    const valueList = series.flatMap((item) => item.values).filter((value) => value !== null);
    const left = 52; const right = 785; const top = 18; const bottom = 276;
    const min = Math.min(...valueList, 0);
    const max = Math.max(...valueList, 1);
    const x = (index, count) => left + (index / Math.max(count - 1, 1)) * (right - left);
    const y = (value) => bottom - ((value - min) / Math.max(max - min, 1)) * (bottom - top);
    for (let index = 0; index <= 4; index += 1) {
      const value = min + (max - min) * (index / 4);
      const lineElement = document.createElementNS("http://www.w3.org/2000/svg", "line");
      lineElement.setAttribute("x1", left); lineElement.setAttribute("x2", right);
      lineElement.setAttribute("y1", y(value)); lineElement.setAttribute("y2", y(value));
      lineElement.setAttribute("class", "grid-line"); grid.append(lineElement);
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", 4); label.setAttribute("y", y(value) + 4); label.setAttribute("class", "axis-label");
      label.textContent = value.toFixed(1); labels.append(label);
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
      label.textContent = formatAxisTimestamp(timestamps[index]);
      labels.append(label);
    }
    series.forEach((item, seriesIndex) => {
      const className = seriesClass(item, seriesIndex);
      let path = "";
      item.values.forEach((value, index) => {
        if (value === null) return;
        const command = index && item.values[index - 1] !== null ? "L" : "M";
        path += `${command}${x(index, item.values.length).toFixed(2)},${y(value).toFixed(2)} `;
        const point = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        point.setAttribute("cx", x(index, item.values.length)); point.setAttribute("cy", y(value)); point.setAttribute("r", item.values.length > 48 ? 2.5 : 4); point.setAttribute("class", `point point-${className.replace("series-", "")}`);
        point.setAttribute("tabindex", "0");
        point.setAttribute("aria-label", `${seriesLabel(item)}, ${formatTimestamp(item.timestamps[index])}: ${value} ${unitForSeries(item)}`);
        point.addEventListener("pointerenter", () => showPoint(item.timestamps[index], value, unitForSeries(item), point));
        point.addEventListener("pointerleave", hidePoint);
        point.addEventListener("focus", () => showPoint(item.timestamps[index], value, unitForSeries(item), point));
        point.addEventListener("blur", hidePoint);
        points.append(point);
      });
      const seriesLine = document.createElementNS("http://www.w3.org/2000/svg", "path");
      seriesLine.setAttribute("d", path.trim());
      seriesLine.setAttribute("class", `series-line ${className}`);
      seriesPaths.append(seriesLine);
    });
  };

  const renderChart = (data) => {
    Object.values(chartDefinitions).forEach(({ element, grid, labels, points, seriesPaths }) => {
      element.hidden = true;
      grid.replaceChildren(); labels.replaceChildren(); points.replaceChildren(); seriesPaths.replaceChildren();
    });
    hidePoint();
    const series = selectedSeries(data);
    Object.entries(chartSeries(series)).forEach(([kind, groupedSeries]) => {
      const usableSeries = groupedSeries.filter((item) => hasSeriesData([item], item.id));
      if (usableSeries.length) renderGraph(chartDefinitions[kind], usableSeries);
    });
  };

  const renderHeader = (data = {}) => {
    const forecast = scenario === "forecast";
    const series = selectedSeries(data);
    const hasPv = hasSeriesData(series, "pv_generation_forecast");
    const hasImportPrice = hasSeriesData(series, "import_price_forecast");
    const hasExportPrice = hasSeriesData(series, "export_price_forecast");
    badge.innerHTML = `<span></span> ${forecast ? "Forecast inputs" : "Historic actuals"}`;
    eyebrow.textContent = forecast ? "Planning inputs" : "Imported series";
    if (forecast) {
      const forecastTypes = [];
      if (hasPv) forecastTypes.push("PV");
      if (hasImportPrice || hasExportPrice) forecastTypes.push("price");
      heading.textContent = forecastTypes.length
        ? `${forecastTypes.join(" and ")} forecast`
        : "Forecast";
    } else {
      heading.textContent = "Household load";
    }
    interpretation.textContent = forecast
      ? "Forecasts are predictions, not measured actuals. Missing intervals remain gaps and are never treated as zero."
      : "These are imported actuals from the configured provider. They are not forecasts and do not describe an optimization plan.";
    legend.replaceChildren();
    const labels = forecast
      ? [
        ["pv_generation_forecast", "PV generation", "legend-0"],
        ["import_price_forecast", "Import price", "legend-1"],
        ["export_price_forecast", "Export price", "legend-2"],
      ].filter(([id]) => hasSeriesData(series, id))
      : [["household_load_actual", "Household load", "legend-0"]];
    labels.forEach(([id, label, legendClass]) => {
      const item = document.createElement("span");
      item.className = `legend-item ${legendClass}`;
      const source = series.find((candidate) => candidate.id === id);
      item.textContent = `${label} (${unitForSeries(source)})`;
      legend.append(item);
    });
  };

  const load = async (start, end, correctionAttempted = false) => {
    content.hidden = true;
    setStatus(`Loading ${scenario === "forecast" ? "forecasts" : "imported actuals"}...`);
    diagnostic("debug", "data_load_started", { scenario, start, end });
    let requestId = "none";
    let responseStatus = "none";
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
      if (data.status === "unavailable") setStatus(data.diagnostics.join(" ") || "The selected data is unavailable.", "error");
      else if (data.status === "empty") setStatus("No data points are available in this range.", "warning");
      else if (data.status === "stale") setStatus("Data is available, but its freshness window has expired.", "warning");
      else if (data.status === "partial") setStatus("Partial coverage is available. Missing intervals are shown as gaps.", "warning");
      else setStatus(`${count} data point${count === 1 ? "" : "s"} loaded.`);
    } catch (error) {
      diagnostic("error", "data_load_failed", { scenario, status: responseStatus, message: error instanceof Error ? error.message : String(error), requestId });
      setStatus(error instanceof Error ? error.message : "The dashboard data could not be loaded.", "error");
    }
  };

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    scenario = tab.id === "forecast-tab" ? "forecast" : "actual";
    diagnostic("info", "tab_clicked", { tab: tab.id, scenario });
    document.querySelectorAll(".tab").forEach((item) => {
      const active = item === tab;
      item.classList.toggle("is-active", active); item.setAttribute("aria-selected", String(active));
    });
    if (!isValidRange(startInput.value, endInput.value)) {
      setStatus("End time must be later than the start time.", "error");
      return;
    }
    load(startInput.value, endInput.value);
  }));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!isValidRange(startInput.value, endInput.value)) { setStatus("End time must be later than the start time.", "error"); return; }
    load(startInput.value, endInput.value);
  });
  renderHeader();
  diagnostic("debug", "initialized", { scenario });
  load(todayValue, tomorrowValue);
})();
