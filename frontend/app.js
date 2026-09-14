const NIGHTWAYS_API_PATH = "/api/nightways";
const LOCAL_API_HOSTNAMES = new Set([
    "localhost",
    "127.0.0.1",
    "[::1]"
]);


function resolveNightwaysApiEndpoint(rawBaseUrl) {

    const configuredBaseUrl = rawBaseUrl.trim();

    if (!configuredBaseUrl) {
        return NIGHTWAYS_API_PATH;
    }


    let baseUrl;

    try {
        baseUrl = new URL(configuredBaseUrl);
    } catch {
        throw new Error(
            "Nightways API base URL must be an absolute HTTP(S) origin."
        );
    }


    if (
        !["http:", "https:"].includes(baseUrl.protocol) ||
        baseUrl.username ||
        baseUrl.password ||
        baseUrl.pathname !== "/" ||
        baseUrl.search ||
        baseUrl.hash
    ) {
        throw new Error(
            "Nightways API base URL must be an HTTP(S) origin " +
            "without credentials, a path, query, or fragment."
        );
    }


    return `${baseUrl.origin}${NIGHTWAYS_API_PATH}`;
}


const configuredApiBaseUrl =
    LOCAL_API_HOSTNAMES.has(window.location.hostname)
        ? ""
        : document.querySelector(
            'meta[name="nightways-api-base-url"]'
        )?.content ?? "";

const API_ENDPOINT = resolveNightwaysApiEndpoint(
    configuredApiBaseUrl
);

const resultsHeading = document.getElementById("results-heading");
const destinationCount = document.getElementById("destination-count");
const destinationList = document.getElementById("destination-list");
const mapElement = document.getElementById("map");
const resultsContent = document.getElementById("results-content");
const searchForm = document.getElementById("search-form");
const searchStatus = document.getElementById("search-status");
const searchButton = document.getElementById("search-button");
const searchLoadingPanel = document.getElementById(
    "search-loading-panel"
);
const searchLoadingTrack = document.getElementById(
    "search-loading-track"
);
const searchLoadingFirstSearchMessage = document.getElementById(
    "search-loading-first-search-message"
);
const originInput = document.getElementById("origin");
const dateInput = document.getElementById("date");
const sortSelect = document.getElementById("destination-sort");
const modeFilterButtons = document.querySelectorAll(
    ".mode-filter-button"
);
const SEARCH_BUTTON_LABEL =
    searchButton.textContent.trim();
const SEARCH_LOADING_LIMIT = 90;
const SEARCH_LOADING_TICK_MS = 180;
const SEARCH_LOADING_TIME_SCALE_MS = 2500;
const SEARCH_LOADING_COMPLETION_MS = 320;
const SEARCH_LOADING_FADE_MS = 240;
const SEARCH_LOADING_PAINT_TIMEOUT_MS = 100;
const FIRST_SEARCH_MESSAGE_DELAY_MS = 8000;
const reducedMotionQuery = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
);

const localDate = new Date();

dateInput.value = [
    localDate.getFullYear(),
    String(localDate.getMonth() + 1).padStart(2, "0"),
    String(localDate.getDate()).padStart(2, "0")
].join("-");


const SEARCH_DATE_PATTERN =
    /^(\d{4})-(\d{2})-(\d{2})$/;


function isValidSearchDate(value) {

    const match = value?.match(
        SEARCH_DATE_PATTERN
    );

    if (!match || match[1] === "0000") {
        return false;
    }


    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const parsedDate = new Date(0);

    parsedDate.setUTCHours(0, 0, 0, 0);
    parsedDate.setUTCFullYear(
        year,
        month - 1,
        day
    );

    return (
        parsedDate.getUTCFullYear() === year &&
        parsedDate.getUTCMonth() === month - 1 &&
        parsedDate.getUTCDate() === day
    );
}


