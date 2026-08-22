const API_ENDPOINT =
    "/api/nightways";
const ORIGIN_SUGGESTION_ENDPOINT =
    "/api/origin-suggestions";
const ORIGIN_SUGGESTION_DEBOUNCE_MS = 300;
const ORIGIN_SUGGESTION_MIN_CHARACTERS = 3;
const ORIGIN_SUGGESTION_DISPLAY_LIMIT = 5;

const resultsHeading = document.getElementById("results-heading");
const destinationCount = document.getElementById("destination-count");
const destinationList = document.getElementById("destination-list");
const mapElement = document.getElementById("map");
const resultsSection = document.getElementById("results");
const searchForm = document.getElementById("search-form");
const searchStatus = document.getElementById("search-status");
const searchButton = document.getElementById("search-button");
const originInput = document.getElementById("origin");
const originAutocomplete = document.getElementById(
    "origin-autocomplete"
);
const originSuggestionsList = document.getElementById(
    "origin-suggestions"
);
const dateInput = document.getElementById("date");
const sortSelect = document.getElementById("destination-sort");
const modeFilterButtons = document.querySelectorAll(
    ".mode-filter-button"
);
const SEARCH_BUTTON_LABEL =
    searchButton.textContent.trim();

const localDate = new Date();

dateInput.value = [
    localDate.getFullYear(),
    String(localDate.getMonth() + 1).padStart(2, "0"),
    String(localDate.getDate()).padStart(2, "0")
].join("-");

const TRAIN_SERVICE_MODES = new Set([
    "TRAIN",
    "LONG_DISTANCE",
    "REGIONAL_RAIL"
]);

const DESTINATION_SORTS = Object.freeze({
    EARLIEST_ARRIVAL: "EARLIEST_ARRIVAL",
    LATEST_DEPARTURE: "LATEST_DEPARTURE",
    LONGEST_JOURNEY: "LONGEST_JOURNEY",
    NAME: "NAME"
});

const DESTINATION_NAME_COLLATOR = new Intl.Collator(
    "en",
    {
        numeric: true,
        sensitivity: "base"
    }
);

const CARD_INTERACTIVE_SELECTOR = [
    "a",
    "button",
    "input",
    "select",
    "textarea",
    "summary",
    "[role]",
    "[contenteditable]:not([contenteditable='false'])",
    "[tabindex]:not([tabindex='-1'])"
].join(",");

let activeMode = "ALL";
let activeSort =
    DESTINATION_SORTS.EARLIEST_ARRIVAL;
let nightwaysData = null;
let currentSearchController = null;
let selectedDestinationView = null;
let suggestionDebounceTimer = null;
let currentSuggestionController = null;
let suggestionRequestSequence = 0;
let activeSuggestionIndex = -1;
let currentSuggestions = [];
let isOriginComposing = false;


// --------------------------------------------------
// MAP
// --------------------------------------------------

const DEFAULT_MAP_CENTER = [50.5, 10.5];
const DEFAULT_MAP_ZOOM = 4;
const ORIGIN_ONLY_ZOOM = 9;
const DESTINATION_FOCUS_ZOOM = 7;

const map = L.map("map").setView(
    DEFAULT_MAP_CENTER,
    DEFAULT_MAP_ZOOM
);

L.tileLayer(
    "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors"
    }
).addTo(map);


// --------------------------------------------------
// MAP PREVIEW CARD STYLES
// --------------------------------------------------

const previewStyle = document.createElement("style");

previewStyle.textContent = `
    .map-preview-card {
        width: 235px;
        box-sizing: border-box;

        background: rgba(8, 29, 54, 0.94);
        backdrop-filter: blur(8px);

        border: 1px solid rgba(170, 210, 245, 0.30);
        border-radius: 16px;

        padding: 18px 18px 16px;

        color: #eef7ff;

        box-shadow:
            0 12px 30px rgba(3, 15, 30, 0.24);

        pointer-events: none;
    }

    .map-preview-card h3 {
        margin: 0 0 3px;

        font-size: 1.15rem;
        letter-spacing: 0.02em;

        color: #ffffff;
    }

    .map-preview-country {
        margin: 0 0 18px;

        font-size: 0.82rem;

        color: rgba(205, 227, 247, 0.72);
    }

    .map-preview-label {
        margin: 0 0 3px;

        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.09em;

        color: rgba(180, 210, 238, 0.65);
    }

    .map-preview-arrival {
        margin: 0 0 15px;

        font-size: 1.45rem;
        font-weight: 600;

        color: #d8ecff;
    }

    .map-preview-meta {
        margin: 0 0 14px;

        font-size: 0.8rem;

        color: rgba(215, 232, 247, 0.82);
    }

    .map-preview-services {
        padding-top: 12px;

        border-top:
            1px solid rgba(180, 215, 245, 0.17);
    }

    .map-preview-service {
        display: flex;
        justify-content: space-between;
        gap: 10px;

        margin: 6px 0;

        font-size: 0.76rem;

        color: rgba(226, 239, 250, 0.88);
    }

    .map-preview-service-time {
        color: #9fc9ee;
        white-space: nowrap;
    }

    .map-preview-placeholder {
        margin: 0;

        font-size: 0.82rem;
        line-height: 1.5;

        color: rgba(215, 232, 247, 0.70);
    }
`;

document.head.appendChild(previewStyle);


// --------------------------------------------------
// MAP PREVIEW CONTROL
// --------------------------------------------------

const previewControl = L.control({
    position: "topright"
});

let previewContainer = null;


previewControl.onAdd = function () {

    previewContainer =
        L.DomUtil.create(
            "div",
            "map-preview-card"
        );

    previewContainer.innerHTML = `
        <p class="map-preview-placeholder">
            Hover over a destination
            to see the overnight connection.
        </p>
    `;

    return previewContainer;
};


previewControl.addTo(map);


// --------------------------------------------------
// ORIGIN MARKER
// --------------------------------------------------

// Teardrop origin marker
const originIcon = L.divIcon({
    className: "",
    html: `
        <div class="origin-pin">
            <div class="origin-pin-center"></div>
        </div>
    `,
    iconSize: [26, 34],
    iconAnchor: [13, 34],
    popupAnchor: [0, -32]
});


const originMarkers =
    L.layerGroup().addTo(map);


