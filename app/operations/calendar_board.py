"""Read-only presentation of recorded stays; never infers live availability."""
from datetime import date
from app.models import Booking, Guest


CHANNELS = {
    'airbnb': ('Airbnb', 'airbnb'),
    'booking_com': ('Booking.com', 'booking'),
    'expedia': ('Expedia', 'expedia'),
    'direct': ('Direct', 'direct'),
    'ctrip': ('Ctrip', 'other'),
}


def add_board(db, data):
    ids = sorted({int(item.split(':')[1]) for row in data['rows'] for item in row['evidence']})
    records = db.query(Booking).filter(Booking.id.in_(ids)).all() if ids else []
    guest_ids = {b.guest_id for b in records if b.guest_id}
    names = {g.id: g.name for g in db.query(Guest).filter(Guest.id.in_(guest_ids)).all()} if guest_ids else {}
    start, end = date.fromisoformat(data['start']), date.fromisoformat(data['end'])
    grouped = {}
    for b in records:
        grouped.setdefault(b.property_id, []).append(b)
    for row in data['rows']:
        lanes = []
        row['board_stays'] = []
        for b in sorted(grouped.get(row['property_id'], []), key=lambda b: (b.check_in, b.check_out, b.id)):
            if b.check_out <= b.check_in:
                continue
            first, last = b.check_in.date(), b.check_out.date()
            same_day = first == last
            # A departure on the first visible date is not an occupied night.
            if last <= start and not (same_day and first == start):
                continue
            if first >= end:
                continue
            left = max(0, (first - start).days)
            right = min(len(data['dates']), (last - start).days)
            if same_day:
                right = min(len(data['dates']), left + 1)
            lane = next((i for i, limit in enumerate(lanes) if limit <= left), len(lanes))
            if lane == len(lanes):
                lanes.append(right)
            else:
                lanes[lane] = right
            raw = getattr(b.platform, 'value', b.platform)
            channel, color = CHANNELS.get(raw, ('Other', 'other'))
            row['board_stays'].append({
                'id': b.id, 'label': 'Blocked' if b.is_block else (names.get(b.guest_id) or f'Reservation #{b.id}'),
                'channel': channel, 'color': 'blocked' if b.is_block else color,
                'column': left + 1, 'span': max(1, right - left), 'lane': lane + 1,
                'start': first.isoformat(), 'end': last.isoformat(), 'same_day': same_day,
                'continues_before': first < start, 'continues_after': last > end,
                'needs_review': bool(b.source_missing_since or any(b.id in day['conflict_ids'] for day in row['days'])),
            })
        row['board_lanes'] = max(1, len(lanes))
        row['board_review'] = bool(row['invalid_booking_ids'] or any(d['conflict_ids'] or d['source_review_ids'] for d in row['days']))
    data['recorded_stays'] = len({b.id for b in records if not b.is_block and b.check_out > b.check_in})
    data['review_properties'] = sum(row['board_review'] for row in data['rows'])
    return data
