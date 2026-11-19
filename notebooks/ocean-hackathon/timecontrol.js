// Ensure 'for' is bound synchronously if available
const currentFor = view.model.get("for_");
if (currentFor) {
  el["for"] = currentFor;
}

// Child components per Storybook specification
const dateEl = document.createElement("eox-timecontrol-date");
dateEl.navigation = true;
el.appendChild(dateEl);

const pickerEl = document.createElement("eox-timecontrol-picker");
pickerEl.showDots = true;
pickerEl.popup = true;
el.appendChild(pickerEl);

const sliderEl = document.createElement("eox-timecontrol-slider");
sliderEl.style.width = "100%";
el.appendChild(sliderEl);

// Date change handler to update the GeoZarr layer slice on the map
function handleDateChange(dateValue) {
  if (!dateValue) return;
  const raw = Array.isArray(dateValue) ? dateValue[0] : dateValue;
  if (!raw) return;
  const dateObj = new Date(raw);
  if (isNaN(dateObj.getTime())) return;

  const isoDate = dateObj.toISOString();
  const dateStr = isoDate.split("T")[0];

  const forSelector = el.for || view.model.get("for_") || "#main-map";
  const mapEl =
    document.querySelector(forSelector) ||
    document.querySelector("eox-map#main-map") ||
    document.querySelector("eox-map");
  if (!mapEl || !mapEl.map) return;

  const layers =
    typeof mapEl.getFlatLayersArray === "function"
      ? mapEl.getFlatLayersArray(mapEl.map.getLayers().getArray())
      : mapEl.map.getLayers().getArray();

  const targetLayer = layers.find(
    (l) =>
      l &&
      typeof l.get === "function" &&
      (l.get("id") === "sea-ice-cube" || l.get("layerDatetime")),
  );
  if (!targetLayer) return;

  const layerDatetime = targetLayer.get("layerDatetime");
  const sliceMap = layerDatetime?.sliceMap;
  const source =
    typeof targetLayer.getSource === "function"
      ? targetLayer.getSource()
      : null;

  if (source && typeof source.updateDimensions === "function" && sliceMap) {
    let sliceIndex = sliceMap[isoDate];
    if (sliceIndex === undefined) {
      const matchedKey = Object.keys(sliceMap).find(
        (k) => k.startsWith(dateStr) || dateStr.startsWith(k.split("T")[0]),
      );
      if (matchedKey !== undefined) {
        sliceIndex = sliceMap[matchedKey];
      }
    }
    if (sliceIndex !== undefined) {
      source.updateDimensions({ time: sliceIndex });
    }
  }

  if (layerDatetime) {
    layerDatetime.currentStep = isoDate;
  }

  if (typeof targetLayer.dispatchEvent === "function") {
    targetLayer.dispatchEvent(
      new CustomEvent("datetime:updated", {
        bubbles: true,
        detail: {
          datetime: isoDate,
          layer: targetLayer,
        },
      }),
    );
  }
}

// Listen for select and change events on timecontrol
el.addEventListener("select", (evt) => {
  handleDateChange(evt.detail?.date);
});

el.addEventListener("change", (evt) => {
  handleDateChange(evt.detail?.date || el.selectedDateRange);
});

// Intercept dateChange method calls
const origDateChange = el.dateChange;
el.dateChange = function (dateRange, instance) {
  if (typeof origDateChange === "function") {
    try {
      origDateChange.call(this, dateRange, instance || this);
    } catch (e) {
      console.warn("dateChange error:", e);
    }
  }
  handleDateChange(dateRange);
};

// Sync with map layer datetime/timeControlValues when available
const syncLayers = () => {
  const forSelector = el.for || view.model.get("for_") || "#main-map";
  const mapEl =
    document.querySelector(forSelector) ||
    document.querySelector("eox-map#main-map") ||
    document.querySelector("eox-map");
  if (!mapEl || !mapEl.map) return;
  const layers =
    typeof mapEl.getFlatLayersArray === "function"
      ? mapEl.getFlatLayersArray(mapEl.map.getLayers().getArray())
      : mapEl.map.getLayers().getArray();
  const targetLayer = layers.find(
    (l) =>
      l &&
      typeof l.get === "function" &&
      (l.get("id") === "sea-ice-cube" || l.get("layerDatetime")),
  );
  if (targetLayer) {
    const tcValues = targetLayer.get("timeControlValues");
    if (
      tcValues &&
      tcValues.length &&
      (!el.controlValues || !el.controlValues.length)
    ) {
      el.controlValues = [
        {
          id: targetLayer.get("id") || "sea-ice-cube",
          name:
            targetLayer.get("name") || targetLayer.get("title") || "GeoZarr",
          timeControlValues: tcValues,
        },
      ];
    }
    if (typeof targetLayer.on === "function") {
      targetLayer.on("change:timeControlValues", () => {
        const updated = targetLayer.get("timeControlValues");
        if (updated && updated.length) {
          el.controlValues = [
            {
              id: targetLayer.get("id") || "sea-ice-cube",
              name:
                targetLayer.get("name") ||
                targetLayer.get("title") ||
                "GeoZarr",
              timeControlValues: updated,
            },
          ];
        }
      });
    }
  }
};

const syncCheck = setInterval(() => {
  syncLayers();
  if (el.controlValues && el.controlValues.length) {
    clearInterval(syncCheck);
  }
}, 200);
setTimeout(() => clearInterval(syncCheck), 15000);
view.el.appendChild(el);