function renderOriginMarker(originName, coordinates) {

    L.circleMarker(
        coordinates,
        {
            radius: 13,
            color: "#d94a5a",
            weight: 1,
            opacity: 0.35,
            fillColor: "#d94a5a",
            fillOpacity: 0.08
        }
    ).addTo(originMarkers);


    const popupContent =
        document.createElement("div");

    const popupOrigin =
        document.createElement("strong");

    popupOrigin.textContent = originName;
    popupContent.append(
        popupOrigin,
        document.createElement("br"),
        "Starting point"
    );


    L.marker(
        coordinates,
        {
            icon: originIcon
        }
    )
        .addTo(originMarkers)
        .bindPopup(popupContent);
}


// Destination marker layer
const destinationMarkers =
    L.layerGroup().addTo(map);


// --------------------------------------------------
// TIME FORMATTING
// --------------------------------------------------

const OFFSET_TIMESTAMP_PATTERN =
    /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/;


function getTimestampDetails(timestamp) {

    if (typeof timestamp !== "string") {
        return null;
    }

    const match = timestamp.match(
        OFFSET_TIMESTAMP_PATTERN
    );

    if (!match) {
        return null;
    }

    const epochMilliseconds =
        new Date(timestamp).getTime();

    if (!Number.isFinite(epochMilliseconds)) {
        return null;
    }

    return {
        timestamp,
        epochMilliseconds,
        localTime: `${match[2]}:${match[3]}`
    };
}


function formatTime(timestamp) {

    return getTimestampDetails(timestamp)
        ?.localTime ?? "Time unavailable";
}


function getEarliestTimestamp(timestamps) {

    let earliestTimestamp = null;

    for (const timestamp of timestamps) {

        const details =
            getTimestampDetails(timestamp);

        if (
            details &&
            (
                !earliestTimestamp ||
                details.epochMilliseconds <
                    earliestTimestamp.epochMilliseconds
            )
        ) {
            earliestTimestamp = details;
        }
    }

    return earliestTimestamp;
}


function getLatestTimestamp(timestamps) {

    let latestTimestamp = null;

    for (const timestamp of timestamps) {

        const details =
            getTimestampDetails(timestamp);

        if (
            details &&
            (
                !latestTimestamp ||
                details.epochMilliseconds >
                    latestTimestamp.epochMilliseconds
            )
        ) {
            latestTimestamp = details;
        }
    }

    return latestTimestamp;
}


function getElapsedDurationMilliseconds(
    departureTimestamp,
    arrivalTimestamp
) {

    const departure =
        getTimestampDetails(departureTimestamp);

    const arrival =
        getTimestampDetails(arrivalTimestamp);

    if (!departure || !arrival) {
        return null;
    }

    const elapsedMilliseconds =
        arrival.epochMilliseconds -
        departure.epochMilliseconds;

    if (elapsedMilliseconds < 0) {
        return null;
    }

    return elapsedMilliseconds;
}


function formatElapsedDuration(
    elapsedMilliseconds
) {

    if (!Number.isFinite(elapsedMilliseconds)) {
        return null;
    }

    const totalMinutes = Math.round(
        elapsedMilliseconds / 60000
    );

    const hours = Math.floor(
        totalMinutes / 60
    );

    const minutes = totalMinutes % 60;

    if (hours === 0) {
        return `${minutes}m`;
    }

    return `${hours}h ${minutes}m`;
}


function getServiceTiming(service) {

    const departure =
        getTimestampDetails(service?.departure);

    const arrival = getEarliestTimestamp(
        Array.isArray(service?.stops)
            ? service.stops.map(stop => stop.arrival)
            : []
    );

    const elapsedMilliseconds =
        getElapsedDurationMilliseconds(
            service?.departure,
            arrival?.timestamp
        );

    return {
        departure,
        arrival,
        elapsedMilliseconds,
        duration: formatElapsedDuration(
            elapsedMilliseconds
        )
    };
}


function formatMode(mode) {

    if (mode === "COACH") {
        return "Bus";
    }

    if (TRAIN_SERVICE_MODES.has(mode)) {
        return "Rail";
    }

    return null;
}


function formatServiceCount(count) {

    return `${count} direct ${
        count === 1 ? "service" : "services"
    }`;
}


function formatStopCount(count) {

    return `${count} ${
        count === 1 ? "stop" : "stops"
    }`;
}


function getDestinationServices(destination) {

    return Array.isArray(destination?.services)
        ? destination.services
        : [];
}


function getDestinationEarliestArrival(
    destination
) {

    return getEarliestTimestamp(
        getDestinationServices(destination)
            .flatMap(service =>
                Array.isArray(service.stops)
                    ? service.stops.map(
                        stop => stop.arrival
                    )
                    : []
            )
    );
}


function getDestinationLatestDeparture(
    destination
) {

    return getLatestTimestamp(
        getDestinationServices(destination)
            .map(service => service.departure)
    );
}


function getDestinationLongestJourney(
    destination
) {

    let longestJourney = null;

    for (
        const service
        of getDestinationServices(destination)
    ) {

        const elapsedMilliseconds =
            getServiceTiming(service)
                .elapsedMilliseconds;

        if (
            Number.isFinite(elapsedMilliseconds) &&
            (
                longestJourney === null ||
                elapsedMilliseconds > longestJourney
            )
        ) {
            longestJourney = elapsedMilliseconds;
        }
    }

    return longestJourney;
}


