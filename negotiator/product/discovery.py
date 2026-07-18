from __future__ import annotations

import argparse
import json
import os
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.nationalPhoneNumber,places.displayName,places.formattedAddress"
)
DEFAULT_FIXTURE = Path(__file__).parents[1] / "fixtures" / "places_response.json"
HttpRequest = Callable[[str, str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class Business:
    name: str
    phone: str
    category: str = "moving_company"
    address: str = ""


def _urllib_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    timeout: float,
) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=dict(headers),
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class PlacesClient:
    """Small Google Places (New) client with an offline, frozen fallback."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http: HttpRequest | None = None,
        fixture: str | Path = DEFAULT_FIXTURE,
        timeout: float = 8.0,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("GOOGLE_PLACES_API_KEY", "")
        self.http = http or _urllib_request
        self.fixture = Path(fixture)
        self.timeout = timeout

    def search_movers(self, city: str) -> list[Business]:
        city = city.strip()
        if not city:
            raise ValueError("city cannot be blank")
        payload = {
            "textQuery": f"movers in {city}",
            "includedType": "moving_company",
            "pageSize": 8,
        }
        try:
            if not self.api_key:
                raise RuntimeError("GOOGLE_PLACES_API_KEY is not configured")
            data = self.http(
                "POST",
                PLACES_URL,
                {
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": FIELD_MASK,
                },
                payload,
                self.timeout,
            )
        except Exception:
            data = json.loads(self.fixture.read_text(encoding="utf-8"))
        return parse_places(data)


def parse_places(data: Mapping[str, Any]) -> list[Business]:
    businesses: list[Business] = []
    for place in data.get("places", []):
        display_name = place.get("displayName", {})
        name = display_name.get("text", "") if isinstance(display_name, Mapping) else str(display_name)
        phone = str(place.get("nationalPhoneNumber", "")).strip()
        if name.strip() and phone:
            businesses.append(
                Business(
                    name=name.strip(),
                    phone=phone,
                    address=str(place.get("formattedAddress", "")).strip(),
                )
            )
    return businesses


def display_list(businesses: Sequence[Business]) -> str:
    if not businesses:
        return "No movers found."
    return "\n".join(
        f"{index}. {business.name} — {business.phone} — {business.address}"
        for index, business in enumerate(businesses, 1)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover moving companies with Google Places")
    parser.add_argument("--city", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    businesses = PlacesClient().search_movers(args.city)
    if args.json:
        print(json.dumps([asdict(item) for item in businesses], ensure_ascii=False, indent=2))
    else:
        print(display_list(businesses))


if __name__ == "__main__":
    main()
