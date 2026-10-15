
import os
import threading
import time
import csv
import io
import re
import zipfile
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template_string, request


app = Flask(__name__)

BUS_KEY = os.environ.get("CTA_BUS_KEY", "")
TRAIN_KEY = os.environ.get("CTA_TRAIN_KEY", "")

BUS_ROUTES_URL = "https://www.ctabustracker.com/bustime/api/v3/getroutes"
BUS_URL = "https://www.ctabustracker.com/bustime/api/v3/getvehicles"
TRAIN_URL = "https://lapi.transitchicago.com/api/1.0/ttpositions.aspx"
GTFS_URL = "https://www.transitchicago.com/downloads/sch_data/google_transit.zip"
GTFS_CACHE_SECONDS = 6 * 60 * 60  # 6 hours

_gtfs_cache = {"expires": 0, "features": None}
_gtfs_lock = threading.Lock()
_arrival_cache = {}
_arrival_cache_lock = threading.Lock()
ARRIVAL_CACHE_SECONDS = 30

# CTA rail route codes and colors.
TRAIN_LINES = {
    "red": ("Red Line", "#c60c30"),
    "blue": ("Blue Line", "#00a1de"),
    "brn": ("Brown Line", "#62361b"),
    "g": ("Green Line", "#009b3a"),
    "org": ("Orange Line", "#f9461c"),
    "p": ("Purple Line", "#522398"),
    "pink": ("Pink Line", "#e27ea6"),
    "y": ("Yellow Line", "#f9e300"),
}

CACHE_SECONDS = 30
_cache = {"expires": 0, "data": None}
_cache_lock = threading.Lock()