function createDestinationTimingHTML(destination) {

    const services =
        getDestinationServices(destination);

    if (services.length === 1) {

        const service = services[0];
        const timing = getServiceTiming(service);
        const mode = formatMode(service.mode);

        let journeyTimes =
            "Timing unavailable";

        if (timing.departure && timing.arrival) {
            journeyTimes = `
                <strong>${timing.departure.localTime}</strong>
                <span aria-hidden="true">→</span>
                <strong>${timing.arrival.localTime}</strong>
            `;

        } else if (timing.departure) {
            journeyTimes = `
                Departs
                <strong>${timing.departure.localTime}</strong>
            `;

        } else if (timing.arrival) {
            journeyTimes = `
                Arrives
                <strong>${timing.arrival.localTime}</strong>
            `;
        }

        const detailParts = [
            timing.duration,
            mode
        ].filter(Boolean);

        return `
            <div class="destination-timing">
                <p class="destination-journey-times">
                    ${journeyTimes}
                </p>

                ${detailParts.length > 0
                    ? `
                        <p class="destination-timing-detail">
                            ${detailParts.join(" · ")}
                        </p>
                    `
                    : ""
                }
            </div>
        `;
    }

    const earliestArrival =
        getDestinationEarliestArrival(destination);

    const latestDeparture =
        getDestinationLatestDeparture(destination);

    if (!earliestArrival && !latestDeparture) {
        return `
            <div class="destination-timing">
                <p class="destination-timing-detail">
                    Timing unavailable
                </p>
            </div>
        `;
    }

    return `
        <div class="destination-timing destination-timing-range">
            ${earliestArrival
                ? `
                    <p>
                        Earliest arrival
                        <strong>${earliestArrival.localTime}</strong>
                    </p>
                `
                : ""
            }

            ${latestDeparture
                ? `
                    <p>
                        Latest departure
                        <strong>${latestDeparture.localTime}</strong>
                    </p>
                `
                : ""
            }
        </div>
    `;
}


function createDestinationCountHTML(destination) {

    const serviceCount =
        getDestinationServices(destination).length;

    const counts = [
        formatServiceCount(serviceCount)
    ];

    if (
        Number.isInteger(destination.station_count) &&
        destination.station_count >= 0
    ) {
        counts.push(
            formatStopCount(
                destination.station_count
            )
        );
    }

    return counts.join(" · ");
}


// --------------------------------------------------
// TRANSPORT MODE FILTERING
// --------------------------------------------------

function serviceMatchesMode(service, mode) {

    if (mode === "ALL") {
        return true;
    }

    if (mode === "COACH") {
        return service.mode === "COACH";
    }

    return TRAIN_SERVICE_MODES.has(
        service.mode
    );
}


function getDestinationForMode(destination) {

    if (activeMode === "ALL") {
        return destination;
    }

    const services =
        destination.services.filter(
            service => serviceMatchesMode(
                service,
                activeMode
            )
        );


    if (services.length === 0) {
        return null;
    }


    let earliestArrival =
        destination.earliest_arrival;

    let earliestTime = Infinity;

    const stationKeys = new Set();


    for (const service of services) {

        for (const stop of service.stops) {

            const arrivalTime =
                new Date(stop.arrival).getTime();

            if (arrivalTime < earliestTime) {

                earliestTime = arrivalTime;
                earliestArrival = stop.arrival;
            }

            stationKeys.add(
                JSON.stringify([
                    stop.station,
                    stop.lat,
                    stop.lon
                ])
            );
        }
    }


    return {
        ...destination,
        earliest_arrival: earliestArrival,
        service_count: services.length,
        station_count: stationKeys.size,
        services
    };
}


// --------------------------------------------------
// DESTINATION SORTING
// --------------------------------------------------

function getDestinationSortMetric(destination) {

    if (
        activeSort ===
        DESTINATION_SORTS.EARLIEST_ARRIVAL
    ) {
        return getDestinationEarliestArrival(
            destination
        )?.epochMilliseconds ?? null;
    }

    if (
        activeSort ===
        DESTINATION_SORTS.LATEST_DEPARTURE
    ) {
        return getDestinationLatestDeparture(
            destination
        )?.epochMilliseconds ?? null;
    }

    if (
        activeSort ===
        DESTINATION_SORTS.LONGEST_JOURNEY
    ) {
        return getDestinationLongestJourney(
            destination
        );
    }

    return null;
}


function compareOptionalNumbers(
    leftValue,
    rightValue,
    direction
) {

    const leftIsValid =
        Number.isFinite(leftValue);

    const rightIsValid =
        Number.isFinite(rightValue);

    if (leftIsValid !== rightIsValid) {
        return leftIsValid ? -1 : 1;
    }

    if (!leftIsValid) {
        return 0;
    }

    if (leftValue === rightValue) {
        return 0;
    }

    return leftValue < rightValue
        ? -direction
        : direction;
}


function getDestinationName(destination) {

    if (
        typeof destination?.city !== "string" ||
        !destination.city.trim()
    ) {
        return null;
    }

    return destination.city.trim();
}


function compareOptionalText(
    leftValue,
    rightValue
) {

    const leftIsValid =
        typeof leftValue === "string" &&
        Boolean(leftValue.trim());

    const rightIsValid =
        typeof rightValue === "string" &&
        Boolean(rightValue.trim());

    if (leftIsValid !== rightIsValid) {
        return leftIsValid ? -1 : 1;
    }

    if (!leftIsValid) {
        return 0;
    }

    return DESTINATION_NAME_COLLATOR.compare(
        leftValue,
        rightValue
    );
}


function compareDestinationViews(
    leftView,
    rightView
) {

    if (activeSort === DESTINATION_SORTS.NAME) {

        const nameComparison = compareOptionalText(
            getDestinationName(leftView.destination),
            getDestinationName(rightView.destination)
        );

        if (nameComparison !== 0) {
            return nameComparison;
        }

        const countryComparison =
            compareOptionalText(
                leftView.destination.country,
                rightView.destination.country
            );

        if (countryComparison !== 0) {
            return countryComparison;
        }

    } else {

        const direction =
            activeSort ===
                DESTINATION_SORTS.EARLIEST_ARRIVAL
                ? 1
                : -1;

        const metricComparison =
            compareOptionalNumbers(
                getDestinationSortMetric(
                    leftView.destination
                ),
                getDestinationSortMetric(
                    rightView.destination
                ),
                direction
            );

        if (metricComparison !== 0) {
            return metricComparison;
        }
    }

    return leftView.canonicalIndex -
        rightView.canonicalIndex;
}


function getVisibleSortedDestinations(
    destinations
) {

    return destinations
        .map((destination, canonicalIndex) => ({
            destination:
                getDestinationForMode(destination),
            canonicalIndex
        }))
        .filter(view => view.destination !== null)
        .sort(compareDestinationViews)
        .map(view => view.destination);
}


function updateModeFilterButtons() {

    for (const button of modeFilterButtons) {

        const isActive =
            button.dataset.mode === activeMode;

        button.classList.toggle(
            "is-active",
            isActive
        );

        button.setAttribute(
            "aria-pressed",
            String(isActive)
        );
    }
}


// --------------------------------------------------
// FIND ONE MAP POSITION FOR EACH DESTINATION
// --------------------------------------------------

