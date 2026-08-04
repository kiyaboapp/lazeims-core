"""Seed Lake Zone data from ShuleyeTu API into LAZEIMS database."""

import asyncio
import httpx
import asyncpg

BASE = "https://api.shuleyetu.co.tz"
DB_URL = "postgresql://postgres:ma0zYn9RzAZbhBOE2Bs235KSwPdTeF4D@127.0.0.1:5432/lazeims"

LAKE_ZONE_REGIONS = {
    "KAGERA": 30,
    "GEITA": 8,
    "MWANZA": 20,
    "SHINYANGA": 7,
    "MARA": 14,
    "SIMIYU": 1,
}

REGION_SLUGS = {
    "KAGERA": "kagera",
    "GEITA": "geita",
    "MWANZA": "mwanza",
    "SHINYANGA": "shinyanga",
    "MARA": "mara",
    "SIMIYU": "simiyu",
}


def fetch_json(url, retries=3):
    import time
    for attempt in range(retries):
        try:
            r = httpx.get(url, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"  WARN: Failed to fetch {url}: {e}")
                return {"schools": []}


async def main():
    conn = await asyncpg.connect(DB_URL)

    # Clean existing data (fresh seed)
    await conn.execute("DELETE FROM schools")
    await conn.execute("DELETE FROM wards")
    await conn.execute("DELETE FROM councils")
    await conn.execute("DELETE FROM regions")
    print("Cleaned existing data.")

    # 1. Seed regions
    region_db_ids = {}
    for name in LAKE_ZONE_REGIONS:
        row = await conn.fetchrow(
            "INSERT INTO regions (name, created_at, updated_at) VALUES ($1, NOW(), NOW()) RETURNING id",
            name,
        )
        region_db_ids[name] = row["id"]
        print(f"  Region: {name} -> id={row['id']}")

    # 2. Seed councils
    council_db_ids = {}
    for region_name, api_region_id in LAKE_ZONE_REGIONS.items():
        resp = httpx.get(f"{BASE}/api/necta/v1/locations/metadata/councils?region_id={api_region_id}", timeout=60)
        councils = resp.json()
        for c in councils:
            row = await conn.fetchrow(
                "INSERT INTO councils (name, region_id, created_at, updated_at) VALUES ($1, $2, NOW(), NOW()) RETURNING id",
                c["name"], region_db_ids[region_name],
            )
            council_db_ids[c["name"]] = row["id"]
        print(f"  {region_name}: {len(councils)} councils")

    # 3. Fetch all schools (which also gives us ward data)
    ward_db_ids = {}
    schools_inserted = 0

    for region_name, slug in REGION_SLUGS.items():
        page = 1
        while True:
            data = fetch_json(f"{BASE}/api/necta/v1/shuleni/region/{slug}/shule-zote?page={page}&page_size=100")
            schools = data.get("schools", [])
            if not schools:
                break

            for s in schools:
                council_name = s.get("council_name")
                ward_name = s.get("ward_name")

                # Ensure council exists
                if council_name and council_name not in council_db_ids:
                    row = await conn.fetchrow(
                        "INSERT INTO councils (name, region_id, created_at, updated_at) VALUES ($1, $2, NOW(), NOW()) RETURNING id",
                        council_name, region_db_ids[region_name],
                    )
                    council_db_ids[council_name] = row["id"]

                # Ensure ward exists
                ward_key = (council_name, ward_name)
                if ward_name and ward_key not in ward_db_ids and council_name in council_db_ids:
                    try:
                        row = await conn.fetchrow(
                            "INSERT INTO wards (name, council_id, created_at, updated_at) VALUES ($1, $2, NOW(), NOW()) RETURNING id",
                            ward_name, council_db_ids[council_name],
                        )
                        ward_db_ids[ward_key] = row["id"]
                    except asyncpg.UniqueViolationError:
                        row = await conn.fetchrow(
                            "SELECT id FROM wards WHERE council_id=$1 AND name=$2",
                            council_db_ids[council_name], ward_name,
                        )
                        if row:
                            ward_db_ids[ward_key] = row["id"]

                # School type - GOVERNMENT or PRIVATE from API
                api_type = s.get("school_type", "")
                if api_type == "PRIVATE":
                    school_type = "PRIVATE"
                elif api_type == "GOVERNMENT":
                    school_type = "GOVERNMENT"
                else:
                    school_type = "UNKNOWN"

                # Level flags from level_label
                level_label = s.get("level_label", "")
                is_primary = level_label == "MSINGI"
                is_olevel = level_label in ("KIDATO CHA 1-4", "KIDATO CHA 1-6")
                is_alevel = level_label in ("KIDATO CHA 1-6", "KIDATO CHA 5-6")

                # Insert school
                council_id = council_db_ids.get(council_name)
                ward_id = ward_db_ids.get(ward_key)
                centre = s.get("centre_number", "").upper()
                if not centre:
                    continue
                try:
                    await conn.execute(
                        """INSERT INTO schools (centre_number, name, school_type, is_primary, is_olevel, is_alevel, region_id, council_id, ward_id, can_download_template, created_at, updated_at)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, false, NOW(), NOW())""",
                        centre, s["school_name"], school_type,
                        is_primary, is_olevel, is_alevel,
                        region_db_ids[region_name], council_id, ward_id,
                    )
                    schools_inserted += 1
                except asyncpg.UniqueViolationError:
                    pass

            total = data.get("total", "?")
            print(f"  {region_name} page {page}: {len(schools)} schools (total: {total})")
            if len(schools) < 100:
                break
            page += 1

    print(f"\nDone! Inserted:")
    print(f"  Regions: {len(region_db_ids)}")
    print(f"  Councils: {len(council_db_ids)}")
    print(f"  Wards: {len(ward_db_ids)}")
    print(f"  Schools: {schools_inserted}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
