"""Client for the public encar.com API: filter facets, search, details and accident history."""
import asyncio
import os
import re
from dataclasses import asdict, dataclass, field

import httpx

import names

API = "https://api.encar.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Referer": "https://www.encar.com/",
    "Origin": "https://www.encar.com",
}

# Fuel: (Encar value, label)
FUELS = [
    ("가솔린", "Gasoline"),
    ("디젤", "Diesel"),
    ("LPG(일반인 구입_)", "LPG"),
    ("가솔린+전기", "Hybrid (gasoline+electric)"),
    ("디젤+전기", "Hybrid (diesel+electric)"),
    ("LPG+전기", "Hybrid (LPG+electric)"),
    ("전기", "Electric"),
    ("수소", "Hydrogen"),
    ("CNG", "CNG"),
    ("가솔린+LPG", "Gasoline+LPG"),
]
FUEL_RU = {
    "가솔린": "Gasoline", "디젤": "Diesel", "LPG": "LPG", "전기": "Electric", "수소": "Hydrogen",
    "가솔린+전기": "Hybrid (gasoline+electric)", "디젤+전기": "Hybrid (diesel+electric)",
    "LPG+전기": "Hybrid (LPG+electric)", "CNG": "CNG", "가솔린+LPG": "Gasoline+LPG",
}


@dataclass
class Option:
    label: str  # shown to the user
    expr: str  # Encar query fragment, e.g. "Manufacturer.기아."
    value: str = ""  # raw Encar value
    count: int = 0