function getDestinationMapPoint(destination) {

    let earliestStop = null;
    let earliestTime = Infinity;

    for (const service of destination.services) {

        for (const stop of service.stops) {

            if (
                typeof stop.lat !== "number" ||
                typeof stop.lon !== "number"
            ) {
                continue;
            }

            const arrivalTime =
                new Date(stop.arrival).getTime();

            if (arrivalTime < earliestTime) {

                earliestTime = arrivalTime;
                earliestStop = stop;
            }
        }
    }

    if (!earliestStop) {
        return null;
    }

    return [
        earliestStop.lat,
        earliestStop.lon
    ];
}


// --------------------------------------------------
// MAP PREVIEW CONTENT
// --------------------------------------------------

function showDestinationPreview(destination) {

    const servicePreview =
        destination.services
            .slice(0, 3)
            .map(service => `
                <div class="map-preview-service">

                    <span>
                        ${service.service}
                    </span>

                    <span class="map-preview-service-time">
                        ${formatTime(service.departure)}
                    </span>

                </div>
            `)
            .join("");


    let moreServices = "";

    if (destination.services.length > 3) {

        moreServices = `
            <div class="map-preview-service">
                <span>
                    + ${destination.services.length - 3}
                    more
                </span>
            </div>
        `;
    }


    previewContainer.innerHTML = `

        <h3>
            ${destination.city}
        </h3>

        <p class="map-preview-country">
            ${destination.country}
        </p>

        <p class="map-preview-label">
            Arrive from
        </p>

        <p class="map-preview-arrival">
            ${formatTime(
                destination.earliest_arrival
            )}
        </p>

        <p class="map-preview-meta">
            ${destination.service_count}
            service(s)
            ·
            ${destination.station_count}
            stop(s)
        </p>

        <div class="map-preview-services">

            ${servicePreview}

            ${moreServices}

        </div>
    `;
}


function clearDestinationPreview() {

    previewContainer.innerHTML = `
        <p class="map-preview-placeholder">
            Hover over a destination
            to see the overnight connection.
        </p>
    `;
}


// --------------------------------------------------
// MARKER VISUAL STATES
// --------------------------------------------------

function highlightDestinationMarker(marker) {

    marker.setRadius(9);

    marker.setStyle({
        color: "#d8ecff",
        weight: 3,
        fillColor: "#4f91d1",
        fillOpacity: 1
    });

    marker.bringToFront();
}


function resetDestinationMarker(marker) {

    marker.setRadius(5);

    marker.setStyle({
        color: "#9bc9f5",
        weight: 1.5,
        fillColor: "#245b93",
        fillOpacity: 0.9
    });
}


function selectDestinationMarker(marker) {

    marker.setRadius(9);

    marker.setStyle({
        color: "#ffffff",
        weight: 3,
        fillColor: "#245b93",
        fillOpacity: 1
    });

    marker.bringToFront();
}


function clearSelectedDestination() {

    if (!selectedDestinationView) {
        return;
    }

    selectedDestinationView.card.classList.remove(
        "is-selected"
    );

    resetDestinationMarker(
        selectedDestinationView.marker
    );

    selectedDestinationView.hitMarker.closePopup();
    selectedDestinationView = null;
}


function selectDestination(destinationView) {

    if (
        selectedDestinationView &&
        selectedDestinationView !== destinationView
    ) {
        clearSelectedDestination();
    }

    selectedDestinationView = destinationView;

    destinationView.card.classList.add(
        "is-selected"
    );

    selectDestinationMarker(
        destinationView.marker
    );

    showDestinationPreview(
        destinationView.destination
    );

    destinationView.hitMarker.openPopup();
}


function navigateToDestinationOnMap(destinationView) {

    selectDestination(
        destinationView
    );

    mapElement.scrollIntoView({
        behavior: "smooth",
        block: "center"
    });

    map.flyTo(
        destinationView.coordinates,
        Math.max(
            map.getZoom(),
            DESTINATION_FOCUS_ZOOM
        ),
        {
            animate: true,
            duration: 0.75
        }
    );
}


function createDestinationPopup(destination) {

    const popupContent =
        document.createElement("div");

    const popupCity =
        document.createElement("strong");

    popupCity.textContent = destination.city;

    popupContent.append(
        popupCity,
        document.createElement("br"),
        destination.country,
        document.createElement("br"),
        `Arrive from ${formatTime(
            destination.earliest_arrival
        )}`
    );

    return popupContent;
}


// --------------------------------------------------
// SERVICE DETAILS
// --------------------------------------------------

function createServiceHTML(service, originName) {

    const encodedOrigin =
        document.createElement("span");

    encodedOrigin.textContent = originName;

    const stopsHTML = service.stops
        .map(stop => `
            <div class="arrival-stop">

                <span class="arrival-time">
                    ${formatTime(stop.arrival)}
                </span>

                <span class="station-name">
                    ${stop.station}
                </span>

            </div>
        `)
        .join("");

    const timing = getServiceTiming(service);
    const mode = formatMode(service.mode);
    const serviceIdentifier =
        service.service || service.trip_id || null;

    const timingParts = [];

    if (timing.departure && timing.arrival) {
        timingParts.push(
            `${timing.departure.localTime} → ${timing.arrival.localTime}`
        );

    } else if (timing.departure) {
        timingParts.push(
            `Departs ${timing.departure.localTime}`
        );

    } else if (timing.arrival) {
        timingParts.push(
            `Arrives ${timing.arrival.localTime}`
        );
    }

    if (timing.duration) {
        timingParts.push(timing.duration);
    }

    const serviceMeta = [
        mode,
        serviceIdentifier
    ].filter(Boolean);


    return `
        <div class="service-block">

            <div class="service-header">
                <strong>
                    ${timingParts.length > 0
                        ? timingParts.join(" · ")
                        : "Timing unavailable"
                    }
                </strong>
            </div>

            ${serviceMeta.length > 0
                ? `
                    <p class="service-meta">
                        ${serviceMeta.join(" · ")}
                    </p>
                `
                : ""
            }

            <div class="departure-info">

                <span>
                    ${timing.departure
                        ?.localTime ?? "Time unavailable"
                    }
                </span>

                <span>
                    from ${encodedOrigin.innerHTML}
                </span>

            </div>

            <div class="route-line"></div>

            <div class="arrival-stops">
                ${stopsHTML}
            </div>

        </div>
    `;
}


