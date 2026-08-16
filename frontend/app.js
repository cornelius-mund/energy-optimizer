(() => {
  "use strict";

  const form = document.querySelector("#range-form");
  const startInput = document.querySelector("#start-date");
  const endInput = document.querySelector("#end-date");
  const status = document.querySelector("#status");
  const content = document.querySelector("#content");
  const details = document.querySelector("#details");

  const pad = (value) => String(value).padStart(2, "0");
  const isoDate = (date) => `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
  const nextDate = (value) => {
    const date = new Date(`${value}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + 1);
    return isoDate(date);
  };

  const today = new Date();
  const todayValue = isoDate(today);
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

  const renderDetails = (data) => {
    details.replaceChildren();
    addDetail("Status", data.status === "stale" ? "Stale actuals" : "Validated actuals");
    addDetail("Source", `${data.source.provider} / ${data.source.entity_id || "default"}`);
    addDetail("Unit", data.unit);
    addDetail("Coverage", data.coverage_start_time ? `${formatTimestamp(data.coverage_start_time)} to ${formatTimestamp(data.coverage_end_time)}` : "No points in range");
    addDetail("Available history", `${formatTimestamp(data.available_start_time)} to ${formatTimestamp(data.available_end_time)}`);
    addDetail("Freshness", data.freshness === "unknown" ? "Not configured" : data.freshness);
    addDetail("Retrieved", formatTimestamp(data.retrieved_at));
    addDetail("Latest observation", formatTimestamp(data.latest_observation_at));
  };

  const renderChart = (data) => {
    const grid = document.querySelector("#grid-lines");
    const labels = document.querySelector("#labels");
    const points = document.querySelector("#points");
    const line = document.querySelector("#line");
    const area = document.querySelector("#area");
    grid.replaceChildren(); labels.replaceChildren(); points.replaceChildren();
    if (!data.load_kw.length) {
      line.setAttribute("d", ""); area.setAttribute("d", "");
      return;
    }
    const left = 52; const right = 785; const top = 18; const bottom = 276;
    const max = Math.max(...data.load_kw, 1);
    const x = (index) => left + (index / Math.max(data.load_kw.length - 1, 1)) * (right - left);
    const y = (value) => bottom - (value / max) * (bottom - top);
    for (let index = 0; index <= 4; index += 1) {
      const value = max * (index / 4);
      const lineElement = document.createElementNS("http://www.w3.org/2000/svg", "line");
      lineElement.setAttribute("x1", left); lineElement.setAttribute("x2", right);
      lineElement.setAttribute("y1", y(value)); lineElement.setAttribute("y2", y(value));
      lineElement.setAttribute("class", "grid-line"); grid.append(lineElement);
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", 4); label.setAttribute("y", y(value) + 4); label.setAttribute("class", "axis-label");
      label.textContent = value.toFixed(1); labels.append(label);
    }
    const path = data.load_kw.map((value, index) => `${index ? "L" : "M"}${x(index).toFixed(2)},${y(value).toFixed(2)}`).join(" ");
    line.setAttribute("d", path);
    area.setAttribute("d", `${path} L${x(data.load_kw.length - 1)},${bottom} L${x(0)},${bottom} Z`);
    data.load_kw.forEach((value, index) => {
      const point = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      point.setAttribute("cx", x(index)); point.setAttribute("cy", y(value)); point.setAttribute("r", data.load_kw.length > 48 ? 2.5 : 4); point.setAttribute("class", "point");
      point.setAttribute("aria-label", `${formatTimestamp(data.timestamps[index])}: ${value} kW`);
      points.append(point);
    });
  };

  const load = async (start, end) => {
    content.hidden = true;
    setStatus("Loading imported actuals...");
    const params = new URLSearchParams({
      start_time: `${start}T00:00:00Z`, end_time: `${nextDate(end)}T00:00:00Z`,
    });
    try {
      const response = await fetch(`/api/v1/historic/household-load?${params}`);
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 404) {
          throw new Error("No persisted household-load history is available yet.");
        }
        if (response.status === 422) {
          throw new Error("The selected time range is invalid. Choose a valid UTC range.");
        }
        if (response.status === 503) {
          throw new Error("Historic data is unavailable because stored provider data could not be recovered.");
        }
        throw new Error(data.detail || "The historic data could not be loaded.");
      }
      renderDetails(data); renderChart(data); content.hidden = false;
      if (data.status === "empty") setStatus("No imported household-load points are available in this range.", "warning");
      else if (data.status === "stale") setStatus("The selected actuals are valid historical data, but the latest provider observation is stale.", "warning");
      else setStatus(`${data.load_kw.length} hourly actual${data.load_kw.length === 1 ? "" : "s"} loaded.`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "The historic data could not be loaded.", "error");
    }
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (endInput.value < startInput.value) {
      setStatus("End date must be on or after the start date.", "error");
      return;
    }
    load(startInput.value, endInput.value);
  });
  load(todayValue, todayValue);
})();
