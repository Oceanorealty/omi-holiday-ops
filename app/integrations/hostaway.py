"""Read-only Hostaway boundary. No token acquisition, writes or shadow promotion."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import requests
from app.models import ExternalMapping, IntegrationRun, ShadowReservation


class IntegrationError(Exception):
    def __init__(self, code, retry_at=None):
        self.code, self.retry_at = code, retry_at
        super().__init__(code)  # Never surface URLs, bodies or credentials.


class HostawayReader:
    BASE = "https://api.hostaway.com/v1"

    def __init__(self, token, *, session=None, max_pages=100, page_size=100):
        if not token or not 1 <= page_size <= 100 or not 1 <= max_pages <= 1000:
            raise ValueError("invalid_reader_configuration")
        self._token = token
        self._session = session or requests.Session()
        self.max_pages, self.page_size = max_pages, page_size

    def reservations(self, *, listing_id, arrival_start, arrival_end):
        # Legacy explicit arrival filters avoid account-dependent dateType support.
        start, end = datetime.strptime(arrival_start, "%Y-%m-%d"), datetime.strptime(arrival_end, "%Y-%m-%d")
        if end < start or (end - start).days > 366 or int(listing_id) < 1:
            raise ValueError("invalid_read_scope")
        params = {"listingId": int(listing_id), "arrivalStartDate": arrival_start,
                  "arrivalEndDate": arrival_end, "limit": self.page_size, "includeResources": 0}
        items, ids, cursors = [], set(), set()
        for _ in range(self.max_pages):
            try:
                resp = self._session.get(self.BASE + "/reservations", params=params.copy(),
                    headers={"Authorization": "Bearer " + self._token, "Accept": "application/json"},
                    timeout=20, allow_redirects=False)
            except requests.RequestException:
                raise IntegrationError("network_error") from None
            if resp.status_code == 429:
                raw = resp.headers.get("X-RateLimit-Retry-After", "")
                raise IntegrationError("rate_limited", int(raw) if raw.isdigit() else None)
            if resp.status_code in (401, 403):
                raise IntegrationError("authorization_required")
            if resp.status_code != 200:
                raise IntegrationError("provider_error")
            try:
                data = resp.json()
            except ValueError:
                raise IntegrationError("invalid_json") from None
            if not isinstance(data, dict) or data.get("status") != "success" or not isinstance(data.get("result"), list):
                raise IntegrationError("invalid_response")
            batch = data["result"]
            if not batch:
                return items
            for obj in batch:
                if not isinstance(obj, dict) or not str(obj.get("id", "")).isdigit():
                    raise IntegrationError("invalid_record")
                # Locally enforce scope even if upstream ignored filters.
                if str(obj.get("listingMapId")) != str(listing_id):
                    raise IntegrationError("scope_mismatch")
                try:
                    arrival = datetime.strptime(obj["arrivalDate"], "%Y-%m-%d")
                except (KeyError, TypeError, ValueError):
                    raise IntegrationError("invalid_record") from None
                if not start <= arrival <= end:
                    raise IntegrationError("scope_mismatch")
                if obj["id"] not in ids:
                    ids.add(obj["id"])
                    items.append(obj)
            cursor = str(batch[-1]["id"])
            if cursor in cursors:
                raise IntegrationError("pagination_stalled")
            cursors.add(cursor)
            params["afterId"] = cursor
        raise IntegrationError("pagination_incomplete")


def _timestamp(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _normalize(obj):
    if not isinstance(obj, dict):
        raise ValueError("invalid_shadow_record")
    # Explicit allowlist; guest PII, proxy email, door codes and cards are discarded.
    data = {k: obj.get(k) for k in ("id", "listingMapId", "status", "arrivalDate", "departureDate", "totalPrice", "currency", "updatedOn")}
    if not str(data["id"]).isdigit() or not str(data["listingMapId"]).isdigit():
        raise ValueError("invalid_shadow_identity")
    ci, co = datetime.strptime(data["arrivalDate"], "%Y-%m-%d"), datetime.strptime(data["departureDate"], "%Y-%m-%d")
    if co <= ci or not isinstance(data["status"], str):
        raise ValueError("invalid_shadow_dates_or_status")
    if data["totalPrice"] is not None:
        try:
            money = Decimal(str(data["totalPrice"]))
            if not money.is_finite():
                raise InvalidOperation()
        except (InvalidOperation, ValueError):
            raise ValueError("invalid_shadow_amount") from None
        data["totalPrice"] = str(money)
    if data["currency"] is not None and (not isinstance(data["currency"], str) or len(data["currency"]) != 3 or not data["currency"].isalpha()):
        raise ValueError("invalid_shadow_currency")
    updated = _timestamp(data["updatedOn"])
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return data, updated, encoded, hashlib.sha256(encoded.encode()).hexdigest()


def ingest_shadow(db, *, provider, account, records):
    if provider != "hostaway" or not account or len(account) > 100 or len(records) > 10000:
        raise ValueError("invalid_shadow_scope")
    normalized = [_normalize(obj) for obj in records]  # Validate entire batch before writes.
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "held": 0}
    with db.begin_nested():
        for data, updated, payload, digest in normalized:
            key = dict(provider=provider, account=account, external_id=str(data["id"]))
            row = db.query(ShadowReservation).filter_by(**key).first()
            if row and row.payload_hash == digest:
                counts["unchanged"] += 1
                continue
            if row and (updated is None or row.source_updated_at is None or updated <= row.source_updated_at):
                counts["held"] += 1  # Ambiguous or older changes require fresh authoritative detail.
                continue
            mapping = db.query(ExternalMapping).filter_by(provider=provider, account=account,
                external_id=str(data["listingMapId"])).first()
            if not row:
                row = ShadowReservation(**key)
                db.add(row)
                counts["inserted"] += 1
            else:
                counts["updated"] += 1
            row.external_listing_id = str(data["listingMapId"])
            row.property_id = mapping.property_id if mapping else None
            row.payload, row.payload_hash, row.source_updated_at = payload, digest, updated
            row.observed_at, row.state = datetime.utcnow(), "mapped" if mapping else "unmapped"
            db.flush()  # Make repeated IDs in the same batch converge even with autoflush=False.
        db.add(IntegrationRun(provider=provider, account=account, state="complete", record_count=len(records)))
    # Caller owns commit; no booking/message/cleaning/accounting writes.
    return counts


def refresh_shadow(db, reader, *, account, listing_id, arrival_start, arrival_end):
    try:
        records = reader.reservations(listing_id=listing_id, arrival_start=arrival_start, arrival_end=arrival_end)
        result = ingest_shadow(db, provider="hostaway", account=account, records=records)
        db.commit()
        return result
    except IntegrationError as exc:
        db.rollback()
        db.add(IntegrationRun(provider="hostaway", account=account, state="failed", error_code=exc.code))
        db.commit()
        raise