// --------------------------------------------------
// OPEN / CLOSE DESTINATION CARD
// --------------------------------------------------

function openDestination(card, destination, originName) {

    const existingDetails =
        card.querySelector(
            ".destination-details"
        );

    const detailsToggle =
        card.querySelector(
            ".destination-details-toggle"
        );


    if (existingDetails) {

        existingDetails.remove();

        card.classList.remove(
            "expanded"
        );

        detailsToggle.textContent = "＋";
        detailsToggle.setAttribute(
            "aria-expanded",
            "false"
        );
        detailsToggle.setAttribute(
            "aria-label",
            `Show services for ${destination.city}`
        );

        return;
    }


    const details =
        document.createElement("div");

    details.className =
        "destination-details";


    const servicesHTML =
        destination.services
            .map(service =>
                createServiceHTML(
                    service,
                    originName
                )
            )
            .join("");


    details.innerHTML = `
        <div class="details-divider"></div>

        ${servicesHTML}
    `;


    card.appendChild(details);

    card.classList.add(
        "expanded"
    );

    detailsToggle.textContent = "−";
    detailsToggle.setAttribute(
        "aria-expanded",
        "true"
    );
    detailsToggle.setAttribute(
        "aria-label",
        `Hide services for ${destination.city}`
    );
}


// --------------------------------------------------
// MAKE SURE DESTINATION IS OPEN
// --------------------------------------------------

function ensureDestinationOpen(card, destination, originName) {

    const existingDetails =
        card.querySelector(
            ".destination-details"
        );


    if (!existingDetails) {

        openDestination(
            card,
            destination,
            originName
        );
    }
}


// --------------------------------------------------
// ORIGIN AUTOCOMPLETE
// --------------------------------------------------

function normalizeOriginSuggestionQuery(value) {

    return value.trim().replace(/\s+/gu, " ");
}


function getUnicodeCharacterCount(value) {

    return Array.from(value).length;
}


function getOriginSuggestionRequestUrl(queryText) {

    const query = new URLSearchParams({
        q: queryText
    });

    return `${ORIGIN_SUGGESTION_ENDPOINT}?${query}`;
}


function getUsableOriginSuggestions(payload) {

    if (!Array.isArray(payload?.suggestions)) {
        return [];
    }

    const suggestions = [];

    for (const suggestion of payload.suggestions) {

        if (
            !suggestion ||
            typeof suggestion !== "object" ||
            typeof suggestion.label !== "string" ||
            !suggestion.label.trim() ||
            typeof suggestion.value !== "string" ||
            !suggestion.value.trim()
        ) {
            continue;
        }

        suggestions.push({
            label: suggestion.label,
            value: suggestion.value
        });

        if (
            suggestions.length ===
            ORIGIN_SUGGESTION_DISPLAY_LIMIT
        ) {
            break;
        }
    }

    return suggestions;
}


function setActiveOriginSuggestion(index) {

    const validIndex =
        Number.isInteger(index) &&
        index >= 0 &&
        index < currentSuggestions.length
            ? index
            : -1;

    activeSuggestionIndex = validIndex;

    const options = originSuggestionsList.querySelectorAll(
        ".origin-suggestion-option"
    );

    for (const [optionIndex, option] of options.entries()) {

        const isActive = optionIndex === validIndex;

        option.classList.toggle("is-active", isActive);
        option.setAttribute(
            "aria-selected",
            String(isActive)
        );
    }

    if (validIndex === -1) {
        originInput.removeAttribute("aria-activedescendant");
        return;
    }

    const activeOption = options[validIndex];

    if (activeOption) {
        originInput.setAttribute(
            "aria-activedescendant",
            activeOption.id
        );
        activeOption.scrollIntoView({
            block: "nearest"
        });
    }
}


function closeOriginSuggestions() {

    currentSuggestions = [];
    activeSuggestionIndex = -1;
    originSuggestionsList.replaceChildren();
    originSuggestionsList.hidden = true;
    originInput.setAttribute("aria-expanded", "false");
    originInput.removeAttribute("aria-activedescendant");
}


function cancelOriginSuggestionRequest() {

    if (suggestionDebounceTimer !== null) {
        clearTimeout(suggestionDebounceTimer);
        suggestionDebounceTimer = null;
    }

    currentSuggestionController?.abort();
    currentSuggestionController = null;
    suggestionRequestSequence += 1;
}


function cancelOriginAutocomplete() {

    cancelOriginSuggestionRequest();
    closeOriginSuggestions();
}


function renderOriginSuggestions(suggestions) {

    closeOriginSuggestions();

    if (suggestions.length === 0) {
        return;
    }

    currentSuggestions = suggestions;

    const fragment = document.createDocumentFragment();

    for (const [index, suggestion] of suggestions.entries()) {

        const option = document.createElement("li");

        option.id = `origin-suggestion-option-${index}`;
        option.className = "origin-suggestion-option";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        option.dataset.suggestionIndex = String(index);
        option.textContent = suggestion.label;

        fragment.append(option);
    }

    originSuggestionsList.append(fragment);
    originSuggestionsList.hidden = false;
    originInput.setAttribute("aria-expanded", "true");
}


async function requestOriginSuggestions(queryText, requestSequence) {

    if (requestSequence !== suggestionRequestSequence) {
        return;
    }

    const requestController = new AbortController();

    currentSuggestionController = requestController;

    try {
        const response = await fetch(
            getOriginSuggestionRequestUrl(queryText),
            {
                signal: requestController.signal
            }
        );

        if (!response.ok) {
            throw new Error("Origin suggestions unavailable.");
        }

        const payload = await response.json();

        if (
            requestSequence !== suggestionRequestSequence ||
            currentSuggestionController !== requestController ||
            normalizeOriginSuggestionQuery(originInput.value) !==
                queryText
        ) {
            return;
        }

        renderOriginSuggestions(
            getUsableOriginSuggestions(payload)
        );

    } catch (error) {

        if (
            error?.name !== "AbortError" &&
            requestSequence === suggestionRequestSequence &&
            currentSuggestionController === requestController
        ) {
            closeOriginSuggestions();
        }

    } finally {

        if (currentSuggestionController === requestController) {
            currentSuggestionController = null;
        }
    }
}


