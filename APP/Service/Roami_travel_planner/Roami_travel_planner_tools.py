import os
import json
import asyncio
import logging
import httpx
from urllib.parse import quote_plus
from langchain_community.tools import DuckDuckGoSearchRun
from langchain.tools import tool
from APP.config.config import settings
from APP.DB.MongoDB.mongobd import MongoDBSessionManager

logger = logging.getLogger(__name__)

os.environ["GPLACES_API_KEY"] = settings.GOOGLE_API_KEY

db = MongoDBSessionManager()

# ─────────────────────────────────────────────
# PLACE SEARCH HELPERS
# ─────────────────────────────────────────────

# Any result whose types contain one of these is city-level or broader.
# We reject these and retry with a more specific query.
TOO_BROAD_TYPES = {
    "country",
    "administrative_area_level_1",   # Division / State
    "administrative_area_level_2",   # District
    "administrative_area_level_3",   # Upazila / County
    "locality",                      # City — e.g. Dhaka
    "postal_code",
}


def _build_map_link(place_id: str, place_name: str) -> str:
    """
    Construct a Google Maps deep link from a confirmed place_id.
    Uses urllib.parse.quote_plus for correct encoding of all characters
    including non-ASCII (Bengali, Arabic, Japanese etc.), commas, apostrophes.
    Returns empty string if place_id is missing — caller must check before using.
    """
    if not place_id:
        return ""
    encoded_name = quote_plus(place_name or "")
    return (
        f"https://www.google.com/maps/search/"
        f"?api=1&query_place_id={place_id}&query={encoded_name}"
    )


def _geocode_address(address: str) -> tuple[float, float] | tuple[None, None]:
    """Convert address to (lat, lng) using Google Geocoding API."""
    try:
        response = httpx.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": settings.GOOGLE_API_KEY},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "OK" and data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
    except Exception as e:
        logger.error(f"[_geocode_address] Failed for '{address}': {e}")
    return None, None


def _nearby_search(keyword: str, lat: float, lng: float) -> list:
    """
    Google Places Nearby Search — sorted by distance (rankby=distance).
    Returns up to 5 nearest results with lat/lng included.
    """
    try:
        params = {
            "keyword": keyword,
            "location": f"{lat},{lng}",
            "rankby": "distance",
            "key": settings.GOOGLE_API_KEY,
        }
        response = httpx.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            logger.error(f"[_nearby_search] API error: {data.get('status')}")
            return []
        results = []
        for place in data.get("results", [])[:5]:
            place_id = place.get("place_id", "")
            name = place.get("name", "")
            vicinity = place.get("vicinity", "")
            loc = place.get("geometry", {}).get("location", {})
            map_link = _build_map_link(place_id, name)
            results.append({
                "status": "ok",
                "name": name,
                "address": vicinity,
                "place_id": place_id,
                "map_link": map_link,
                "map_link_markdown": f"[{name}]({map_link})" if map_link else None,
                "lat": loc.get("lat"),
                "lng": loc.get("lng"),
            })
        logger.info(f"[_nearby_search] {len(results)} results for '{keyword}' near {lat},{lng}")
        return results
    except Exception as e:
        logger.error(f"[_nearby_search] Error: {e}", exc_info=True)
        return []