def as_list(value):
    """Normalize an API value that may be a list, a single object, or null."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check_api_errors(payload):
    """Ignore route-specific 'no data' errors; raise for other API errors."""
    api_errors = as_list(payload.get("error"))
    fatal_errors = []

    for error in api_errors:
        if (
            isinstance(error, dict)
            and error.get("rt")
            and "no data found for parameter"
            in str(error.get("msg", "")).lower()
        ):
            # This route has no data in this response; continue with others.
            continue

        fatal_errors.append(error)

    if fatal_errors:
        raise RuntimeError(str(fatal_errors))


def fetch_buses():
    if not BUS_KEY:
        raise RuntimeError("Missing CTA_BUS_KEY")

    common_params = {"key": BUS_KEY, "format": "json"}

    # Get the route designators served by the system.
    response = requests.get(
        BUS_ROUTES_URL,
        params=common_params,
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json().get("bustime-response", {})

    check_api_errors(payload)

    routes = as_list(payload.get("routes"))
    bus_routes = [
        {
            "code": str(route["rt"]),
            "name": str(route.get("rtnm") or "").strip(),
        }
        for route in routes
        if isinstance(route, dict) and route.get("rt")
    ]
    route_ids = [route["code"] for route in bus_routes]

    if not route_ids:
        raise RuntimeError("CTA returned no bus routes")

    buses = []

    # Request vehicles in batches of up to 10 routes.
    for start in range(0, len(route_ids), 10):
        route_batch = route_ids[start:start + 10]

        response = requests.get(
            BUS_URL,
            params={
                **common_params,
                "rt": ",".join(route_batch),
                "tmres": "s",
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json().get("bustime-response", {})

        check_api_errors(payload)

        for vehicle in as_list(payload.get("vehicle")):
            lat = number(vehicle.get("lat"))
            lon = number(vehicle.get("lon"))

            if lat is None or lon is None:
                continue

            buses.append({
                "type": "bus",
                "id": str(vehicle.get("vid", "Bus")),
                "route": str(vehicle.get("rt", "")),
                "destination": str(vehicle.get("des", "")),
                "lat": lat,
                "lon": lon,
                "heading": vehicle.get("hdg"),
                "updated": vehicle.get("tmstmp", ""),
                "color": "#1769aa",
            })

    return {"vehicles": buses, "routes": bus_routes}


def fetch_trains():
    if not TRAIN_KEY:
        raise RuntimeError("Missing CTA_TRAIN_KEY")

    response = requests.get(
        TRAIN_URL,
        params={
            "key": TRAIN_KEY,
            "rt": ",".join(TRAIN_LINES.keys()),
            "outputType": "JSON",
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json().get("ctatt", {})

    if payload.get("errCd") not in (None, "0", 0):
        raise RuntimeError(
            payload.get("errNm") or f"CTA error {payload['errCd']}"
        )

    trains = []

    for route in as_list(payload.get("route")):
        route_code = str(route.get("@name", "")).lower()
        line_name, color = TRAIN_LINES.get(
            route_code,
            (route_code, "#555555"),
        )

        for train in as_list(route.get("train")):
            lat = number(train.get("lat"))
            lon = number(train.get("lon"))

            # Some schedule-based entries may not have a live position.
            if lat is None or lon is None:
                continue

            trains.append({
                "type": "train",
                "id": str(train.get("rn", "Train")),
                "route": line_name,
                "route_code": route_code,
                "destination": str(train.get("destNm", "")),
                "next_station": str(train.get("nextStaNm", "")),
                "lat": lat,
                "lon": lon,
                "heading": train.get("heading"),
                "updated": train.get("prdt", ""),
                "color": color,
            })

    return trains


def get_vehicle_data():
    now = time.time()

    if _cache["data"] is not None and now < _cache["expires"]:
        return _cache["data"]

    # Keep simultaneous page requests from triggering duplicate API calls.
    with _cache_lock:
        now = time.time()

        if _cache["data"] is not None and now < _cache["expires"]:
            return _cache["data"]

        vehicles = []
        errors = {}
        bus_routes = []

        try:
            bus_data = fetch_buses()
            vehicles.extend(bus_data["vehicles"])
            bus_routes = bus_data["routes"]
        except Exception as exc:
            errors["buses"] = str(exc)

        try:
            vehicles.extend(fetch_trains())
        except Exception as exc:
            errors["trains"] = str(exc)

        train_lines = [
            {"code": code, "name": name, "color": color}
            for code, (name, color) in TRAIN_LINES.items()
        ]

        data = {
            "vehicles": vehicles,
            "bus_routes": bus_routes,
            "train_lines": train_lines,
            "errors": errors,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        _cache["data"] = data
        _cache["expires"] = time.time() + CACHE_SECONDS
        return data


@app.get("/api/vehicles")
def api_vehicles():
    return jsonify(get_vehicle_data())


def normalize_route(value):
    """Normalize route names/codes for matching GTFS entries."""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def load_gtfs_shape_features():
    """Download/cache CTA GTFS shapes and stops for bus and train routes."""
    now = time.time()
    if _gtfs_cache["features"] is not None and now < _gtfs_cache["expires"]:
        return _gtfs_cache["features"]

    with _gtfs_lock:
        now = time.time()
        if _gtfs_cache["features"] is not None and now < _gtfs_cache["expires"]:
            return _gtfs_cache["features"]

        response = requests.get(GTFS_URL, timeout=90)
        response.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(response.content)) as feed:
            # Associate GTFS route IDs with the route codes used by the map.
            route_id_to_map_route = {}

            with feed.open("routes.txt") as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                for route in csv.DictReader(text):
                    route_id = route.get("route_id", "")
                    route_type = route.get("route_type", "")
                    short_name = route.get("route_short_name", "")
                    long_name = route.get("route_long_name", "")

                    candidates = {
                        normalize_route(route_id),
                        normalize_route(short_name),
                        normalize_route(long_name),
                    }

                    if route_type == "3":  # GTFS bus
                        # Bus Tracker route code usually matches GTFS short name.
                        code = short_name.strip() or route_id.strip()
                        if code:
                            route_id_to_map_route[route_id] = {
                                "type": "bus",
                                "code": code,
                                "name": f"Route {code}",
                                "color": "#1769aa",
                            }

                    elif route_type == "1":  # GTFS rail
                        for code, (line_name, color) in TRAIN_LINES.items():
                            aliases = {
                                normalize_route(code),
                                normalize_route(line_name),
                                normalize_route(line_name.replace(" Line", "")),
                            }

                            # Include common GTFS abbreviations.
                            if code == "brn":
                                aliases.add("brown")
                            elif code == "g":
                                aliases.add("green")
                            elif code == "org":
                                aliases.add("orange")
                            elif code == "p":
                                aliases.add("purple")
                            elif code == "y":
                                aliases.add("yellow")

                            if candidates & aliases:
                                route_id_to_map_route[route_id] = {
                                    "type": "train",
                                    "code": code,
                                    "name": line_name,
                                    "color": color,
                                }
                                break

            # Map trips to routes and collect each route's shape IDs.
            shape_id_to_routes = {}
            trip_id_to_route = {}

            with feed.open("trips.txt") as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                for trip in csv.DictReader(text):
                    mapped_route = route_id_to_map_route.get(
                        trip.get("route_id", "")
                    )
                    if not mapped_route:
                        continue

                    trip_id = trip.get("trip_id", "")
                    shape_id = trip.get("shape_id", "")

                    if trip_id:
                        trip_id_to_route[trip_id] = mapped_route

                    if shape_id:
                        shape_id_to_routes.setdefault(shape_id, []).append(
                            mapped_route
                        )

            wanted_shape_ids = set(shape_id_to_routes)
            points_by_shape = {}

            with feed.open("shapes.txt") as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                for point in csv.DictReader(text):
                    shape_id = point.get("shape_id", "")
                    if shape_id not in wanted_shape_ids:
                        continue

                    try:
                        lat = float(point["shape_pt_lat"])
                        lon = float(point["shape_pt_lon"])
                        sequence = int(point["shape_pt_sequence"])
                    except (KeyError, TypeError, ValueError):
                        continue

                    points_by_shape.setdefault(shape_id, []).append(
                        (sequence, [lon, lat])
                    )

            features = []
            seen = set()

            for shape_id, route_entries in shape_id_to_routes.items():
                points = points_by_shape.get(shape_id, [])
                coordinates = [
                    coordinate
                    for _, coordinate in sorted(points, key=lambda item: item[0])
                ]

                if len(coordinates) < 2:
                    continue

                for route in route_entries:
                    # Avoid duplicate shapes for the same route.
                    unique_key = (route["type"], route["code"], shape_id)
                    if unique_key in seen:
                        continue
                    seen.add(unique_key)

                    features.append({
                        "type": "Feature",
                        "properties": {
                            "vehicle_type": route["type"],
                            "route_code": route["code"],
                            "route_name": route["name"],
                            "color": route["color"],
                        },
                        "geometry": {
                            "type": "LineString",
                            "coordinates": coordinates,
                        },
                    })

            # Associate scheduled stops with the routes used by their trips.
            route_stop_ids = {}

            with feed.open("stop_times.txt") as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                for stop_time in csv.DictReader(text):
                    mapped_route = trip_id_to_route.get(
                        stop_time.get("trip_id", "")
                    )
                    stop_id = stop_time.get("stop_id", "")

                    if mapped_route and stop_id:
                        route_key = (
                            mapped_route["type"],
                            mapped_route["code"],
                        )
                        route_stop_ids.setdefault(route_key, set()).add(stop_id)

            # Read stop coordinates and names from the GTFS feed.
            stops_by_id = {}

            with feed.open("stops.txt") as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                for stop in csv.DictReader(text):
                    stop_id = stop.get("stop_id", "")

                    try:
                        lat = float(stop.get("stop_lat", ""))
                        lon = float(stop.get("stop_lon", ""))
                    except (TypeError, ValueError):
                        continue

                    if stop_id:
                        stops_by_id[stop_id] = {
                            "id": stop_id,
                            "name": stop.get("stop_name", "").strip(),
                            "lat": lat,
                            "lon": lon,
                            "parent_station": stop.get(
                                "parent_station", ""
                            ).strip(),
                        }

            route_details = {
                (route["type"], route["code"]): route
                for route in route_id_to_map_route.values()
            }

            seen_stops = set()

            for route_key, stop_ids in route_stop_ids.items():
                route = route_details.get(route_key)
                if not route:
                    continue

                for stop_id in stop_ids:
                    stop = stops_by_id.get(stop_id)
                    if not stop:
                        continue

                    # Use the parent station when available to group rail platforms.
                    if route["type"] == "train" and stop["parent_station"]:
                        stop = stops_by_id.get(stop["parent_station"], stop)

                    unique_key = (route["type"], route["code"], stop["id"])
                    if unique_key in seen_stops:
                        continue
                    seen_stops.add(unique_key)

                    features.append({
                        "type": "Feature",
                        "properties": {
                            "feature_type": "stop",
                            "vehicle_type": route["type"],
                            "route_code": route["code"],
                            "route_name": route["name"],
                            "stop_name": stop["name"] or "Unnamed stop",
                            "station_id": stop["id"] if route["type"] == "train" else "",
                            "bus_stop_id": stop["id"] if route["type"] == "bus" else "",
                            "color": route["color"],
                        },
                        "geometry": {
                            "type": "Point",
                            "coordinates": [stop["lon"], stop["lat"]],
                        },
                    })

        _gtfs_cache["features"] = features
        _gtfs_cache["expires"] = time.time() + GTFS_CACHE_SECONDS
        return features


@app.get("/api/route-shapes")
def api_route_shapes():
    try:
        features = load_gtfs_shape_features()

        # "*" means all routes; an empty value means none selected.
        bus_arg = request.args.get("bus", "*")
        train_arg = request.args.get("train", "*")

        selected_bus = (
            None if bus_arg == "*"
            else set(filter(None, bus_arg.split(",")))
        )
        selected_train = (
            None if train_arg == "*"
            else set(filter(None, train_arg.split(",")))
        )

        matching = []
        for feature in features:
            if feature["geometry"]["type"] != "LineString":
                continue

            props = feature["properties"]

            if props["vehicle_type"] == "bus":
                selected = selected_bus
            else:
                selected = selected_train

            if selected is None or props["route_code"] in selected:
                matching.append(feature)

        return jsonify({
            "type": "FeatureCollection",
            "features": matching,
        })

    except Exception as exc:
        app.logger.exception("Could not load CTA GTFS route shapes")
        return jsonify({"error": str(exc)}), 502


@app.get("/api/route-stops")
def api_route_stops():
    try:
        features = load_gtfs_shape_features()

        bus_arg = request.args.get("bus", "*")
        train_arg = request.args.get("train", "*")

        selected_bus = (
            None if bus_arg == "*"
            else set(filter(None, bus_arg.split(",")))
        )
        selected_train = (
            None if train_arg == "*"
            else set(filter(None, train_arg.split(",")))
        )

        matching = []

        for feature in features:
            if feature["geometry"]["type"] != "Point":
                continue

            props = feature["properties"]
            selected = (
                selected_bus
                if props["vehicle_type"] == "bus"
                else selected_train
            )

            if selected is None or props["route_code"] in selected:
                matching.append(feature)

        return jsonify({
            "type": "FeatureCollection",
            "features": matching,
        })

    except Exception as exc:
        app.logger.exception("Could not load CTA GTFS stops")
        return jsonify({"error": str(exc)}), 502

TRAIN_ARRIVALS_URL = "https://lapi.transitchicago.com/api/1.0/ttarrivals.aspx"

TRAIN_DIRECTIONS = {
    "red": {
        "1": "Howard-bound",
        "5": "95th/Dan Ryan-bound",
    },
    "blue": {
        "1": "O’Hare-bound",
        "5": "Forest Park-bound",
    },
    "brn": {
        "1": "Kimball-bound",
        "5": "Loop-bound",
    },
    "g": {
        "1": "Harlem/Lake-bound",
        "5": "Ashland/63rd- or Cottage Grove-bound",
    },
    "org": {
        "1": "Loop-bound",
        "5": "Midway-bound",
    },
    "p": {
        "1": "Linden-bound",
        "5": "Howard- or Loop-bound",
    },
    "pink": {
        "1": "Loop-bound",
        "5": "54th/Cermak-bound",
    },
    "y": {
        "1": "Skokie-bound",
        "5": "Howard-bound",
    },
}


@app.get("/api/station-arrivals")
def api_station_arrivals():
    station_id = request.args.get("station_id", "").strip()

    # CTA rail stop IDs are 3xxxx; parent station IDs are 4xxxx.
    if (
        len(station_id) != 5
        or not station_id.isdigit()
        or station_id[0] not in ("3", "4")
    ):
        return jsonify({"error": "Invalid CTA train station ID"}), 400

    if not TRAIN_KEY:
        return jsonify({"error": "Missing CTA_TRAIN_KEY"}), 500

    now = time.time()
    cached = _arrival_cache.get(station_id)
    if cached and now < cached["expires"]:
        return jsonify(cached["data"])

    # Ask for arrivals by parent station (mapid) or individual platform (stpid).
    id_param = "mapid" if station_id.startswith("4") else "stpid"

    try:
        response = requests.get(
            TRAIN_ARRIVALS_URL,
            params={
                "key": TRAIN_KEY,
                id_param: station_id,
                "max": 20,
                "outputType": "JSON",
            },
            timeout=15,
        )
        response.raise_for_status()

        ctatt = response.json().get("ctatt", {})
        error_code = str(ctatt.get("errCd", "0"))
        if error_code not in ("0", ""):
            raise RuntimeError(
                ctatt.get("errNm") or f"CTA error {error_code}"
            )

        arrivals = []
        for eta in as_list(ctatt.get("eta")):
            line_code = str(eta.get("rt", "")).lower()
            line_name, color = TRAIN_LINES.get(
                line_code,
                (eta.get("rt", "Train"), "#555555"),
            )

            direction_code = str(eta.get("trDr", ""))
            direction = TRAIN_DIRECTIONS.get(
                line_code, {}
            ).get(direction_code) or str(eta.get("destNm", "Direction unavailable"))

            is_scheduled = str(eta.get("isSch", "0")).lower() in (
                "1", "true", "yes"
            )

            arrivals.append({
                "line": line_name,
                "color": color,
                "direction": direction,
                "destination": str(eta.get("destNm", "")),
                "platform": str(eta.get("stpDe", "")),
                "estimated_time": str(eta.get("arrT", "")),
                "status": "Scheduled" if is_scheduled else "Tracked",
                "run": str(eta.get("rn", "")),
            })

        data = {
            "station_name": str(ctatt.get("staNm", "")),
            "arrivals": arrivals,
        }

        _arrival_cache[station_id] = {
            "expires": time.time() + ARRIVAL_CACHE_SECONDS,
            "data": data,
        }
        return jsonify(data)

    except Exception as exc:
        app.logger.exception("Could not load train station arrivals")
        return jsonify({"error": str(exc)}), 502

BUS_PREDICTIONS_URL = (
    "https://www.ctabustracker.com/bustime/api/v3/getpredictions"
)

@app.get("/api/bus-stop-arrivals")
def api_bus_stop_arrivals():
    stop_id = request.args.get("stop_id", "").strip()
    route_code = request.args.get("route", "").strip()

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", stop_id):
        return jsonify({"error": "Invalid CTA bus stop ID"}), 400

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", route_code):
        return jsonify({"error": "Invalid CTA bus route"}), 400

    if not BUS_KEY:
        return jsonify({"error": "Missing CTA_BUS_KEY"}), 500

    cache_key = f"bus:{stop_id}:{route_code}"
    now = time.time()

    with _arrival_cache_lock:
        cached = _arrival_cache.get(cache_key)
        if cached and now < cached["expires"]:
            return jsonify(cached["data"])

    try:
        response = requests.get(
            BUS_PREDICTIONS_URL,
            params={
                "key": BUS_KEY,
                "format": "json",
                "stpid": stop_id,
                "rt": route_code,
                "top": 10,
                "tmres": "m",
            },
            timeout=15,
        )
        response.raise_for_status()

        payload = response.json().get("bustime-response", {})
        check_api_errors(payload)

        predictions = []

        for prediction in as_list(payload.get("prd")):
            is_delayed = str(prediction.get("dly", "false")).lower() in (
                "1", "true", "yes"
            )

            predictions.append({
                "line": str(
                    prediction.get("rtdd")
                    or prediction.get("rt")
                    or route_code
                ),
                "direction": str(prediction.get("rtdir", "")),
                "destination": str(prediction.get("des", "")),
                "estimated_time": str(prediction.get("prdtm", "")),
                "status": "Delayed" if is_delayed else "Tracked",
                "vehicle": str(prediction.get("vid", "")),
                "type": str(prediction.get("typ", "")),
            })

        data = {"predictions": predictions}

        with _arrival_cache_lock:
            _arrival_cache[cache_key] = {
                "expires": time.time() + ARRIVAL_CACHE_SECONDS,
                "data": data,
            }

        return jsonify(data)

    except Exception as exc:
        app.logger.exception("Could not load CTA bus-stop predictions")
        return jsonify({"error": str(exc)}), 502

@app.get("/")
def index():
    return render_template_string(PAGE)


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CTA Live Map</title>

  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  >

  <style>
* {
  box-sizing: border-box;
}

body {
  height: 100vh;
  margin: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  font: 16px system-ui, sans-serif;
  color: #222;
}

header {
  position: relative;
  z-index: 1000;
  flex: 0 0 auto;
  padding: 12px 16px;
  background: #17212b;
  color: white;
}

.header-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

h1 {
  margin: 0 0 4px;
  font-size: 1.25rem;
}

#status {
  font-size: .9rem;
}

#filterToggle {
  flex: 0 0 auto;
  padding: 8px 12px;
  border: 1px solid #63717d;
  border-radius: 7px;
  background: #263744;
  color: white;
  font: inherit;
  cursor: pointer;
}

#filterToggle:hover {
  background: #344957;
}

#filters {
  position: absolute;
  top: calc(100% + 8px);
  right: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: min(360px, calc(100vw - 32px));
  max-height: min(70vh, 560px);
  overflow-y: auto;
  padding: 14px;
  border: 1px solid #d5dbe0;
  border-radius: 10px;
  background: white;
  color: #222;
  box-shadow: 0 8px 24px rgb(0 0 0 / 22%);
}

#filters[hidden] {
  display: none;
}

.filter-group {
  min-width: 0;
  max-height: 210px;
  overflow-y: auto;
  padding-bottom: 8px;
  border-bottom: 1px solid #e3e6e8;
}

.filter-group:last-child {
  padding-bottom: 0;
  border-bottom: 0;
}

.filter-group strong {
  position: sticky;
  top: 0;
  display: block;
  padding: 4px 0;
  background: white;
}

.filter-option {
  display: block;
  padding: 3px 0;
  white-space: nowrap;
  font-size: .9rem;
}

.filter-option input {
  margin-right: 6px;
}

.train-route-icon {
  border: 0;
  background: transparent;
}

.train-route-circle {
  display: block;
  width: 30px;
  height: 30px;
  border: 2px solid white;
  border-radius: 50%;
  box-shadow: 0 1px 4px rgb(0 0 0 / 40%);
}

.bus-route-icon {
  border: 0;
  background: transparent;
}

.bus-route-number {
  display: flex;
  width: 30px;
  height: 30px;
  align-items: center;
  justify-content: center;
  border: 2px solid white;
  border-radius: 50%;
  background: #1769aa;
  color: white;
  font-size: 12px;
  font-weight: 700;
  box-shadow: 0 1px 4px rgb(0 0 0 / 40%);
}

#map {
  flex: 1 1 auto;
  min-height: 350px;
}
#loadingOverlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: grid;
  place-items: center;
  padding: 24px;
  background: linear-gradient(145deg, #101820, #203746);
  color: #fff;
  transition: opacity 300ms ease, visibility 300ms ease;
}

#loadingOverlay.hidden {
  visibility: hidden;
  opacity: 0;
  pointer-events: none;
}

.loading-card {
  width: min(420px, 100%);
  padding: 32px 28px;
  border: 1px solid rgb(255 255 255 / 14%);
  border-radius: 20px;
  background: rgb(255 255 255 / 7%);
  box-shadow: 0 24px 70px rgb(0 0 0 / 30%);
  text-align: center;
  backdrop-filter: blur(12px);
}

.loading-mark {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 9px;
  margin: 0 auto 22px;
}

.loading-dot {
  width: 18px;
  height: 18px;
  border: 3px solid white;
  border-radius: 50%;
  animation: loading-pulse 1.2s ease-in-out infinite;
}

.loading-dot:nth-child(2) {
  background: #c60c30;
  animation-delay: 0.15s;
}

.loading-dot:nth-child(3) {
  background: #00a1de;
  animation-delay: 0.3s;
}

.loading-dot:nth-child(4) {
  background: #009b3a;
  animation-delay: 0.45s;
}

@keyframes loading-pulse {
  0%, 100% { transform: scale(.8); opacity: .55; }
  50% { transform: scale(1.15); opacity: 1; }
}

.loading-card h2 {
  margin: 0 0 9px;
  font-size: 1.5rem;
}

.loading-card p {
  margin: 0;
  color: #d2dce2;
  line-height: 1.5;
}

#retryLoad {
  display: none;
  margin-top: 20px;
  padding: 10px 18px;
  border: 0;
  border-radius: 8px;
  background: #fff;
  color: #17212b;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}

#loadingOverlay.has-error #retryLoad {
  display: inline-block;
}

#loadingOverlay.has-error .loading-mark {
  display: none;
}

@media (prefers-reduced-motion: reduce) {
  .loading-dot {
    animation: none;
  }

  #loadingOverlay {
    transition: none;
  }
}
/* --- Refined application theme --- */
:root {
  color-scheme: light;
  --ink: #17212b;
  --muted: #647482;
  --line: #e4e9ed;
  --surface: #ffffff;
  --surface-soft: #f5f8fa;
  --accent: #1769aa;
  --shadow: 0 12px 34px rgb(17 35 49 / 16%);
}

body {
  background: #eef2f5;
  color: var(--ink);
  font-family:
    Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif;
  letter-spacing: -0.01em;
}

header {
  padding: 16px 20px;
  background:
    radial-gradient(ellipse at top left, rgb(56 83 101 / 40%), transparent 55%),
    linear-gradient(145deg, #101820, #203746);
  color: #fff;
  box-shadow: 0 4px 18px rgb(16 24 32 / 20%);
}

h1 {
  margin: 0 0 5px;
  font-size: clamp(1.1rem, 2vw, 1.35rem);
  font-weight: 700;
  letter-spacing: -0.035em;
}

#status {
  color: #c7d3dc;
  font-size: 0.84rem;
  line-height: 1.4;
}

#filterToggle {
  min-height: 42px;
  padding: 9px 15px;
  border: 1px solid rgb(255 255 255 / 20%);
  border-radius: 12px;
  background: rgb(255 255 255 / 9%);
  color: #fff;
  font-size: 0.9rem;
  font-weight: 650;
  box-shadow: 0 4px 12px rgb(0 0 0 / 12%);
  transition:
    background 160ms ease,
    border-color 160ms ease,
    transform 160ms ease;
}

#filterToggle:hover {
  border-color: rgb(255 255 255 / 35%);
  background: rgb(255 255 255 / 16%);
  transform: translateY(-1px);
}

#filterToggle:focus-visible,
#retryLoad:focus-visible,
#filters input:focus-visible {
  outline: 3px solid #7cc8ff;
  outline-offset: 3px;
}

/* Settings dropdown */
#filters {
  gap: 10px;
  width: min(380px, calc(100vw - 28px));
  max-height: min(76vh, 620px);
  padding: 16px;
  border: 1px solid rgb(255 255 255 / 65%);
  border-radius: 16px;
  background: rgb(255 255 255 / 97%);
  color: var(--ink);
  box-shadow: var(--shadow);
  backdrop-filter: blur(16px);
}

#filters[hidden] {
  display: none;
}

.filter-group {
  max-height: 230px;
  padding: 2px 4px 10px;
  border-bottom-color: var(--line);
}

.filter-group strong {
  padding: 8px 4px;
  color: #667887;
  font-size: 0.72rem;
  font-weight: 750;
  letter-spacing: 0.09em;
  text-transform: uppercase;
}

.filter-option {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 1px 0;
  padding: 7px 8px;
  border-radius: 8px;
  color: #334451;
  font-size: 0.88rem;
  line-height: 1.35;
  white-space: normal;
  transition: background 140ms ease;
}

.filter-option:hover {
  background: var(--surface-soft);
}

.filter-option input {
  flex: 0 0 auto;
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: var(--accent);
  cursor: pointer;
}

#routeShapeStatus,
#stopStatus {
  padding: 8px 10px;
  border-radius: 9px;
  background: #f1f6f9;
  color: #526675;
  font-size: 0.78rem;
  font-weight: 600;
}

/* Map controls */
.leaflet-control-zoom {
  overflow: hidden;
  border: 1px solid rgb(23 33 43 / 10%) !important;
  border-radius: 11px !important;
  box-shadow: 0 4px 14px rgb(23 33 43 / 14%) !important;
}

.leaflet-control-zoom a {
  width: 34px !important;
  height: 34px !important;
  color: var(--ink) !important;
  line-height: 34px !important;
}

.leaflet-control-attribution {
  border-radius: 7px 0 0 0;
  background: rgb(255 255 255 / 86%) !important;
  color: #667887 !important;
  backdrop-filter: blur(8px);
}

/* Arrival and vehicle popups */
.leaflet-popup-content-wrapper {
  overflow: hidden;
  border: 1px solid rgb(23 33 43 / 8%);
  border-radius: 14px;
  box-shadow: 0 12px 34px rgb(17 35 49 / 18%);
}

.leaflet-popup-content {
  margin: 15px 17px;
  color: var(--ink);
  font-family: inherit;
  font-size: 0.88rem;
  line-height: 1.45;
}

.leaflet-popup-content strong {
  display: block;
  margin-bottom: 8px;
  font-size: 1rem;
  font-weight: 750;
  letter-spacing: -0.02em;
}

.leaflet-popup-content p {
  margin: 8px 0;
  color: var(--muted);
}

.leaflet-popup-tip {
  box-shadow: 0 4px 8px rgb(17 35 49 / 8%);
}

.leaflet-popup-close-button {
  color: #70808c !important;
}

.leaflet-popup-content table {
  border-collapse: separate !important;
  border-spacing: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 9px;
  font-size: 0.8rem;
}

.leaflet-popup-content th {
  padding: 8px 7px !important;
  background: #f3f6f8;
  color: #667887;
  font-size: 0.68rem;
  font-weight: 750;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.leaflet-popup-content td {
  padding: 8px 7px;
  border-top: 1px solid var(--line);
  vertical-align: top;
}

.leaflet-popup-content tr:first-child td {
  border-top: 0;
}

.leaflet-popup-content td:first-child {
  font-weight: 700;
}

@media (max-width: 600px) {
  header {
    padding: 13px 14px;
  }

  .header-main {
    gap: 10px;
  }

  #filterToggle {
    min-height: 40px;
    padding: 8px 11px;
  }

  #filters {
    top: calc(100% + 6px);
    right: 10px;
    width: min(380px, calc(100vw - 20px));
    max-height: 72vh;
  }

  .leaflet-popup-content {
    margin: 13px;
  }
}

@media (prefers-reduced-motion: reduce) {
  #filterToggle,
  .filter-option {
    transition: none;
  }
}
#filters {
  visibility: hidden;
  opacity: 0;
  transform: translateY(-8px) scale(0.98);
  transform-origin: top right;
  transition:
    opacity 180ms ease,
    transform 180ms ease,
    visibility 0s linear 180ms;
}

#filters.is-open {
  visibility: visible;
  opacity: 1;
  transform: translateY(0) scale(1);
  transition:
    opacity 180ms ease,
    transform 180ms ease,
    visibility 0s;
}

@media (prefers-reduced-motion: reduce) {
  #filters {
    transition: none;
  }
}
#routeShapeStatus.is-loading,
#stopStatus.is-loading {
  display: flex;
  align-items: center;
  gap: 9px;
}

#routeShapeStatus.is-loading::before,
#stopStatus.is-loading::before {
  content: "";
  flex: 0 0 14px;
  width: 14px;
  height: 14px;
  border: 2px solid #cbd5dc;
  border-top-color: #1769aa;
  border-radius: 50%;
  animation: status-spin 700ms linear infinite;
}

@keyframes status-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  #routeShapeStatus.is-loading::before,
  #stopStatus.is-loading::before {
    animation: none;
  }
}
  </style>
</head>

<body>
<header>
  <div class="header-main">
    <div>
      <h1>CTA Live Map</h1>
      <div id="status">Loading vehicle locations…</div>
    </div>

    <button
      id="filterToggle"
      type="button"
      aria-expanded="false"
      aria-controls="filters"
    >
      Settings <span aria-hidden="true">▾</span>
    </button>
  </div>
  <div id="filters" hidden></div>
</header>

<div id="map"></div>

<div id="loadingOverlay" role="status" aria-live="polite">
  <div class="loading-card">
    <div class="loading-mark" aria-hidden="true">
      <span class="loading-dot"></span>
      <span class="loading-dot"></span>
      <span class="loading-dot"></span>
      <span class="loading-dot"></span>
    </div>

    <h2 id="loadingTitle">Waiting for trains</h2>
    <p id="loadingMessage">
      Retrieving vehicle locations…
    </p>
    <button id="retryLoad" type="button">Try again</button>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<script>
const statusEl = document.getElementById("status");
const loadingOverlay = document.getElementById("loadingOverlay");
const loadingTitle = document.getElementById("loadingTitle");
const loadingMessage = document.getElementById("loadingMessage");
const retryLoad = document.getElementById("retryLoad");
let hasLoadedOnce = false;

retryLoad.addEventListener("click", refresh);
const filterToggle = document.getElementById("filterToggle");
const filterPanel = document.getElementById("filters");

let filterCloseTimer;

function setFilterPanelOpen(open) {
  clearTimeout(filterCloseTimer);

  if (open) {
    filterPanel.hidden = false;
    filterToggle.setAttribute("aria-expanded", "true");

    requestAnimationFrame(() => {
      if (!filterPanel.hidden) {
        filterPanel.classList.add("is-open");
      }
    });
  } else {
    filterPanel.classList.remove("is-open");
    filterToggle.setAttribute("aria-expanded", "false");

    filterCloseTimer = setTimeout(() => {
      filterPanel.hidden = true;
    }, 180);
  }
}

filterToggle.addEventListener("click", () => {
  setFilterPanelOpen(filterPanel.hidden);
});

document.addEventListener("click", event => {
  if (!event.target.closest("#filters, #filterToggle")) {
    setFilterPanelOpen(false);
  }
});

document.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    setFilterPanelOpen(false);
  }
});

window.addEventListener("error", event => {
  statusEl.textContent = `Page script error: ${event.message}`;
});

window.addEventListener("unhandledrejection", event => {
  statusEl.textContent = `Page error: ${event.reason}`;
});

const map = L.map("map").setView([41.8781, -87.6298], 11);
const canvasRenderer = L.canvas({ padding: 0.5 });

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors"
}).addTo(map);

const markers = L.layerGroup().addTo(map);

const routeShapesLayer = L.geoJSON(null, {
  style: feature => ({
    color: feature.properties.color || "#555",
    weight: feature.properties.vehicle_type === "bus" ? 3 : 5,
    opacity: 0.65
  }),
  onEachFeature: (feature, layer) => {
    layer.bindPopup(
      `${feature.properties.route_name} scheduled route`
    );
  }
}).addTo(map);

const routeStopsLayer = L.geoJSON(null, {
  pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
    radius: feature.properties.vehicle_type === "bus" ? 4 : 6,
    color: "#fff",
    weight: 1,
    fillColor: feature.properties.color || "#555",
    fillOpacity: 0.95
  }),

  onEachFeature: (feature, layer) => {
    const p = feature.properties;
    const isBusStop = p.vehicle_type === "bus" && p.bus_stop_id;
    const isTrainStation = p.vehicle_type === "train" && p.station_id;

    // Keep a simple popup if this feature has no tracker ID.
    if (!isBusStop && !isTrainStation) {
      const popup = document.createElement("div");
      const kind = p.vehicle_type === "bus" ? "bus stop" : "station";
      popup.textContent = `${p.stop_name} — ${p.route_name} ${kind}`;
      layer.bindPopup(popup);
      return;
    }

    layer.bindPopup("Loading arrivals…", { maxWidth: 420 });

    layer.on("popupopen", async () => {
      const content = document.createElement("div");
      const heading = document.createElement("strong");
      heading.textContent = p.stop_name || "Stop";
      content.appendChild(heading);

      const message = document.createElement("p");
      message.textContent = "Loading arrivals…";
      content.appendChild(message);
      layer.setPopupContent(content);

      try {
        let url;

        if (isBusStop) {
          const params = new URLSearchParams({
            stop_id: String(p.bus_stop_id),
            route: String(p.route_code)
          });
          url = `/api/bus-stop-arrivals?${params}`;
        } else {
          const params = new URLSearchParams({
            station_id: String(p.station_id)
          });
          url = `/api/station-arrivals?${params}`;
        }

        const response = await fetch(url);
        const data = await response.json();

        if (!response.ok) {
          throw new Error(data.error || `Server returned ${response.status}`);
        }

        const rows = isBusStop
          ? (data.predictions || []).map(item => ({
              line: `${item.line || p.route_code}`,
              color: p.color,
              direction: item.direction,
              destination: item.destination,
              time: item.estimated_time,
              status: item.status
            }))
          : (data.arrivals || []).map(item => ({
              line: item.line,
              color: item.color,
              direction: item.direction,
              destination: item.destination,
              platform: item.platform,
              time: item.estimated_time,
              status: item.status
            }));

        content.replaceChildren(heading);

        if (rows.length === 0) {
          const empty = document.createElement("p");
          empty.textContent = "No arrivals are currently available.";
          content.appendChild(empty);
          layer.setPopupContent(content);
          return;
        }

        const table = document.createElement("table");
        table.style.borderCollapse = "collapse";
        table.style.marginTop = "8px";
        table.style.width = "100%";

        const header = table.insertRow();
        for (const title of ["Line", "Direction", "Time", "Status"]) {
          const cell = document.createElement("th");
          cell.textContent = title;
          cell.style.textAlign = "left";
          cell.style.padding = "3px 6px 3px 0";
          header.appendChild(cell);
        }

        for (const arrival of rows) {
          const row = table.insertRow();

          const lineCell = row.insertCell();
          lineCell.textContent = arrival.line || "—";
          lineCell.style.color = arrival.color || "#222";
          lineCell.style.fontWeight = "bold";

          const directionCell = row.insertCell();
          const direction = [
            arrival.destination,
            arrival.platform
          ].filter(Boolean);
          directionCell.textContent = direction.join(" · ") || "—";

          const timeCell = row.insertCell();
          timeCell.textContent = formatArrivalTime(arrival.time);
          timeCell.style.whiteSpace = "nowrap";

          const statusCell = row.insertCell();
          statusCell.textContent = arrival.status || "—";
          statusCell.style.whiteSpace = "nowrap";
        }

        content.appendChild(table);
        layer.setPopupContent(content);
        } catch (error) {
        const errorText = String(error.message || error);

        message.textContent =
            isBusStop && /no service scheduled/i.test(errorText)
            ? "No service scheduled"
            : `Could not load arrivals: ${errorText}`;

        layer.setPopupContent(content);
        }
    });
  }
}).addTo(map);

let showRoutePaths = false;
let shapeRequestId = 0;
let showStops = false;
let stopRequestId = 0;

let currentData = null;

// null means all routes/lines are selected.
const selected = {
  bus: null,
  train: null
};

let routeDataWarmup = null;

function warmRouteData() {
  if (!routeDataWarmup) {
    routeDataWarmup = fetch("/api/route-shapes?bus=&train=")
      .then(async response => {
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.error || `Server returned ${response.status}`);
        }
        return response.json();
      })
      .catch(error => {
        console.warn("Route data warmup failed:", error);
        routeDataWarmup = null; // allow another attempt later
      });
  }

  return routeDataWarmup;
}

function getOptions(data, type) {
  if (type === "bus") {
    return (data.bus_routes || []).map(route => ({
      key: String(route.code),
      label: `${route.code} ${route.name}`.trim()
    }));
  }

  return (data.train_lines || []).map(line => ({
    key: line.code,
    label: line.name
  }));
}

function renderFilters(data) {
  const container = document.getElementById("filters");
  container.replaceChildren();

  const shapeStatus = document.createElement("div");
  shapeStatus.id = "routeShapeStatus";
  shapeStatus.className = "filter-option";
  shapeStatus.textContent = showRoutePaths
    ? "Route paths enabled"
    : "Route paths are off";
  container.appendChild(shapeStatus);

  const pathLabel = document.createElement("label");
  pathLabel.className = "filter-option";

  const pathCheckbox = document.createElement("input");
  pathCheckbox.type = "checkbox";
  pathCheckbox.checked = showRoutePaths;

  pathCheckbox.addEventListener("change", () => {
    showRoutePaths = pathCheckbox.checked;
    refreshRouteShapes();
  });

  pathLabel.append(
    pathCheckbox,
    document.createTextNode(" Show selected route paths")
  );
  container.appendChild(pathLabel);

const stopStatus = document.createElement("div");
stopStatus.id = "stopStatus";
stopStatus.className = "filter-option";
stopStatus.textContent = showStops
  ? "Stops and stations enabled"
  : "Stops and stations are off";
container.appendChild(stopStatus);

const stopsLabel = document.createElement("label");
stopsLabel.className = "filter-option";

const stopsCheckbox = document.createElement("input");
stopsCheckbox.type = "checkbox";
stopsCheckbox.checked = showStops;

stopsCheckbox.addEventListener("change", () => {
  showStops = stopsCheckbox.checked;
  refreshRouteStops();
});

stopsLabel.append(
  stopsCheckbox,
  document.createTextNode(" Show stops and stations")
);
container.appendChild(stopsLabel);

  for (const [type, title] of [
    ["bus", "Bus routes"],
    ["train", "Train lines"]
  ]) {
    const options = getOptions(data, type);
    const keys = options.map(option => option.key);

    // Remove selections that are no longer available.
    if (selected[type] !== null) {
      selected[type] = new Set(
        [...selected[type]].filter(key => keys.includes(key))
      );

      if (selected[type].size === keys.length) {
        selected[type] = null;
      }
    }

    const group = document.createElement("div");
    group.className = "filter-group";

    const heading = document.createElement("strong");
    heading.textContent = title;
    group.appendChild(heading);

    const allLabel = document.createElement("label");
    allLabel.className = "filter-option";

    const allCheckbox = document.createElement("input");
    allCheckbox.type = "checkbox";
    allCheckbox.checked =
      selected[type] === null ||
      selected[type].size === keys.length;

    allCheckbox.addEventListener("change", () => {
      selected[type] = allCheckbox.checked ? null : new Set();
      renderFilters(currentData);
      renderVehicles();
      refreshRouteShapes();
      refreshRouteStops();
    });

    allLabel.append(
      allCheckbox,
      document.createTextNode(" All")
    );
    group.appendChild(allLabel);

    for (const option of options) {
      const label = document.createElement("label");
      label.className = "filter-option";

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked =
        selected[type] === null ||
        selected[type].has(option.key);

      checkbox.addEventListener("change", () => {
        if (selected[type] === null) {
          selected[type] = new Set(keys);
        }

        if (checkbox.checked) {
          selected[type].add(option.key);
        } else {
          selected[type].delete(option.key);
        }

        if (selected[type].size === keys.length) {
          selected[type] = null;
        }

        renderFilters(currentData);
        renderVehicles();
        refreshRouteShapes();
        refreshRouteStops();
      });

      label.append(
        checkbox,
        document.createTextNode(` ${option.label}`)
      );
      group.appendChild(label);
    }

    container.appendChild(group);
  }
}

const vehicleMarkers = new Map();
const MAX_PREDICTION_SECONDS = 90;
const CORRECTION_MS = 1400;
let vehicleAnimationFrame = null;

function vehicleKey(vehicle) {
  const route = vehicle.route_code || vehicle.route || "";
  return `${vehicle.type}:${route}:${vehicle.id}`;
}

function bearingBetween(a, b) {
  const toRad = degrees => degrees * Math.PI / 180;
  const toDeg = radians => radians * 180 / Math.PI;

  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const dLon = toRad(b.lon - a.lon);

  const y = Math.sin(dLon) * Math.cos(lat2);
  const x =
    Math.cos(lat1) * Math.sin(lat2) -
    Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);

  return (toDeg(Math.atan2(y, x)) + 360) % 360;
}

function distanceMeters(a, b) {
  const toRad = degrees => degrees * Math.PI / 180;
  const earthRadius = 6371000;

  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const dLat = lat2 - lat1;
  const dLon = toRad(b.lon - a.lon);

  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;

  return 2 * earthRadius * Math.asin(Math.sqrt(h));
}

function pointAhead(lat, lon, bearing, distance) {
  const toRad = degrees => degrees * Math.PI / 180;
  const toDeg = radians => radians * 180 / Math.PI;
  const earthRadius = 6371000;

  const angularDistance = distance / earthRadius;
  const bearingRad = toRad(bearing);
  const lat1 = toRad(lat);
  const lon1 = toRad(lon);

  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(angularDistance) +
    Math.cos(lat1) * Math.sin(angularDistance) * Math.cos(bearingRad)
  );

  const lon2 = lon1 + Math.atan2(
    Math.sin(bearingRad) * Math.sin(angularDistance) * Math.cos(lat1),
    Math.cos(angularDistance) - Math.sin(lat1) * Math.sin(lat2)
  );

  return [toDeg(lat2), toDeg(lon2)];
}

function maxVehicleSpeed(vehicle) {
  // Conservative initial estimates, used until actual positions
  // provide a speed estimate.
  const mph = vehicle.type === "bus" ? 12 : 20;
  return mph * 0.44704;
}

function animateVehicleMarkers(now) {
  for (const state of vehicleMarkers.values()) {
    const marker = state.marker;

    if (state.correction) {
      const elapsed = now - state.correction.started;
      const progress = Math.min(elapsed / CORRECTION_MS, 1);
      const eased = progress * progress * (3 - 2 * progress);

      const lat =
        state.correction.from.lat +
        (state.correction.to.lat - state.correction.from.lat) * eased;
      const lon =
        state.correction.from.lng +
        (state.correction.to.lng - state.correction.from.lng) * eased;

      marker.setLatLng([lat, lon]);

      if (progress >= 1) {
        state.correction = null;
      }
    } else if (state.type === "train") {
      // Don't extrapolate trains in a straight line: tracks curve.
      // Hold the last API position until the next update arrives.
      marker.setLatLng([state.anchor.lat, state.anchor.lon]);
    } else {
      // Keep the existing estimated movement for buses.
      const elapsedSeconds = Math.min(
        (now - state.anchorTime) / 1000,
        MAX_PREDICTION_SECONDS
      );

      const distance = state.speedMps * elapsedSeconds;
      const [lat, lon] = pointAhead(
        state.anchor.lat,
        state.anchor.lon,
        state.bearing,
        distance
      );

      marker.setLatLng([lat, lon]);
    }
  }

  vehicleAnimationFrame = requestAnimationFrame(animateVehicleMarkers);
}

function renderVehicles() {
  if (!currentData) return;

  const busRouteNames = new Map(
    (currentData.bus_routes || []).map(route => [
      String(route.code),
      String(route.name || "").trim()
    ])
  );

  const fetchedAt = Date.parse(currentData.fetched_at);
  const sampleTime = Number.isFinite(fetchedAt) ? fetchedAt : Date.now();
  const seen = new Set();

  for (const vehicle of currentData.vehicles || []) {
    const key = vehicleKey(vehicle);
    seen.add(key);

    const routeKey = vehicle.type === "bus"
      ? String(vehicle.route)
      : String(vehicle.route_code);

    const selection = selected[vehicle.type];
    const isVisible = selection === null || selection.has(routeKey);

    const isBus = vehicle.type === "bus";
    const routeName = isBus ? busRouteNames.get(routeKey) : "";
    const routeLabel = isBus
      ? `${routeKey} ${routeName || ""}`.trim()
      : vehicle.route;

    const label = isBus ? "Bus" : "Train";
    const details = [
      `<b>${label} ${vehicle.id ?? ""}</b>`,
      `${routeLabel}${vehicle.destination ? " → " + vehicle.destination : ""}`,
      vehicle.next_station ? `Next station: ${vehicle.next_station}` : "",
      vehicle.heading ? `Heading: ${vehicle.heading}°` : "",
      vehicle.updated ? `Position time: ${vehicle.updated}` : ""
    ].filter(Boolean).join("<br>");

    let state = vehicleMarkers.get(key);

    if (!state) {
      let marker;

      if (isBus) {
        const routeNumber = document.createElement("span");
        routeNumber.className = "bus-route-number";
        routeNumber.textContent = routeKey;

        marker = L.marker([vehicle.lat, vehicle.lon], {
          icon: L.divIcon({
            className: "bus-route-icon",
            html: routeNumber,
            iconSize: [30, 30],
            iconAnchor: [15, 15]
          })
        });
      } else {
        const trainCircle = document.createElement("span");
        trainCircle.className = "train-route-circle";
        trainCircle.style.backgroundColor = vehicle.color || "#555";

        marker = L.marker([vehicle.lat, vehicle.lon], {
          icon: L.divIcon({
            className: "train-route-icon",
            html: trainCircle,
            iconSize: [30, 30],
            iconAnchor: [15, 15]
          })
        });
      }

      marker.bindPopup(details);

      const heading = Number(vehicle.heading);
      state = {
        marker,
        type: vehicle.type,
        sample: { lat: vehicle.lat, lon: vehicle.lon },
        sampleTime,
        anchor: { lat: vehicle.lat, lon: vehicle.lon },
        anchorTime: performance.now(),
        bearing: Number.isFinite(heading) ? heading : 0,
        speedMps: Number.isFinite(heading) ? maxVehicleSpeed(vehicle) : 0,
        correction: null
      };

      vehicleMarkers.set(key, state);
    } else {
      state.marker.setPopupContent(details);

      // Ignore cached responses: they aren't new position samples.
      if (sampleTime > state.sampleTime) {
        const nextSample = { lat: vehicle.lat, lon: vehicle.lon };
        const elapsedSeconds = (sampleTime - state.sampleTime) / 1000;
        const distance = distanceMeters(state.sample, nextSample);

        if (elapsedSeconds >= 5 && elapsedSeconds <= 240) {
          const maxSpeed = vehicle.type === "bus"
            ? 40 * 0.44704
            : 65 * 0.44704;

          if (distance > 3) {
            const measuredSpeed = distance / elapsedSeconds;

            if (measuredSpeed <= maxSpeed) {
              state.speedMps = measuredSpeed;
              state.bearing = bearingBetween(state.sample, nextSample);
            }
          } else {
            // Repeated coordinates suggest the vehicle may have stopped.
            state.speedMps = 0;
          }
        }

        const currentPosition = state.marker.getLatLng();
        const difference = distanceMeters(
          { lat: currentPosition.lat, lon: currentPosition.lng },
          nextSample
        );

        state.correction = difference > 3
          ? {
              from: currentPosition,
              to: L.latLng(nextSample.lat, nextSample.lon),
              started: performance.now()
            }
          : null;

        state.anchor = nextSample;
        state.anchorTime = performance.now();
        state.sample = nextSample;
        state.sampleTime = sampleTime;

        const apiHeading = Number(vehicle.heading);
        if (
          !state.speedMps &&
          Number.isFinite(apiHeading) &&
          distance <= 3
        ) {
          state.bearing = apiHeading;
        }
      }
    }

    if (isVisible) {
      markers.addLayer(state.marker);
    } else {
      markers.removeLayer(state.marker);
    }
  }

  // Remove markers for vehicles no longer returned by the API.
  for (const [key, state] of vehicleMarkers) {
    if (!seen.has(key)) {
      markers.removeLayer(state.marker);
      vehicleMarkers.delete(key);
    }
  }

  if (vehicleAnimationFrame === null) {
    vehicleAnimationFrame = requestAnimationFrame(animateVehicleMarkers);
  }
}

async function refreshRouteShapes() {
  const requestId = ++shapeRequestId;
  const shapeStatus = document.getElementById("routeShapeStatus");

if (!showRoutePaths || !currentData) {
  routeShapesLayer.clearLayers();

  if (shapeStatus) {
    shapeStatus.classList.remove("is-loading");
    shapeStatus.removeAttribute("aria-busy");
    shapeStatus.textContent = "Route paths are off";
  }

  return;
}


  const busRoutes = currentData.bus_routes || [];
  const trainLines = currentData.train_lines || [];

  const busCodes = selected.bus === null
    ? busRoutes.map(route => String(route.code))
    : [...selected.bus];

  const trainCodes = selected.train === null
    ? trainLines.map(line => String(line.code))
    : [...selected.train];

  const params = new URLSearchParams({
    bus: busCodes.join(","),
    train: trainCodes.join(",")
  });

if (shapeStatus) {
  shapeStatus.textContent = "Loading route paths…";
  shapeStatus.classList.add("is-loading");
  shapeStatus.setAttribute("aria-busy", "true");
}


  try {
    const response = await fetch(`/api/route-shapes?${params}`);
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || `Server returned ${response.status}`);
    }

    if (requestId !== shapeRequestId) return;

    routeShapesLayer.clearLayers();
    routeShapesLayer.addData(data);
    if (shapeStatus) {
    shapeStatus.classList.remove("is-loading");
    shapeStatus.removeAttribute("aria-busy");
    }

    const bounds = routeShapesLayer.getBounds();
    console.log("Route shapes received:", data.features?.length || 0);
    console.log("Bounds valid:", bounds.isValid());

    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [30, 30] });
    }

    const count = data.features?.length || 0;
    if (shapeStatus) {
      shapeStatus.textContent = count
        ? `Showing ${count} scheduled route shape(s)`
        : "No GTFS shapes matched the selected routes";
    }
} catch (error) {
  if (requestId === shapeRequestId && shapeStatus) {
    shapeStatus.classList.remove("is-loading");
    shapeStatus.removeAttribute("aria-busy");
    shapeStatus.textContent =
      `Could not load route paths: ${error.message}`;
  }
  console.error("Could not load route shapes:", error);
}
}

function formatArrivalTime(value) {
  const match = String(value || "").match(/(?:T|\s)(\d{1,2}):(\d{2})/);

  if (!match) return value || "—";

  const hour24 = Number(match[1]);
  const minute = match[2];

  if (hour24 < 0 || hour24 > 23) return value || "—";

  const suffix = hour24 >= 12 ? "PM" : "AM";
  const hour12 = hour24 % 12 || 12;

  return `${hour12}:${minute} ${suffix}`;
}

async function refreshRouteStops() {
  const requestId = ++stopRequestId;
  const stopStatus = document.getElementById("stopStatus");

if (!showStops || !currentData) {
  routeStopsLayer.clearLayers();

  if (stopStatus) {
    stopStatus.classList.remove("is-loading");
    stopStatus.removeAttribute("aria-busy");
    stopStatus.textContent = showStops
      ? "Stops and stations enabled"
      : "Stops and stations are off";
  }

  return;
}


  const busRoutes = currentData.bus_routes || [];
  const trainLines = currentData.train_lines || [];

  const busCodes = selected.bus === null
    ? busRoutes.map(route => String(route.code))
    : [...selected.bus];

  const trainCodes = selected.train === null
    ? trainLines.map(line => String(line.code))
    : [...selected.train];

  const params = new URLSearchParams({
    bus: busCodes.join(","),
    train: trainCodes.join(",")
  });

if (stopStatus) {
  stopStatus.textContent = "Loading stops and stations…";
  stopStatus.classList.add("is-loading");
  stopStatus.setAttribute("aria-busy", "true");
}

  try {
    const response = await fetch(`/api/route-stops?${params}`);
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || `Server returned ${response.status}`);
    }

    if (requestId !== stopRequestId) return;

    routeStopsLayer.clearLayers();
    routeStopsLayer.addData(data);

if (stopStatus) {
  stopStatus.classList.remove("is-loading");
  stopStatus.removeAttribute("aria-busy");
}

    if (stopStatus) {
      stopStatus.textContent =
        `Showing ${data.features?.length || 0} stops and stations`;
    }
} catch (error) {
  if (requestId === stopRequestId && stopStatus) {
    stopStatus.classList.remove("is-loading");
    stopStatus.removeAttribute("aria-busy");
    stopStatus.textContent =
      `Could not load stops: ${error.message}`;
  }
  console.error("Could not load CTA stops:", error);
}

}

async function refresh() {
  statusEl.textContent = "Retrieving vehicle locations…";

if (!hasLoadedOnce) {
  loadingOverlay.classList.remove("hidden", "has-error");
  loadingTitle.textContent = "Looking for trains";
  loadingMessage.textContent =
    "Retrieving train locations…";
}
   const routeWarmup = warmRouteData();

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);

  try {
    const response = await fetch("/api/vehicles", {
      signal: controller.signal
    });

    if (!response.ok) {
      throw new Error(`Server returned HTTP ${response.status}`);
    }

    currentData = await response.json();

    statusEl.textContent =
      `${currentData.vehicles.length} vehicles received; drawing map…`;

    renderFilters(currentData);
    await new Promise(requestAnimationFrame);
    renderVehicles();
    
    if (!hasLoadedOnce) {
      loadingMessage.textContent = "Preparing route paths and stops… (May take a while)";
      await routeWarmup;
    }

    const count = currentData.vehicles.length;
    const errors = Object.entries(currentData.errors || {})
      .map(([name, message]) => `${name}: ${message}`)
      .join(" · ");

    statusEl.textContent =
      `${count} vehicles available · Updated ${
        new Date(currentData.fetched_at).toLocaleTimeString()
      }`
      + (errors ? ` · API issue: ${errors}` : "");
      hasLoadedOnce = true;
      loadingOverlay.classList.add("hidden");
  } catch (error) {
    const errorMessage = error.name === "AbortError"
  ? "The vehicle API took over 30 seconds to respond."
  : `Could not load vehicle locations: ${error.message || error}`;

statusEl.textContent = errorMessage;

if (!hasLoadedOnce) {
  loadingOverlay.classList.add("has-error");
  loadingTitle.textContent = "Couldn't load the map";
  loadingMessage.textContent = errorMessage;
}

  } finally {
    clearTimeout(timeout);
  }
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=3000, debug=False)