function scheduleOriginSuggestions() {

    cancelOriginSuggestionRequest();
    closeOriginSuggestions();

    const queryText = normalizeOriginSuggestionQuery(
        originInput.value
    );

    if (
        getUnicodeCharacterCount(queryText) <
        ORIGIN_SUGGESTION_MIN_CHARACTERS
    ) {
        return;
    }

    const requestSequence = suggestionRequestSequence;

    suggestionDebounceTimer = setTimeout(
        () => {
            suggestionDebounceTimer = null;
            requestOriginSuggestions(
                queryText,
                requestSequence
            );
        },
        ORIGIN_SUGGESTION_DEBOUNCE_MS
    );
}


function selectOriginSuggestion(index) {

    const suggestion = currentSuggestions[index];

    if (!suggestion) {
        return false;
    }

    originInput.value = suggestion.value;
    cancelOriginAutocomplete();
    originInput.focus({
        preventScroll: true
    });

    return true;
}


function moveActiveOriginSuggestion(direction) {

    if (
        originSuggestionsList.hidden ||
        currentSuggestions.length === 0
    ) {
        return false;
    }

    let nextIndex;

    if (activeSuggestionIndex === -1) {
        nextIndex = direction > 0
            ? 0
            : currentSuggestions.length - 1;
    } else {
        nextIndex = (
            activeSuggestionIndex +
            direction +
            currentSuggestions.length
        ) % currentSuggestions.length;
    }

    setActiveOriginSuggestion(nextIndex);
    return true;
}


function getOriginSuggestionIndex(target) {

    if (!(target instanceof Element)) {
        return -1;
    }

    const option = target.closest(
        ".origin-suggestion-option"
    );

    if (!option || !originSuggestionsList.contains(option)) {
        return -1;
    }

    const index = Number(option.dataset.suggestionIndex);

    return Number.isInteger(index) ? index : -1;
}


originInput.addEventListener(
    "input",
    event => {

        if (event.isComposing || isOriginComposing) {
            cancelOriginAutocomplete();
            return;
        }

        scheduleOriginSuggestions();
    }
);


originInput.addEventListener(
    "compositionstart",
    () => {
        isOriginComposing = true;
        cancelOriginAutocomplete();
    }
);


originInput.addEventListener(
    "compositionend",
    () => {
        isOriginComposing = false;
        scheduleOriginSuggestions();
    }
);


originInput.addEventListener(
    "keydown",
    event => {

        if (event.isComposing || isOriginComposing) {
            return;
        }

        if (event.key === "ArrowDown") {

            if (moveActiveOriginSuggestion(1)) {
                event.preventDefault();
            }

            return;
        }

        if (event.key === "ArrowUp") {

            if (moveActiveOriginSuggestion(-1)) {
                event.preventDefault();
            }

            return;
        }

        if (
            event.key === "Enter" &&
            activeSuggestionIndex !== -1
        ) {
            event.preventDefault();
            event.stopPropagation();
            selectOriginSuggestion(activeSuggestionIndex);
            return;
        }

        if (
            event.key === "Escape" &&
            (
                !originSuggestionsList.hidden ||
                suggestionDebounceTimer !== null ||
                currentSuggestionController !== null
            )
        ) {
            event.preventDefault();
            event.stopPropagation();
            cancelOriginAutocomplete();
        }
    }
);


originSuggestionsList.addEventListener(
    "pointerover",
    event => {
        const index = getOriginSuggestionIndex(event.target);

        if (index !== -1) {
            setActiveOriginSuggestion(index);
        }
    }
);


originSuggestionsList.addEventListener(
    "pointerdown",
    event => {
        const index = getOriginSuggestionIndex(event.target);

        if (index === -1) {
            return;
        }

        event.preventDefault();
        selectOriginSuggestion(index);
    }
);


originSuggestionsList.addEventListener(
    "click",
    event => {
        const index = getOriginSuggestionIndex(event.target);

        if (index !== -1) {
            selectOriginSuggestion(index);
        }
    }
);


originAutocomplete.addEventListener(
    "focusout",
    event => {
        const nextTarget = event.relatedTarget;

        if (
            !(nextTarget instanceof Node) ||
            !originAutocomplete.contains(nextTarget)
        ) {
            cancelOriginAutocomplete();
        }
    }
);


document.addEventListener(
    "pointerdown",
    event => {

        if (!originAutocomplete.contains(event.target)) {
            cancelOriginAutocomplete();
        }
    }
);


// --------------------------------------------------
// LOAD NIGHTWAYS DATA
// --------------------------------------------------

function getSubmittedSearch() {

    return {
        origin: originInput.value.trim(),
        date: dateInput.value
    };
}


function getNightwaysRequestUrl(submittedSearch) {

    const query = new URLSearchParams(submittedSearch);

    return `${API_ENDPOINT}?${query}`;
}


const GENERIC_LOAD_ERROR =
    "Could not load overnight destinations.";


const NETWORK_LOAD_ERROR =
    "Could not connect to Nightways. Check your network connection and try again.";


const TEMPORARY_SERVICE_ERROR =
    "Nightways is temporarily unable to search this origin. Please try again.";


const EMPTY_RESULTS_MESSAGE =
    "No direct overnight destinations were found for this date.";


const NIGHTWAYS_ERROR_MESSAGES = Object.freeze({
    origin_not_found:
        "Nightways could not find this city. Check the spelling and try again.",
    origin_ambiguous:
        "Multiple places match this origin. Add a country or region and try again.",
    invalid_origin:
        "Enter a valid European city and try again.",
    origin_candidate_limit_exceeded:
        "Nightways cannot safely search this origin yet. Please try another nearby city.",
    origin_boundary_not_found:
        "Nightways found this city, but could not determine its search area. Please try another nearby city.",
    origin_boundary_ambiguous:
        "Nightways found this city, but could not determine its search area. Please try another nearby city.",
    origin_boundary_service_failed:
        TEMPORARY_SERVICE_ERROR,
    transitous_failed:
        TEMPORARY_SERVICE_ERROR,
    gisco_dataset_unavailable:
        TEMPORARY_SERVICE_ERROR,
    gisco_dataset_invalid:
        TEMPORARY_SERVICE_ERROR,
    locality_resolution_failed:
        TEMPORARY_SERVICE_ERROR
});


class NightwaysApiError extends Error {

