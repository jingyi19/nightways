const DATA_FILE = "./data/nightways_dresden_2026-08-14.json";

const destinationCount = document.getElementById("destination-count");
const destinationList = document.getElementById("destination-list");
const searchButton = document.getElementById("search-button");
const modeFilterButtons = document.querySelectorAll(
    ".mode-filter-button"
);

const TRAIN_SERVICE_MODES = new Set([
    "TRAIN",
    "LONG_DISTANCE",
    "REGIONAL_RAIL"
]);

let activeMode = "ALL";
let nightwaysData = null;


// --------------------------------------------------
// MAP
// --------------------------------------------------

const DRESDEN_COORDINATES = [51.0504, 13.7373];

const map = L.map("map").setView(
    [50.5, 10.5],
    4
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
// DRESDEN ORIGIN
// --------------------------------------------------

// Soft red outer ring
L.circleMarker(
    DRESDEN_COORDINATES,
    {
        radius: 13,
        color: "#d94a5a",
        weight: 1,
        opacity: 0.35,
        fillColor: "#d94a5a",
        fillOpacity: 0.08
    }
).addTo(map);


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


L.marker(
    DRESDEN_COORDINATES,
    {
        icon: originIcon
    }
)
    .addTo(map)
    .bindPopup(
        "<strong>Dresden</strong><br>Starting point"
    );


// Destination marker layer
const destinationMarkers =
    L.layerGroup().addTo(map);


// --------------------------------------------------
// TIME FORMATTING
// --------------------------------------------------

function formatTime(dateString) {

    return dateString.slice(11, 16);
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


// --------------------------------------------------
// SERVICE DETAILS
// --------------------------------------------------

function createServiceHTML(service) {

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


    return `
        <div class="service-block">

            <div class="service-header">

                <strong>
                    ${service.service}
                </strong>

                <span>
                    ${service.mode}
                </span>

            </div>

            <div class="departure-info">

                <span>
                    ${formatTime(service.departure)}
                </span>

                <span>
                    from Dresden
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

function openDestination(card, destination) {

    const existingDetails =
        card.querySelector(
            ".destination-details"
        );


    if (existingDetails) {

        existingDetails.remove();

        card.classList.remove(
            "expanded"
        );

        return;
    }


    const details =
        document.createElement("div");

    details.className =
        "destination-details";


    const servicesHTML =
        destination.services
            .map(createServiceHTML)
            .join("");


    details.innerHTML = `
        <div class="details-divider"></div>

        ${servicesHTML}
    `;


    card.appendChild(details);

    card.classList.add(
        "expanded"
    );
}


// --------------------------------------------------
// MAKE SURE DESTINATION IS OPEN
// --------------------------------------------------

function ensureDestinationOpen(card, destination) {

    const existingDetails =
        card.querySelector(
            ".destination-details"
        );


    if (!existingDetails) {

        openDestination(
            card,
            destination
        );
    }
}


// --------------------------------------------------
// LOAD NIGHTWAYS DATA
// --------------------------------------------------

async function loadNightwaysData(reloadData = true) {

    destinationCount.textContent =
        "Loading...";

    destinationList.innerHTML = "";

    destinationMarkers.clearLayers();

    clearDestinationPreview();


    try {

        let data = nightwaysData;


        if (reloadData || !data) {

            const response =
                await fetch(DATA_FILE);


            if (!response.ok) {

                throw new Error(
                    `Could not load data: ${response.status}`
                );
            }


            data = await response.json();

            nightwaysData = data;
        }


        const visibleDestinations =
            data.destinations
                .map(getDestinationForMode)
                .filter(destination =>
                    destination !== null
                );


        destinationCount.textContent =
            `${visibleDestinations.length} direct overnight destinations`;


        const mapBounds =
            L.latLngBounds([
                DRESDEN_COORDINATES
            ]);


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

                    <span class="expand-icon">
                        ＋
                    </span>

                </div>

                <p>
                    Arrive from

                    <strong>
                        ${formatTime(
                            destination.earliest_arrival
                        )}
                    </strong>
                </p>

                <p>
                    ${destination.service_count}
                    service(s)

                    ·

                    ${destination.station_count}
                    stop(s)
                </p>
            `;


            // Click card
            card.addEventListener(
                "click",
                () => {

                    openDestination(
                        card,
                        destination
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


                // ----------------------------------
                // SHOW / HIDE PREVIEW
                // ----------------------------------

                function showPreview() {

                    highlightDestinationMarker(
                        marker
                    );

                    showDestinationPreview(
                        destination
                    );
                }


                function hidePreview() {

                    resetDestinationMarker(
                        marker
                    );

                    clearDestinationPreview();
                }


                // ----------------------------------
                // MAP CLICK
                // ----------------------------------

                function selectDestinationFromMap() {

                    showPreview();

                    ensureDestinationOpen(
                        card,
                        destination
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
        // FIT MAP TO DESTINATIONS
        // ----------------------------------

        map.fitBounds(
            mapBounds,
            {
                padding: [30, 30]
            }
        );


    } catch (error) {

        console.error(error);

        destinationCount.textContent =
            "Could not load overnight destinations.";
    }
}


// --------------------------------------------------
// SEARCH BUTTON
// --------------------------------------------------

searchButton.addEventListener(
    "click",
    () => {

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
// INITIAL LOAD
// --------------------------------------------------

updateModeFilterButtons();

loadNightwaysData(true);