def _find_place(query: str, lat: float = None, lng: float = None, radius: int = 5000) -> dict:
    """
    Call Google Places Find Place API.
    Optionally bias results toward a lat/lng coordinate.
    """
    params = {
        "input": query,
        "inputtype": "textquery",
        "fields": "place_id,name,formatted_address,types,geometry",
        "key": settings.GOOGLE_API_KEY,
    }
    if lat and lng:
        params["locationbias"] = f"circle:{radius}@{lat},{lng}"

    response = httpx.get(
        "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
        params=params,
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def _get_place_details(place_id: str) -> dict:
    """Fetch full place details by place_id."""
    params = {
        "place_id": place_id,
        "fields": (
            "place_id,name,formatted_address,"
            "website,url,geometry,types,"
            "address_components,international_phone_number"
        ),
        "key": settings.GOOGLE_API_KEY,
    }
    response = httpx.get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params=params,
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def _is_too_broad(types: list) -> bool:
    """Return True if the result is city-level or broader."""
    return bool(TOO_BROAD_TYPES.intersection(set(types)))


def _extract_neighborhood(address_components: list) -> str:
    """
    Extract the most specific named area from address components.
    Priority: sublocality_level_1 → sublocality → neighborhood → locality
    """
    priority = [
        "sublocality_level_1",
        "sublocality_level_2",
        "sublocality",
        "neighborhood",
        "locality",
    ]
    component_map = {}
    for component in address_components:
        for t in component.get("types", []):
            component_map[t] = component.get("long_name", "")

    for p in priority:
        if p in component_map:
            return component_map[p]
    return ""


def _resolve_single_place(query: str) -> dict:
    """
    Internal helper: run the full place-lookup pipeline for one query.
    Returns a dict with status, name, address, map_link, place_id, lat, lng,
    neighborhood, website, is_specific.
    """
    try:
        data = _find_place(query)

        if data.get("status") != "OK" or not data.get("candidates"):
            return {
                "status": "not_found",
                "query": query,
                "message": (
                    f"No results found for: {query}. "
                    f"Try including neighborhood, city, and country."
                ),
            }

        candidate = data["candidates"][0]
        place_id = candidate["place_id"]
        candidate_types = candidate.get("types", [])

        if _is_too_broad(candidate_types):
            refined_query = f"{query} area neighborhood landmark"
            data = _find_place(refined_query)
            if data.get("status") == "OK" and data.get("candidates"):
                candidate = data["candidates"][0]
                place_id = candidate["place_id"]
                candidate_types = candidate.get("types", [])

        details_data = _get_place_details(place_id)

        if details_data.get("status") != "OK":
            return {
                "status": "error",
                "query": query,
                "message": f"Place details fetch failed: {details_data.get('status')}",
            }

        result = details_data.get("result", {})
        name = result.get("name", "")
        address = result.get("formatted_address", "")
        website = result.get("website", "")
        place_id_confirmed = result.get("place_id", place_id)
        address_components = result.get("address_components", [])
        result_types = result.get("types", [])
        geometry = result.get("geometry", {})
        location = geometry.get("location", {})

        neighborhood = _extract_neighborhood(address_components)
        is_specific = not _is_too_broad(result_types)

        # Always construct the link using _build_map_link so the format is
        # always: ?api=1&query_place_id={place_id}&query={encoded_name}
        # Never use the raw `url` field from the API — it has a different
        # format (?cid=...) that does not match the required structure.
        map_link = _build_map_link(place_id_confirmed, name)

        if not map_link:
            # place_id missing — mark as not specific so the AI tells the user
            # to search manually rather than rendering a broken link.
            is_specific = False
            logger.warning(
                f"[_resolve_single_place] Could not build map link for: {name}"
            )
        else:
            logger.info(f"[_resolve_single_place] map_link={map_link}")

        return {
            "status": "ok",
            "query": query,
            "name": name,
            "neighborhood": neighborhood,
            "address": address,
            "website": website,
            # map_link — the complete verified Google Maps URL.
            # place_id is intentionally excluded so the AI cannot construct
            # a partial URL from it.
            "map_link": map_link,
            # map_link_markdown — the link pre-formatted as a markdown hyperlink.
            # USE THIS directly in the response by copying it character-for-character.
            # Do NOT rewrite, shorten, or reconstruct this string.
            # Example output: [Swan Oyster Depot](https://www.google.com/maps/search/...)
            "map_link_markdown": f"[{name}]({map_link})" if map_link else None,
            "lat": location.get("lat"),
            "lng": location.get("lng"),
            "is_specific": is_specific,
        }

    except Exception as e:
        logger.error(f"[_resolve_single_place] Error for '{query}': {e}", exc_info=True)
        return {"status": "error", "query": query, "message": str(e)}


def _get_distance(origin: str, destination: str) -> dict:
    """
    Internal helper: call Distance Matrix API for one origin→destination pair.
    Returns a dict with status, distance, duration, suggestion, advice.
    """
    is_usa = any(
        indicator in origin.upper()
        for indicator in (" USA", ", USA", " US", ", US", "UNITED STATES")
    )
    units = "imperial" if is_usa else "metric"

    params = {
        "origins": origin,
        "destinations": destination,
        "mode": "driving",
        "units": units,
        "key": settings.GOOGLE_API_KEY,
    }

    try:
        response = httpx.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("status") != "OK":
            return {"status": "error", "message": f"Distance Matrix API error: {data.get('status')}"}

        element = data["rows"][0]["elements"][0]
        element_status = element.get("status")

        if element_status != "OK":
            return {
                "status": "error",
                "message": f"Could not calculate route: {element_status}.",
            }

        distance_text = element["distance"]["text"]
        duration_text = element["duration"]["text"]

        if is_usa:
            distance_value = element["distance"]["value"] / 1609.34
            unit_label = "miles"
        else:
            distance_value = element["distance"]["value"] / 1000.0
            unit_label = "km"

        if distance_value <= 1.0:
            suggestion = "walk"
            advice = (
                f"It's about {distance_text} from where you are — "
                f"an {duration_text} walk."
            )
            duration_text = "10 min by walk"

        elif distance_value <= 5:
            suggestion = "car or rideshare"
            advice = (
                f"It's {distance_text} away — a bit far to walk comfortably. "
                f"A car or rideshare would be the better call."
            )
            duration_text = f"{duration_text} by car or rideshare"
        elif distance_value <= 20:
            suggestion = "car or rideshare"
            advice = (
                f"It's {distance_text} away — a bit far to walk comfortably. "
                f"A car or rideshare would be the better call."
            )
            duration_text = f"{duration_text} by car or rideshare duration may vary depending on traffic"
        else:
            suggestion = "car or rideshare"
            advice = (
                f"It's {distance_text} away — a bit far to walk comfortably. "
                f"A car or rideshare  would be the better call."
            )
            duration_text = f"None"


        return {
            "status": "ok",
            "origin": origin,
            "destination": destination,
            "distance_text": distance_text,
            "duration_text": duration_text,
            f"distance_{unit_label}": f'{round(distance_value, 2)} {unit_label} | {duration_text}',
            "unit": unit_label,
            "suggestion": suggestion,
            "advice": advice,
        }

    except Exception as e:
        logger.error(f"[_get_distance] Error: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────
# TOOLS
# ─────────────────────────────────────────────

@tool
async def get_user_info(user_id: str) -> str:
    """
    ALWAYS call this tool FIRST — before any other tool — on every user request.

    Retrieves stored user profile including:
    - name (for personalized responses)
    - location / city / country (to find nearby places and localize all recommendations)
    - tripExperience
    - soloTravelConfidence
    - placeId (for personalized recommendations)

    Use the returned location as the geographic anchor for all subsequent
    web_search and google_place_search queries.

    Never assume or guess user details. Always fetch first.
    If this call fails, ask the user for their location before proceeding.
    """
    try:
        result = await db.get_user(user_id)
        logger.info(f"[get_user_info] Result: {result}")
        return result
    except Exception as e:
        logger.error(f"[get_user_info] Failed: {e}", exc_info=True)
        return json.dumps({"status": "error", "message": str(e)})


@tool
def google_place_search(query: str) -> str:
    """
    Look up ONE specific place to retrieve its verified name, address,
    map_link (built from place_id), website, and coordinates.

    WHEN TO CALL:
    - You are recommending exactly one place and need its verified details.
    - The user asks "where is", "address of", or "map link for" a single location.

    For 2 or more places in the same response, use get_multiple_places_and_distances
    instead — it is faster and eliminates tool-sequencing errors.

    QUERY FORMAT — always include full location context:
      [place name] [neighborhood] [city] [country]
      "Hatirjheel lake Rampura Dhaka Bangladesh"
      "Fuglen Coffee Tomigaya Shibuya Tokyo Japan"

    AFTER a successful call, use the returned fields as follows:
      • name              → display name
      • address           → full street address
      • map_link_markdown → COPY THIS VERBATIM into your response.
                            It is already formatted as [Name](URL).
                            Do NOT rewrite, reconstruct, or shorten it.
                            Do NOT use map_link to build your own markdown.
      • website           → include only when traveler intends to visit or book
      • is_specific       → if False, do NOT use map_link_markdown;
                            tell the traveler to search directly instead.

    Never construct a map link from memory or training data.
    Never call get_distance_to_place separately after this — if you need distance,
    use get_multiple_places_and_distances which handles both in one call.
    """
    logger.info(f"[google_place_search] Query: {query}")
    result = _resolve_single_place(query)
    logger.info(f"[google_place_search] Result: {result}")
    return json.dumps(result)


@tool
def nearby_place_search(keyword: str, origin: str) -> str:
    """
    *** USE THIS TOOL when the user says "near me", "nearest", or "closest". ***

    Uses Google Places Nearby Search API sorted by real distance (rankby=distance).
    Far more accurate than google_place_search for proximity queries.

    PARAMETERS:
    - keyword: what to search for. E.g. "Pizzaburg", "coffee shop", "Swapno"
    - origin: MUST be "lat,lng|any label"
        Example: "23.7461,90.4152|user location"
        GPS coords before "|" are used. Never omit the coords.

    RETURNS: JSON list of up to 5 nearest places with distance and duration.
    Always copy map_link_markdown verbatim into your response.
    """
    logger.info(f"[nearby_place_search] keyword={keyword} origin={origin}")

    origin_lat, origin_lng = None, None
    address = origin
    if "|" in origin:
        coords_part, address = origin.split("|", 1)
        try:
            origin_lat, origin_lng = map(float, coords_part.split(","))
        except ValueError:
            pass

    if origin_lat is None:
        origin_lat, origin_lng = _geocode_address(address)

    if origin_lat is None:
        return json.dumps({"status": "error", "message": "Could not determine location coordinates."})

    nearby_results = _nearby_search(keyword, origin_lat, origin_lng)

    if not nearby_results:
        return json.dumps({"status": "error", "message": f"No {keyword} found near your location."})

    combined = []
    for place in nearby_results:
        entry = dict(place)
        # Use exact lat/lng coords for accurate distance — never use address string
        place_lat = place.get("lat")
        place_lng = place.get("lng")
        dest = f"{place_lat},{place_lng}" if place_lat and place_lng else place.get("address", "")
        dist = _get_distance(f"{origin_lat},{origin_lng}", dest)
        if dist.get("status") == "ok":
            entry.update({
                "distance_text": dist.get("distance_text"),
                "duration_text": dist.get("duration_text"),
                "suggestion": dist.get("suggestion"),
                "advice": dist.get("advice"),
            })
        combined.append(entry)

    return json.dumps({"status": "ok", "results": combined})


@tool
def get_distance_to_place(origin: str, destination: str) -> str:
    """
    Calculate the walking distance and duration between the traveler's current location
    and a confirmed destination address.

    WHEN TO CALL:
    - You already have the confirmed destination address from a prior
      google_place_search call in this same turn, AND you are only handling
      a single place in this response.
    - For 2 or more places, use get_multiple_places_and_distances instead.

    PARAMETERS:
    - origin: traveler's current location as a string
        Example: "Mirpur 10, Dhaka, Bangladesh"
    - destination: the FULL confirmed address returned by google_place_search —
        never pass a guessed or partial address.
        Example: "Hatirjheel Lake, Rampura, Dhaka 1219, Bangladesh"

    RESPONSE BEHAVIOR:
    - distance <= 1.0 mile/km  → walkable; mention walking time.
    - distance <= 100 miles/km → car or rideshare .
    - Unit is automatic: miles inside USA, km outside USA.

    IMPORTANT: Call google_place_search FIRST for the confirmed address.
    Then call this tool. Never call both simultaneously — this tool requires
    the address that google_place_search returns.
    """
    logger.info(f"[get_distance_to_place] origin={origin} | destination={destination}")
    result = _get_distance(origin, destination)
    logger.info(f"[get_distance_to_place] Result: {result}")
    return json.dumps(result)


@tool
def get_multiple_places_and_distances(origin: str, place_queries: list) -> str:
    """
    *** USE THIS TOOL whenever the response includes 2 or more places. ***

    Performs google_place_search AND get_distance_to_place for every place
    in a single tool call — using Python concurrency internally.

    This eliminates the sequential search → distance loop that causes the AI
    to skip tool calls or hallucinate addresses when handling multi-place responses.

    WHEN TO CALL:
    - User asks for N cafes, restaurants, parks, hotels, or any other category
      where N >= 2.
    - You are building a Step 1 scannable list with 2–10 places.
    - Any multi-location response in Roaming Mode.

    DO NOT use google_place_search + get_distance_to_place separately when
    you need results for more than one place — use this tool instead.

    PARAMETERS:
    - origin: traveler's confirmed current location as a plain string.
        Example: "Mirpur 10, Dhaka, Bangladesh"
    - place_queries: a Python list of query strings, one per place.
        Each query must follow the format: [place name] [neighborhood] [city] [country]
        Example: [
            "Shuruchi Restaurant Dhanmondi Dhaka Bangladesh",
            "Star Kabab Old Dhaka Bangladesh",
            "Panshi Restaurant Mirpur Dhaka Bangladesh"
        ]
        Minimum 2 entries. Maximum 10 entries.

    RETURNS:
    A JSON list. Each item contains:
    - query             → the original query string (for your reference)
    - name              → verified place name
    - address           → full street address
    - neighborhood      → extracted neighborhood name
    - map_link_markdown → COPY THIS VERBATIM into your response.
                          Already formatted as [Name](URL). Do NOT rewrite it.
    - map_link          → raw URL (available if needed; prefer map_link_markdown)
    - website           → website if available
    - is_specific       → False means city-level result; skip map_link_markdown,
                          tell traveler to search directly
    - distance_text     → human-readable distance (e.g. "1.2 km")
    - duration_text     → walking time (e.g. "15 mins")
    - suggestion        → "walk" or "car or rideshare"
    - advice            → one-sentence distance advice to include in your response
    - place_error       → present only if place lookup failed for this entry
    - distance_error    → present only if distance calculation failed for this entry

    USAGE IN RESPONSE:
    - Copy map_link_markdown exactly — never rewrite, reconstruct, or shorten it.
    - Use distance_text and advice exactly as returned for every place.
    - If is_specific is False, skip map_link_markdown and write:
        "Search [place name] [neighborhood] in Google Maps directly."
    - If place_error is set, omit that place and fill the slot with the next result.
    """
    logger.info(
        f"[get_multiple_places_and_distances] origin={origin} | "
        f"queries={place_queries}"
    )

    if not place_queries or len(place_queries) < 1:
        return json.dumps({
            "status": "error",
            "message": "place_queries must contain at least 1 entry.",
        })

    # ── Resolve all places concurrently using threads ─────────────────────
    async def _resolve_all():
        loop = asyncio.get_event_loop()

        place_futures = [
            loop.run_in_executor(None, _resolve_single_place, q)
            for q in place_queries
        ]
        place_results = await asyncio.gather(*place_futures)

        # Build distance destinations from confirmed addresses
        # Use address if lookup succeeded; fall back to query string
        destinations = []
        for pr in place_results:
            if pr.get("status") == "ok" and pr.get("address"):
                destinations.append(pr["address"])
            else:
                destinations.append(None)

        distance_futures = []
        for dest in destinations:
            if dest:
                distance_futures.append(
                    loop.run_in_executor(None, _get_distance, origin, dest)
                )
            else:
                # Placeholder for failed place lookups
                distance_futures.append(asyncio.coroutine(lambda: {"status": "skipped"})())

        distance_results = await asyncio.gather(*distance_futures, return_exceptions=True)

        combined = []
        for i, (pr, dr) in enumerate(zip(place_results, distance_results)):
            entry = {"query": place_queries[i]}

            if pr.get("status") == "ok":
                map_link = pr.get("map_link", "")
                name_val = pr.get("name", "")
                entry.update({
                    "name": name_val,
                    "neighborhood": pr.get("neighborhood"),
                    "address": pr.get("address"),
                    "map_link": map_link,
                    # Pre-formatted markdown hyperlink — copy verbatim into response.
                    # Do NOT reconstruct this from map_link or name separately.
                    "map_link_markdown": f"[{name_val}]({map_link})" if map_link else None,
                    "website": pr.get("website"),
                    "is_specific": pr.get("is_specific"),
                    "lat": pr.get("lat"),
                    "lng": pr.get("lng"),
                })
            else:
                entry["place_error"] = pr.get("message", "Place lookup failed.")

            if isinstance(dr, Exception):
                entry["distance_error"] = str(dr)
            elif isinstance(dr, dict) and dr.get("status") == "ok":
                entry.update({
                    "distance_text": dr.get("distance_text"),
                    "duration_text": dr.get("duration_text"),
                    "suggestion": dr.get("suggestion"),
                    "advice": dr.get("advice"),
                    "unit": dr.get("unit"),
                })
                # Include the numeric distance under its dynamic key
                for k, v in dr.items():
                    if k.startswith("distance_") and k not in ("distance_text",):
                        entry[k] = v
            elif isinstance(dr, dict):
                entry["distance_error"] = dr.get("message", "Distance lookup failed.")

            combined.append(entry)

        return combined

    # ── Run async resolver (handles both sync and async call contexts) ────
    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an already-running event loop (e.g. FastAPI)
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, _resolve_all())
                    combined = future.result()
            else:
                combined = loop.run_until_complete(_resolve_all())
        except RuntimeError:
            combined = asyncio.run(_resolve_all())

        logger.info(f"[get_multiple_places_and_distances] Completed {len(combined)} entries.")
        return json.dumps({"status": "ok", "results": combined})

    except Exception as e:
        logger.error(f"[get_multiple_places_and_distances] Failed: {e}", exc_info=True)
        return json.dumps({"status": "error", "message": str(e)})


@tool
def web_search(query: str) -> str:
    """
    Search the web for real-time or specific travel information.

    WHEN TO CALL:
    - Current opening hours, entry fees, or booking requirements
    - Seasonal conditions, local events, or recent closures
    - Transportation options, routes, or travel advisories
    - Any fact you are not fully confident about

    BEFORE calling this tool:
    - Always call get_user_info first to include the user's city/country in the query.
    - Always append the user's location to the query for locally relevant results.
        Example: instead of "best ramen spots", search "best ramen spots Osaka Japan 2026"

    DO NOT call for: general cultural knowledge or well-established facts you already hold.
    Do not call this tool more than 3 times in a single response.

    After getting results:
    - If specific places are mentioned, follow up with get_multiple_places_and_distances
      to get verified addresses, map links, and distances for all of them in one call.
    """
    logger.info(f"[web_search] Query: {query}")
    search = DuckDuckGoSearchRun()
    try:
        result = search.run(query)
        logger.info(f"[web_search] Success, length={len(result)}")
        return result
    except Exception as e:
        logger.error(f"[web_search] Failed: {e}", exc_info=True)
        return json.dumps({"status": "error", "message": f"Search failed: {str(e)}"})


# ─────────────────────────────────────────────

def get_all_tools():
    """Return list of all available tools in priority order."""
    return [
        get_user_info,                       # Always first
        nearby_place_search,                 # *** PREFERRED for "near me" / "nearest" ***
        web_search,                          # Real-time info
        google_place_search,                 # Single known place lookup
        get_distance_to_place,               # Single place distance
        get_multiple_places_and_distances,   # *** PREFERRED for 2+ known places ***
    ]

# import os
# import json
# import asyncio
# import logging
# import httpx
# from urllib.parse import quote_plus
# from langchain_community.tools import DuckDuckGoSearchRun
# from langchain.tools import tool
# from APP.config.config import settings
# from APP.DB.MongoDB.mongobd import MongoDBSessionManager

# logger = logging.getLogger(__name__)

# os.environ["GPLACES_API_KEY"] = settings.GOOGLE_API_KEY

# db = MongoDBSessionManager()

# # ─────────────────────────────────────────────
# # PLACE SEARCH HELPERS
# # ─────────────────────────────────────────────

# # Any result whose types contain one of these is city-level or broader.
# # We reject these and retry with a more specific query.
# TOO_BROAD_TYPES = {
#     "country",
#     "administrative_area_level_1",   # Division / State
#     "administrative_area_level_2",   # District
#     "administrative_area_level_3",   # Upazila / County
#     "locality",                      # City — e.g. Dhaka
#     "postal_code",
# }


# def _build_map_link(place_id: str, place_name: str) -> str:
#     """
#     Construct a Google Maps deep link from a confirmed place_id.
#     Uses urllib.parse.quote_plus for correct encoding of all characters
#     including non-ASCII (Bengali, Arabic, Japanese etc.), commas, apostrophes.
#     Returns empty string if place_id is missing — caller must check before using.
#     """
#     if not place_id:
#         return ""
#     encoded_name = quote_plus(place_name or "")
#     return (
#         f"https://www.google.com/maps/search/"
#         f"?api=1&query_place_id={place_id}&query={encoded_name}"
#     )


# def _geocode_address(address: str) -> tuple[float, float] | tuple[None, None]:
#     """
#     Convert a plain address string to (lat, lng) using Google Geocoding API.
#     This is MORE accurate than _find_place for home addresses like
#     'House 02 Road No. 10, Dhaka 1219' because Geocoding API is built
#     specifically for address → coordinates resolution.
#     Returns (lat, lng) or (None, None) on failure.
#     """
#     try:
#         response = httpx.get(
#             "https://maps.googleapis.com/maps/api/geocode/json",
#             params={"address": address, "key": settings.GOOGLE_API_KEY},
#             timeout=10,
#         )
#         response.raise_for_status()
#         data = response.json()
#         if data.get("status") == "OK" and data.get("results"):
#             loc = data["results"][0]["geometry"]["location"]
#             logger.info(f"[_geocode_address] {address} → {loc['lat']}, {loc['lng']}")
#             return loc["lat"], loc["lng"]
#     except Exception as e:
#         logger.error(f"[_geocode_address] Failed for '{address}': {e}")
#     return None, None


# def _nearby_search(keyword: str, lat: float, lng: float, radius: int = 3000) -> list:
#     """
#     Use Google Places Nearby Search API to find places near exact GPS coordinates.
#     This is the CORRECT API for 'nearest X near me' — it searches by proximity,
#     not by text relevance. Returns up to 5 nearest results sorted by distance.
#     """
#     try:
#         params = {
#             "keyword": keyword,
#             "location": f"{lat},{lng}",
#             "rankby": "distance",  # sort by nearest first, ignores radius
#             "key": settings.GOOGLE_API_KEY,
#         }
#         response = httpx.get(
#             "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
#             params=params,
#             timeout=10,
#         )
#         response.raise_for_status()
#         data = response.json()

#         if data.get("status") not in ("OK", "ZERO_RESULTS"):
#             logger.error(f"[_nearby_search] API error: {data.get('status')}")
#             return []

#         results = []
#         for place in data.get("results", [])[:5]:
#             place_id = place.get("place_id", "")
#             name = place.get("name", "")
#             vicinity = place.get("vicinity", "")
#             loc = place.get("geometry", {}).get("location", {})
#             map_link = _build_map_link(place_id, name)
#             results.append({
#                 "status": "ok",
#                 "name": name,
#                 "address": vicinity,
#                 "place_id": place_id,
#                 "map_link": map_link,
#                 "map_link_markdown": f"[{name}]({map_link})" if map_link else None,
#                 "lat": loc.get("lat"),
#                 "lng": loc.get("lng"),
#                 "is_specific": True,
#             })
#         logger.info(f"[_nearby_search] Found {len(results)} results for '{keyword}' near {lat},{lng}")
#         return results
#     except Exception as e:
#         logger.error(f"[_nearby_search] Error: {e}", exc_info=True)
#         return []


# def _find_place(query: str, lat: float = None, lng: float = None, radius: int = 5000) -> dict:
#     """
#     Call Google Places Find Place API.
#     Optionally bias results toward a lat/lng coordinate.
#     """
#     params = {
#         "input": query,
#         "inputtype": "textquery",
#         "fields": "place_id,name,formatted_address,types,geometry",
#         "key": settings.GOOGLE_API_KEY,
#     }
#     if lat and lng:
#         params["locationbias"] = f"circle:{radius}@{lat},{lng}"

#     response = httpx.get(
#         "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
#         params=params,
#         timeout=10,
#     )
#     response.raise_for_status()
#     return response.json()


# def _get_place_details(place_id: str) -> dict:
#     """Fetch full place details by place_id."""
#     params = {
#         "place_id": place_id,
#         "fields": (
#             "place_id,name,formatted_address,"
#             "website,url,geometry,types,"
#             "address_components,international_phone_number"
#         ),
#         "key": settings.GOOGLE_API_KEY,
#     }
#     response = httpx.get(
#         "https://maps.googleapis.com/maps/api/place/details/json",
#         params=params,
#         timeout=10,
#     )
#     response.raise_for_status()
#     return response.json()


# def _is_too_broad(types: list) -> bool:
#     """Return True if the result is city-level or broader."""
#     return bool(TOO_BROAD_TYPES.intersection(set(types)))


# def _extract_neighborhood(address_components: list) -> str:
#     """
#     Extract the most specific named area from address components.
#     Priority: sublocality_level_1 → sublocality → neighborhood → locality
#     """
#     priority = [
#         "sublocality_level_1",
#         "sublocality_level_2",
#         "sublocality",
#         "neighborhood",
#         "locality",
#     ]
#     component_map = {}
#     for component in address_components:
#         for t in component.get("types", []):
#             component_map[t] = component.get("long_name", "")

#     for p in priority:
#         if p in component_map:
#             return component_map[p]
#     return ""


# def _resolve_single_place(query: str, lat: float = None, lng: float = None) -> dict:
#     """
#     Internal helper: run the full place-lookup pipeline for one query.
#     Returns a dict with status, name, address, map_link, place_id, lat, lng,
#     neighborhood, website, is_specific.
#     Pass lat/lng to bias results toward the user's current location.
#     """
#     try:
#         data = _find_place(query, lat=lat, lng=lng)

#         if data.get("status") != "OK" or not data.get("candidates"):
#             return {
#                 "status": "not_found",
#                 "query": query,
#                 "message": (
#                     f"No results found for: {query}. "
#                     f"Try including neighborhood, city, and country."
#                 ),
#             }

#         candidate = data["candidates"][0]
#         place_id = candidate["place_id"]
#         candidate_types = candidate.get("types", [])

#         if _is_too_broad(candidate_types):
#             refined_query = f"{query} area neighborhood landmark"
#             data = _find_place(refined_query, lat=lat, lng=lng)
#             if data.get("status") == "OK" and data.get("candidates"):
#                 candidate = data["candidates"][0]
#                 place_id = candidate["place_id"]
#                 candidate_types = candidate.get("types", [])

#         details_data = _get_place_details(place_id)

#         if details_data.get("status") != "OK":
#             return {
#                 "status": "error",
#                 "query": query,
#                 "message": f"Place details fetch failed: {details_data.get('status')}",
#             }

#         result = details_data.get("result", {})
#         name = result.get("name", "")
#         address = result.get("formatted_address", "")
#         website = result.get("website", "")
#         place_id_confirmed = result.get("place_id", place_id)
#         address_components = result.get("address_components", [])
#         result_types = result.get("types", [])
#         geometry = result.get("geometry", {})
#         location = geometry.get("location", {})

#         neighborhood = _extract_neighborhood(address_components)
#         is_specific = not _is_too_broad(result_types)

#         # Always construct the link using _build_map_link so the format is
#         # always: ?api=1&query_place_id={place_id}&query={encoded_name}
#         # Never use the raw `url` field from the API — it has a different
#         # format (?cid=...) that does not match the required structure.
#         map_link = _build_map_link(place_id_confirmed, name)

#         if not map_link:
#             # place_id missing — mark as not specific so the AI tells the user
#             # to search manually rather than rendering a broken link.
#             is_specific = False
#             logger.warning(
#                 f"[_resolve_single_place] Could not build map link for: {name}"
#             )
#         else:
#             logger.info(f"[_resolve_single_place] map_link={map_link}")

#         return {
#             "status": "ok",
#             "query": query,
#             "name": name,
#             "neighborhood": neighborhood,
#             "address": address,
#             "website": website,
#             # map_link — the complete verified Google Maps URL.
#             # place_id is intentionally excluded so the AI cannot construct
#             # a partial URL from it.
#             "map_link": map_link,
#             # map_link_markdown — the link pre-formatted as a markdown hyperlink.
#             # USE THIS directly in the response by copying it character-for-character.
#             # Do NOT rewrite, shorten, or reconstruct this string.
#             # Example output: [Swan Oyster Depot](https://www.google.com/maps/search/...)
#             "map_link_markdown": f"[{name}]({map_link})" if map_link else None,
#             "lat": location.get("lat"),
#             "lng": location.get("lng"),
#             "is_specific": is_specific,
#         }

#     except Exception as e:
#         logger.error(f"[_resolve_single_place] Error for '{query}': {e}", exc_info=True)
#         return {"status": "error", "query": query, "message": str(e)}


# def _get_distance(origin: str, destination: str) -> dict:
#     """
#     Internal helper: call Distance Matrix API for one origin→destination pair.
#     Returns a dict with status, distance, duration, suggestion, advice.
#     """
#     is_usa = any(
#         indicator in origin.upper()
#         for indicator in (" USA", ", USA", " US", ", US", "UNITED STATES")
#     )
#     units = "imperial" if is_usa else "metric"

#     params = {
#         "origins": origin,
#         "destinations": destination,
#         "mode": "driving",
#         "units": units,
#         "key": settings.GOOGLE_API_KEY,
#     }

#     try:
#         response = httpx.get(
#             "https://maps.googleapis.com/maps/api/distancematrix/json",
#             params=params,
#             timeout=10,
#         )
#         response.raise_for_status()
#         data = response.json()

#         if data.get("status") != "OK":
#             return {"status": "error", "message": f"Distance Matrix API error: {data.get('status')}"}

#         element = data["rows"][0]["elements"][0]
#         element_status = element.get("status")

#         if element_status != "OK":
#             return {
#                 "status": "error",
#                 "message": f"Could not calculate route: {element_status}.",
#             }

#         distance_text = element["distance"]["text"]
#         duration_text = element["duration"]["text"]

#         if is_usa:
#             distance_value = element["distance"]["value"] / 1609.34
#             unit_label = "miles"
#         else:
#             distance_value = element["distance"]["value"] / 1000.0
#             unit_label = "km"

#         if distance_value <= 1.0:
#             suggestion = "walk"
#             advice = (
#                 f"It's about {distance_text} from where you are — "
#                 f"an {duration_text} walk."
#             )
#             duration_text = "10 min by walk"

#         elif distance_value <= 5:
#             suggestion = "car or rideshare"
#             advice = (
#                 f"It's {distance_text} away — a bit far to walk comfortably. "
#                 f"A car or rideshare would be the better call."
#             )
#             duration_text = f"{duration_text} by car or rideshare"
#         elif distance_value <= 20:
#             suggestion = "car or rideshare"
#             advice = (
#                 f"It's {distance_text} away — a bit far to walk comfortably. "
#                 f"A car or rideshare would be the better call."
#             )
#             duration_text = f"{duration_text} by car or rideshare duration may vary depending on traffic"
#         else:
#             suggestion = "car or rideshare"
#             advice = (
#                 f"It's {distance_text} away — a bit far to walk comfortably. "
#                 f"A car or rideshare  would be the better call."
#             )
#             duration_text = f"None"


#         return {
#             "status": "ok",
#             "origin": origin,
#             "destination": destination,
#             "distance_text": distance_text,
#             "duration_text": duration_text,
#             f"distance_{unit_label}": f'{round(distance_value, 2)} {unit_label} | {duration_text}',
#             "unit": unit_label,
#             "suggestion": suggestion,
#             "advice": advice,
#         }

#     except Exception as e:
#         logger.error(f"[_get_distance] Error: {e}", exc_info=True)
#         return {"status": "error", "message": str(e)}


# # ─────────────────────────────────────────────
# # TOOLS
# # ─────────────────────────────────────────────

# @tool
# async def get_user_info(user_id: str) -> str:
#     """
#     ALWAYS call this tool FIRST — before any other tool — on every user request.

#     Retrieves stored user profile including:
#     - name (for personalized responses)
#     - location / city / country (to find nearby places and localize all recommendations)
#     - tripExperience
#     - soloTravelConfidence
#     - placeId (for personalized recommendations)

#     Use the returned location as the geographic anchor for all subsequent
#     web_search and google_place_search queries.

#     Never assume or guess user details. Always fetch first.
#     If this call fails, ask the user for their location before proceeding.
#     """
#     try:
#         result = await db.get_user(user_id)
#         logger.info(f"[get_user_info] Result: {result}")
#         return result
#     except Exception as e:
#         logger.error(f"[get_user_info] Failed: {e}", exc_info=True)
#         return json.dumps({"status": "error", "message": str(e)})


# @tool
# def google_place_search(query: str) -> str:
#     """
#     Look up ONE specific place to retrieve its verified name, address,
#     map_link (built from place_id), website, and coordinates.

#     WHEN TO CALL:
#     - You are recommending exactly one place and need its verified details.
#     - The user asks "where is", "address of", or "map link for" a single location.

#     For 2 or more places in the same response, use get_multiple_places_and_distances
#     instead — it is faster and eliminates tool-sequencing errors.

#     QUERY FORMAT — always include full location context:
#       [place name] [neighborhood] [city] [country]
#       "Hatirjheel lake Rampura Dhaka Bangladesh"
#       "Fuglen Coffee Tomigaya Shibuya Tokyo Japan"

#     AFTER a successful call, use the returned fields as follows:
#       • name              → display name
#       • address           → full street address
#       • map_link_markdown → COPY THIS VERBATIM into your response.
#                             It is already formatted as [Name](URL).
#                             Do NOT rewrite, reconstruct, or shorten it.
#                             Do NOT use map_link to build your own markdown.
#       • website           → include only when traveler intends to visit or book
#       • is_specific       → if False, do NOT use map_link_markdown;
#                             tell the traveler to search directly instead.

#     Never construct a map link from memory or training data.
#     Never call get_distance_to_place separately after this — if you need distance,
#     use get_multiple_places_and_distances which handles both in one call.
#     """
#     logger.info(f"[google_place_search] Query: {query}")
#     result = _resolve_single_place(query)
#     logger.info(f"[google_place_search] Result: {result}")
#     return json.dumps(result)


# @tool
# def nearby_place_search(keyword: str, origin: str) -> str:
#     """
#     *** USE THIS TOOL when the user says "near me", "nearest", or "closest". ***

#     Finds places near the user's EXACT GPS coordinates using Google Places
#     Nearby Search API — sorted by real distance, not text relevance.
#     This is more accurate than google_place_search for proximity queries.

#     PARAMETERS:
#     - keyword: what to search for. E.g. "Pizzaburg", "coffee shop", "Swapno"
#     - origin: MUST be in format "lat,lng|address"
#         Example: "23.7461,90.4152|House 02 Road 10 Dhaka"
#         The GPS coordinates before "|" are used for the search.
#         If no GPS available, pass just the address string.

#     RETURNS:
#     A JSON list of up to 5 nearest places, each with:
#     - name, address, map_link_markdown, distance_text, duration_text, advice

#     Always use map_link_markdown verbatim in your response.
#     """
#     logger.info(f"[nearby_place_search] keyword={keyword} origin={origin}")

#     # Extract GPS from origin
#     origin_lat, origin_lng = None, None
#     address = origin
#     if "|" in origin:
#         coords_part, address = origin.split("|", 1)
#         try:
#             origin_lat, origin_lng = map(float, coords_part.split(","))
#         except ValueError:
#             pass

#     if origin_lat is None:
#         origin_lat, origin_lng = _geocode_address(address)

#     if origin_lat is None:
#         return json.dumps({"status": "error", "message": "Could not determine location coordinates."})

#     nearby_results = _nearby_search(keyword, origin_lat, origin_lng)

#     if not nearby_results:
#         return json.dumps({"status": "error", "message": f"No {keyword} found near your location."})

#     # Add distances for each result
#     combined = []
#     for place in nearby_results:
#         entry = dict(place)
#         dest = place.get("address") or f"{place.get('lat')},{place.get('lng')}"
#         dist = _get_distance(f"{origin_lat},{origin_lng}", dest)
#         if dist.get("status") == "ok":
#             entry.update({
#                 "distance_text": dist.get("distance_text"),
#                 "duration_text": dist.get("duration_text"),
#                 "suggestion": dist.get("suggestion"),
#                 "advice": dist.get("advice"),
#             })
#         combined.append(entry)

#     return json.dumps({"status": "ok", "results": combined})


# @tool
# def get_distance_to_place(origin: str, destination: str) -> str:
#     """
#     Calculate the walking distance and duration between the traveler's current location
#     and a confirmed destination address.

#     WHEN TO CALL:
#     - You already have the confirmed destination address from a prior
#       google_place_search call in this same turn, AND you are only handling
#       a single place in this response.
#     - For 2 or more places, use get_multiple_places_and_distances instead.

#     PARAMETERS:
#     - origin: traveler's current location as a string
#         Example: "Mirpur 10, Dhaka, Bangladesh"
#     - destination: the FULL confirmed address returned by google_place_search —
#         never pass a guessed or partial address.
#         Example: "Hatirjheel Lake, Rampura, Dhaka 1219, Bangladesh"

#     RESPONSE BEHAVIOR:
#     - distance <= 1.0 mile/km  → walkable; mention walking time.
#     - distance <= 100 miles/km → car or rideshare .
#     - Unit is automatic: miles inside USA, km outside USA.

#     IMPORTANT: Call google_place_search FIRST for the confirmed address.
#     Then call this tool. Never call both simultaneously — this tool requires
#     the address that google_place_search returns.
#     """
#     logger.info(f"[get_distance_to_place] origin={origin} | destination={destination}")
#     result = _get_distance(origin, destination)
#     logger.info(f"[get_distance_to_place] Result: {result}")
#     return json.dumps(result)


# @tool
# def get_multiple_places_and_distances(origin: str, place_queries: list) -> str:
#     """
#     *** USE THIS TOOL whenever the response includes 2 or more places. ***

#     Performs google_place_search AND get_distance_to_place for every place
#     in a single tool call — using Python concurrency internally.

#     This eliminates the sequential search → distance loop that causes the AI
#     to skip tool calls or hallucinate addresses when handling multi-place responses.

#     WHEN TO CALL:
#     - User asks for N cafes, restaurants, parks, hotels, or any other category
#       where N >= 2.
#     - You are building a Step 1 scannable list with 2–10 places.
#     - Any multi-location response in Roaming Mode.

#     DO NOT use google_place_search + get_distance_to_place separately when
#     you need results for more than one place — use this tool instead.

#     PARAMETERS:
#     - origin: traveler's confirmed current location as a plain string.
#         Example: "Mirpur 10, Dhaka, Bangladesh"
#     - place_queries: a Python list of query strings, one per place.
#         Each query must follow the format: [place name] [neighborhood] [city] [country]
#         Example: [
#             "Shuruchi Restaurant Dhanmondi Dhaka Bangladesh",
#             "Star Kabab Old Dhaka Bangladesh",
#             "Panshi Restaurant Mirpur Dhaka Bangladesh"
#         ]
#         Minimum 2 entries. Maximum 10 entries.

#     RETURNS:
#     A JSON list. Each item contains:
#     - query             → the original query string (for your reference)
#     - name              → verified place name
#     - address           → full street address
#     - neighborhood      → extracted neighborhood name
#     - map_link_markdown → COPY THIS VERBATIM into your response.
#                           Already formatted as [Name](URL). Do NOT rewrite it.
#     - map_link          → raw URL (available if needed; prefer map_link_markdown)
#     - website           → website if available
#     - is_specific       → False means city-level result; skip map_link_markdown,
#                           tell traveler to search directly
#     - distance_text     → human-readable distance (e.g. "1.2 km")
#     - duration_text     → walking time (e.g. "15 mins")
#     - suggestion        → "walk" or "car or rideshare"
#     - advice            → one-sentence distance advice to include in your response
#     - place_error       → present only if place lookup failed for this entry
#     - distance_error    → present only if distance calculation failed for this entry

#     USAGE IN RESPONSE:
#     - Copy map_link_markdown exactly — never rewrite, reconstruct, or shorten it.
#     - Use distance_text and advice exactly as returned for every place.
#     - If is_specific is False, skip map_link_markdown and write:
#         "Search [place name] [neighborhood] in Google Maps directly."
#     - If place_error is set, omit that place and fill the slot with the next result.
#     """
#     logger.info(
#         f"[get_multiple_places_and_distances] origin={origin} | "
#         f"queries={place_queries}"
#     )

#     if not place_queries or len(place_queries) < 1:
#         return json.dumps({
#             "status": "error",
#             "message": "place_queries must contain at least 1 entry.",
#         })

#     # ── Resolve all places concurrently using threads ─────────────────────
#     async def _resolve_all():
#         loop = asyncio.get_event_loop()

#         # Parse GPS coords injected into origin string as "lat,lng|address"
#         # If frontend sent real GPS, origin will be "23.7461,90.4152|House 02..."
#         # Otherwise fall back to geocoding the address string
#         origin_lat, origin_lng = None, None
#         if "|" in origin:
#             coords_part = origin.split("|")[0]
#             try:
#                 origin_lat, origin_lng = map(float, coords_part.split(","))
#                 logger.info(f"[get_multiple_places_and_distances] Using GPS: {origin_lat},{origin_lng}")
#             except ValueError:
#                 pass

#         if origin_lat is None:
#             origin_lat, origin_lng = _geocode_address(origin)
#             logger.info(f"[get_multiple_places_and_distances] Geocoded origin: {origin_lat},{origin_lng}")

#         place_futures = [
#             loop.run_in_executor(None, _resolve_single_place, q, origin_lat, origin_lng)
#             for q in place_queries
#         ]
#         place_results = await asyncio.gather(*place_futures)

#         # Build distance destinations from confirmed addresses
#         # Use address if lookup succeeded; fall back to query string
#         destinations = []
#         for pr in place_results:
#             if pr.get("status") == "ok" and pr.get("address"):
#                 destinations.append(pr["address"])
#             else:
#                 destinations.append(None)

#         distance_futures = []
#         for dest in destinations:
#             if dest:
#                 distance_futures.append(
#                     loop.run_in_executor(None, _get_distance, origin, dest)
#                 )
#             else:
#                 # Placeholder for failed place lookups
#                 distance_futures.append(asyncio.coroutine(lambda: {"status": "skipped"})())

#         distance_results = await asyncio.gather(*distance_futures, return_exceptions=True)

#         combined = []
#         for i, (pr, dr) in enumerate(zip(place_results, distance_results)):
#             entry = {"query": place_queries[i]}

#             if pr.get("status") == "ok":
#                 map_link = pr.get("map_link", "")
#                 name_val = pr.get("name", "")
#                 entry.update({
#                     "name": name_val,
#                     "neighborhood": pr.get("neighborhood"),
#                     "address": pr.get("address"),
#                     "map_link": map_link,
#                     # Pre-formatted markdown hyperlink — copy verbatim into response.
#                     # Do NOT reconstruct this from map_link or name separately.
#                     "map_link_markdown": f"[{name_val}]({map_link})" if map_link else None,
#                     "website": pr.get("website"),
#                     "is_specific": pr.get("is_specific"),
#                     "lat": pr.get("lat"),
#                     "lng": pr.get("lng"),
#                 })
#             else:
#                 entry["place_error"] = pr.get("message", "Place lookup failed.")

#             if isinstance(dr, Exception):
#                 entry["distance_error"] = str(dr)
#             elif isinstance(dr, dict) and dr.get("status") == "ok":
#                 entry.update({
#                     "distance_text": dr.get("distance_text"),
#                     "duration_text": dr.get("duration_text"),
#                     "suggestion": dr.get("suggestion"),
#                     "advice": dr.get("advice"),
#                     "unit": dr.get("unit"),
#                 })
#                 # Include the numeric distance under its dynamic key
#                 for k, v in dr.items():
#                     if k.startswith("distance_") and k not in ("distance_text",):
#                         entry[k] = v
#             elif isinstance(dr, dict):
#                 entry["distance_error"] = dr.get("message", "Distance lookup failed.")

#             combined.append(entry)

#         return combined

#     # ── Run async resolver (handles both sync and async call contexts) ────
#     try:
#         try:
#             loop = asyncio.get_event_loop()
#             if loop.is_running():
#                 # We're inside an already-running event loop (e.g. FastAPI)
#                 import concurrent.futures
#                 with concurrent.futures.ThreadPoolExecutor() as pool:
#                     future = pool.submit(asyncio.run, _resolve_all())
#                     combined = future.result()
#             else:
#                 combined = loop.run_until_complete(_resolve_all())
#         except RuntimeError:
#             combined = asyncio.run(_resolve_all())

#         logger.info(f"[get_multiple_places_and_distances] Completed {len(combined)} entries.")
#         return json.dumps({"status": "ok", "results": combined})

#     except Exception as e:
#         logger.error(f"[get_multiple_places_and_distances] Failed: {e}", exc_info=True)
#         return json.dumps({"status": "error", "message": str(e)})


# @tool
# def web_search(query: str) -> str:
#     """
#     Search the web for real-time or specific travel information.

#     WHEN TO CALL:
#     - Current opening hours, entry fees, or booking requirements
#     - Seasonal conditions, local events, or recent closures
#     - Transportation options, routes, or travel advisories
#     - Any fact you are not fully confident about

#     BEFORE calling this tool:
#     - Always call get_user_info first to include the user's city/country in the query.
#     - Always append the user's location to the query for locally relevant results.
#         Example: instead of "best ramen spots", search "best ramen spots Osaka Japan 2026"

#     DO NOT call for: general cultural knowledge or well-established facts you already hold.
#     Do not call this tool more than 3 times in a single response.

#     After getting results:
#     - If specific places are mentioned, follow up with get_multiple_places_and_distances
#       to get verified addresses, map links, and distances for all of them in one call.
#     """
#     logger.info(f"[web_search] Query: {query}")
#     search = DuckDuckGoSearchRun()
#     try:
#         result = search.run(query)
#         logger.info(f"[web_search] Success, length={len(result)}")
#         return result
#     except Exception as e:
#         logger.error(f"[web_search] Failed: {e}", exc_info=True)
#         return json.dumps({"status": "error", "message": f"Search failed: {str(e)}"})


# # ─────────────────────────────────────────────

# def get_all_tools():
#     """Return list of all available tools in priority order."""
#     return [
#         get_user_info,                       # Always first — anchors all location context
#         nearby_place_search,                 # *** PREFERRED for "near me" / "nearest" queries ***
#         web_search,                          # Real-time info
#         google_place_search,                 # Single known place lookup
#         get_distance_to_place,               # Single place distance (after google_place_search)
#         get_multiple_places_and_distances,   # *** PREFERRED for 2+ known places ***
#     ]

# --------------------------------------------------------
# import os
# import json
# import asyncio
# import logging
# import httpx
# from urllib.parse import quote_plus
# from langchain_community.tools import DuckDuckGoSearchRun
# from langchain.tools import tool
# from APP.config.config import settings
# from APP.DB.MongoDB.mongobd import MongoDBSessionManager

# logger = logging.getLogger(__name__)

# os.environ["GPLACES_API_KEY"] = settings.GOOGLE_API_KEY

# db = MongoDBSessionManager()

# # ─────────────────────────────────────────────
# # PLACE SEARCH HELPERS
# # ─────────────────────────────────────────────

# # Any result whose types contain one of these is city-level or broader.
# # We reject these and retry with a more specific query.
# TOO_BROAD_TYPES = {
#     "country",
#     "administrative_area_level_1",   # Division / State
#     "administrative_area_level_2",   # District
#     "administrative_area_level_3",   # Upazila / County
#     "locality",                      # City — e.g. Dhaka
#     "postal_code",
# }


# def _build_map_link(place_id: str, place_name: str) -> str:
#     """
#     Construct a Google Maps deep link from a confirmed place_id.
#     Uses urllib.parse.quote_plus for correct encoding of all characters
#     including non-ASCII (Bengali, Arabic, Japanese etc.), commas, apostrophes.
#     Returns empty string if place_id is missing — caller must check before using.
#     """
#     if not place_id:
#         return ""
#     encoded_name = quote_plus(place_name or "")
#     return (
#         f"https://www.google.com/maps/search/"
#         f"?api=1&query_place_id={place_id}&query={encoded_name}"
#     )


# def _find_place(query: str, lat: float = None, lng: float = None, radius: int = 5000) -> dict:
#     """
#     Call Google Places Find Place API.
#     Optionally bias results toward a lat/lng coordinate.
#     """
#     params = {
#         "input": query,
#         "inputtype": "textquery",
#         "fields": "place_id,name,formatted_address,types,geometry",
#         "key": settings.GOOGLE_API_KEY,
#     }
#     if lat and lng:
#         params["locationbias"] = f"circle:{radius}@{lat},{lng}"

#     response = httpx.get(
#         "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
#         params=params,
#         timeout=10,
#     )
#     response.raise_for_status()
#     return response.json()


# def _get_place_details(place_id: str) -> dict:
#     """Fetch full place details by place_id."""
#     params = {
#         "place_id": place_id,
#         "fields": (
#             "place_id,name,formatted_address,"
#             "website,url,geometry,types,"
#             "address_components,international_phone_number"
#         ),
#         "key": settings.GOOGLE_API_KEY,
#     }
#     response = httpx.get(
#         "https://maps.googleapis.com/maps/api/place/details/json",
#         params=params,
#         timeout=10,
#     )
#     response.raise_for_status()
#     return response.json()


# def _is_too_broad(types: list) -> bool:
#     """Return True if the result is city-level or broader."""
#     return bool(TOO_BROAD_TYPES.intersection(set(types)))


# def _extract_neighborhood(address_components: list) -> str:
#     """
#     Extract the most specific named area from address components.
#     Priority: sublocality_level_1 → sublocality → neighborhood → locality
#     """
#     priority = [
#         "sublocality_level_1",
#         "sublocality_level_2",
#         "sublocality",
#         "neighborhood",
#         "locality",
#     ]
#     component_map = {}
#     for component in address_components:
#         for t in component.get("types", []):
#             component_map[t] = component.get("long_name", "")

#     for p in priority:
#         if p in component_map:
#             return component_map[p]
#     return ""


# def _resolve_single_place(query: str) -> dict:
#     """
#     Internal helper: run the full place-lookup pipeline for one query.
#     Returns a dict with status, name, address, map_link, place_id, lat, lng,
#     neighborhood, website, is_specific.
#     """
#     try:
#         data = _find_place(query)

#         if data.get("status") != "OK" or not data.get("candidates"):
#             return {
#                 "status": "not_found",
#                 "query": query,
#                 "message": (
#                     f"No results found for: {query}. "
#                     f"Try including neighborhood, city, and country."
#                 ),
#             }

#         candidate = data["candidates"][0]
#         place_id = candidate["place_id"]
#         candidate_types = candidate.get("types", [])

#         if _is_too_broad(candidate_types):
#             refined_query = f"{query} area neighborhood landmark"
#             data = _find_place(refined_query)
#             if data.get("status") == "OK" and data.get("candidates"):
#                 candidate = data["candidates"][0]
#                 place_id = candidate["place_id"]
#                 candidate_types = candidate.get("types", [])

#         details_data = _get_place_details(place_id)

#         if details_data.get("status") != "OK":
#             return {
#                 "status": "error",
#                 "query": query,
#                 "message": f"Place details fetch failed: {details_data.get('status')}",
#             }

#         result = details_data.get("result", {})
#         name = result.get("name", "")
#         address = result.get("formatted_address", "")
#         website = result.get("website", "")
#         place_id_confirmed = result.get("place_id", place_id)
#         address_components = result.get("address_components", [])
#         result_types = result.get("types", [])
#         geometry = result.get("geometry", {})
#         location = geometry.get("location", {})

#         neighborhood = _extract_neighborhood(address_components)
#         is_specific = not _is_too_broad(result_types)

#         # Always construct the link using _build_map_link so the format is
#         # always: ?api=1&query_place_id={place_id}&query={encoded_name}
#         # Never use the raw `url` field from the API — it has a different
#         # format (?cid=...) that does not match the required structure.
#         map_link = _build_map_link(place_id_confirmed, name)

#         if not map_link:
#             # place_id missing — mark as not specific so the AI tells the user
#             # to search manually rather than rendering a broken link.
#             is_specific = False
#             logger.warning(
#                 f"[_resolve_single_place] Could not build map link for: {name}"
#             )
#         else:
#             logger.info(f"[_resolve_single_place] map_link={map_link}")

#         return {
#             "status": "ok",
#             "query": query,
#             "name": name,
#             "neighborhood": neighborhood,
#             "address": address,
#             "website": website,
#             # map_link — the complete verified Google Maps URL.
#             # place_id is intentionally excluded so the AI cannot construct
#             # a partial URL from it.
#             "map_link": map_link,
#             # map_link_markdown — the link pre-formatted as a markdown hyperlink.
#             # USE THIS directly in the response by copying it character-for-character.
#             # Do NOT rewrite, shorten, or reconstruct this string.
#             # Example output: [Swan Oyster Depot](https://www.google.com/maps/search/...)
#             "map_link_markdown": f"[{name}]({map_link})" if map_link else None,
#             "lat": location.get("lat"),
#             "lng": location.get("lng"),
#             "is_specific": is_specific,
#         }

#     except Exception as e:
#         logger.error(f"[_resolve_single_place] Error for '{query}': {e}", exc_info=True)
#         return {"status": "error", "query": query, "message": str(e)}


# def _get_distance(origin: str, destination: str) -> dict:
#     """
#     Internal helper: call Distance Matrix API for one origin→destination pair.
#     Returns a dict with status, distance, duration, suggestion, advice.
#     """
#     is_usa = any(
#         indicator in origin.upper()
#         for indicator in (" USA", ", USA", " US", ", US", "UNITED STATES")
#     )
#     units = "imperial" if is_usa else "metric"

#     params = {
#         "origins": origin,
#         "destinations": destination,
#         "mode": "driving",
#         "units": units,
#         "key": settings.GOOGLE_API_KEY,
#     }

#     try:
#         response = httpx.get(
#             "https://maps.googleapis.com/maps/api/distancematrix/json",
#             params=params,
#             timeout=10,
#         )
#         response.raise_for_status()
#         data = response.json()

#         if data.get("status") != "OK":
#             return {"status": "error", "message": f"Distance Matrix API error: {data.get('status')}"}

#         element = data["rows"][0]["elements"][0]
#         element_status = element.get("status")

#         if element_status != "OK":
#             return {
#                 "status": "error",
#                 "message": f"Could not calculate route: {element_status}.",
#             }

#         distance_text = element["distance"]["text"]
#         duration_text = element["duration"]["text"]

#         if is_usa:
#             distance_value = element["distance"]["value"] / 1609.34
#             unit_label = "miles"
#         else:
#             distance_value = element["distance"]["value"] / 1000.0
#             unit_label = "km"

#         if distance_value <= 1.0:
#             suggestion = "walk"
#             advice = (
#                 f"It's about {distance_text} from where you are — "
#                 f"an {duration_text} walk."
#             )
#             duration_text = "10 min by walk"

#         elif distance_value <= 5:
#             suggestion = "car or rideshare"
#             advice = (
#                 f"It's {distance_text} away — a bit far to walk comfortably. "
#                 f"A car or rideshare would be the better call."
#             )
#             duration_text = f"{duration_text} by car or rideshare"
#         elif distance_value <= 20:
#             suggestion = "car or rideshare"
#             advice = (
#                 f"It's {distance_text} away — a bit far to walk comfortably. "
#                 f"A car or rideshare would be the better call."
#             )
#             duration_text = f"{duration_text} by car or rideshare duration may vary depending on traffic"
#         else:
#             suggestion = "car or rideshare"
#             advice = (
#                 f"It's {distance_text} away — a bit far to walk comfortably. "
#                 f"A car or rideshare  would be the better call."
#             )
#             duration_text = f"None"


#         return {
#             "status": "ok",
#             "origin": origin,
#             "destination": destination,
#             "distance_text": distance_text,
#             "duration_text": f"{duration_text} by walk",
#             f"distance_{unit_label}": f'{round(distance_value, 2)} {unit_label} | {duration_text}',
#             "unit": unit_label,
#             "suggestion": suggestion,
#             "advice": advice,
#         }

#     except Exception as e:
#         logger.error(f"[_get_distance] Error: {e}", exc_info=True)
#         return {"status": "error", "message": str(e)}


# # ─────────────────────────────────────────────
# # TOOLS
# # ─────────────────────────────────────────────

# @tool
# async def get_user_info(user_id: str) -> str:
#     """
#     ALWAYS call this tool FIRST — before any other tool — on every user request.

#     Retrieves stored user profile including:
#     - name (for personalized responses)
#     - location / city / country (to find nearby places and localize all recommendations)
#     - tripExperience
#     - soloTravelConfidence
#     - placeId (for personalized recommendations)

#     Use the returned location as the geographic anchor for all subsequent
#     web_search and google_place_search queries.

#     Never assume or guess user details. Always fetch first.
#     If this call fails, ask the user for their location before proceeding.
#     """
#     try:
#         result = await db.get_user(user_id)
#         logger.info(f"[get_user_info] Result: {result}")
#         return result
#     except Exception as e:
#         logger.error(f"[get_user_info] Failed: {e}", exc_info=True)
#         return json.dumps({"status": "error", "message": str(e)})


# @tool
# def google_place_search(query: str) -> str:
#     """
#     Look up ONE specific place to retrieve its verified name, address,
#     map_link (built from place_id), website, and coordinates.

#     WHEN TO CALL:
#     - You are recommending exactly one place and need its verified details.
#     - The user asks "where is", "address of", or "map link for" a single location.

#     For 2 or more places in the same response, use get_multiple_places_and_distances
#     instead — it is faster and eliminates tool-sequencing errors.

#     QUERY FORMAT — always include full location context:
#       [place name] [neighborhood] [city] [country]
#       "Hatirjheel lake Rampura Dhaka Bangladesh"
#       "Fuglen Coffee Tomigaya Shibuya Tokyo Japan"

#     AFTER a successful call, use the returned fields as follows:
#       • name              → display name
#       • address           → full street address
#       • map_link_markdown → COPY THIS VERBATIM into your response.
#                             It is already formatted as [Name](URL).
#                             Do NOT rewrite, reconstruct, or shorten it.
#                             Do NOT use map_link to build your own markdown.
#       • website           → include only when traveler intends to visit or book
#       • is_specific       → if False, do NOT use map_link_markdown;
#                             tell the traveler to search directly instead.

#     Never construct a map link from memory or training data.
#     Never call get_distance_to_place separately after this — if you need distance,
#     use get_multiple_places_and_distances which handles both in one call.
#     """
#     logger.info(f"[google_place_search] Query: {query}")
#     result = _resolve_single_place(query)
#     logger.info(f"[google_place_search] Result: {result}")
#     return json.dumps(result)


# @tool
# def get_distance_to_place(origin: str, destination: str) -> str:
#     """
#     Calculate the walking distance and duration between the traveler's current location
#     and a confirmed destination address.

#     WHEN TO CALL:
#     - You already have the confirmed destination address from a prior
#       google_place_search call in this same turn, AND you are only handling
#       a single place in this response.
#     - For 2 or more places, use get_multiple_places_and_distances instead.

#     PARAMETERS:
#     - origin: traveler's current location as a string
#         Example: "Mirpur 10, Dhaka, Bangladesh"
#     - destination: the FULL confirmed address returned by google_place_search —
#         never pass a guessed or partial address.
#         Example: "Hatirjheel Lake, Rampura, Dhaka 1219, Bangladesh"

#     RESPONSE BEHAVIOR:
#     - distance <= 1.0 mile/km  → walkable; mention walking time.
#     - distance <= 100 miles/km → car or rideshare .
#     - Unit is automatic: miles inside USA, km outside USA.

#     IMPORTANT: Call google_place_search FIRST for the confirmed address.
#     Then call this tool. Never call both simultaneously — this tool requires
#     the address that google_place_search returns.
#     """
#     logger.info(f"[get_distance_to_place] origin={origin} | destination={destination}")
#     result = _get_distance(origin, destination)
#     logger.info(f"[get_distance_to_place] Result: {result}")
#     return json.dumps(result)


# @tool
# def get_multiple_places_and_distances(origin: str, place_queries: list) -> str:
#     """
#     *** USE THIS TOOL whenever the response includes 2 or more places. ***

#     Performs google_place_search AND get_distance_to_place for every place
#     in a single tool call — using Python concurrency internally.

#     This eliminates the sequential search → distance loop that causes the AI
#     to skip tool calls or hallucinate addresses when handling multi-place responses.

#     WHEN TO CALL:
#     - User asks for N cafes, restaurants, parks, hotels, or any other category
#       where N >= 2.
#     - You are building a Step 1 scannable list with 2–10 places.
#     - Any multi-location response in Roaming Mode.

#     DO NOT use google_place_search + get_distance_to_place separately when
#     you need results for more than one place — use this tool instead.

#     PARAMETERS:
#     - origin: traveler's confirmed current location as a plain string.
#         Example: "Mirpur 10, Dhaka, Bangladesh"
#     - place_queries: a Python list of query strings, one per place.
#         Each query must follow the format: [place name] [neighborhood] [city] [country]
#         Example: [
#             "Shuruchi Restaurant Dhanmondi Dhaka Bangladesh",
#             "Star Kabab Old Dhaka Bangladesh",
#             "Panshi Restaurant Mirpur Dhaka Bangladesh"
#         ]
#         Minimum 2 entries. Maximum 10 entries.

#     RETURNS:
#     A JSON list. Each item contains:
#     - query             → the original query string (for your reference)
#     - name              → verified place name
#     - address           → full street address
#     - neighborhood      → extracted neighborhood name
#     - map_link_markdown → COPY THIS VERBATIM into your response.
#                           Already formatted as [Name](URL). Do NOT rewrite it.
#     - map_link          → raw URL (available if needed; prefer map_link_markdown)
#     - website           → website if available
#     - is_specific       → False means city-level result; skip map_link_markdown,
#                           tell traveler to search directly
#     - distance_text     → human-readable distance (e.g. "1.2 km")
#     - duration_text     → walking time (e.g. "15 mins")
#     - suggestion        → "walk" or "car or rideshare"
#     - advice            → one-sentence distance advice to include in your response
#     - place_error       → present only if place lookup failed for this entry
#     - distance_error    → present only if distance calculation failed for this entry

#     USAGE IN RESPONSE:
#     - Copy map_link_markdown exactly — never rewrite, reconstruct, or shorten it.
#     - Use distance_text and advice exactly as returned for every place.
#     - If is_specific is False, skip map_link_markdown and write:
#         "Search [place name] [neighborhood] in Google Maps directly."
#     - If place_error is set, omit that place and fill the slot with the next result.
#     """
#     logger.info(
#         f"[get_multiple_places_and_distances] origin={origin} | "
#         f"queries={place_queries}"
#     )

#     if not place_queries or len(place_queries) < 1:
#         return json.dumps({
#             "status": "error",
#             "message": "place_queries must contain at least 1 entry.",
#         })

#     # ── Resolve all places concurrently using threads ─────────────────────
#     async def _resolve_all():
#         loop = asyncio.get_event_loop()

#         place_futures = [
#             loop.run_in_executor(None, _resolve_single_place, q)
#             for q in place_queries
#         ]
#         place_results = await asyncio.gather(*place_futures)

#         # Build distance destinations from confirmed addresses
#         # Use address if lookup succeeded; fall back to query string
#         destinations = []
#         for pr in place_results:
#             if pr.get("status") == "ok" and pr.get("address"):
#                 destinations.append(pr["address"])
#             else:
#                 destinations.append(None)

#         distance_futures = []
#         for dest in destinations:
#             if dest:
#                 distance_futures.append(
#                     loop.run_in_executor(None, _get_distance, origin, dest)
#                 )
#             else:
#                 # Placeholder for failed place lookups
#                 distance_futures.append(asyncio.coroutine(lambda: {"status": "skipped"})())

#         distance_results = await asyncio.gather(*distance_futures, return_exceptions=True)

#         combined = []
#         for i, (pr, dr) in enumerate(zip(place_results, distance_results)):
#             entry = {"query": place_queries[i]}

#             if pr.get("status") == "ok":
#                 map_link = pr.get("map_link", "")
#                 name_val = pr.get("name", "")
#                 entry.update({
#                     "name": name_val,
#                     "neighborhood": pr.get("neighborhood"),
#                     "address": pr.get("address"),
#                     "map_link": map_link,
#                     # Pre-formatted markdown hyperlink — copy verbatim into response.
#                     # Do NOT reconstruct this from map_link or name separately.
#                     "map_link_markdown": f"[{name_val}]({map_link})" if map_link else None,
#                     "website": pr.get("website"),
#                     "is_specific": pr.get("is_specific"),
#                     "lat": pr.get("lat"),
#                     "lng": pr.get("lng"),
#                 })
#             else:
#                 entry["place_error"] = pr.get("message", "Place lookup failed.")

#             if isinstance(dr, Exception):
#                 entry["distance_error"] = str(dr)
#             elif isinstance(dr, dict) and dr.get("status") == "ok":
#                 entry.update({
#                     "distance_text": dr.get("distance_text"),
#                     "duration_text": dr.get("duration_text"),
#                     "suggestion": dr.get("suggestion"),
#                     "advice": dr.get("advice"),
#                     "unit": dr.get("unit"),
#                 })
#                 # Include the numeric distance under its dynamic key
#                 for k, v in dr.items():
#                     if k.startswith("distance_") and k not in ("distance_text",):
#                         entry[k] = v
#             elif isinstance(dr, dict):
#                 entry["distance_error"] = dr.get("message", "Distance lookup failed.")

#             combined.append(entry)

#         return combined

#     # ── Run async resolver (handles both sync and async call contexts) ────
#     try:
#         try:
#             loop = asyncio.get_event_loop()
#             if loop.is_running():
#                 # We're inside an already-running event loop (e.g. FastAPI)
#                 import concurrent.futures
#                 with concurrent.futures.ThreadPoolExecutor() as pool:
#                     future = pool.submit(asyncio.run, _resolve_all())
#                     combined = future.result()
#             else:
#                 combined = loop.run_until_complete(_resolve_all())
#         except RuntimeError:
#             combined = asyncio.run(_resolve_all())

#         logger.info(f"[get_multiple_places_and_distances] Completed {len(combined)} entries.")
#         return json.dumps({"status": "ok", "results": combined})

#     except Exception as e:
#         logger.error(f"[get_multiple_places_and_distances] Failed: {e}", exc_info=True)
#         return json.dumps({"status": "error", "message": str(e)})


# @tool
# def web_search(query: str) -> str:
#     """
#     Search the web for real-time or specific travel information.

#     WHEN TO CALL:
#     - Current opening hours, entry fees, or booking requirements
#     - Seasonal conditions, local events, or recent closures
#     - Transportation options, routes, or travel advisories
#     - Any fact you are not fully confident about

#     BEFORE calling this tool:
#     - Always call get_user_info first to include the user's city/country in the query.
#     - Always append the user's location to the query for locally relevant results.
#         Example: instead of "best ramen spots", search "best ramen spots Osaka Japan 2026"

#     DO NOT call for: general cultural knowledge or well-established facts you already hold.
#     Do not call this tool more than 3 times in a single response.

#     After getting results:
#     - If specific places are mentioned, follow up with get_multiple_places_and_distances
#       to get verified addresses, map links, and distances for all of them in one call.
#     """
#     logger.info(f"[web_search] Query: {query}")
#     search = DuckDuckGoSearchRun()
#     try:
#         result = search.run(query)
#         logger.info(f"[web_search] Success, length={len(result)}")
#         return result
#     except Exception as e:
#         logger.error(f"[web_search] Failed: {e}", exc_info=True)
#         return json.dumps({"status": "error", "message": f"Search failed: {str(e)}"})


# # ─────────────────────────────────────────────

# def get_all_tools():
#     """Return list of all available tools in priority order."""
#     return [
#         get_user_info,                       # Always first — anchors all location context
#         web_search,                          # Real-time info
#         google_place_search,                 # Single place lookup
#         get_distance_to_place,               # Single place distance (after google_place_search)
#         get_multiple_places_and_distances,   # *** PREFERRED for 2+ places — place + distance in one call ***
#     ]