    constructor(message, code = null, status = null) {

        super(message);
        this.name = "NightwaysApiError";
        this.code = code;
        this.status = status;
    }
}


class NightwaysNetworkError extends Error {

    constructor() {

        super(NETWORK_LOAD_ERROR);
        this.name = "NightwaysNetworkError";
    }
}


function setDestinationControlsDisabled(disabled) {

    for (const button of modeFilterButtons) {
        button.disabled = disabled;
    }

    sortSelect.disabled = disabled;
}


function updateDestinationControlsAvailability() {

    setDestinationControlsDisabled(
        !nightwaysData || currentSearchController !== null
    );
}


function showSearchStatus(message, isError = false) {

    searchStatus.hidden = false;
    searchStatus.classList.toggle("is-error", isError);
    searchStatus.setAttribute(
        "role",
        isError ? "alert" : "status"
    );
    searchStatus.setAttribute(
        "aria-live",
        isError ? "assertive" : "polite"
    );
    searchStatus.textContent = message;
}


function clearSearchStatus() {

    searchStatus.textContent = "";
    searchStatus.hidden = true;
    searchStatus.classList.remove("is-error");
    searchStatus.setAttribute("role", "status");
    searchStatus.setAttribute("aria-live", "polite");
}


function setSearchPending(pending, submittedOrigin = "") {

    originInput.disabled = pending;
    dateInput.disabled = pending;
    searchButton.disabled = pending;
    searchButton.textContent = pending
        ? "Searching…"
        : SEARCH_BUTTON_LABEL;
    resultsSection.setAttribute(
        "aria-busy",
        String(pending)
    );

    if (pending) {
        showSearchStatus(
            `Searching overnight routes from ${submittedOrigin}…`
        );
    }

    updateDestinationControlsAvailability();
}


async function getNightwaysApiError(response) {

    try {

        const payload = await response.json();
        const code = payload?.detail?.code;

        if (
            typeof code === "string" &&
            Object.prototype.hasOwnProperty.call(
                NIGHTWAYS_ERROR_MESSAGES,
                code
            )
        ) {
            return new NightwaysApiError(
                NIGHTWAYS_ERROR_MESSAGES[code],
                code,
                response.status
            );
        }

    } catch (error) {
        return new NightwaysApiError(
            GENERIC_LOAD_ERROR,
            null,
            response.status
        );
    }

    return new NightwaysApiError(
        GENERIC_LOAD_ERROR,
        null,
        response.status
    );
}


