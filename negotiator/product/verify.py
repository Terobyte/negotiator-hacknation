from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


BASE_URL = "https://mobile.fmcsa.dot.gov/qc/services/carriers"
DEFAULT_FIXTURE = Path(__file__).parents[1] / "fixtures" / "fmcsa_fallback.json"
HttpGet = Callable[[str, Mapping[str, str], float], Mapping[str, Any]]


def _urllib_get(url: str, headers: Mapping[str, str], timeout: float) -> Mapping[str, Any]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class FMCSAClient:
    """QCMobile carrier verification; never falls back to HTML scraping."""

    def __init__(
        self,
        *,
        web_key: str | None = None,
        http: HttpGet | None = None,
        fixture: str | Path = DEFAULT_FIXTURE,
        timeout: float = 8.0,
    ) -> None:
        self.web_key = web_key if web_key is not None else os.getenv("FMCSA_WEB_KEY", "")
        self.http = http or _urllib_get
        self.fixture = Path(fixture)
        self.timeout = timeout

    def verify_dot(self, dot: str | int) -> dict[str, Any]:
        identifier = _digits(dot, "USDOT")
        try:
            return {
                "query": {"type": "USDOT", "value": identifier},
                "carrier": self._get(f"/{identifier}"),
                "authority": self._get(f"/{identifier}/authority"),
                "oos": self._get(f"/{identifier}/oos"),
                "fallback": False,
            }
        except Exception:
            return self._fallback("USDOT", identifier)

    def verify_mc(self, mc: str | int) -> dict[str, Any]:
        identifier = _digits(mc, "MC")
        try:
            carrier = self._get(f"/docket-number/{identifier}")
            result: dict[str, Any] = {
                "query": {"type": "MC", "value": identifier},
                "carrier": carrier,
                "fallback": False,
            }
            dot = _find_dot_number(carrier)
            if dot:
                result["authority"] = self._get(f"/{dot}/authority")
                result["oos"] = self._get(f"/{dot}/oos")
            return result
        except Exception:
            return self._fallback("MC", identifier)

    def _get(self, path: str) -> Mapping[str, Any]:
        if not self.web_key:
            raise RuntimeError("FMCSA_WEB_KEY is not configured")
        query = urllib.parse.urlencode({"webKey": self.web_key})
        return self.http(f"{BASE_URL}{path}?{query}", {"Accept": "application/json"}, self.timeout)

    def _fallback(self, kind: str, value: str) -> dict[str, Any]:
        frozen = json.loads(self.fixture.read_text(encoding="utf-8"))
        selected = frozen.get(kind, frozen)
        result = dict(selected)
        result["query"] = {"type": kind, "value": value}
        result["fallback"] = True
        return result


def _digits(value: str | int, label: str) -> str:
    text = str(value).strip().upper().removeprefix(label).replace("-", "")
    if not text.isdigit():
        raise ValueError(f"{label} must contain digits")
    return text


def _find_dot_number(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key.lower() in {"dotnumber", "dot_number", "usdot"} and str(child).isdigit():
                return str(child)
            found = _find_dot_number(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_dot_number(child)
            if found:
                return found
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a carrier with FMCSA QCMobile")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dot")
    group.add_argument("--mc")
    args = parser.parse_args()
    client = FMCSAClient()
    result = client.verify_dot(args.dot) if args.dot else client.verify_mc(args.mc)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