@dataclass
class Filters:
    brand: Option | None = None
    group: Option | None = None  # model (ModelGroup)
    gen: Option | None = None  # generation (Model)
    fuel: Option | None = None
    price: tuple[int | None, int | None] = (None, None)  # in 만원
    year: tuple[int | None, int | None] = (None, None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Filters":
        opt = lambda o: Option(**o) if o else None
        return cls(
            opt(d.get("brand")), opt(d.get("group")), opt(d.get("gen")), opt(d.get("fuel")),
            tuple(d.get("price", (None, None))), tuple(d.get("year", (None, None))),
        )

    def query(self) -> str:
        parts = ["Hidden.N.", "CarType.A."]
        for o in (self.brand, self.group, self.gen, self.fuel):
            if o:
                parts.append(o.expr)
        lo, hi = self.price
        if lo is not None or hi is not None:
            parts.append(f"Price.range({lo if lo is not None else ''}..{hi if hi is not None else ''}).")
        lo, hi = self.year
        if lo is not None or hi is not None:
            parts.append(
                f"Year.range({f'{lo}01' if lo else ''}..{f'{hi}12' if hi else ''})."
            )
        return "(And." + "_.".join(parts) + ")"


def _rng(pair: tuple[int | None, int | None], unit: str = "") -> str:
    lo, hi = pair
    if lo is None and hi is None:
        return "any"
    return f"{'' if lo is None else f'from {lo}'}{' ' if lo is not None and hi is not None else ''}{'' if hi is None else f'to {hi}'}{unit}"


def describe(f: Filters) -> str:
    return (
        f"Brand: {f.brand.label if f.brand else 'any'}\n"
        f"Model: {f.group.label if f.group else 'any'}\n"
        f"Generation: {f.gen.label if f.gen else 'any'}\n"
        f"Fuel: {f.fuel.label if f.fuel else 'any'}\n"
        f"Price: {_rng(f.price, ' 만원')}\n"
        f"Year: {_rng(f.year)}"
    )


def _find_node(nav: dict, name: str) -> dict | None:
    for node in nav.get("Nodes", []):
        if node.get("Name") == name:
            return node
        for facet in node.get("Facets", []):
            found = _find_node(facet.get("Refinements", {}), name)
            if found:
                return found
    return None


@dataclass
class Car:
    id: str
    title: str = ""
    year_month: str = ""
    fuel: str = ""
    trim: str = ""
    price: int = 0
    mileage: int = 0
    published: str = ""
    accidents: str = "no data"
    photo: str = ""
    url: str = field(init=False)

    def __post_init__(self):
        self.url = f"https://fem.encar.com/cars/detail/{self.id}"


class Encar:
    def __init__(self) -> None:
        # ENCAR_PROXY=http://user:pass@host:port uses that proxy; ENCAR_DIRECT=1 ignores system proxies
        self.http = httpx.AsyncClient(
            headers=HEADERS,
            timeout=30,
            proxy=os.getenv("ENCAR_PROXY") or None,
            trust_env=not os.getenv("ENCAR_DIRECT"),
        )
        self._sem = asyncio.Semaphore(6)
        self.names: dict[str, str] = names.load()  # "brand|group" (Korean) -> English group name

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, url: str, **params) -> dict:
        async with self._sem:
            for attempt in range(3):
                try:
                    r = await self.http.get(url, params=params)
                    r.raise_for_status()
                    return r.json()
                except (httpx.TransportError, httpx.HTTPStatusError):
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1 + attempt)
        raise RuntimeError("unreachable")

    async def _search(self, f: Filters, offset: int = 0, limit: int = 1, inav: bool = False) -> dict:
        params = {"count": "true", "q": f.query(), "sr": f"|ModifiedDate|{offset}|{limit}"}
        if inav:
            params["inav"] = "|Metadata|Sort"
        return await self._get(f"{API}/search/car/list/general", **params)

    async def facets(self, f: Filters, level: str) -> list[Option]:
        """level: Manufacturer | ModelGroup | Model — options for the current filters."""
        data = await self._search(f, inav=True)
        node = _find_node(data["iNav"], level)
        out = []
        for fc in (node or {}).get("Facets", []):
            if not fc.get("Count"):
                continue
            value, meta = fc["Value"], fc.get("Metadata", {})
            label = value
            if level == "Manufacturer":
                label = (meta.get("EngName") or [""])[0] or names.translate(value)
            elif level == "Model":
                en = self.names.get(self._key(f.brand, f.group), "")
                label = names.generation(value, f.group.value if f.group else "", en)
                start = (meta.get("ModelStartDate") or [""])[0]
                end = (meta.get("ModelEndDate") or [""])[0]
                if start:
                    label += f" ({start[:4]}-{end[:4] if end else 'present'})"
            out.append(Option(label, fc["Expression"], value, fc["Count"]))
        if level == "ModelGroup":
            await self._name_groups(f, out)
        return out

    @staticmethod
    def _key(brand: Option | None, group: Option | None) -> str:
        return f"{brand.value if brand else ''}|{group.value if group else ''}"

    async def _name_groups(self, f: Filters, opts: list[Option]) -> None:
        """English model names: Encar only has them on car cards, so sample one car per model."""
        todo = [o for o in opts if names.has_hangul(o.value) and self._key(f.brand, o) not in self.names]

        async def fetch(o: Option) -> None:
            try:
                sub = Filters(brand=f.brand, group=o)
                row = (await self._search(sub, 0, 1))["SearchResults"][0]
                d = await self._get(f"{API}/v1/readside/vehicle/{row['Id']}")
                en = d["category"].get("modelGroupEnglishName")
                if en:
                    self.names[self._key(f.brand, o)] = en
            except (httpx.HTTPError, ValueError, KeyError, IndexError):
                pass

        if todo:
            await asyncio.gather(*(fetch(o) for o in todo))
            names.save(self.names)
        for o in opts:
            o.label = self.names.get(self._key(f.brand, o)) or names.translate(o.value)

    async def scan(self, f: Filters, cap: int = 5000) -> tuple[int, dict[str, dict]]:
        """All listing rows (id -> row) matching the filters, up to `cap`; returns (total, rows)."""
        rows: dict[str, dict] = {}
        total = offset = 0
        while True:
            data = await self._search(f, offset, 500)
            total = data.get("Count", 0)
            batch = data.get("SearchResults", [])
            rows.update({str(r["Id"]): r for r in batch})
            offset += len(batch)
            if not batch or offset >= min(cap, total):
                break
        return total, rows

    async def cars(self, rows: list[dict]) -> list[Car]:
        return list(await asyncio.gather(*(self._car(r) for r in rows)))

    async def count(self, f: Filters) -> int:
        return (await self._search(f)).get("Count", 0)

    async def page(self, f: Filters, offset: int, limit: int) -> tuple[int, list[Car]]:
        data = await self._search(f, offset, limit)
        cars = await asyncio.gather(*(self._car(r) for r in data["SearchResults"]))
        return data.get("Count", 0), list(cars)

    async def _car(self, row: dict) -> Car:
        cid = str(row["Id"])
        ym = str(int(row.get("Year") or 0))
        car = Car(
            id=cid,
            title=f"{names.translate(row.get('Manufacturer', ''))} {names.translate(row.get('Model', ''))}".strip(),
            year_month=f"{ym[:4]}-{ym[4:6]}" if len(ym) >= 6 else ym,
            fuel=FUEL_RU.get(row.get("FuelType", ""), row.get("FuelType", "")),
            trim=names.translate(" ".join(x for x in (row.get("Badge"), row.get("BadgeDetail")) if x)),
            price=int(row.get("Price") or 0),
            mileage=int(row.get("Mileage") or 0),
            photo=f"https://ci.encar.com{row['Photo']}001.jpg" if row.get("Photo") else "",
        )
        try:
            d = await self._get(f"{API}/v1/readside/vehicle/{cid}")
            m = d.get("manage", {})
            pub = m.get("firstAdvertisedDateTime") or m.get("registDateTime") or ""
            car.published = pub.replace("T", " ")[:16] + " (KST)" if pub else ""
            cat = d.get("category", {})
            trim = " ".join(
                x for x in (cat.get("gradeEnglishName"), cat.get("gradeDetailEnglishName")) if x
            )
            car.trim = trim or car.trim
            group_ko, group_en = cat.get("modelGroupName", ""), cat.get("modelGroupEnglishName", "")
            if names.has_hangul(group_ko) and group_en:
                self.names[f"{cat.get('manufacturerName', '')}|{group_ko}"] = group_en
            car.title = (
                f"{cat.get('manufacturerEnglishName') or car.title.split(' ')[0]} "
                f"{names.generation(cat.get('modelName', ''), group_ko, group_en)}"
            ).strip()
            car.fuel = FUEL_RU.get(d.get("spec", {}).get("fuelName", ""), car.fuel)
            vno = d.get("vehicleNo")
            if vno:
                car.accidents = await self._accidents(d.get("vehicleId") or cid, vno)
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        return car

    async def _accidents(self, vid, vehicle_no: str) -> str:
        try:
            r = await self._get(f"{API}/v1/readside/record/vehicle/{vid}/open", vehicleNo=vehicle_no)
        except (httpx.HTTPError, ValueError):
            return "no data"
        mine, other = r.get("myAccidentCnt", 0), r.get("otherAccidentCnt", 0)
        return f"{mine + other} (own car: {mine}, other party: {other})"


def parse_range(text: str, lo_min: int = 0, hi_max: int = 10**9) -> tuple[int | None, int | None]:
    """'1000-3000', '-3000', '1000-', '2020' → (lo, hi). Raises ValueError."""
    t = text.strip().replace(" ", "").replace("–", "-").replace("—", "-")
    if re.fullmatch(r"\d+", t):
        return int(t), int(t)
    m = re.fullmatch(r"(\d*)-(\d*)", t)
    if not m or not (m[1] or m[2]):
        raise ValueError
    lo = int(m[1]) if m[1] else None
    hi = int(m[2]) if m[2] else None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    for v in (lo, hi):
        if v is not None and not lo_min <= v <= hi_max:
            raise ValueError
    return lo, hi
