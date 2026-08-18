(() => {
  "use strict";

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
  const nextDate = (value) => {
    const date = new Date(`${value}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + 1);
    return isoDate(date);
  };
  const todayValue = isoDate(new Date());
  startInput.value = todayValue;
  endInput.value = todayValue;

  const setStatus = (message, kind = "") => {
    status.className = `status ${kind}`;
    status.textContent = message;
  };

  const formatTimestamp = (value) => new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
  }).format(new Date(value));

  const addDetail = (label, value) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    details.append(term, description);
  };

  const selectedSeries = (data) => data.series || [];
  const adjustStartDate = (data) => {
    if (scenario !== "actual" || !data.series?.[0]?.available_start_time) return;
    const availableDate = isoDate(new Date(data.series[0].available_start_time));
    if (availableDate > startInput.value) startInput.value = availableDate;
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
    addDetail("Unit", first.unit);
    addDetail("Coverage", first.available_start_time ? `${formatTimestamp(first.available_start_time)} to ${formatTimestamp(first.available_end_time)}` : "No points in range");
    addDetail("Freshness", first.freshness);
    addDetail("Retrieved", first.retrieved_at ? formatTimestamp(first.retrieved_at) : "Not available");
    if (first.generated_at) addDetail("Generated", formatTimestamp(first.generated_at));
    if (first.published_at) addDetail("Published", formatTimestamp(first.published_at));
  };

  const renderChart = (data) => {
    const grid = document.querySelector("#grid-lines");
    const labels = document.querySelector("#labels");
    const points = document.querySelector("#points");
    const seriesPaths = document.querySelector("#series-paths");
    const line = document.querySelector("#line");
    const area = document.querySelector("#area");
    grid.replaceChildren(); labels.replaceChildren(); points.replaceChildren(); seriesPaths.replaceChildren();
    hidePoint();
    const series = selectedSeries(data);
    if (!series.length || !series.some((item) => item.timestamps.length)) {
      line.setAttribute("d", ""); area.setAttribute("d", "");
      return;
    }
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
    series.forEach((item, seriesIndex) => {
      let path = "";
      item.values.forEach((value, index) => {
        if (value === null) return;
        const command = index && item.values[index - 1] !== null ? "L" : "M";
        path += `${command}${x(index, item.values.length).toFixed(2)},${y(value).toFixed(2)} `;
        const point = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        point.setAttribute("cx", x(index, item.values.length)); point.setAttribute("cy", y(value)); point.setAttribute("r", item.values.length > 48 ? 2.5 : 4); point.setAttribute("class", `point point-${seriesIndex}`);
        point.setAttribute("tabindex", "0");
        point.setAttribute("aria-label", `${item.id}, ${formatTimestamp(item.timestamps[index])}: ${value} ${item.unit}`);
        point.addEventListener("pointerenter", () => showPoint(item.timestamps[index], value, item.unit, point));
        point.addEventListener("pointerleave", hidePoint);
        point.addEventListener("focus", () => showPoint(item.timestamps[index], value, item.unit, point));
        point.addEventListener("blur", hidePoint);
        points.append(point);
      });
      const seriesLine = document.createElementNS("http://www.w3.org/2000/svg", "path");
      seriesLine.setAttribute("d", path.trim());
      seriesLine.setAttribute("class", `series-line series-${seriesIndex}`);
      seriesPaths.append(seriesLine);
    });
    line.setAttribute("d", "");
    area.setAttribute("d", "");
  };

  const renderHeader = () => {
    const forecast = scenario === "forecast";
    badge.innerHTML = `<span></span> ${forecast ? "Forecast inputs" : "Historic actuals"}`;
    eyebrow.textContent = forecast ? "Planning inputs" : "Imported series";
    heading.textContent = forecast ? "PV and price forecast" : "Household load";
    interpretation.textContent = forecast
      ? "Forecasts are predictions, not measured actuals. Missing intervals remain gaps and are never treated as zero."
      : "These are imported actuals from the configured provider. They are not forecasts and do not describe an optimization plan.";
    legend.replaceChildren();
    const labels = forecast ? ["PV generation", "Import price", "Export price"] : ["Household load"];
    labels.forEach((label, index) => {
      const item = document.createElement("span");
      item.className = `legend-item legend-${index}`;
      item.textContent = label;
      legend.append(item);
    });
  };

  const load = async (start, end) => {
    content.hidden = true;
    setStatus(`Loading ${scenario === "forecast" ? "forecasts" : "imported actuals"}...`);
    const params = new URLSearchParams({
      start_time: `${start}T00:00:00Z`, end_time: `${nextDate(end)}T00:00:00Z`, scenario_kind: scenario,
    });
    try {
      const response = await fetch(`/api/v1/dashboard/data?${params}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "The dashboard data could not be loaded.");
      adjustStartDate(data); renderHeader(); renderDetails(data); renderChart(data); content.hidden = false;
      const count = selectedSeries(data).reduce((total, item) => total + item.values.filter((value) => value !== null).length, 0);
      if (data.status === "unavailable") setStatus(data.diagnostics.join(" ") || "The selected data is unavailable.", "error");
      else if (data.status === "empty") setStatus("No data points are available in this range.", "warning");
      else if (data.status === "stale") setStatus("Data is available, but its freshness window has expired.", "warning");
      else if (data.status === "partial") setStatus("Partial coverage is available. Missing intervals are shown as gaps.", "warning");
      else setStatus(`${count} data point${count === 1 ? "" : "s"} loaded.`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "The dashboard data could not be loaded.", "error");
    }
  };

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    scenario = tab.id === "forecast-tab" ? "forecast" : "actual";
    document.querySelectorAll(".tab").forEach((item) => {
      const active = item === tab;
      item.classList.toggle("is-active", active); item.setAttribute("aria-selected", String(active));
    });
    load(startInput.value, endInput.value);
  }));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (endInput.value < startInput.value) { setStatus("End date must be on or after the start date.", "error"); return; }
    load(startInput.value, endInput.value);
  });
  renderHeader();
  load(todayValue, todayValue);
})();
