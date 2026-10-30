# Calendly

Files consultations booked through the firm's Calendly organization. The contact is matched or created, the consult appointment is made, and reschedules and cancellations follow. It is in Beta: its tests run against a fake Calendly that follows Calendly's published API, but it has not run against a live organization yet.

## What it does

- **Which bookings.** On the connection's page, the firm picks which of its Calendly event types are consultations, from Calendly's live list. Only bookings of those types are filed; others are ignored. The CRM never guesses from event type names.
- **The contact.** A booking is matched to a contact by email or phone. If there is none, a contact is created at the consultation stage. A note on the new contact records the booking link's tracking values.
- **The appointment.** The consult appointment gets the event's name, time, and location, and the answers to the booking questions. It is assigned to the staff member hosting the event when their CRM email matches. If a calendar is connected for them, it goes on their calendar like any appointment.
- **Reschedules and cancellations.** A reschedule moves the same appointment to the new time. A cancellation marks it cancelled.
- **Lead source.** The connection can fill a contact choice field for new contacts, such as the lead-source field the operating dashboard groups by. It uses a tracking value: `utm_source` by default, or `utm_medium` or `utm_campaign`. The value must match one of the field's choices, ignoring case, spaces, and punctuation; for example, `google-ads` fills "Google Ads". A value that matches no choice leaves the field empty. Existing contacts are never changed.

## Setting it up

1. Calendly webhooks need a paid Calendly plan (Standard, Teams, or Enterprise).
2. A Calendly organization owner or admin creates a personal access token under **Profile > Developer > Personal access tokens**. Give it these scopes; webhooks deliver bookings only to a token that can read scheduled events:
   - `users:read`
   - `event_types:read`
   - `scheduled_events:read`
   - `webhooks:write`
3. In the CRM, a superuser opens **Connections > Calendly**, pastes the token, reviews what the connection sends and receives, and connects.
4. On the connection's page, under **Settings**, tick the event types that are consultations. Optionally choose the contact field to fill and which tracking value to use. Save.
5. Turn on webhooks with **Create the organization webhook in Calendly** ticked. The CRM creates one organization webhook for bookings and cancellations, signed with a new secret. Issuing a new address later replaces it; disconnecting deletes it.

The webhook address must be reachable from Calendly. If Calendly cannot deliver to it for 24 hours, it disables the webhook; turn webhooks on again to create a new one.

## How deliveries are checked

Each delivery carries a `Calendly-Webhook-Signature` header with a time and an HMAC-SHA256 signature of that time and the body, made with the webhook's secret. The CRM refuses a delivery with a wrong signature, or one whose time is more than three minutes off, so a captured delivery cannot be replayed later.
