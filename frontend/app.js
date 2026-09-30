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
  const status = document.querySelector("#status");
  const content = document.querySelector("#content");
  const details = document.querySelector("#details");
  const tooltip = document.querySelector("#point-tooltip");
  const badge = document.querySelector("#scenario-badge");
  const heading = document.querySelector("#chart-heading");
  const eyebrow = document.querySelector("#series-eyebrow");
  const legend = document.querySelector("#legend");
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
    battery: {
      element: document.querySelector("#battery-chart"),
      grid: document.querySelector("#battery-grid-lines"),
      labels: document.querySelector("#battery-labels"),
      points: document.querySelector("#battery-points"),
      seriesPaths: document.querySelector("#battery-series-paths"),
      axisUnit: document.querySelector("#battery-axis-unit"),
      title: document.querySelector("#battery-chart-title"),
      description: document.querySelector("#battery-chart-description"),
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

  const renderGraph = (definition, series) => {
    const { element, grid, labels, points, seriesPaths, axisUnit, title, description } = definition;
    grid.replaceChildren(); labels.replaceChildren(); points.replaceChildren(); seriesPaths.replaceChildren();
      element.removeAttribute("hidden");
    const unit = unitForSeries(series[0]);
    axisUnit.textContent = unit;
    title.textContent = `${series.map(seriesLabel).join(" and ")} (${unit})`;
    description.textContent = `${series.map((item) => `${seriesLabel(item)} in ${unitForSeries(item)}`).join("; ")}. Missing intervals remain gaps.`;
    const valueList = series.flatMap((item) => item.values);
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
      label.textContent = formatAxisTimestamp(timestamps[index]);
      labels.append(label);
    }
    series.forEach((item, seriesIndex) => {
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
      element.setAttribute("hidden", "");
      grid.replaceChildren(); labels.replaceChildren(); points.replaceChildren(); seriesPaths.replaceChildren();
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
    chartNote.textContent = "Each point represents one hourly interval. Charts are separated by unit. Focus a point to inspect it.";
    Object.entries(chartSeries(series)).forEach(([kind, groupedSeries]) => {
      const usableSeries = groupedSeries.filter((item) => hasSeriesData([item], item.id));
      if (usableSeries.length) renderGraph(chartDefinitions[kind], usableSeries);
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
      parts.push(`previous ${excludedNumber(point.previous_value)}${point.unit ? ` ${point.unit}` : ""} at ${formatAxisTimestamp(point.previous_timestamp)}`);
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
          appendCell(row, formatAxisTimestamp(hour.hour_start), repeated);
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
          appendCell(row, point ? formatAxisTimestamp(point.timestamp) : "-");
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
      rangeHeading.textContent = "Choose a UTC time window";
      rangeHelp.textContent = "Lists every hour in the window that was left out of the imported history. The end time is exclusive and must be later than the start.";
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
    rangeHeading.textContent = efficiency ? "Complete retained history" : "Choose a UTC time window";
    rangeHelp.textContent = efficiency
      ? "Ratios use the complete retained battery and inverter history. Coverage and retrieval time are shown beside the results."
      : "Use UTC date and time boundaries. The end time is exclusive and must be later than the start.";
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
    legend.replaceChildren();
    const labels = forecast
      ? [
        ["pv_generation_forecast", "PV generation", "legend-0"],
        ["import_price_forecast", "Import price", "legend-1"],
        ["export_price_forecast", "Export price", "legend-2"],
      ].filter(([id]) => hasSeriesData(series, id))
      : efficiency
        ? []
      : [
        ["household_load_actual", "Household load", "legend-0"],
        ["grid_import_actual", "Grid import", "legend-1"],
        ["grid_export_actual", "Grid export", "legend-2"],
        ["import_price_actual", "Import price", "legend-1"],
        ["export_price_actual", "Export price", "legend-2"],
        ["battery_state_of_charge_actual", "Battery state of charge", "legend-0"],
      ].filter(([id]) => hasSeriesData(series, id));
    labels.forEach(([id, label, legendClass]) => {
      const item = document.createElement("span");
      item.className = `legend-item ${legendClass}`;
      const source = series.find((candidate) => candidate.id === id);
      item.textContent = `${label} (${unitForSeries(source)})`;
      legend.append(item);
    });
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