async function loadNightwaysData(reloadData = true) {

    if (!reloadData && !nightwaysData) {
        return;
    }


    let requestController = null;
    let submittedSearch = null;

    if (reloadData) {

        submittedSearch = getSubmittedSearch();

        currentSearchController?.abort();

        requestController = new AbortController();
        currentSearchController = requestController;
        nightwaysData = null;

        setSearchPending(
            true,
            submittedSearch.origin
        );

        resultsHeading.textContent =
            "Overnight destinations";

        map.setView(
            DEFAULT_MAP_CENTER,
            DEFAULT_MAP_ZOOM
        );
    }

    if (reloadData) {
        destinationCount.textContent = "";
    }

    clearSelectedDestination();

    destinationList.innerHTML = "";

    destinationMarkers.clearLayers();

    originMarkers.clearLayers();

    clearDestinationPreview();


    try {

        let data = nightwaysData;


        if (reloadData) {

            let response;

            try {
                response = await fetch(
                    getNightwaysRequestUrl(
                        submittedSearch
                    ),
                    {
                        signal: requestController.signal
                    }
                );
            } catch (error) {
                if (error?.name === "AbortError") {
                    throw error;
                }

                throw new NightwaysNetworkError();
            }


            if (!response.ok) {

                throw await getNightwaysApiError(
                    response
                );
            }


            data = await response.json();

            if (
                currentSearchController !==
                requestController
            ) {
                return;
            }

            nightwaysData = data;
        }


        const originName = data.origin;
        const latitude =
            data.origin_coordinates?.latitude;
        const longitude =
            data.origin_coordinates?.longitude;

        if (
            typeof originName !== "string" ||
            !originName.trim() ||
            !Number.isFinite(latitude) ||
            !Number.isFinite(longitude)
        ) {
            throw new Error(
                "Nightways returned invalid origin data."
            );
        }

        const originCoordinates = [
            latitude,
            longitude
        ];

        renderOriginMarker(
            originName,
            originCoordinates
        );


        const visibleDestinations =
            getVisibleSortedDestinations(
                data.destinations
            );


        resultsHeading.textContent =
            `Overnight destinations from ${originName}`;

        destinationCount.textContent =
            Array.isArray(data.destinations) &&
            data.destinations.length === 0
                ? EMPTY_RESULTS_MESSAGE
                : `${visibleDestinations.length} direct overnight destinations`;


        const mapBounds =
            L.latLngBounds([
                originCoordinates
            ]);

        let destinationMarkerCount = 0;


        for (
            const destination
            of visibleDestinations
        ) {

            // ----------------------------------
            // DESTINATION CARD
            // ----------------------------------

            const card =
                document.createElement("div");


            card.className =
                "destination-card";


            card.innerHTML = `
                <div class="destination-summary">

                    <div>

                        <h3>
                            ${destination.city}
                        </h3>

                        <p>
                            ${destination.country}
                        </p>

                    </div>

                    <button
                        class="destination-details-toggle"
                        type="button"
                        aria-expanded="false"
                    >
                        ＋
                    </button>

                </div>

                ${createDestinationTimingHTML(
                    destination
                )}

                <p class="destination-counts">
                    ${createDestinationCountHTML(
                        destination
                    )}
                </p>
            `;


            const detailsToggle =
                card.querySelector(
                    ".destination-details-toggle"
                );


            detailsToggle.setAttribute(
                "aria-label",
                `Show services for ${destination.city}`
            );


            detailsToggle.addEventListener(
                "click",
                event => {

                    event.stopPropagation();

                    openDestination(
                        card,
                        destination,
                        originName
                    );
                }
            );


            destinationList.appendChild(
                card
            );


            // ----------------------------------
            // DESTINATION COORDINATES
            // ----------------------------------

            const coordinates =
                getDestinationMapPoint(
                    destination
                );


            if (coordinates) {

                destinationMarkerCount += 1;

                // ----------------------------------
                // VISIBLE MARKER
                // ----------------------------------

                const marker =
                    L.circleMarker(
                        coordinates,
                        {
                            radius: 5,
                            color: "#9bc9f5",
                            weight: 1.5,
                            fillColor: "#245b93",
                            fillOpacity: 0.9,
                            interactive: false
                        }
                    );


                // ----------------------------------
                // INVISIBLE HIT AREA
                // ----------------------------------

                const hitMarker =
                    L.circleMarker(
                        coordinates,
                        {
                            radius: 11,
                            stroke: false,
                            fill: true,
                            fillColor: "#ffffff",
                            fillOpacity: 0.001,
                            interactive: true
                        }
                    );


                hitMarker.bindPopup(
                    createDestinationPopup(
                        destination
                    )
                );


                const destinationView = {
                    destination,
                    card,
                    marker,
                    hitMarker,
                    coordinates
                };


                card.classList.add(
                    "is-map-target"
                );


                card.addEventListener(
                    "click",
                    event => {

                        if (
                            event.target.closest(
                                CARD_INTERACTIVE_SELECTOR
                            ) ||
                            event.target.closest(
                                ".destination-details"
                            )
                        ) {
                            return;
                        }

                        navigateToDestinationOnMap(
                            destinationView
                        );
                    }
                );


                // ----------------------------------
                // SHOW / HIDE PREVIEW
                // ----------------------------------

                function showPreview() {

                    if (
                        selectedDestinationView ===
                        destinationView
                    ) {
                        selectDestinationMarker(
                            marker
                        );

                    } else {
                        highlightDestinationMarker(
                            marker
                        );
                    }

                    showDestinationPreview(
                        destination
                    );
                }


                function hidePreview() {

                    if (
                        selectedDestinationView !==
                        destinationView
                    ) {
                        resetDestinationMarker(
                            marker
                        );
                    }


                    if (selectedDestinationView) {
                        showDestinationPreview(
                            selectedDestinationView
                                .destination
                        );

                    } else {
                        clearDestinationPreview();
                    }
                }


                // ----------------------------------
                // MAP CLICK
                // ----------------------------------

                function selectDestinationFromMap() {

                    selectDestination(
                        destinationView
                    );

                    ensureDestinationOpen(
                        card,
                        destination,
                        originName
                    );


                    card.scrollIntoView({
                        behavior: "smooth",
                        block: "center"
                    });
                }


                // ----------------------------------
                // LARGE INVISIBLE HIT AREA EVENTS
                // ----------------------------------

                hitMarker.on(
                    "mouseover",
                    showPreview
                );


                hitMarker.on(
                    "mouseout",
                    hidePreview
                );


                hitMarker.on(
                    "click",
                    selectDestinationFromMap
                );


                // ----------------------------------
                // CARD HOVER EVENTS
                // ----------------------------------

                card.addEventListener(
                    "mouseenter",
                    showPreview
                );


                card.addEventListener(
                    "mouseleave",
                    hidePreview
                );


                // ----------------------------------
                // ADD TO MAP
                // ----------------------------------

                hitMarker.addTo(
                    destinationMarkers
                );


                marker.addTo(
                    destinationMarkers
                );


                mapBounds.extend(
                    coordinates
                );
            }
        }


        // ----------------------------------
        // FIT MAP TO ORIGIN AND DESTINATIONS
        // ----------------------------------

        if (destinationMarkerCount === 0) {

            map.setView(
                originCoordinates,
                ORIGIN_ONLY_ZOOM
            );

        } else {

            map.fitBounds(
                mapBounds,
                {
                    padding: [30, 30]
                }
            );
        }


        if (reloadData) {
            clearSearchStatus();
        }


    } catch (error) {

        if (
            error?.name === "AbortError" ||
            (
                requestController &&
                currentSearchController !==
                    requestController
            )
        ) {
            return;
        }


        nightwaysData = null;
        updateDestinationControlsAvailability();
        destinationList.innerHTML = "";
        destinationMarkers.clearLayers();
        originMarkers.clearLayers();
        clearDestinationPreview();

        resultsHeading.textContent =
            "Overnight destinations";

        map.setView(
            DEFAULT_MAP_CENTER,
            DEFAULT_MAP_ZOOM
        );

        if (
            !(error instanceof NightwaysApiError) &&
            !(error instanceof NightwaysNetworkError)
        ) {
            console.error(error);
        }

        destinationCount.textContent = "";

        showSearchStatus(
            error instanceof NightwaysApiError ||
            error instanceof NightwaysNetworkError
                ? error.message
                : GENERIC_LOAD_ERROR,
            true
        );

    } finally {

        if (
            requestController &&
            currentSearchController === requestController
        ) {
            currentSearchController = null;

            if (
                !searchStatus.hidden &&
                !searchStatus.classList.contains(
                    "is-error"
                )
            ) {
                clearSearchStatus();
            }

            setSearchPending(false);
        }
    }
}


// --------------------------------------------------
// SEARCH FORM
// --------------------------------------------------

searchForm.addEventListener(
    "keydown",
    event => {

        if (
            event.key !== "Enter" ||
            event.isComposing ||
            isOriginComposing ||
            (
                event.target !== originInput &&
                event.target !== dateInput
            )
        ) {
            return;
        }

        event.preventDefault();
        searchForm.requestSubmit();
    }
);


searchForm.addEventListener(
    "submit",
    event => {

        event.preventDefault();
        cancelOriginAutocomplete();

        if (currentSearchController) {
            return;
        }

        loadNightwaysData(true);
    }
);


// --------------------------------------------------
// MODE FILTER BUTTONS
// --------------------------------------------------

for (const button of modeFilterButtons) {

    button.addEventListener(
        "click",
        () => {

            activeMode =
                button.dataset.mode;

            updateModeFilterButtons();

            loadNightwaysData(false);
        }
    );
}


// --------------------------------------------------
// DESTINATION SORT
// --------------------------------------------------

sortSelect.addEventListener(
    "change",
    () => {

        if (
            !Object.values(DESTINATION_SORTS)
                .includes(sortSelect.value)
        ) {
            return;
        }

        activeSort = sortSelect.value;

        loadNightwaysData(false);
    }
);


// --------------------------------------------------
// INITIAL LOAD
// --------------------------------------------------

updateModeFilterButtons();
updateDestinationControlsAvailability();

loadNightwaysData(true);