function initializeSearchFromUrl() {

    const query = new URLSearchParams(
        window.location.search
    );
    const hasOrigin = query.has("from");
    const hasDate = query.has("date");

    if (!hasOrigin && !hasDate) {
        return {
            shouldSearch: true,
            replaceUrlOnSuccess: false
        };
    }


    const requestedOrigin =
        query.get("from")?.trim() ?? "";
    const requestedDate = query.get("date");

    originInput.value = requestedOrigin;

    if (isValidSearchDate(requestedDate)) {
        dateInput.value = requestedDate;
    }

    return {
        shouldSearch: Boolean(requestedOrigin),
        replaceUrlOnSuccess: Boolean(requestedOrigin)
    };
}


const initialSearchState = initializeSearchFromUrl();

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
let currentSearchLoading = null;
let hasReceivedBackendResponse = false;
let selectedDestinationView = null;


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


function replaceSearchUrl(submittedSearch) {

    const url = new URL(window.location.href);

    url.searchParams.set(
        "from",
        submittedSearch.origin
    );
    url.searchParams.set(
        "date",
        submittedSearch.date
    );

    window.history.replaceState(
        window.history.state,
        "",
        url
    );
}


const GENERIC_LOAD_ERROR =
    "Could not load overnight destinations. Please try again.";


const NETWORK_LOAD_ERROR =
    "Could not connect to Nightways. Check your network connection and try again.";


const TEMPORARY_SERVICE_ERROR =
    "Nightways is temporarily unable to complete this search. Please try again.";


const EMPTY_RESULTS_MESSAGE =
    "No direct overnight destinations were found for this date. Try another date or a nearby departure city.";


const FILTER_EMPTY_RESULTS_MESSAGES = Object.freeze({
    TRAIN:
        "No train destinations match this search. Choose All to see every overnight option.",
    COACH:
        "No bus destinations match this search. Choose All to see every overnight option."
});


