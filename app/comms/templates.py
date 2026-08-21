from app.models import MessageTemplate, TriggerEvent

DEFAULTS = {
    TriggerEvent.booking_confirmed: dict(
        subject="Your stay at {property_name} is confirmed",
        body=(
            "Hi {guest_name},\n\n"
            "Your booking at {property_name} is confirmed for "
            "{check_in} to {check_out}. We'll send check-in details closer to your arrival.\n\n"
            "Looking forward to hosting you!"
        ),
    ),
    TriggerEvent.pre_arrival: dict(
        subject="Getting ready for your stay at {property_name}",
        body=(
            "Hi {guest_name},\n\n"
            "Your check-in at {property_name} is coming up on {check_in}. "
            "We'll send exact check-in instructions on the day. Let us know if you have any questions before then."
        ),
    ),
    TriggerEvent.check_in_day: dict(
        subject="Check-in day at {property_name}",
        body=(
            "Hi {guest_name},\n\n"
            "Today's the day! Full check-in details, WiFi and house rules for {property_name}: "
            "{guest_portal_url}\n\n"
            "If anything comes up during your stay, just reply to this email."
        ),
    ),
    TriggerEvent.post_checkout: dict(
        subject="Thanks for staying at {property_name}",
        body=(
            "Hi {guest_name},\n\n"
            "Thanks for staying with us at {property_name}! We hope you had a great time.\n\n"
            "If you have a minute, we'd really appreciate a review.{review_links}"
        ),
    ),
    TriggerEvent.guest_re_engagement: dict(
        subject="Come back and stay with us again, {guest_name}",
        body=(
            "Hi {guest_name},\n\n"
            "It's been a while since your stay at {property_name} — we'd love to host you again! "
            "Book direct with us for the best rate.\n\n"
            "Hope to see you back on the Gold Coast soon."
        ),
        active=False,
    ),
}


def seed_default_templates(db) -> None:
    existing = {t.trigger_event for t in db.query(MessageTemplate).all()}
    for event, content in DEFAULTS.items():
        if event not in existing:
            db.add(MessageTemplate(trigger_event=event, **content))
    db.commit()


def render(template: MessageTemplate, booking) -> tuple[str, str]:
    prop = booking.property
    review_links = []
    if prop.airbnb_review_url:
        review_links.append(f"Airbnb: {prop.airbnb_review_url}")
    if prop.google_review_url:
        review_links.append(f"Google: {prop.google_review_url}")
    review_links_text = ("\n\n" + "\n".join(review_links)) if review_links else ""

    guest_portal_url = (
        f"https://ops.omiholiday.com/guest/{booking.guest_portal_token}"
        if booking.guest_portal_token
        else ""
    )

    ctx = {
        "guest_name": (booking.guest.name if booking.guest and booking.guest.name else "there"),
        "property_name": prop.name,
        "check_in": booking.check_in.strftime("%Y-%m-%d"),
        "check_out": booking.check_out.strftime("%Y-%m-%d"),
        "review_links": review_links_text,
        "guest_portal_url": guest_portal_url,
        "door_code": booking.door_code or "",
    }

    use_zh = (
        booking.guest
        and booking.guest.language == "zh"
        and template.subject_zh
        and template.body_zh
    )
    subject = template.subject_zh if use_zh else template.subject
    body = template.body_zh if use_zh else template.body
    return subject.format(**ctx), body.format(**ctx)