const NIGHTWAYS_ERROR_MESSAGES = Object.freeze({
    origin_not_found:
        "Nightways could not find this city. Check the spelling and try again.",
    origin_ambiguous:
        "Multiple places match this origin. Try a more specific place name.",
    invalid_origin:
        "Enter a valid European city and try again.",
    origin_candidate_limit_exceeded:
        "Nightways found too many possible matches for this origin. Try another nearby city.",
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


function showDestinationSummary(
    destinations,
    visibleDestinations
) {

    const isEmpty = visibleDestinations.length === 0;

    destinationCount.classList.toggle(
        "is-empty",
        isEmpty
    );

    if (!isEmpty) {
        destinationCount.textContent =
            `${visibleDestinations.length} direct overnight destinations`;
        return;
    }

    destinationCount.textContent =
        Array.isArray(destinations) &&
        destinations.length > 0
            ? FILTER_EMPTY_RESULTS_MESSAGES[activeMode] ??
                "No destinations match the current filter. Choose All to see every overnight option."
            : EMPTY_RESULTS_MESSAGE;
}


function clearDestinationSummary() {

    destinationCount.textContent = "";
    destinationCount.classList.remove("is-empty");
}


function setSearchLoadingProgress(state, progress) {

    if (currentSearchLoading !== state) {
        return;
    }

    const boundedProgress = Math.max(
        0,
        Math.min(100, progress)
    );

    state.progress = boundedProgress;
    searchLoadingTrack.style.setProperty(
        "--search-progress",
        `${boundedProgress}%`
    );
}


function advanceSearchLoading(state) {

    if (currentSearchLoading !== state) {
        return;
    }

    const elapsedTime = Math.max(
        0,
        window.performance.now() - state.startedAt
    );

    setSearchLoadingProgress(
        state,
        SEARCH_LOADING_LIMIT * elapsedTime /
            (elapsedTime + SEARCH_LOADING_TIME_SCALE_MS)
    );

    state.progressTimerId = window.setTimeout(
        () => advanceSearchLoading(state),
        SEARCH_LOADING_TICK_MS
    );
}


function hideSearchLoadingFirstSearchMessage(state) {

    if (state.firstSearchMessageTimerId !== null) {
        window.clearTimeout(
            state.firstSearchMessageTimerId
        );
        state.firstSearchMessageTimerId = null;
    }

    searchLoadingFirstSearchMessage.hidden = true;
}


function recordBackendResponse() {

    hasReceivedBackendResponse = true;

    if (currentSearchLoading) {
        hideSearchLoadingFirstSearchMessage(
            currentSearchLoading
        );
    }
}


function closeSearchLoading(state, completed) {

    if (currentSearchLoading !== state) {
        return;
    }

    if (state.progressTimerId !== null) {
        window.clearTimeout(state.progressTimerId);
    }

    if (state.completionTimerId !== null) {
        window.clearTimeout(state.completionTimerId);
    }

    if (state.fadeTimerId !== null) {
        window.clearTimeout(state.fadeTimerId);
    }

    hideSearchLoadingFirstSearchMessage(state);

    const resolveCompletion = state.resolveCompletion;
    const resolveFade = state.resolveFade;

    state.progressTimerId = null;
    state.completionTimerId = null;
    state.fadeTimerId = null;
    state.resolveCompletion = null;
    state.resolveFade = null;
    currentSearchLoading = null;

    searchLoadingPanel.classList.remove("is-fading");
    searchLoadingPanel.hidden = true;
    resultsContent.hidden = false;
    mapElement.removeAttribute("inert");
    searchLoadingTrack.style.setProperty(
        "--search-progress",
        "0%"
    );

    resolveCompletion?.(completed);
    resolveFade?.(completed);
}


function startSearchLoading(requestController) {

    if (currentSearchLoading) {
        closeSearchLoading(currentSearchLoading, false);
    }

    const state = {
        requestController,
        startedAt: window.performance.now(),
        progress: 0,
        progressTimerId: null,
        completionTimerId: null,
        fadeTimerId: null,
        firstSearchMessageTimerId: null,
        resolveCompletion: null,
        resolveFade: null,
        completionPromise: null,
        fadePromise: null
    };

    currentSearchLoading = state;

    clearSearchStatus();
    resultsContent.hidden = true;
    mapElement.setAttribute("inert", "");
    searchLoadingPanel.classList.remove("is-fading");
    searchLoadingPanel.hidden = false;
    setSearchLoadingProgress(state, 0);

    searchLoadingFirstSearchMessage.hidden = true;

    if (!hasReceivedBackendResponse) {
        state.firstSearchMessageTimerId = window.setTimeout(
            () => {
                state.firstSearchMessageTimerId = null;

                if (
                    currentSearchLoading !== state ||
                    hasReceivedBackendResponse
                ) {
                    return;
                }

                searchLoadingFirstSearchMessage.hidden = false;
            },
            FIRST_SEARCH_MESSAGE_DELAY_MS
        );
    }

    if (reducedMotionQuery.matches) {
        setSearchLoadingProgress(
            state,
            SEARCH_LOADING_LIMIT / 2
        );
        return;
    }

    state.progressTimerId = window.setTimeout(
        () => advanceSearchLoading(state),
        SEARCH_LOADING_TICK_MS
    );
}


function finishSearchLoading(requestController) {

    const state = currentSearchLoading;

    if (
        !state ||
        state.requestController !== requestController
    ) {
        return Promise.resolve(false);
    }

    if (state.completionPromise) {
        return state.completionPromise;
    }

    if (state.progressTimerId !== null) {
        window.clearTimeout(state.progressTimerId);
        state.progressTimerId = null;
    }

    setSearchLoadingProgress(state, 100);

    state.completionPromise = new Promise(resolve => {

        state.resolveCompletion = resolve;
        state.completionTimerId = window.setTimeout(
            () => {
                if (currentSearchLoading !== state) {
                    return;
                }

                state.completionTimerId = null;
                state.resolveCompletion = null;
                resolve(true);
            },
            reducedMotionQuery.matches
                ? 80
                : SEARCH_LOADING_COMPLETION_MS
        );
    });

    return state.completionPromise;
}


function waitForSearchLoadingPaint() {

    return new Promise(resolve => {

        let firstFrameId = null;
        let secondFrameId = null;
        let settled = false;

        const finish = () => {
            if (settled) {
                return;
            }

            settled = true;
            window.clearTimeout(timeoutId);

            if (firstFrameId !== null) {
                window.cancelAnimationFrame(firstFrameId);
            }

            if (secondFrameId !== null) {
                window.cancelAnimationFrame(secondFrameId);
            }

            resolve();
        };

        const timeoutId = window.setTimeout(
            finish,
            SEARCH_LOADING_PAINT_TIMEOUT_MS
        );

        firstFrameId = window.requestAnimationFrame(() => {
            firstFrameId = null;
            secondFrameId = window.requestAnimationFrame(
                finish
            );
        });
    });
}


function fadeSearchLoading(requestController) {

    const state = currentSearchLoading;

    if (
        !state ||
        state.requestController !== requestController
    ) {
        return Promise.resolve(false);
    }

    if (state.fadePromise) {
        return state.fadePromise;
    }

    if (reducedMotionQuery.matches) {
        closeSearchLoading(state, true);
        return Promise.resolve(true);
    }

    state.fadePromise = new Promise(resolve => {

        state.resolveFade = resolve;
        searchLoadingPanel.classList.add("is-fading");
        state.fadeTimerId = window.setTimeout(
            () => closeSearchLoading(state, true),
            SEARCH_LOADING_FADE_MS
        );
    });

    return state.fadePromise;
}


function stopSearchLoading(requestController = null) {

    const state = currentSearchLoading;

    if (
        !state ||
        (
            requestController &&
            state.requestController !== requestController
        )
    ) {
        return;
    }

    closeSearchLoading(state, false);
}


function setSearchPending(pending) {

    originInput.disabled = pending;
    dateInput.disabled = pending;
    searchButton.disabled = pending;
    searchButton.textContent = pending
        ? "Searching…"
        : SEARCH_BUTTON_LABEL;
    resultsContent.setAttribute(
        "aria-busy",
        String(pending)
    );

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


async function loadNightwaysData(
    reloadData = true,
    replaceUrlOnSuccess = false
) {

    if (!reloadData && !nightwaysData) {
        return;
    }


    let requestController = null;
    let submittedSearch = null;
    let loadingCompletionPromise = null;

    if (reloadData) {

        submittedSearch = getSubmittedSearch();

        currentSearchController?.abort();

        requestController = new AbortController();
        currentSearchController = requestController;
        nightwaysData = null;

        setSearchPending(true);
        startSearchLoading(requestController);

        resultsHeading.textContent =
            "Overnight destinations";

        map.setView(
            DEFAULT_MAP_CENTER,
            DEFAULT_MAP_ZOOM
        );
    }

    if (reloadData) {
        clearDestinationSummary();
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

                recordBackendResponse();
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

        const visibleDestinations =
            getVisibleSortedDestinations(
                data.destinations
            );


        if (reloadData) {
            nightwaysData = data;
            loadingCompletionPromise =
                finishSearchLoading(
                    requestController
                );
        }


        renderOriginMarker(
            originName,
            originCoordinates
        );


        resultsHeading.textContent =
            `Overnight destinations from ${originName}`;

        if (!reloadData) {
            showDestinationSummary(
                data.destinations,
                visibleDestinations
            );
        }


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
            const loadingCompleted =
                await loadingCompletionPromise;

            if (
                !loadingCompleted ||
                currentSearchController !==
                    requestController
            ) {
                return;
            }

            await waitForSearchLoadingPaint();

            if (
                currentSearchController !==
                    requestController
            ) {
                return;
            }

            const loadingFaded =
                await fadeSearchLoading(
                    requestController
                );

            if (
                !loadingFaded ||
                currentSearchController !==
                    requestController
            ) {
                return;
            }

            showDestinationSummary(
                data.destinations,
                visibleDestinations
            );

            clearSearchStatus();

            if (replaceUrlOnSuccess) {
                replaceSearchUrl(submittedSearch);
            }
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


        stopSearchLoading(requestController);

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

        clearDestinationSummary();

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
            stopSearchLoading(requestController);
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

        if (currentSearchController) {
            return;
        }

        loadNightwaysData(true, true);
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

if (initialSearchState.shouldSearch) {
    loadNightwaysData(
        true,
        initialSearchState.replaceUrlOnSuccess
    );
}
